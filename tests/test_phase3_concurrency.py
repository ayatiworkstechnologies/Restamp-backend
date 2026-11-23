"""Phase 3 — contact unlocks and credit purchases under REAL concurrency, plus
the u1v2w3x4y5z6 backfill on real historical rows.

Separate connections and real COMMITs, so this module runs only with
RESTAMP_TEST_REQUIRE_DISPOSABLE=1 (disposable database; restamp_dev is
refused by conftest). Everything created is deleted in `finally` blocks.

Proven: same user + same property in parallel -> one unlock, one charge;
parallel different properties with ONE free unlock (or ONE credit) left ->
exactly one succeeds, the other gets 402, balance never negative; the same
verified payment credited by two parallel confirmations -> +25 once; a retry
after a "timeout" (the first request committed) is free. Migration backfill:
1 / 3 / 5 distinct historical reveals (with duplicates) -> all permanently
unlocked, free usage = min(3, distinct), nothing added or revoked.
"""
import os
import threading
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import bindparam, text

from app.db import SessionLocal, engine
from app.models import Payment
from app.services import contact_credits as svc

pytestmark = pytest.mark.skipif(
    os.environ.get("RESTAMP_TEST_REQUIRE_DISPOSABLE") != "1",
    reason="commits real rows: runs only against the disposable test database",
)

_seq = {"n": 0}


@pytest.fixture()
def world():
    """Committed owner (phone identity) + 5 VERIFIED/AVAILABLE listings + buyer."""
    _seq["n"] += 1
    tag = f"p3c{os.getpid() % 1000:03d}{_seq['n']:02d}"
    created = {"users": [], "chain": {}}
    with engine.begin() as conn:
        ids = {}
        for role in ("owner", "buyer"):
            conn.execute(text("INSERT INTO users (display_name) VALUES (:n)"), {"n": f"{tag} {role}"})
            ids[role] = conn.execute(text("SELECT LAST_INSERT_ID()")).scalar()
            created["users"].append(ids[role])
            conn.execute(text("INSERT INTO user_current_role (user_id, role) VALUES (:u, 'BUYER')"), {"u": ids[role]})
        conn.execute(text("INSERT INTO auth_identities (user_id, provider, provider_identifier) VALUES (:u, 'phone', :p)"),
                     {"u": ids["owner"], "p": f"+9190{ids['owner'] % 10**8:08d}"})
        chain = created["chain"]
        parent = None
        for table, col in (("cities", None), ("districts", "city_id"), ("taluks", "district_id"),
                           ("villages", "taluk_id"), ("localities", "village_id")):
            if col is None:
                conn.execute(text(f"INSERT INTO {table} (name) VALUES (:n)"), {"n": f"{tag}-{table}"})
            else:
                conn.execute(text(f"INSERT INTO {table} ({col}, name) VALUES (:p, :n)"), {"p": parent, "n": f"{tag}-{table}"})
            parent = conn.execute(text("SELECT LAST_INSERT_ID()")).scalar()
            chain[table] = parent
        conn.execute(text("INSERT INTO postcodes (locality_id, pincode) VALUES (:l, :p)"),
                     {"l": parent, "p": f"9{_seq['n']:02d}{os.getpid() % 1000:03d}"})
        chain["postcodes"] = conn.execute(text("SELECT LAST_INSERT_ID()")).scalar()
        conn.execute(text("INSERT INTO physical_properties (postcode_id, user_id) VALUES (:p, :u)"),
                     {"p": chain["postcodes"], "u": ids["owner"]})
        pp = conn.execute(text("SELECT LAST_INSERT_ID()")).scalar()
        listings = []
        for i in range(5):
            conn.execute(text("INSERT INTO property_listings (physical_property_id, user_id, title, price_paise, price_period,"
                              " verification_status, listing_status, transaction_type, property_type) VALUES"
                              " (:pp, :u, :t, 100, 'TOTAL', 'VERIFIED', 'AVAILABLE', 'BUY', 'APARTMENT')"),
                         {"pp": pp, "u": ids["owner"], "t": f"{tag} {i}"})
            listings.append(conn.execute(text("SELECT LAST_INSERT_ID()")).scalar())
    try:
        yield {"buyer": ids["buyer"], "owner": ids["owner"], "listings": listings}
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM contact_credit_ledger WHERE user_id IN :ids").bindparams(
                bindparam("ids", expanding=True)), {"ids": created["users"]})
            conn.execute(text("DELETE FROM payments WHERE user_id IN :ids").bindparams(
                bindparam("ids", expanding=True)), {"ids": created["users"]})
            conn.execute(text("DELETE FROM users WHERE id IN :ids").bindparams(
                bindparam("ids", expanding=True)), {"ids": created["users"]})
            for table in ("postcodes", "localities", "villages", "taluks", "districts", "cities"):
                if table in chain:
                    conn.execute(text(f"DELETE FROM {table} WHERE id=:i"), {"i": chain[table]})


def _set_account(uid, free_used, balance):
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO contact_credit_accounts (user_id, free_unlocks_used, credit_balance, created_at,"
                          " updated_at) VALUES (:u, :f, :b, UTC_TIMESTAMP(), UTC_TIMESTAMP())"
                          " ON DUPLICATE KEY UPDATE free_unlocks_used=:f, credit_balance=:b"),
                     {"u": uid, "f": free_used, "b": balance})


def _parallel(fns):
    barrier = threading.Barrier(len(fns), timeout=20)
    results = [None] * len(fns)

    def run(i, fn):
        db = SessionLocal()
        try:
            barrier.wait()
            try:
                results[i] = fn(db)
                db.execute(text("SELECT SLEEP(0.2)"))
                db.commit()
            except HTTPException as exc:
                db.rollback()
                results[i] = exc
        finally:
            db.close()

    threads = [threading.Thread(target=run, args=(i, f)) for i, f in enumerate(fns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return results


def _state(uid):
    with engine.connect() as conn:
        acct = conn.execute(text("SELECT free_unlocks_used, credit_balance FROM contact_credit_accounts WHERE user_id=:u"),
                            {"u": uid}).fetchone()
        unlocks = conn.execute(text("SELECT COUNT(*) FROM property_contact_unlocks WHERE user_id=:u"), {"u": uid}).scalar()
        ledger = conn.execute(text("SELECT COUNT(*) FROM contact_credit_ledger WHERE user_id=:u"), {"u": uid}).scalar()
    return (tuple(acct) if acct else None), unlocks, ledger


def test_same_property_in_parallel_charges_once(world):
    uid, lid = world["buyer"], world["listings"][0]
    res = _parallel([lambda db: svc.unlock(db, uid, lid)] * 3)
    labels = sorted(r["unlock_status"] for r in res)
    assert labels == ["ALREADY_UNLOCKED", "ALREADY_UNLOCKED", "UNLOCKED_FREE"], res
    assert _state(uid) == ((1, 0), 1, 1)


def test_last_free_unlock_cannot_be_spent_twice(world):
    uid, l1, l2 = world["buyer"], world["listings"][0], world["listings"][1]
    _set_account(uid, 2, 0)
    res = _parallel([lambda db: svc.unlock(db, uid, l1), lambda db: svc.unlock(db, uid, l2)])
    ok = [r for r in res if isinstance(r, dict)]
    err = [r for r in res if isinstance(r, HTTPException)]
    assert len(ok) == 1 and len(err) == 1 and err[0].status_code == 402, res
    assert _state(uid) == ((3, 0), 1, 1)


def test_last_credit_cannot_be_spent_twice(world):
    uid, l1, l2, l3 = world["buyer"], *world["listings"][:3]
    _set_account(uid, 3, 1)
    res = _parallel([lambda db: svc.unlock(db, uid, l1), lambda db: svc.unlock(db, uid, l2),
                     lambda db: svc.unlock(db, uid, l3)])
    ok = [r for r in res if isinstance(r, dict)]
    assert len(ok) == 1 and ok[0]["unlock_status"] == "UNLOCKED_CREDIT"
    assert sum(isinstance(r, HTTPException) and r.status_code == 402 for r in res) == 2
    assert _state(uid) == ((3, 0), 1, 1)  # balance 0, never negative


def test_parallel_payment_confirmations_credit_once(world):
    uid = world["buyer"]
    with engine.begin() as conn:
        plan_id = conn.execute(text("SELECT id FROM plans WHERE code=:c"), {"c": svc.PACK_CODE}).scalar()
        conn.execute(text("INSERT INTO payments (user_id, provider, provider_payment_id, amount_paise, currency, status,"
                          " plan_id, idempotency_key, paid_at) VALUES (:u, 'RAZORPAY', :pp, 49900, 'INR', 'SUCCESS', :pl,"
                          " :k, UTC_TIMESTAMP())"),
                     {"u": uid, "pp": f"pay_{uuid.uuid4().hex[:12]}", "pl": plan_id, "k": uuid.uuid4().hex})
        pid = conn.execute(text("SELECT LAST_INSERT_ID()")).scalar()

    def confirm(db):
        # Same order as payments._confirm_and_activate: payment row lock first.
        pay = db.query(Payment).filter(Payment.id == pid).with_for_update().one()
        return svc.grant_for_payment(db, pay)

    res = _parallel([confirm, confirm, confirm])
    assert sorted(r["activated"] for r in res) == [False, False, True]
    assert _state(uid)[0] == (0, 25)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM contact_credit_ledger WHERE payment_id=:p"), {"p": pid}).scalar() == 1


def test_retry_after_timeout_is_free(world):
    uid, lid = world["buyer"], world["listings"][0]
    _set_account(uid, 3, 2)
    db = SessionLocal()
    try:
        assert svc.unlock(db, uid, lid)["unlock_status"] == "UNLOCKED_CREDIT"
        db.commit()  # committed; the client never saw the answer
    finally:
        db.close()
    db = SessionLocal()
    try:
        assert svc.unlock(db, uid, lid)["unlock_status"] == "ALREADY_UNLOCKED"
        db.commit()
    finally:
        db.close()
    assert _state(uid) == ((3, 1), 1, 1)


# --- migration backfill on real rows ---------------------------------------------------


def _alembic(cmd, rev):
    from alembic import command
    from alembic.config import Config

    cfg = Config(os.path.join(os.path.dirname(os.path.dirname(__file__)), "alembic.ini"))
    getattr(command, cmd)(cfg, rev)


def test_migration_backfills_existing_reveals(world):
    buyers = {}
    ls = world["listings"]
    with engine.begin() as conn:
        for n_distinct in (1, 3, 5):
            conn.execute(text("INSERT INTO users (display_name) VALUES ('p3 legacy')"))
            u = conn.execute(text("SELECT LAST_INSERT_ID()")).scalar()
            buyers[n_distinct] = u
            for i in range(n_distinct):
                for _dup in range(2):  # every property revealed twice
                    conn.execute(text("INSERT INTO contact_reveal_audits (user_id, property_listing_id, revealed_at)"
                                      " VALUES (:u, :l, UTC_TIMESTAMP() - INTERVAL :d DAY)"),
                                 {"u": u, "l": ls[i], "d": 100 - i})
    try:
        _alembic("downgrade", "t0u1v2w3x4y5")
        _alembic("upgrade", "head")
        with engine.connect() as conn:
            for n_distinct, u in buyers.items():
                acct = conn.execute(text("SELECT free_unlocks_used, credit_balance FROM contact_credit_accounts"
                                         " WHERE user_id=:u"), {"u": u}).fetchone()
                assert tuple(acct) == (min(3, n_distinct), 0)
                rows = conn.execute(text("SELECT property_listing_id, source FROM property_contact_unlocks WHERE user_id=:u"
                                         " ORDER BY unlocked_at"), {"u": u}).fetchall()
                assert [r[0] for r in rows] == ls[:n_distinct] and {r[1] for r in rows} == {"LEGACY"}
                free = conn.execute(text("SELECT SUM(free_delta), COUNT(*) FROM contact_credit_ledger WHERE user_id=:u"
                                         " AND entry_type='LEGACY_UNLOCK'"), {"u": u}).fetchone()
                assert (int(free[0]), free[1]) == (min(3, n_distinct), n_distinct)
            pack = conn.execute(text("SELECT status, price_paise, role, visible FROM plans WHERE code=:c"),
                                {"c": svc.PACK_CODE}).fetchone()
            assert tuple(pack)[1:] == (0, None, 0)
        # Behaviour after migration: 1-distinct user keeps 2 free; 5-distinct none, all 5 still open.
        db = SessionLocal()
        try:
            assert svc.unlock(db, buyers[1], ls[0])["unlock_status"] == "ALREADY_UNLOCKED"
            assert svc.unlock(db, buyers[1], ls[1])["free_unlocks_remaining"] == 1
            for lid in ls:
                assert svc.unlock(db, buyers[5], lid)["unlock_status"] == "ALREADY_UNLOCKED"
            db.rollback()
        finally:
            db.close()
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM contact_credit_ledger WHERE user_id IN :ids").bindparams(
                bindparam("ids", expanding=True)), {"ids": list(buyers.values())})
            conn.execute(text("DELETE FROM users WHERE id IN :ids").bindparams(
                bindparam("ids", expanding=True)), {"ids": list(buyers.values())})
