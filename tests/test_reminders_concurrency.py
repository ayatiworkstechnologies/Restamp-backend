"""Reminders and push delivery under REAL concurrency (notification Phase 4, revision 2).

Committed rows on the disposable test database, real worker functions with
their own sessions (SessionLocal), several threads at once:
- concurrent and repeated reminder scans create each reminder exactly once
  (in-app row, email twin, push twin);
- concurrent push workers send every (push, device) pair exactly once;
- reschedule / cancellation / owner reply between scan and delivery: both
  channels drop the reminder;
- a worker that claimed a reminder and died: the row is reclaimed after the
  lease and re-checked before sending (sent once if still valid, dropped if not).
Runs only with RESTAMP_TEST_REQUIRE_DISPOSABLE=1 and deletes everything it
creates. In-memory providers only: nothing is really sent.
"""
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import bindparam, text

from app.db import SessionLocal, engine
from app.models import PropertyVisit
from app.services import email as email_svc
from app.services import email_providers, push_providers
from app.services import enquiries as enquiry_svc
from app.services import push as push_svc
from app.services import reminders as rem_svc

pytestmark = pytest.mark.skipif(
    os.environ.get("RESTAMP_TEST_REQUIRE_DISPOSABLE") != "1",
    reason="commits real rows: runs only against the disposable test database",
)

_seq = {"n": 0}


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


class _SlowPush(push_providers.MemoryProvider):
    """Thread-safe in-memory provider; the delay widens race windows."""

    def __init__(self):
        super().__init__()
        self.lock = threading.Lock()

    def send(self, message, idempotency_key):
        time.sleep(0.02)
        with self.lock:
            return super().send(message, idempotency_key)


@pytest.fixture()
def world():
    """Owner (2 devices) + buyer (1 device), both with verified email; a live
    listing; a CONFIRMED visit 20 h ahead; a NEW enquiry 2.5 h old. Removed afterwards."""
    _seq["n"] += 1
    tag = f"{os.getpid() % 1000:03d}{_seq['n']:02d}"
    created = {"users": [], "chain": {}}
    now = _now()
    with engine.begin() as conn:
        ids = {}
        for role in ("owner", "buyer"):
            conn.execute(text("INSERT INTO users (display_name) VALUES (:n)"), {"n": f"RemConc {role} {tag}"})
            ids[role] = conn.execute(text("SELECT LAST_INSERT_ID()")).scalar()
            created["users"].append(ids[role])
            conn.execute(text("INSERT INTO user_current_role (user_id, role) VALUES (:u, :r)"),
                         {"u": ids[role], "r": "OWNER" if role == "owner" else "BUYER"})
            conn.execute(text("INSERT INTO user_emails (user_id, email, verified_at, created_at, updated_at) "
                              "VALUES (:u, :e, :t, :t, :t)"), {"u": ids[role], "e": f"{role}.{tag}@example.com", "t": now})
        chain = created["chain"]
        conn.execute(text("INSERT INTO cities (name) VALUES (:n)"), {"n": f"RemCity{tag}"})
        chain["cities"] = conn.execute(text("SELECT LAST_INSERT_ID()")).scalar()
        conn.execute(text("INSERT INTO districts (city_id, name) VALUES (:c, 'RemD')"), {"c": chain["cities"]})
        chain["districts"] = conn.execute(text("SELECT LAST_INSERT_ID()")).scalar()
        conn.execute(text("INSERT INTO taluks (district_id, name) VALUES (:d, 'RemT')"), {"d": chain["districts"]})
        chain["taluks"] = conn.execute(text("SELECT LAST_INSERT_ID()")).scalar()
        conn.execute(text("INSERT INTO villages (taluk_id, name) VALUES (:t, 'RemV')"), {"t": chain["taluks"]})
        chain["villages"] = conn.execute(text("SELECT LAST_INSERT_ID()")).scalar()
        conn.execute(text("INSERT INTO localities (village_id, name) VALUES (:v, :n)"), {"v": chain["villages"], "n": f"RemLoc{tag}"})
        chain["localities"] = conn.execute(text("SELECT LAST_INSERT_ID()")).scalar()
        conn.execute(text("INSERT INTO postcodes (locality_id, pincode) VALUES (:l, :p)"),
                     {"l": chain["localities"], "p": f"8{tag}"})
        chain["postcodes"] = conn.execute(text("SELECT LAST_INSERT_ID()")).scalar()
        conn.execute(text("INSERT INTO physical_properties (postcode_id, user_id) VALUES (:p, :u)"),
                     {"p": chain["postcodes"], "u": ids["owner"]})
        pp = conn.execute(text("SELECT LAST_INSERT_ID()")).scalar()
        conn.execute(text(
            "INSERT INTO property_listings (physical_property_id, user_id, title, price_paise, transaction_type, "
            "property_type, verification_status, listing_status, submitted_at) "
            "VALUES (:pp, :u, :t, 500000000, 'BUY', 'APARTMENT', 'VERIFIED', 'AVAILABLE', :s)"),
            {"pp": pp, "u": ids["owner"], "t": f"Reminder Race Flat {tag}", "s": now})
        ids["listing"] = conn.execute(text("SELECT LAST_INSERT_ID()")).scalar()
        fernet = push_svc._fernet()
        ids["devices"] = {}
        for role, count in (("owner", 2), ("buyer", 1)):
            ids["devices"][role] = []
            for i in range(count):
                token = f"ExponentPushToken[remconc{tag}{role}{i}aaaaaaaaaaaa]"
                conn.execute(text(
                    "INSERT INTO push_devices (user_id, platform, token_hash, token_enc, token_prefix, status, "
                    "registered_at, refreshed_at, created_at, updated_at) VALUES (:u, 'ios', :h, :e, :p, 'ACTIVE', :t, :t, :t, :t)"),
                    {"u": ids[role], "h": push_svc.hash_token(token), "e": fernet.encrypt(token.encode()).decode(),
                     "p": push_svc.mask_token(token), "t": now})
                ids["devices"][role].append(conn.execute(text("SELECT LAST_INSERT_ID()")).scalar())
    mail = email_providers.MemoryProvider()
    push = _SlowPush()
    email_providers.set_provider_for_tests(mail)
    push_providers.set_provider_for_tests(push)
    s = SessionLocal()
    try:
        visit = PropertyVisit(property_listing_id=ids["listing"], buyer_user_id=ids["buyer"], owner_user_id=ids["owner"],
                              scheduled_at=now + timedelta(hours=20), status="CONFIRMED", initiator="BUYER")
        s.add(visit)
        s.flush()
        ids["visit"] = int(visit.id)
        enquiry_svc.create_enquiry(s, ids["buyer"], ids["listing"], "Is it available?", source_role="BUYER")
        s.commit()
        ids["enquiry"] = s.execute(text("SELECT id FROM enquiries WHERE buyer_user_id=:b"), {"b": ids["buyer"]}).scalar()
        s.execute(text("UPDATE enquiries SET created_at = created_at - INTERVAL 150 MINUTE WHERE id=:e"), {"e": ids["enquiry"]})
        s.commit()
    finally:
        s.close()
    try:
        yield ids, mail, push
    finally:
        email_providers.set_provider_for_tests(None)
        push_providers.set_provider_for_tests(None)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id IN :ids").bindparams(bindparam("ids", expanding=True)),
                         {"ids": created["users"]})
            for table in ("postcodes", "localities", "villages", "taluks", "districts", "cities"):
                if table in chain:
                    conn.execute(text(f"DELETE FROM {table} WHERE id=:i"), {"i": chain[table]})


def _q(sql, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).fetchall()


def _count(table, user_id, template):
    col = "type" if table == "notifications" else "template"
    return _q(f"SELECT COUNT(*) FROM {table} WHERE user_id=:u AND {col}=:t", u=user_id, t=template)[0][0]


def _in_threads(fn, n=4):
    errors, results = [], []
    barrier = threading.Barrier(n)

    def run():
        try:
            barrier.wait(timeout=10)
            results.append(fn())
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(repr(exc))

    threads = [threading.Thread(target=run) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert errors == []
    return results


def _status(table, user_id, template):
    return _q(f"SELECT status, last_error FROM {table} WHERE user_id=:u AND template=:t", u=user_id, t=template)[0]


# --- scans --------------------------------------------------------------------------------

def test_concurrent_and_repeated_scans_create_each_reminder_once(world):
    ids, mail, push = world
    _in_threads(rem_svc.run_scan, n=4)
    for _ in range(3):
        rem_svc.run_scan()  # later passes find nothing new
    for uid in (ids["owner"], ids["buyer"]):
        assert _count("notifications", uid, "VISIT_REMINDER") == 1
        assert _count("email_outbox", uid, "VISIT_REMINDER") == 1
        assert _count("push_outbox", uid, "VISIT_REMINDER") == 1
    assert _count("notifications", ids["owner"], "LEAD_REMINDER") == 1
    assert _count("email_outbox", ids["owner"], "LEAD_REMINDER") == 1
    assert _count("push_outbox", ids["owner"], "LEAD_REMINDER") == 1
    assert _count("notifications", ids["buyer"], "LEAD_REMINDER") == 0  # owners only


def test_concurrent_push_workers_send_each_device_exactly_once(world):
    ids, mail, push = world
    rem_svc.run_scan()
    _in_threads(push_svc.process_outbox, n=3)
    rows = _q("SELECT d.outbox_id, d.device_id, d.status, d.attempts FROM push_deliveries d "
              "JOIN push_outbox o ON o.id = d.outbox_id WHERE o.user_id IN (:a, :b)", a=ids["owner"], b=ids["buyer"])
    assert rows and all(r[2] == "SENT" and r[3] == 1 for r in rows)
    keys = [k for _, k in push.sent]
    assert len(keys) == len(rows) == len(set(keys))  # every (push, device) pair sent once
    # Owner: visit reminder + lead reminder + the enquiry's ENQUIRY_NEW, on 2 devices each.
    owner_rows = [r for r in rows if r[1] in ids["devices"]["owner"]]
    assert len(owner_rows) == 3 * 2


# --- stale events between scan and delivery (real worker sessions) -------------------------

def _deliver():
    email_svc.process_outbox()
    push_svc.process_outbox()


def test_reschedule_between_scan_and_delivery(world):
    ids, mail, push = world
    rem_svc.run_scan()
    with engine.begin() as conn:
        conn.execute(text("UPDATE property_visits SET scheduled_at = scheduled_at + INTERVAL 3 HOUR WHERE id=:v"), {"v": ids["visit"]})
    _deliver()
    for uid in (ids["owner"], ids["buyer"]):
        assert _status("email_outbox", uid, "VISIT_REMINDER") == ("FAILED", "superseded")
        assert _status("push_outbox", uid, "VISIT_REMINDER") == ("FAILED", "superseded")
    rem_svc.run_scan()  # the new time is still inside 24 h: a NEW reminder for the NEW time
    for uid in (ids["owner"], ids["buyer"]):
        assert _count("notifications", uid, "VISIT_REMINDER") == 2


def test_cancellation_between_scan_and_delivery(world):
    ids, mail, push = world
    rem_svc.run_scan()
    with engine.begin() as conn:
        conn.execute(text("UPDATE property_visits SET status='CANCELLED' WHERE id=:v"), {"v": ids["visit"]})
    _deliver()
    for uid in (ids["owner"], ids["buyer"]):
        assert _status("email_outbox", uid, "VISIT_REMINDER") == ("FAILED", "superseded")
        assert _status("push_outbox", uid, "VISIT_REMINDER") == ("FAILED", "superseded")
    assert not [m for m, _ in mail.sent if "visit is coming up" in m.subject]


def test_owner_reply_between_scan_and_delivery(world):
    ids, mail, push = world
    rem_svc.run_scan()
    s = SessionLocal()
    try:
        enquiry_svc.reply_as_owner(s, ids["owner"], ids["enquiry"], "Yes, it is available.")
        s.commit()
    finally:
        s.close()
    _deliver()
    assert _status("email_outbox", ids["owner"], "LEAD_REMINDER") == ("FAILED", "superseded")
    assert _status("push_outbox", ids["owner"], "LEAD_REMINDER") == ("FAILED", "superseded")
    assert not [m for m, _ in mail.sent if "waiting for your reply" in m.subject]
    rem_svc.run_scan()
    assert _count("notifications", ids["owner"], "LEAD_REMINDER") == 1  # answered: never reminded again


# --- restart after a reminder was claimed ------------------------------------------------------

def _claim_and_die(user_id, template):
    with engine.begin() as conn:
        conn.execute(text("UPDATE push_outbox SET status='PROCESSING', attempts=1, locked_at=UTC_TIMESTAMP() "
                          "WHERE user_id=:u AND template=:t"), {"u": user_id, "t": template})


def _expire_lease(user_id, template):
    with engine.begin() as conn:
        conn.execute(text("UPDATE push_outbox SET locked_at = UTC_TIMESTAMP() - INTERVAL :s SECOND "
                          "WHERE user_id=:u AND template=:t"), {"s": push_svc.LEASE_SECONDS + 5, "u": user_id, "t": template})


def test_restart_after_claim_resends_once_when_still_valid(world):
    ids, mail, push = world
    rem_svc.run_scan()
    _claim_and_die(ids["buyer"], "VISIT_REMINDER")
    push_svc.process_outbox()
    assert _status("push_outbox", ids["buyer"], "VISIT_REMINDER")[0] == "PROCESSING"  # live lease: left alone
    _expire_lease(ids["buyer"], "VISIT_REMINDER")
    push_svc.process_outbox()
    push_svc.process_outbox()
    assert _status("push_outbox", ids["buyer"], "VISIT_REMINDER") == ("SENT", None)
    rows = _q("SELECT d.status FROM push_deliveries d JOIN push_outbox o ON o.id=d.outbox_id "
              "WHERE o.user_id=:u AND o.template='VISIT_REMINDER'", u=ids["buyer"])
    assert [r[0] for r in rows] == ["SENT"]
    assert len([k for _, k in push.sent if k.startswith("restamp-push-")]) == len(set(k for _, k in push.sent))


def test_restart_after_claim_rechecks_and_drops_a_cancelled_visit(world):
    ids, mail, push = world
    rem_svc.run_scan()
    _claim_and_die(ids["buyer"], "VISIT_REMINDER")
    with engine.begin() as conn:
        conn.execute(text("UPDATE property_visits SET status='CANCELLED' WHERE id=:v"), {"v": ids["visit"]})
    _expire_lease(ids["buyer"], "VISIT_REMINDER")
    sent_before = len(push.sent)
    push_svc.process_outbox()
    assert _status("push_outbox", ids["buyer"], "VISIT_REMINDER") == ("FAILED", "superseded")
    assert all("buyer" not in m.to for m, _ in push.sent[sent_before:])
