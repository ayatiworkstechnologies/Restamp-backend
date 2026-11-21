"""Notification concurrency and transaction-failure behaviour against REAL MySQL
with separate connections and real COMMITs (not the shared rollback session).

These tests commit rows, so they run only with RESTAMP_TEST_REQUIRE_DISPOSABLE=1
(the disposable test database; restamp_dev is refused by conftest). Every row a
test creates is deleted in a `finally` block: users (cascading to their
listings, enquiries, messages, notifications, roles) and the location chain,
whose pincode is outside the Chennai range so no other test can allocate it.

Proven here:
- Follow-up coalescing under REPEATABLE READ: a request whose snapshot predates
  another request's committed notification still sees it (sequential and
  truly blocking two-thread interleavings), with a control test showing the
  old snapshot-only check really does produce the duplicate.
- The create_enquiry IntegrityError race branch: the loser blocks on the
  active_enquiries unique key, appends to the winner's enquiry and is
  coalesced into the winner's ENQUIRY_NEW (no ENQUIRY_MESSAGE).
- A real lock-wait timeout (1205) inside notify_safely() is re-raised and the
  business change is never committed.
- The real worker entry point (run_worker_once: its own SessionLocal and
  commit) notifies the owner exactly once.
"""
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.db import SessionLocal, engine
from app.services import enquiries as enquiry_svc
from app.services import notifications as svc

pytestmark = pytest.mark.skipif(
    os.environ.get("RESTAMP_TEST_REQUIRE_DISPOSABLE") != "1",
    reason="commits real rows: runs only against the disposable test database",
)

_seq = {"n": 0}


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


@pytest.fixture()
def world():
    """Committed owner + buyer + live listing; everything removed afterwards."""
    _seq["n"] += 1
    tag = f"{os.getpid() % 1000:03d}{_seq['n']:02d}"
    created = {"users": [], "chain": None}
    with engine.begin() as conn:
        ids = {}
        for role in ("owner", "buyer"):
            conn.execute(text("INSERT INTO users (display_name) VALUES (:n)"), {"n": f"Conc {role} {tag}"})
            ids[role] = conn.execute(text("SELECT LAST_INSERT_ID()")).scalar()
            created["users"].append(ids[role])
        conn.execute(text("INSERT INTO user_current_role (user_id, role) VALUES (:u, 'OWNER')"), {"u": ids["owner"]})
        conn.execute(text("INSERT INTO user_current_role (user_id, role) VALUES (:u, 'BUYER')"), {"u": ids["buyer"]})
        chain = {}
        conn.execute(text("INSERT INTO cities (name) VALUES (:n)"), {"n": f"ConcCity{tag}"})
        chain["cities"] = conn.execute(text("SELECT LAST_INSERT_ID()")).scalar()
        conn.execute(text("INSERT INTO districts (city_id, name) VALUES (:c, 'ConcD')"), {"c": chain["cities"]})
        chain["districts"] = conn.execute(text("SELECT LAST_INSERT_ID()")).scalar()
        conn.execute(text("INSERT INTO taluks (district_id, name) VALUES (:d, 'ConcT')"), {"d": chain["districts"]})
        chain["taluks"] = conn.execute(text("SELECT LAST_INSERT_ID()")).scalar()
        conn.execute(text("INSERT INTO villages (taluk_id, name) VALUES (:t, 'ConcV')"), {"t": chain["taluks"]})
        chain["villages"] = conn.execute(text("SELECT LAST_INSERT_ID()")).scalar()
        conn.execute(text("INSERT INTO localities (village_id, name) VALUES (:v, :n)"),
                     {"v": chain["villages"], "n": f"ConcLoc{tag}"})
        chain["localities"] = conn.execute(text("SELECT LAST_INSERT_ID()")).scalar()
        conn.execute(text("INSERT INTO postcodes (locality_id, pincode) VALUES (:l, :p)"),
                     {"l": chain["localities"], "p": f"9{tag}"})
        chain["postcodes"] = conn.execute(text("SELECT LAST_INSERT_ID()")).scalar()
        created["chain"] = chain
        conn.execute(text("INSERT INTO physical_properties (postcode_id, user_id) VALUES (:p, :u)"),
                     {"p": chain["postcodes"], "u": ids["owner"]})
        pp = conn.execute(text("SELECT LAST_INSERT_ID()")).scalar()
        conn.execute(text(
            "INSERT INTO property_listings (physical_property_id, user_id, title, price_paise, transaction_type, "
            "property_type, verification_status, listing_status, submitted_at) "
            "VALUES (:pp, :u, :t, 500000000, 'BUY', 'APARTMENT', 'VERIFIED', 'AVAILABLE', :s)"),
            {"pp": pp, "u": ids["owner"], "t": f"Concurrency Flat {tag}", "s": _now()})
        ids["listing"] = conn.execute(text("SELECT LAST_INSERT_ID()")).scalar()
    try:
        yield ids
    finally:
        with engine.begin() as conn:
            # Users cascade to physical properties, listings (and their
            # verifications/media), enquiries, messages, notifications, roles.
            conn.execute(text("DELETE FROM users WHERE id IN :ids").bindparams(
                __import__("sqlalchemy").bindparam("ids", expanding=True)), {"ids": created["users"]})
            chain = created["chain"] or {}
            for table in ("postcodes", "localities", "villages", "taluks", "districts", "cities"):
                if table in chain:
                    conn.execute(text(f"DELETE FROM {table} WHERE id=:i"), {"i": chain[table]})


def _fresh(sql, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).fetchall()


def _owner_types(owner_id):
    return [r[0] for r in _fresh("SELECT type FROM notifications WHERE user_id=:u ORDER BY id", u=owner_id)]


def _send(session, w, message):
    return enquiry_svc.create_enquiry(session, w["buyer"], w["listing"], message, source_role="BUYER")


def _open_enquiry_with_read_new(w):
    """Committed enquiry whose ENQUIRY_NEW the owner has already read, so the
    next follow-up is NOT coalesced into it (it should notify exactly once)."""
    s = SessionLocal()
    try:
        _, created = _send(s, w, "Opening message")
        assert created
        s.commit()
    finally:
        s.close()
    with engine.begin() as conn:
        conn.execute(text("UPDATE notifications SET read_at=:t WHERE user_id=:u"), {"t": _now(), "u": w["owner"]})
    assert _owner_types(w["owner"]) == ["ENQUIRY_NEW"]


def _snapshot(session, w):
    """Open the session's transaction and fix its REPEATABLE READ view NOW
    (a consistent read of notifications, as the real request path does
    before it takes the enquiry lock)."""
    session.execute(text("SELECT COUNT(*) FROM notifications WHERE user_id=:u"), {"u": w["owner"]}).scalar()


def test_follow_up_after_a_committed_follow_up_is_coalesced_despite_old_snapshot(world):
    w = world
    _open_enquiry_with_read_new(w)
    sb, sa = SessionLocal(), SessionLocal()
    try:
        _snapshot(sb, w)                     # B's read view predates A's commit
        _send(sa, w, "Follow-up from A")
        sa.commit()                          # A inserted + committed ENQUIRY_MESSAGE
        assert _owner_types(w["owner"]) == ["ENQUIRY_NEW", "ENQUIRY_MESSAGE"]
        # B's own snapshot really is stale (that was the bug's root cause) ...
        assert sb.execute(text("SELECT COUNT(*) FROM notifications WHERE user_id=:u AND type='ENQUIRY_MESSAGE'"),
                          {"u": w["owner"]}).scalar() == 0
        _send(sb, w, "Follow-up from B")     # ... yet the coalescing check sees A's row
        sb.commit()
    finally:
        sa.close()
        sb.close()
    assert _owner_types(w["owner"]) == ["ENQUIRY_NEW", "ENQUIRY_MESSAGE"]
    bodies = [r[0] for r in _fresh(
        "SELECT m.body FROM enquiry_messages m JOIN enquiries e ON e.id=m.enquiry_id "
        "WHERE e.buyer_user_id=:b ORDER BY m.id", b=w["buyer"])]
    assert bodies == ["Opening message", "Follow-up from A", "Follow-up from B"]  # business intact


def test_control_snapshot_only_check_would_duplicate(world, monkeypatch):
    """Control: with the previous snapshot-only coalescing query, the same
    interleaving stores TWO follow-up notifications. Proves the test above
    exercises the real race rather than passing trivially."""
    def snapshot_only(db, recipient_id, entity_type, entity_id, types, since):
        from app.models import Notification
        return db.query(Notification.id).filter(
            Notification.user_id == recipient_id, Notification.entity_type == entity_type,
            Notification.entity_id == entity_id, Notification.type.in_(types),
            Notification.read_at.is_(None), Notification.deleted_at.is_(None),
            Notification.created_at >= since).first() is not None

    monkeypatch.setattr(svc, "_recent_unread_exists", snapshot_only)
    w = world
    _open_enquiry_with_read_new(w)
    sb, sa = SessionLocal(), SessionLocal()
    try:
        _snapshot(sb, w)
        _send(sa, w, "Follow-up from A")
        sa.commit()
        _send(sb, w, "Follow-up from B")
        sb.commit()
    finally:
        sa.close()
        sb.close()
    assert _owner_types(w["owner"]) == ["ENQUIRY_NEW", "ENQUIRY_MESSAGE", "ENQUIRY_MESSAGE"]


def test_concurrent_follow_up_waits_on_enquiry_lock_then_coalesces(world):
    w = world
    _open_enquiry_with_read_new(w)
    sa, sb = SessionLocal(), SessionLocal()
    result = {}
    try:
        _snapshot(sb, w)                     # B's view is fixed before A does anything
        _send(sa, w, "Follow-up from A")     # A holds the enquiry row lock, uncommitted

        def run_b():
            try:
                _send(sb, w, "Follow-up from B")  # blocks on SELECT ... FOR UPDATE
                result["done_at"] = time.monotonic()
                sb.commit()
            except Exception as exc:  # surfaced below
                result["error"] = exc

        t = threading.Thread(target=run_b)
        t.start()
        time.sleep(1.5)
        assert t.is_alive(), "B should be waiting on A's enquiry row lock"
        committed_at = time.monotonic()
        sa.commit()
        t.join(timeout=30)
        assert not t.is_alive()
    finally:
        sa.close()
        sb.close()
    assert "error" not in result, result.get("error")
    assert result["done_at"] >= committed_at  # B proceeded only after A committed
    assert _owner_types(w["owner"]) == ["ENQUIRY_NEW", "ENQUIRY_MESSAGE"]


def test_create_enquiry_integrity_error_race_branch_is_coalesced(world):
    w = world
    sa, sb = SessionLocal(), SessionLocal()
    result = {}
    try:
        _snapshot(sb, w)
        body_a, created_a = _send(sa, w, "First from A")   # new enquiry + ENQUIRY_NEW, uncommitted
        assert created_a

        def run_b():
            try:
                # Blocks on the active_enquiries unique key, then (after A's
                # commit) gets IntegrityError -> race branch -> appends to A's.
                result["out"] = _send(sb, w, "Second from B")
                sb.commit()
            except Exception as exc:
                result["error"] = exc

        t = threading.Thread(target=run_b)
        t.start()
        time.sleep(1.5)
        assert t.is_alive(), "B should be waiting on the active_enquiries unique key"
        sa.commit()
        t.join(timeout=30)
        assert not t.is_alive()
    finally:
        sa.close()
        sb.close()
    assert "error" not in result, result.get("error")
    body_b, created_b = result["out"]
    assert created_b is False and body_b["id"] == body_a["id"]  # B reused A's enquiry
    assert len(_fresh("SELECT id FROM enquiries WHERE buyer_user_id=:b", b=w["buyer"])) == 1
    assert len(_fresh("SELECT m.id FROM enquiry_messages m JOIN enquiries e ON e.id=m.enquiry_id "
                      "WHERE e.buyer_user_id=:b", b=w["buyer"])) == 2
    # One "new enquiry" for the owner; B's message coalesced into it.
    assert _owner_types(w["owner"]) == ["ENQUIRY_NEW"]
    assert _fresh("SELECT id FROM notifications WHERE user_id=:u", u=w["buyer"]) == []  # actor


def test_real_lock_wait_timeout_is_reraised_and_business_change_not_committed(world):
    w = world
    holder = engine.connect()
    s = SessionLocal()
    try:
        holder.begin()
        # Another transaction holds an X lock on the recipient's users row; the
        # notification INSERT's foreign-key check needs a shared lock on it.
        holder.execute(text("SELECT id FROM users WHERE id=:u FOR UPDATE"), {"u": w["owner"]})
        s.execute(text("SET SESSION innodb_lock_wait_timeout = 1"))
        # The business change of this "request".
        s.execute(text("UPDATE property_listings SET title='Changed by failed request' WHERE id=:l"),
                  {"l": w["listing"]})
        with pytest.raises(OperationalError) as info:
            svc.notify_safely(s, "LISTING_VERIFIED", recipient_id=w["owner"], actor_id=None,
                              entity_id=w["listing"], listing_id=w["listing"], dedupe_key="lockwait:1")
        assert svc._mysql_code(info.value) == 1205  # re-raised, not swallowed
        s.rollback()  # what get_db's close does after the 500
    finally:
        s.close()
        holder.rollback()
        holder.close()
    assert _fresh("SELECT title FROM property_listings WHERE id=:l", l=w["listing"])[0][0] != \
        "Changed by failed request"
    assert _owner_types(w["owner"]) == []


def test_real_worker_entry_point_notifies_owner_exactly_once(world):
    from app.worker import run_worker_once

    w = world
    past = _now() - timedelta(minutes=5)
    with engine.begin() as conn:
        conn.execute(text("UPDATE property_listings SET verification_status='PENDING', submitted_at=:t "
                          "WHERE id=:l"), {"t": past, "l": w["listing"]})
    assert run_worker_once() >= 1  # the worker's own SessionLocal + commit
    assert _fresh("SELECT verification_status FROM property_listings WHERE id=:l",
                  l=w["listing"])[0][0] == "VERIFIED"
    rows = _fresh("SELECT type, audience, property_listing_id FROM notifications WHERE user_id=:u", u=w["owner"])
    assert [tuple(r) for r in rows] == [("LISTING_VERIFIED", "OWNER", w["listing"])]
    assert run_worker_once() == 0  # nothing due any more
    assert len(_fresh("SELECT id FROM notifications WHERE user_id=:u", u=w["owner"])) == 1
