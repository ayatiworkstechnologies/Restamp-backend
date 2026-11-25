"""Phase 2 — conflicting Owner/Dealer activations under REAL concurrency.

Separate connections and real COMMITs (not the shared rollback session), so
this module runs only with RESTAMP_TEST_REQUIRE_DISPOSABLE=1 (the disposable
test database; restamp_dev is refused by conftest). Every row a test creates
is deleted in a `finally` block.

Proven here: two transactions that try to give the SAME account different
poster types at the same moment (Owner plan vs Dealer plan activation, both
through capabilities.claim_poster_type under the users row lock) can never
both succeed: exactly one wins, the other gets 409 POSTER_TYPE_CONFLICT after
waiting for the winner's commit, and the stored poster type is the winner's.
The same holds for the payment path's non-raising try_claim.
"""
import os
import threading

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.db import SessionLocal, engine
from app.services import capabilities

pytestmark = pytest.mark.skipif(
    os.environ.get("RESTAMP_TEST_REQUIRE_DISPOSABLE") != "1",
    reason="commits real rows: runs only against the disposable test database",
)


@pytest.fixture()
def user_id():
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO users (display_name) VALUES ('P2 concurrency')"))
        uid = conn.execute(text("SELECT LAST_INSERT_ID()")).scalar()
        conn.execute(text("INSERT INTO user_current_role (user_id, role) VALUES (:u, 'BUYER')"), {"u": uid})
    try:
        yield uid
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id=:u"), {"u": uid})


def _race(uid, fn):
    """Run fn(db, role) for OWNER and BROKER in two threads that both hold
    their transaction open until both have attempted the claim."""
    barrier = threading.Barrier(2, timeout=20)
    results: dict[str, object] = {}

    def worker(role):
        db = SessionLocal()
        try:
            barrier.wait()
            try:
                results[role] = fn(db, role)
                # Hold the lock briefly so the other claim really has to wait.
                db.execute(text("SELECT SLEEP(0.3)"))
                db.commit()
            except HTTPException as exc:
                db.rollback()
                results[role] = exc
        finally:
            db.close()

    threads = [threading.Thread(target=worker, args=(r,)) for r in ("OWNER", "BROKER")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return results


def _stored(uid):
    with engine.connect() as conn:
        return conn.execute(text("SELECT poster_type FROM users WHERE id=:u"), {"u": uid}).scalar()


def test_concurrent_claims_exactly_one_wins(user_id):
    results = _race(user_id, lambda db, role: capabilities.claim_poster_type(db, user_id, role))
    errors = {r: v for r, v in results.items() if isinstance(v, HTTPException)}
    stored = _stored(user_id)
    if stored == "BROKER":
        # Dealer won: the Owner claim is a permitted add-on (no change).
        assert errors == {}, results
    else:
        # Owner won: the Dealer claim is refused; the type never flips.
        assert stored == "OWNER" and list(errors) == ["BROKER"], results
        assert errors["BROKER"].status_code == 409 and errors["BROKER"].detail["code"] == "POSTER_TYPE_CONFLICT"


def test_concurrent_try_claims_exactly_one_true(user_id):
    results = _race(user_id, lambda db, role: capabilities.try_claim(db, user_id, role))
    if _stored(user_id) == "BROKER":
        assert results == {"OWNER": True, "BROKER": True}, results
    else:
        assert results == {"OWNER": True, "BROKER": False}, results
    winner = [r for r, ok in results.items() if ok][0]
    assert _stored(user_id) == winner
