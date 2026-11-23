"""Email outbox under REAL concurrency (notification Phase 3).

Two worker passes run at the same time on separate MySQL connections against
committed rows: every email is sent exactly once (FOR UPDATE SKIP LOCKED +
the PROCESSING lease). These tests commit rows, so they run only with
RESTAMP_TEST_REQUIRE_DISPOSABLE=1 (the disposable test database) and delete
everything they create. No real email is sent (in-memory provider).
"""
import os
import threading
import time
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from app.db import SessionLocal, engine
from app.models import EmailOutbox
from app.services import email as email_svc
from app.services import email_providers

pytestmark = pytest.mark.skipif(
    os.environ.get("RESTAMP_TEST_REQUIRE_DISPOSABLE") != "1",
    reason="commits real rows: runs only against the disposable test database",
)

ROWS = 24


class _SlowProvider:
    """Thread-safe in-memory provider; the delay widens the race window."""

    name = "memory"
    enabled = True

    def __init__(self):
        self.lock = threading.Lock()
        self.keys = []

    def send(self, message, idempotency_key):
        time.sleep(0.02)
        with self.lock:
            self.keys.append(idempotency_key)
        return f"m-{idempotency_key}"


@pytest.fixture()
def queued():
    """A committed user with a verified address and ROWS due outbox rows."""
    now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO users (display_name) VALUES (:n)"), {"n": f"EmailConc {os.getpid()}"})
        uid = conn.execute(text("SELECT LAST_INSERT_ID()")).scalar()
        conn.execute(text("INSERT INTO user_emails (user_id, email, verified_at, created_at, updated_at) "
                          "VALUES (:u, 'conc@example.com', :t, :t, :t)"), {"u": uid, "t": now})
        for i in range(ROWS):
            conn.execute(text(
                "INSERT INTO email_outbox (user_id, category, template, to_email, payload, dedupe_key, status, "
                "attempts, next_attempt_at, created_at) VALUES (:u, 'LISTING', 'LISTING_VERIFIED', "
                "'conc@example.com', :p, :k, 'PENDING', 0, :t, :t)"),
                {"u": uid, "p": '{"message": "Your listing has been verified.", "target": "OWNER_LISTING"}',
                 "k": f"conc:{i}", "t": now})
    provider = _SlowProvider()
    email_providers.set_provider_for_tests(provider)
    try:
        yield uid, provider
    finally:
        email_providers.set_provider_for_tests(None)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})  # cascades to email rows


def test_two_workers_send_every_email_exactly_once(queued):
    uid, provider = queued
    results, errors = [], []
    barrier = threading.Barrier(3)

    def worker():
        try:
            barrier.wait(timeout=10)
            total = 0
            for _ in range(10):
                counts = email_svc.process_outbox(limit=5)
                total += counts["sent"]
                if not any(counts.values()):
                    break
            results.append(total)
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert errors == []
    assert sum(results) == ROWS
    assert len(provider.keys) == ROWS and len(set(provider.keys)) == ROWS  # no row was sent twice
    db = SessionLocal()
    try:
        rows = db.query(EmailOutbox).filter(EmailOutbox.user_id == uid).all()
        assert len(rows) == ROWS
        assert all(r.status == "SENT" and r.attempts == 1 and r.locked_at is None and r.sent_at for r in rows)
    finally:
        db.close()


def test_a_row_locked_by_another_worker_is_skipped_not_waited_for(queued):
    uid, provider = queued
    holder = SessionLocal()
    other = SessionLocal()
    try:
        first = holder.query(EmailOutbox).filter(EmailOutbox.user_id == uid).order_by(EmailOutbox.id).first()
        holder.query(EmailOutbox).filter(EmailOutbox.id == first.id).with_for_update().one()  # worker A mid-claim
        started = time.monotonic()
        claimed = email_svc._claim(other, ROWS)  # worker B
        assert time.monotonic() - started < 5  # SKIP LOCKED: no waiting on A's row
        ids = {i for i, _ in claimed}
        assert first.id not in ids and len(ids) == ROWS - 1
    finally:
        holder.rollback()
        holder.close()
        other.close()
