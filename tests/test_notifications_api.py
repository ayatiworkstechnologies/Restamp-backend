"""Notification API (list / unread count / read / read-all / soft delete) and
the generation helper's reliability guarantees (notification Phase 1).

API: user-scoped (another user's row is a 404 for read and delete, never
listed), unauthenticated 401, suspended 403, pagination caps, audience /
category / unread-only filters, badge counts, idempotent read, audience-scoped
read-all, soft delete (row kept, hidden, key still blocks re-creation).

Reliability: duplicate keys (pre-check and a real MySQL 1062 race), a
notification failure (DB error or exception) never breaks the business
operation, a genuine business flush error is never swallowed, and a
notification rolls back with its surrounding transaction.
Rollback-isolated `client` fixture on the local test database.
"""
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models import EnquiryMessage, Notification
from app.services import notifications as svc
from tests.conftest import auth_header
from tests.test_notifications_events import _pair, _rows
from tests.test_visits import FUTURE, _buyer

ENDPOINTS = (
    ("GET", "/notifications"),
    ("GET", "/notifications/unread-count"),
    ("POST", "/notifications/read-all"),
    ("POST", "/notifications/1/read"),
    ("DELETE", "/notifications/1"),
)


def _user(c):
    out = _buyer(c)
    return out["user_id"], auth_header(out["access_token"])


def _seed(session, uid, n=1, type_="ENQUIRY_NEW", audience=None, key="seed"):
    rows = []
    for i in range(n):
        row = svc.notify_safely(session, type_, recipient_id=uid, actor_id=None, entity_id=i + 1,
                                audience=audience, dedupe_key=f"{key}:{type_}:{audience}:{i}")
        assert row is not None
        rows.append(row)
    session.flush()
    return rows


# --- authentication / isolation ---------------------------------------------------------

def test_unauthenticated_requests_are_rejected(client):
    c, _ = client
    for method, path in ENDPOINTS:
        assert c.request(method, path).status_code == 401, path
        assert c.request(method, path, headers={"Authorization": "Bearer junk"}).status_code == 401, path


def test_suspended_account_follows_existing_auth_rule(client):
    c, session = client
    uid, h = _user(c)
    _seed(session, uid)
    session.execute(text("UPDATE users SET status='suspended' WHERE id=:u"), {"u": uid})
    session.expire_all()
    for method, path in ENDPOINTS:
        r = c.request(method, path, headers=h)
        assert r.status_code == 403 and r.json() == {"detail": "Account disabled"}, path


def test_users_never_see_or_change_each_others_notifications(client):
    c, session = client
    a_id, ah = _user(c)
    b_id, bh = _user(c)
    (a_row,) = _seed(session, a_id)
    assert c.get("/notifications", headers=bh).json()["total"] == 0
    assert c.get("/notifications/unread-count", headers=bh).json() == {"unread_count": 0}
    r = c.post(f"/notifications/{a_row.id}/read", headers=bh)
    assert r.status_code == 404 and r.json() == {"detail": "Not found"}
    assert c.delete(f"/notifications/{a_row.id}", headers=bh).status_code == 404
    assert c.post("/notifications/read-all", headers=bh).json() == {"updated": 0, "unread_count": 0}
    row = _rows(session, a_id)[0]
    assert row["read_at"] is None and row["deleted_at"] is None  # untouched
    assert c.get("/notifications", headers=ah).json()["items"][0]["id"] == a_row.id
    # Missing id: same 404 as a foreign one (no existence oracle).
    assert c.post("/notifications/999999999/read", headers=ah).json() == {"detail": "Not found"}


def test_list_has_no_user_id_input(client):
    c, session = client
    a_id, _ = _user(c)
    b_id, bh = _user(c)
    _seed(session, a_id)
    # Unknown params are ignored; there is no way to ask for someone else's rows.
    r = c.get("/notifications", params={"user_id": a_id}, headers=bh)
    assert r.status_code == 200 and r.json()["total"] == 0


# --- functionality ------------------------------------------------------------------------

def test_pagination_filters_and_counts(client):
    c, session = client
    uid, h = _user(c)
    _seed(session, uid, 3, "ENQUIRY_NEW")                       # OWNER / PROPERTIES
    _seed(session, uid, 2, "VISIT_CONFIRMED", audience="BUYER")  # BUYER / VISITS
    _seed(session, uid, 1, "ACCOUNT_REACTIVATED")                # ACCOUNT / SYSTEM

    body = c.get("/notifications", params={"page_size": 4}, headers=h).json()
    assert body["total"] == 6 and body["unread_count"] == 6 and len(body["items"]) == 4
    assert body["page"] == 1 and body["page_size"] == 4
    ids = [i["id"] for i in body["items"]]
    assert ids == sorted(ids, reverse=True)  # newest first
    page2 = c.get("/notifications", params={"page_size": 4, "page": 2}, headers=h).json()
    assert len(page2["items"]) == 2 and not set(ids) & {i["id"] for i in page2["items"]}
    assert c.get("/notifications", params={"page": 9}, headers=h).json()["items"] == []

    owner = c.get("/notifications", params={"audience": "OWNER"}, headers=h).json()
    assert owner["total"] == 3 and owner["unread_count"] == 3
    assert {i["audience"] for i in owner["items"]} == {"OWNER"}
    visits = c.get("/notifications", params={"category": "VISITS"}, headers=h).json()
    assert visits["total"] == 2 and {i["category"] for i in visits["items"]} == {"VISITS"}
    assert visits["unread_count"] == 6  # badge ignores category
    assert c.get("/notifications/unread-count", params={"audience": "ACCOUNT"}, headers=h).json() == {"unread_count": 1}
    assert c.get("/notifications/unread-count", headers=h).json() == {"unread_count": 6}

    one = body["items"][0]["id"]
    r = c.post(f"/notifications/{one}/read", headers=h)
    assert r.status_code == 200 and r.json()["is_read"] is True
    assert r.json()["read_at"].endswith("Z")  # explicit UTC, like other timestamps
    unread = c.get("/notifications", params={"unread_only": True}, headers=h).json()
    assert unread["total"] == 5 and one not in {i["id"] for i in unread["items"]}

    for params in ({"page_size": 101}, {"page_size": 0}, {"page": 0}, {"audience": "ADMIN"},
                   {"category": "PROMO"}, {"audience": "owner"}):
        r = c.get("/notifications", params=params, headers=h)
        assert r.status_code == 422 and r.json() == {"detail": "Invalid request"}, params
    assert c.get("/notifications/unread-count", params={"audience": "X"}, headers=h).status_code == 422
    assert c.post("/notifications/read-all", params={"audience": "X"}, headers=h).status_code == 422


def test_item_shape_and_explicit_utc(client):
    c, session = client
    uid, h = _user(c)
    _seed(session, uid)
    item = c.get("/notifications", headers=h).json()["items"][0]
    assert set(item) == {"id", "type", "category", "audience", "title", "body", "entity_type", "entity_id",
                         "property_listing_id", "cover_image_url", "target", "is_read", "read_at", "created_at"}
    assert item["is_read"] is False and item["read_at"] is None
    assert item["created_at"].endswith("Z") or item["created_at"].endswith("+00:00")


def test_mark_read_is_idempotent_and_read_all_respects_audience(client):
    c, session = client
    uid, h = _user(c)
    (owner_row,) = _seed(session, uid, 1, "ENQUIRY_NEW")
    _seed(session, uid, 2, "VISIT_CONFIRMED", audience="BUYER")
    first = c.post(f"/notifications/{owner_row.id}/read", headers=h).json()["read_at"]
    again = c.post(f"/notifications/{owner_row.id}/read", headers=h).json()["read_at"]
    assert first == again  # keeps the first read time

    r = c.post("/notifications/read-all", params={"audience": "BUYER"}, headers=h)
    assert r.status_code == 200 and r.json() == {"updated": 2, "unread_count": 0}
    assert c.post("/notifications/read-all", headers=h).json() == {"updated": 0, "unread_count": 0}
    assert c.get("/notifications/unread-count", headers=h).json() == {"unread_count": 0}


def test_soft_delete_hides_row_and_keeps_dedupe_key(client):
    c, session = client
    uid, h = _user(c)
    rows = _seed(session, uid, 2, "ENQUIRY_NEW", key="del")
    victim = rows[0]
    assert c.delete(f"/notifications/{victim.id}", headers=h).status_code == 204
    assert c.delete(f"/notifications/{victim.id}", headers=h).status_code == 204  # idempotent for owner
    body = c.get("/notifications", headers=h).json()
    assert body["total"] == 1 and victim.id not in {i["id"] for i in body["items"]}
    assert body["unread_count"] == 1
    assert c.post(f"/notifications/{victim.id}/read", headers=h).status_code == 404
    assert c.post("/notifications/read-all", headers=h).json()["updated"] == 1
    stored = {r["id"]: r for r in _rows(session, uid)}
    assert stored[victim.id]["deleted_at"] is not None and stored[victim.id]["read_at"] is None
    # Re-running the same event cannot resurrect it.
    again = svc.notify_safely(session, "ENQUIRY_NEW", recipient_id=uid, actor_id=None, entity_id=1,
                              dedupe_key=stored[victim.id]["dedupe_key"])
    assert again is None and len(_rows(session, uid)) == 2


def test_get_requests_never_write(client):
    c, session = client
    uid, h = _user(c)
    _seed(session, uid, 2)
    c.get("/notifications", headers=h)
    c.get("/notifications/unread-count", headers=h)
    assert all(r["read_at"] is None for r in _rows(session, uid))


def test_cover_image_only_for_live_listing(client):
    c, session = client
    owner_id, oh, buyer_id, bh, lid = _pair(c)
    session.execute(text("INSERT INTO property_media (property_listing_id, media_type, url, order_index) "
                         "VALUES (:l, 'IMAGE', 'https://t.test/cover.jpg', 0)"), {"l": lid})
    assert c.post("/buyer/enquiries", json={"property_listing_id": lid}, headers=bh).status_code == 201
    item = c.get("/notifications", headers=oh).json()["items"][0]
    assert item["cover_image_url"] == "https://t.test/cover.jpg" and item["property_listing_id"] == lid
    session.execute(text("UPDATE property_listings SET deleted_at=NOW() WHERE id=:l"), {"l": lid})
    session.expire_all()
    assert c.get("/notifications", headers=oh).json()["items"][0]["cover_image_url"] is None


# --- reliability --------------------------------------------------------------------------

def test_duplicate_key_is_ignored_via_precheck_and_via_real_unique_violation(client, monkeypatch):
    c, session = client
    uid, _ = _user(c)
    kw = dict(recipient_id=uid, actor_id=None, entity_id=7, dedupe_key="race:1")
    assert svc.notify_safely(session, "ENQUIRY_NEW", **kw) is not None
    assert svc.notify_safely(session, "ENQUIRY_NEW", **kw) is None  # pre-check
    # Simulate a concurrent writer that slipped past the pre-check: the INSERT
    # hits MySQL 1062 inside the savepoint and is treated as already notified.
    monkeypatch.setattr(svc, "_already_notified", lambda *a, **k: False)
    assert svc.notify_safely(session, "ENQUIRY_NEW", **kw) is None
    assert len(_rows(session, uid)) == 1
    # The session (and so the surrounding business transaction) is still usable.
    assert svc.notify_safely(session, "ENQUIRY_NEW", recipient_id=uid, actor_id=None,
                             entity_id=8, dedupe_key="race:2") is not None
    assert len(_rows(session, uid)) == 2


def test_actor_and_missing_recipient_are_skipped(client):
    c, session = client
    uid, _ = _user(c)
    assert svc.notify_safely(session, "ENQUIRY_NEW", recipient_id=uid, actor_id=uid,
                             entity_id=1, dedupe_key="self") is None
    assert svc.notify_safely(session, "ENQUIRY_NEW", recipient_id=None, actor_id=None,
                             entity_id=1, dedupe_key="nobody") is None
    assert _rows(session, uid) == []


def test_db_error_inside_notification_does_not_break_enquiry(client, monkeypatch):
    c, session = client
    owner_id, oh, buyer_id, bh, lid = _pair(c)

    def broken_label(db, listing_id):
        db.execute(text("SELECT * FROM table_that_does_not_exist"))  # real DB error in the savepoint

    monkeypatch.setattr(svc, "_listing_label", broken_label)
    r = c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": "Hello"}, headers=bh)
    assert r.status_code == 201, r.text
    assert session.execute(text("SELECT COUNT(*) FROM enquiries WHERE buyer_user_id=:b"),
                           {"b": buyer_id}).scalar() == 1
    assert session.execute(text("SELECT COUNT(*) FROM enquiry_messages m JOIN enquiries e ON e.id=m.enquiry_id "
                                "WHERE e.buyer_user_id=:b"), {"b": buyer_id}).scalar() == 1
    assert _rows(session, owner_id) == []
    # Business keeps working afterwards in the same session.
    assert c.get("/owner/leads", headers=oh).json()["total"] == 1


def test_exception_inside_notification_does_not_break_visit_or_lead_close(client, monkeypatch):
    c, session = client
    owner_id, oh, buyer_id, bh, lid = _pair(c)
    c.post("/buyer/enquiries", json={"property_listing_id": lid}, headers=bh)

    def boom(*a, **k):
        raise RuntimeError("template bug")

    monkeypatch.setattr(svc, "_listing_label", boom)
    r = c.post("/buyer/visits", json={"property_listing_id": lid, "scheduled_at": FUTURE(4)}, headers=bh)
    assert r.status_code == 201, r.text
    eid = session.execute(text("SELECT id FROM enquiries WHERE buyer_user_id=:b"), {"b": buyer_id}).scalar()
    r = c.patch(f"/owner/leads/{eid}", json={"status": "CLOSED", "closing_reason": "Sold"}, headers=oh)
    assert r.status_code == 200 and r.json()["status"] == "CLOSED"
    assert session.execute(text("SELECT status FROM property_visits WHERE buyer_user_id=:b"),
                           {"b": buyer_id}).scalar() == "CANCELLED"
    # Only the ENQUIRY_NEW from before the patch exists.
    assert [r["type"] for r in _rows(session, owner_id)] == ["ENQUIRY_NEW"]
    assert _rows(session, buyer_id) == []


def test_business_flush_error_is_not_swallowed(client):
    c, session = client
    uid, _ = _user(c)
    # A pending (unflushed) business row that violates a foreign key.
    session.add(EnquiryMessage(enquiry_id=987654321, sender_user_id=uid, body="x",
                               created_at=svc._utcnow_naive()))
    with pytest.raises(IntegrityError):
        svc.notify_safely(session, "ENQUIRY_NEW", recipient_id=uid, actor_id=None,
                          entity_id=1, dedupe_key="never")
    session.rollback()
    assert session.query(Notification).filter(Notification.user_id == uid).count() == 0


def test_notification_rolls_back_with_its_business_transaction(client):
    c, session = client
    uid, _ = _user(c)
    sp = session.begin_nested()  # the router-level savepoint around a business change
    assert svc.notify_safely(session, "ENQUIRY_NEW", recipient_id=uid, actor_id=None,
                             entity_id=1, dedupe_key="rolled-back") is not None
    sp.rollback()  # the business operation failed after the notification was queued
    assert _rows(session, uid) == []


def test_failed_business_request_leaves_no_notification(client):
    c, session = client
    owner_id, oh, buyer_id, bh, lid = _pair(c)
    # Enquiring on your own listing is refused (422) before any lead exists.
    r = c.post("/users/me/role", json={"role": "BUYER"}, headers=oh)
    assert r.status_code == 200
    assert c.post("/buyer/enquiries", json={"property_listing_id": lid}, headers=oh).status_code == 422
    assert _rows(session, owner_id) == []


def test_body_and_title_respect_column_limits(client):
    c, session = client
    owner_id, oh, buyer_id, bh, lid = _pair(c, "T" * 250)
    assert c.post("/buyer/enquiries", json={"property_listing_id": lid}, headers=bh).status_code == 201
    row = _rows(session, owner_id)[0]
    assert len(row["body"]) <= 500 and len(row["title"]) <= 160
    assert "…" in row["body"]  # long listing titles are trimmed, not overflowing


# --- transaction-level failures (M2) --------------------------------------------------
# SIMULATED: these raise the exact SQLAlchemy/PyMySQL exception types from inside
# the notification savepoint. A REAL lock-wait timeout (1205) is reproduced with
# two connections in tests/test_notifications_concurrency.py; a real deadlock is
# not reproduced deterministically here.

def _mysql_operational(code, msg):
    import pymysql
    from sqlalchemy.exc import OperationalError
    return OperationalError("INSERT INTO notifications ...", {}, pymysql.err.OperationalError(code, msg))


@pytest.mark.parametrize("code", [1213, 1205])
def test_simulated_deadlock_and_lock_wait_are_reraised(client, monkeypatch, code):
    from sqlalchemy.exc import OperationalError
    c, session = client
    uid, _ = _user(c)

    def fail(db, listing_id):
        raise _mysql_operational(code, "Deadlock found" if code == 1213 else "Lock wait timeout exceeded")

    monkeypatch.setattr(svc, "_listing_label", fail)
    with pytest.raises(OperationalError) as info:
        svc.notify_safely(session, "ENQUIRY_NEW", recipient_id=uid, actor_id=None,
                          entity_id=1, dedupe_key=f"fatal:{code}")
    assert svc._mysql_code(info.value) == code


def test_simulated_deadlock_fails_the_business_request_instead_of_reporting_success(client, monkeypatch):
    from sqlalchemy.exc import OperationalError
    c, session = client
    owner_id, oh, buyer_id, bh, lid = _pair(c)

    def fail(db, listing_id):
        raise _mysql_operational(1213, "Deadlock found")

    monkeypatch.setattr(svc, "_listing_label", fail)
    # The router never reaches db.commit(); the app's 500 handler re-raises in TestClient.
    with pytest.raises(OperationalError):
        c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": "Hi"}, headers=bh)


def test_pending_rollback_error_is_reraised(client, monkeypatch):
    from sqlalchemy.exc import PendingRollbackError
    c, session = client
    uid, _ = _user(c)

    def fail(db, listing_id):
        raise PendingRollbackError("transaction is inactive")

    monkeypatch.setattr(svc, "_listing_label", fail)
    with pytest.raises(PendingRollbackError):
        svc.notify_safely(session, "ENQUIRY_NEW", recipient_id=uid, actor_id=None,
                          entity_id=1, dedupe_key="pending")


def test_ordinary_operational_error_stays_isolated_in_the_savepoint(client, monkeypatch):
    c, session = client
    owner_id, oh, buyer_id, bh, lid = _pair(c)

    def fail(db, listing_id):  # a statement-level error (not 1213/1205): confined to the savepoint
        raise _mysql_operational(1146, "Table doesn't exist")

    monkeypatch.setattr(svc, "_listing_label", fail)
    r = c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": "Hi"}, headers=bh)
    assert r.status_code == 201, r.text
    assert c.get("/owner/leads", headers=oh).json()["total"] == 1
    assert _rows(session, owner_id) == []
