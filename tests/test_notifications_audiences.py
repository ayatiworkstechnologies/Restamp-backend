"""M5: multi-audience notification filtering on the real endpoints.

GET /notifications, GET /notifications/unread-count and POST
/notifications/read-all accept a REPEATED `audience` query parameter
(`?audience=BUYER&audience=OWNER`) and act on the union of the requested
audiences. Omitted = every audience; one value = unchanged behaviour; every
repeated value is honoured (never only the last); duplicates never duplicate
rows or counts; unknown or comma-joined values are a 422. Everything stays
scoped to the authenticated user. Rollback-isolated (`client` fixture).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import Notification
from tests.conftest import auth_header
from tests.test_owner_leads import fresh_buyer

# Per user: 2 BUYER (1 unread), 3 OWNER (2 unread), 1 ACCOUNT (unread).
SEED = [("BUYER", True), ("BUYER", False), ("OWNER", True), ("OWNER", True), ("OWNER", False), ("ACCOUNT", True)]
CATEGORY = {"BUYER": "PROPERTIES", "OWNER": "VISITS", "ACCOUNT": "SYSTEM"}
TYPE = {"BUYER": "ENQUIRY_REPLY", "OWNER": "VISIT_REQUESTED", "ACCOUNT": "ACCOUNT_REACTIVATED"}


def _seed(session, user_id, tag):
    base = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0) - timedelta(hours=1)
    ids = {}
    for i, (aud, unread) in enumerate(SEED):
        row = Notification(user_id=user_id, audience=aud, type=TYPE[aud], category=CATEGORY[aud],
                           title=f"{aud} {i}", body=f"{tag} body {i}", dedupe_key=f"m5:{tag}:{i}",
                           created_at=base + timedelta(minutes=i), read_at=None if unread else base)
        session.add(row)
        session.flush()
        ids[row.id] = (aud, unread)
    return ids


@pytest.fixture()
def two_users(client):
    c, session = client
    a, b = fresh_buyer(c), fresh_buyer(c)
    a_ids = _seed(session, a["user_id"], "A")
    b_ids = _seed(session, b["user_id"], "B")
    return c, session, auth_header(a["access_token"]), a_ids, auth_header(b["access_token"]), b_ids


def _expected(ids, audiences):
    """Row ids (newest first), total and unread for an audience selection."""
    chosen = [rid for rid, (aud, _) in ids.items() if not audiences or aud in audiences]
    unread = sum(1 for rid in chosen if ids[rid][1])
    return sorted(chosen, reverse=True), len(chosen), unread


def _get(c, h, path, audiences):
    params = [("audience", a) for a in audiences] + [("page_size", "100")]
    return c.get(path, params=params, headers=h)


SELECTIONS = [(), ("BUYER",), ("OWNER",), ("ACCOUNT",), ("BUYER", "OWNER"), ("BUYER", "OWNER", "ACCOUNT"),
              ("OWNER", "ACCOUNT")]


@pytest.mark.parametrize("audiences", SELECTIONS, ids=lambda a: "+".join(a) or "none")
def test_list_and_unread_count_use_identical_semantics(two_users, audiences):
    c, _, ah, a_ids, _, _ = two_users
    ids, total, unread = _expected(a_ids, set(audiences))
    r = _get(c, ah, "/notifications", audiences)
    assert r.status_code == 200, r.text
    body = r.json()
    got = [n["id"] for n in body["items"]]
    assert got == ids  # exactly the union, newest first, each row once
    assert len(set(got)) == len(got)
    assert body["total"] == total and body["unread_count"] == unread
    assert {n["audience"] for n in body["items"]} == ({a for a, _ in SEED} if not audiences else set(audiences))
    count = _get(c, ah, "/notifications/unread-count", audiences)
    assert count.status_code == 200 and count.json() == {"unread_count": unread}


def test_every_repeated_value_is_honoured_not_only_the_last(two_users):
    c, _, ah, a_ids, _, _ = two_users
    both = _get(c, ah, "/notifications", ("BUYER", "OWNER")).json()
    last_only = _get(c, ah, "/notifications", ("OWNER",)).json()
    assert both["total"] == 5 and last_only["total"] == 3
    assert {n["audience"] for n in both["items"]} == {"BUYER", "OWNER"}


def test_duplicate_audience_values_do_not_duplicate_rows_or_counts(two_users):
    c, _, ah, a_ids, _, _ = two_users
    ids, total, unread = _expected(a_ids, {"BUYER", "OWNER"})
    r = _get(c, ah, "/notifications", ("OWNER", "BUYER", "OWNER", "OWNER"))
    assert r.status_code == 200
    assert [n["id"] for n in r.json()["items"]] == ids
    assert r.json()["total"] == total and r.json()["unread_count"] == unread
    assert _get(c, ah, "/notifications/unread-count", ("BUYER", "BUYER")).json() == {"unread_count": 1}


def test_pagination_over_a_multi_audience_union(two_users):
    c, _, ah, a_ids, _, _ = two_users
    ids, total, _ = _expected(a_ids, {"BUYER", "OWNER"})
    pages = []
    for page in (1, 2, 3):
        params = [("audience", "BUYER"), ("audience", "OWNER"), ("page", str(page)), ("page_size", "2")]
        body = c.get("/notifications", params=params, headers=ah).json()
        assert body["total"] == total
        pages.extend(n["id"] for n in body["items"])
    assert pages == ids  # no gap, no repeat across pages


@pytest.mark.parametrize("bad", [["ADMIN"], ["BUYER,OWNER"], ["BUYER", "NOPE"], [""]])
def test_unknown_or_comma_joined_values_are_422_on_every_endpoint(two_users, bad):
    c, session, ah, a_ids, _, _ = two_users
    params = [("audience", v) for v in bad]
    for method, path in (("get", "/notifications"), ("get", "/notifications/unread-count"),
                         ("post", "/notifications/read-all")):
        r = getattr(c, method)(path, params=params, headers=ah)
        assert r.status_code == 422 and r.json() == {"detail": "Invalid request"}, (method, path, bad)
    # Nothing was marked read by the refused read-all.
    assert _get(c, ah, "/notifications/unread-count", ()).json() == {"unread_count": 4}


def test_read_all_affects_only_the_selected_audiences(two_users):
    c, _, ah, a_ids, bh, _ = two_users
    r = c.post("/notifications/read-all", params=[("audience", "BUYER"), ("audience", "OWNER")], headers=ah)
    assert r.status_code == 200
    assert r.json() == {"updated": 3, "unread_count": 0}  # 1 BUYER + 2 OWNER unread, now read
    assert _get(c, ah, "/notifications/unread-count", ("ACCOUNT",)).json() == {"unread_count": 1}  # untouched
    assert _get(c, ah, "/notifications/unread-count", ()).json() == {"unread_count": 1}
    # Repeat is a no-op.
    again = c.post("/notifications/read-all", params=[("audience", "BUYER"), ("audience", "OWNER")], headers=ah)
    assert again.json() == {"updated": 0, "unread_count": 0}
    # User B's rows are untouched.
    assert _get(c, bh, "/notifications/unread-count", ()).json() == {"unread_count": 4}


def test_single_audience_behaviour_unchanged(two_users):
    c, _, ah, a_ids, _, _ = two_users
    for aud in ("BUYER", "OWNER", "ACCOUNT"):
        ids, total, unread = _expected(a_ids, {aud})
        body = c.get("/notifications", params={"audience": aud, "page_size": 100}, headers=ah).json()
        assert [n["id"] for n in body["items"]] == ids and body["total"] == total and body["unread_count"] == unread
        assert c.get("/notifications/unread-count", params={"audience": aud}, headers=ah).json() == {
            "unread_count": unread}
    r = c.post("/notifications/read-all", params={"audience": "OWNER"}, headers=ah)
    assert r.json() == {"updated": 2, "unread_count": 0}
    assert _get(c, ah, "/notifications/unread-count", ()).json() == {"unread_count": 2}


def test_user_isolation_across_every_audience_selection(two_users):
    c, _, ah, a_ids, bh, b_ids = two_users
    for audiences in SELECTIONS:
        seen = {n["id"] for n in _get(c, ah, "/notifications", audiences).json()["items"]}
        assert seen.isdisjoint(b_ids), audiences
        assert seen <= set(a_ids)
    b_row = next(iter(b_ids))
    assert c.post(f"/notifications/{b_row}/read", headers=ah).status_code == 404
    assert c.delete(f"/notifications/{b_row}", headers=ah).status_code == 404
    assert c.post("/notifications/read-all", params=[("audience", "BUYER"), ("audience", "OWNER"),
                                                     ("audience", "ACCOUNT")], headers=ah).status_code == 200
    assert _get(c, bh, "/notifications/unread-count", ("BUYER", "OWNER", "ACCOUNT")).json() == {"unread_count": 4}
    assert all(n["is_read"] is False or b_ids[n["id"]][1] is False
               for n in _get(c, bh, "/notifications", ()).json()["items"])


def test_unauthenticated_requests_are_refused(two_users):
    c, *_ = two_users
    for method, path in (("get", "/notifications"), ("get", "/notifications/unread-count"),
                         ("post", "/notifications/read-all")):
        r = getattr(c, method)(path, params=[("audience", "BUYER"), ("audience", "OWNER")])
        assert r.status_code == 401, (method, path)
