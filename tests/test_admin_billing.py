"""Admin billing reads: GET /admin/subscriptions and GET /admin/payments.

Read-only views over subscriptions / payments. No payment gateway exists, so the
only payments are the SIMULATED ones written by the buyer/agent subscribe
endpoints: every row must carry `simulated: true`, and a simulated
provider_payment_id is withheld (it is not a transaction). Covered: 401 / 403
for non-admins and for an admin role without reports.view, filters,
pagination, derived EXPIRED status, and that the reads never write.
Rollback-isolated; counts asserted as deltas.
"""
from sqlalchemy import text

from tests.conftest import auth_header
from tests.test_admin import _admin, _user


def _subscribe(c, h, code="RESTAMP_BUYER_PLUS"):
    r = c.post(f"/buyer/plans/{code}/subscribe", headers=h)
    assert r.status_code == 200, r.text


def test_billing_reads_require_admin(client):
    c, session = client
    for path in ("/admin/subscriptions", "/admin/payments"):
        assert c.get(path).status_code == 401
        for role in ("BUYER", "OWNER", "BROKER"):
            h, _ = _user(c, role)
            r = c.get(path, headers=h)
            assert r.status_code == 403 and r.json()["detail"] == "Forbidden"
        # forged role claims in a tampered token do not help: invalid signature -> 401
        assert c.get(path, headers=auth_header("x.y.z")).status_code == 401


def test_billing_reads_need_reports_view(client):
    c, session = client
    h, _ = _admin(c, session, admin_role="LISTING_REVIEWER")
    assert c.get("/admin/subscriptions", headers=h).status_code == 403
    assert c.get("/admin/payments", headers=h).status_code == 403
    h2, _ = _admin(c, session, admin_role="OPERATIONS_MANAGER")
    assert c.get("/admin/subscriptions", headers=h2).status_code == 200
    assert c.get("/admin/payments", headers=h2).status_code == 200


def test_simulated_subscription_is_flagged_never_a_real_payment(client):
    c, session = client
    ah, _ = _admin(c, session)
    bh, buyer_id = _user(c, "BUYER")
    _subscribe(c, bh)

    subs = c.get(f"/admin/subscriptions?user_id={buyer_id}", headers=ah).json()
    assert subs["total"] == 1
    row = subs["items"][0]
    assert row["plan_code"] == "RESTAMP_BUYER_PLUS"
    assert row["status"] == "ACTIVE"
    assert row["simulated"] is True and row["payment_provider"] == "SIMULATED"
    assert row["user_id"] == buyer_id and row["end_date"] is not None

    pays = c.get(f"/admin/payments?user_id={buyer_id}", headers=ah).json()
    assert pays["total"] == 1
    p = pays["items"][0]
    assert p["simulated"] is True and p["provider"] == "SIMULATED"
    assert p["provider_payment_id"] is None  # a SIM-… id is never shown as a transaction id
    assert p["amount_paise"] == row["price_paise"]
    assert set(p) .isdisjoint({"idempotency_key", "provider_order_id"})


def test_expired_status_is_derived_and_filters_work(client):
    c, session = client
    ah, _ = _admin(c, session)
    bh, buyer_id = _user(c, "BUYER")
    _subscribe(c, bh)
    session.execute(text("UPDATE subscriptions SET end_date = NOW() - INTERVAL 1 DAY WHERE user_id=:u"), {"u": buyer_id})
    session.flush()

    rows = c.get(f"/admin/subscriptions?user_id={buyer_id}", headers=ah).json()["items"]
    assert rows[0]["status"] == "EXPIRED"
    assert c.get(f"/admin/subscriptions?user_id={buyer_id}&status=EXPIRED", headers=ah).json()["total"] == 1
    assert c.get(f"/admin/subscriptions?user_id={buyer_id}&status=ACTIVE", headers=ah).json()["total"] == 0
    assert c.get(f"/admin/payments?user_id={buyer_id}&status=FAILED", headers=ah).json()["total"] == 0
    assert c.get("/admin/subscriptions?status=BOGUS", headers=ah).status_code == 422
    assert c.get("/admin/payments?page_size=101", headers=ah).status_code == 422


def test_pagination_and_reads_never_write(client):
    c, session = client
    ah, _ = _admin(c, session)
    bh, buyer_id = _user(c, "BUYER")
    _subscribe(c, bh, "RESTAMP_BUYER_PLUS")
    _subscribe(c, bh, "RESTAMP_BUYER_PRO")
    before = session.execute(text("SELECT COUNT(*) FROM audit_logs")).scalar()

    p1 = c.get(f"/admin/subscriptions?user_id={buyer_id}&page_size=1&page=1", headers=ah).json()
    p2 = c.get(f"/admin/subscriptions?user_id={buyer_id}&page_size=1&page=2", headers=ah).json()
    assert p1["total"] == 2 and len(p1["items"]) == 1 and len(p2["items"]) == 1
    assert p1["items"][0]["id"] != p2["items"][0]["id"]
    assert p1["items"][0]["id"] > p2["items"][0]["id"]  # newest first
    c.get("/admin/payments", headers=ah)
    assert session.execute(text("SELECT COUNT(*) FROM audit_logs")).scalar() == before
