"""Phase 3 — permanent property-contact unlocks, 3 lifetime free unlocks,
purchased 25-credit packs, legacy buyer plans, Dealer separation, security.
Rollback-isolated (concurrency + migration tests live in separate modules
that run only on the disposable database).
"""
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.services import payments as payments_svc
from tests.conftest import (
    auth_header,
    captured_otp_code,
    fixture_admin_headers,
    grant_test_posting_plan,
    make_postcode,
)
from tests.test_admin import _admin
from tests.test_payments import (  # noqa: F401  (pytest fixtures)
    _captured_payload,
    _checkout_sig,
    _webhook,
    fake_rzp,
    razorpay,
)
from tests.test_viewed_properties import _seed_listing

_n = {"i": 0, "p": 0}


def _phone():
    _n["i"] += 1
    return f"+916884{_n['i']:06d}"


def _login(c, phone, account_type=None):
    assert c.post("/auth/otp/request", json={"phone": phone}).status_code == 200
    body = {"phone": phone, "code": captured_otp_code(phone)}
    if account_type:
        body["account_type"] = account_type
    r = c.post("/auth/otp/verify", json=body)
    assert r.status_code == 200, r.text
    return auth_header(r.json()["access_token"]), r.json()["user_id"]


def _user(c, account_type=None):
    phone = _phone()
    h, uid = _login(c, phone, account_type)
    return h, uid, phone


def _reveal(c, h, lid):
    return c.post(f"/buyer/listings/{lid}/contact", headers=h)


def _summary(c, h):
    r = c.get("/buyer/contact-credits", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def _listings(session, n):
    return [_seed_listing(session, title=f"P3 listing {i}") for i in range(n)]


def _price_pack(c, price=49900):
    r = c.put("/admin/contact-credits/pack", headers=fixture_admin_headers(c),
              json={"price_paise": price, "active": True, "note": "test price"})
    assert r.status_code == 200, r.text
    return r.json()


def _buy_pack(c, h, pay_id):
    co = c.post("/payments/checkout", headers=h, json={"kind": "credits", "code": "RESTAMP_CONTACT_CREDITS_25"})
    assert co.status_code == 200, co.text
    co = co.json()
    r = c.post("/payments/verify", headers=h, json={"razorpay_order_id": co["razorpay_order_id"],
                                                    "razorpay_payment_id": pay_id,
                                                    "razorpay_signature": _checkout_sig(co["razorpay_order_id"], pay_id)})
    return co, r


def _counts(session, uid):
    unlocks = session.execute(text("SELECT COUNT(*) FROM property_contact_unlocks WHERE user_id=:u"), {"u": uid}).scalar()
    ledger = session.execute(text("SELECT COUNT(*) FROM contact_credit_ledger WHERE user_id=:u"), {"u": uid}).scalar()
    return unlocks, ledger


# --- 5.1 free lifetime unlocks ---------------------------------------------------------


def test_three_free_lifetime_unlocks_then_credits_required(client):
    c, session = client
    lids = _listings(session, 4)
    h, uid, _ = _user(c)
    s = _summary(c, h)
    assert (s["free_unlocks_total"], s["free_unlocks_used"], s["free_unlocks_remaining"], s["credit_balance"]) == (3, 0, 3, 0)
    for i, lid in enumerate(lids[:3]):
        r = _reveal(c, h, lid)
        assert r.status_code == 200, r.text
        assert r.json()["unlock_status"] == "UNLOCKED_FREE" and r.json()["phone"]
        assert r.json()["free_unlocks_remaining"] == 2 - i
    r = _reveal(c, h, lids[3])
    assert r.status_code == 402
    d = r.json()["detail"]
    assert d["code"] == "BUYER_PLAN_REQUIRED" and d["reason"] == "CONTACT_CREDITS_REQUIRED"
    assert d["free_unlocks_used"] == 3 and d["credit_balance"] == 0 and len(d["phone_last4"]) == 4
    assert "phone" not in d
    assert _counts(session, uid) == (3, 3)
    # Reopen / refresh / retry: no charge
    for _ in range(3):
        r = _reveal(c, h, lids[0])
        assert r.status_code == 200 and r.json()["unlock_status"] == "ALREADY_UNLOCKED"
    assert _counts(session, uid) == (3, 3)
    assert c.get(f"/buyer/unlocks/{lids[0]}", headers=h).json()["unlocked"] is True
    assert c.get(f"/buyer/unlocks/{lids[3]}", headers=h).json()["unlocked"] is False


def test_unlocks_survive_login_mode_switch_time_and_subscriptions(client):
    c, session = client
    ah, _ = _admin(c, session)
    lids = _listings(session, 4)
    h, uid, phone = _user(c, "OWNER")
    assert _reveal(c, h, lids[0]).json()["unlock_status"] == "UNLOCKED_FREE"
    # New login (other device): same account, same usage
    h2, uid2 = _login(c, phone)
    assert uid2 == uid and _summary(c, h2)["free_unlocks_used"] == 1
    assert _reveal(c, h2, lids[0]).json()["unlock_status"] == "ALREADY_UNLOCKED"
    # Mode switches (Owner mode is the starting mode here)
    for mode in ("BUYER", "OWNER"):
        assert c.post("/users/me/role", json={"role": mode}, headers=h2).status_code == 200
        assert _reveal(c, h2, lids[0]).json()["unlock_status"] == "ALREADY_UNLOCKED"
    # Owner plan activation, expiry and renewal never touch contact usage
    plan = c.post("/admin/plans", headers=ah, json={"code": "TEST_P3_OWNER_1", "role": "OWNER", "name": "P3",
                                                    "price_paise": 100, "duration_days": 30}).json()["code"]
    assert c.post(f"/admin/users/{uid}/subscription", headers=ah, json={"plan_code": plan, "reason": "t"}).status_code == 200
    session.execute(text("UPDATE subscriptions SET end_date = NOW() - INTERVAL 1 DAY WHERE user_id=:u"), {"u": uid})
    session.flush()
    assert c.post(f"/admin/users/{uid}/subscription", headers=ah, json={"plan_code": plan, "reason": "t"}).status_code == 200
    assert _summary(c, h2)["free_unlocks_used"] == 1
    # No 30-day reset: age everything 400 days, still counted
    session.execute(text("UPDATE property_contact_unlocks SET unlocked_at = NOW() - INTERVAL 400 DAY WHERE user_id=:u"),
                    {"u": uid})
    session.execute(text("UPDATE contact_reveal_audits SET revealed_at = NOW() - INTERVAL 400 DAY WHERE user_id=:u"),
                    {"u": uid})
    session.flush()
    for lid in lids[1:3]:
        assert _reveal(c, h2, lid).json()["unlock_status"] == "UNLOCKED_FREE"
    assert _reveal(c, h2, lids[3]).status_code == 402
    assert _reveal(c, h2, lids[0]).json()["unlock_status"] == "ALREADY_UNLOCKED"


def test_invalid_and_own_listings_consume_nothing(client):
    c, session = client
    h, uid, _ = _user(c)
    assert _reveal(c, h, 999999999).status_code == 404
    hidden = _seed_listing(session, verification="PENDING")
    assert _reveal(c, h, hidden).status_code == 404
    assert c.post("/buyer/listings/abc/contact", headers=h).status_code == 422
    assert _reveal(c, None, hidden).status_code == 401
    assert _counts(session, uid) == (0, 0)
    assert session.execute(text("SELECT COUNT(*) FROM contact_credit_accounts WHERE user_id=:u"), {"u": uid}).scalar() == 0


# --- 5.3 purchased credits -------------------------------------------------------------


def test_pack_not_purchasable_until_priced_and_admin_only_price(client, razorpay, fake_rzp):
    c, session = client
    h, uid, _ = _user(c)
    s = _summary(c, h)
    assert s["pack"]["credits"] == 25 and s["pack"]["available"] is False and s["pack"]["price_paise"] is None
    r = c.post("/payments/checkout", headers=h, json={"kind": "credits", "code": "RESTAMP_CONTACT_CREDITS_25"})
    assert r.status_code == 404 and fake_rzp.calls == []
    # Price is admin-only (configure_platform)
    body = {"price_paise": 49900, "active": True}
    assert c.put("/admin/contact-credits/pack", json=body).status_code == 401
    assert c.put("/admin/contact-credits/pack", headers=h, json=body).status_code == 403
    rh, _ = _admin(c, session, "LISTING_REVIEWER")
    assert c.put("/admin/contact-credits/pack", headers=rh, json=body).status_code == 403
    assert c.put("/admin/contact-credits/pack", headers=fixture_admin_headers(c), json={"price_paise": 5}).status_code == 422
    out = _price_pack(c, 49900)
    assert out == {"code": "RESTAMP_CONTACT_CREDITS_25", "credits": 25, "price_paise": 49900, "currency": "INR",
                   "available": True}
    assert session.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='CONTACT_CREDIT_PACK_UPDATED'")).scalar() >= 1
    # The pack is never a subscription plan
    plans = c.get("/subscriptions/plans").json()["items"]
    assert all(p["code"] != "RESTAMP_CONTACT_CREDITS_25" for p in plans)
    # Wrong code under the credits kind
    assert c.post("/payments/checkout", headers=h, json={"kind": "credits", "code": "RESTAMP_BUYER_PLUS"}).status_code == 404


def test_purchase_adds_25_once_and_credit_unlocks(client, razorpay, fake_rzp):
    c, session = client
    _price_pack(c, 49900)
    lids = _listings(session, 5)
    h, uid, _ = _user(c)
    for lid in lids[:3]:
        assert _reveal(c, h, lid).status_code == 200
    # Bad signature: nothing added
    co = c.post("/payments/checkout", headers=h, json={"kind": "credits", "code": "RESTAMP_CONTACT_CREDITS_25"}).json()
    assert co["amount_paise"] == 49900 and co["plan"]["kind"] == "credits"
    bad = c.post("/payments/verify", headers=h, json={"razorpay_order_id": co["razorpay_order_id"],
                                                      "razorpay_payment_id": "pay_P3X", "razorpay_signature": "0" * 64})
    assert bad.status_code == 400 and _summary(c, h)["credit_balance"] == 0
    # Failed payment webhook: nothing added
    assert _webhook(c, {"event": "payment.failed", "payload": {"payment": {"entity": {
        "id": "pay_P3F", "order_id": co["razorpay_order_id"], "amount": 49900, "status": "failed"}}}}).status_code == 200
    assert _summary(c, h)["credit_balance"] == 0
    # Verified: +25 exactly once (verify retry + webhook replays)
    r = c.post("/payments/verify", headers=h, json={"razorpay_order_id": co["razorpay_order_id"],
                                                    "razorpay_payment_id": "pay_P3A",
                                                    "razorpay_signature": _checkout_sig(co["razorpay_order_id"], "pay_P3A")})
    assert r.status_code == 200 and r.json()["activated"] is True and r.json()["credits_added"] == 25
    again = c.post("/payments/verify", headers=h, json={"razorpay_order_id": co["razorpay_order_id"],
                                                        "razorpay_payment_id": "pay_P3A",
                                                        "razorpay_signature": _checkout_sig(co["razorpay_order_id"], "pay_P3A")})
    assert again.json()["activated"] is False and again.json()["credits_added"] == 0
    for evt in ("evt_p3_1", "evt_p3_2"):
        assert _webhook(c, _captured_payload(co["razorpay_order_id"], 49900, "pay_P3A"), evt).status_code == 200
    assert _summary(c, h)["credit_balance"] == 25
    assert session.execute(text("SELECT COUNT(*) FROM contact_credit_ledger WHERE user_id=:u AND entry_type='PURCHASE'"),
                           {"u": uid}).scalar() == 1
    # Credit unlock: -1, reopen free
    r = _reveal(c, h, lids[3])
    assert r.json()["unlock_status"] == "UNLOCKED_CREDIT" and r.json()["credit_balance"] == 24
    assert _reveal(c, h, lids[3]).json()["unlock_status"] == "ALREADY_UNLOCKED"
    assert _summary(c, h)["credit_balance"] == 24
    hist = c.get("/buyer/contact-credits/history", headers=h).json()
    types = [i["entry_type"] for i in hist["items"]]
    assert types[:2] == ["CREDIT_UNLOCK", "PURCHASE"] and types.count("FREE_UNLOCK") == 3
    credit_row = hist["items"][0]
    assert credit_row["credits_delta"] == -1 and credit_row["balance_after"] == 24 and credit_row["listing_id"] == lids[3]
    # Payment history (own only) shows the purchase
    assert any(p.get("plan_code") == "RESTAMP_CONTACT_CREDITS_25" for p in c.get("/payments/history", headers=h).json()["items"])


def test_credits_never_expire_and_survive_poster_expiry(client):
    c, session = client
    lids = _listings(session, 2)
    for account_type, role in (("OWNER", "OWNER"), ("DEALER", "BROKER")):
        h, uid, _ = _user(c, account_type)
        grant_test_posting_plan(c, h, role)
        session.execute(text("INSERT INTO contact_credit_accounts (user_id, free_unlocks_used, credit_balance, created_at,"
                             " updated_at) VALUES (:u, 3, 2, NOW() - INTERVAL 800 DAY, NOW() - INTERVAL 800 DAY)"), {"u": uid})
        session.execute(text("UPDATE subscriptions SET end_date = NOW() - INTERVAL 1 DAY WHERE user_id=:u"), {"u": uid})
        session.flush()
        assert _summary(c, h)["credit_balance"] == 2
        c.post("/users/me/role", json={"role": "BUYER"}, headers=h)
        r = _reveal(c, h, lids[0])
        assert r.json()["unlock_status"] == "UNLOCKED_CREDIT" and r.json()["credit_balance"] == 1


def test_negative_balance_impossible(client):
    c, session = client
    _h, uid, _ = _user(c)
    session.execute(text("INSERT INTO contact_credit_accounts (user_id, free_unlocks_used, credit_balance, created_at,"
                         " updated_at) VALUES (:u, 3, 0, NOW(), NOW())"), {"u": uid})
    session.flush()
    sp = session.begin_nested()
    with pytest.raises(OperationalError, match="3819"):  # MySQL CHECK violation
        session.execute(text("UPDATE contact_credit_accounts SET credit_balance = -1 WHERE user_id=:u"), {"u": uid})
        session.flush()
    sp.rollback()
    sp = session.begin_nested()
    with pytest.raises(OperationalError, match="3819"):  # MySQL CHECK violation
        session.execute(text("UPDATE contact_credit_accounts SET free_unlocks_used = 4 WHERE user_id=:u"), {"u": uid})
        session.flush()
    sp.rollback()


# --- 4.5 legacy PLUS / PRO / UNLIMITED -------------------------------------------------------


def _buyer_plan(c, ah, limit, days=30):
    _n["p"] += 1
    body = {"code": f"TEST_P3_BUY_{_n['p']:04d}", "role": "BUYER", "name": f"P3 buyer {_n['p']}",
            "price_paise": 100, "duration_days": days}
    if limit is not None:
        body["lead_limit"] = limit
    r = c.post("/admin/plans", headers=ah, json=body)
    assert r.status_code == 201, r.text
    return r.json()["code"]


def test_active_legacy_plan_honoured_then_free_unlocks(client):
    c, session = client
    ah, _ = _admin(c, session)
    lids = _listings(session, 4)
    h, uid, _ = _user(c)
    assert c.post(f"/admin/users/{uid}/subscription", headers=ah,
                  json={"plan_code": _buyer_plan(c, ah, 2), "reason": "legacy"}).status_code == 200
    s = _summary(c, h)
    assert s["legacy_plan"]["lead_limit"] == 2 and s["legacy_plan"]["leads_remaining"] == 2
    assert [_reveal(c, h, lid).json()["unlock_status"] for lid in lids[:2]] == ["UNLOCKED_PLAN"] * 2
    assert _summary(c, h)["free_unlocks_used"] == 0  # plan unlocks do not consume free unlocks
    assert _reveal(c, h, lids[2]).json()["unlock_status"] == "UNLOCKED_FREE"
    # After the plan ends, plan unlocks stay permanent
    session.execute(text("UPDATE subscriptions SET end_date = NOW() - INTERVAL 1 DAY WHERE user_id=:u"), {"u": uid})
    session.flush()
    assert _summary(c, h)["legacy_plan"] is None
    assert _reveal(c, h, lids[0]).json()["unlock_status"] == "ALREADY_UNLOCKED"
    assert _reveal(c, h, lids[3]).json()["unlock_status"] == "UNLOCKED_FREE"


@pytest.mark.parametrize("code", ["RESTAMP_BUYER_PLUS", "RESTAMP_BUYER_PRO", "RESTAMP_BUYER_UNLIMITED"])
def test_existing_catalogue_plans_keep_working(client, code):
    c, session = client
    if session.execute(text("SELECT COUNT(*) FROM plans WHERE code=:c"), {"c": code}).scalar() == 0:
        pytest.skip(f"{code} plan row not seeded in this database")
    ah, _ = _admin(c, session)
    lids = _listings(session, 4)
    h, uid, _ = _user(c)
    assert c.post(f"/admin/users/{uid}/subscription", headers=ah, json={"plan_code": code, "reason": "legacy"}).status_code == 200
    assert [_reveal(c, h, lid).json()["unlock_status"] for lid in lids] == ["UNLOCKED_PLAN"] * 4
    assert _summary(c, h)["free_unlocks_used"] == 0 and _summary(c, h)["credit_balance"] == 0


def test_unlimited_legacy_plan_has_no_cap(client):
    c, session = client
    ah, _ = _admin(c, session)
    lids = _listings(session, 5)
    h, uid, _ = _user(c)
    assert c.post(f"/admin/users/{uid}/subscription", headers=ah,
                  json={"plan_code": _buyer_plan(c, ah, None), "reason": "legacy"}).status_code == 200
    assert {_reveal(c, h, lid).json()["unlock_status"] for lid in lids} == {"UNLOCKED_PLAN"}


def test_legacy_plan_bought_with_verified_payment(client, razorpay, fake_rzp):
    c, session = client
    if session.execute(text("SELECT COUNT(*) FROM plans WHERE code='RESTAMP_BUYER_PLUS'")).scalar() == 0:
        pytest.skip("RESTAMP_BUYER_PLUS not seeded")
    lids = _listings(session, 2)
    h, uid, _ = _user(c)
    co = c.post("/payments/checkout", headers=h, json={"kind": "buyer", "code": "RESTAMP_BUYER_PLUS"}).json()
    r = c.post("/payments/verify", headers=h, json={"razorpay_order_id": co["razorpay_order_id"],
                                                    "razorpay_payment_id": "pay_P3L",
                                                    "razorpay_signature": _checkout_sig(co["razorpay_order_id"], "pay_P3L")})
    assert r.status_code == 200 and r.json()["activated"] is True
    assert _reveal(c, h, lids[0]).json()["unlock_status"] == "UNLOCKED_PLAN"


# --- 4.7 Dealer separation ----------------------------------------------------------------


def _dealer_covering(c, session, lid):
    h, uid, _ = _user(c, "DEALER")
    pc = session.execute(text("SELECT pp.postcode_id FROM property_listings l JOIN physical_properties pp "
                              "ON pp.id = l.physical_property_id WHERE l.id=:l"), {"l": lid}).scalar()
    session.execute(text("INSERT INTO broker_postcode_access_current (broker_user_id, postcode_id, granted_by) "
                         "VALUES (:u, :p, :u)"), {"u": uid, "p": pc})
    session.flush()
    return h, uid


def test_dealer_area_access_is_separate_from_buyer_credits(client):
    c, session = client
    lid, other = _listings(session, 2)
    h, uid = _dealer_covering(c, session, lid)
    r = _reveal(c, h, lid)
    assert r.status_code == 200 and r.json()["unlock_status"] == "DEALER_AREA"
    assert _counts(session, uid) == (0, 0)
    s = _summary(c, h)
    assert s["free_unlocks_used"] == 0 and s["credit_balance"] == 0 and s["unlocked_properties"] == 0
    assert c.get(f"/buyer/unlocks/{lid}", headers=h).json()["unlocked"] is False  # no buyer unlock granted
    # Outside its areas in Dealer mode: the Dealer gate, never buyer credits
    r = _reveal(c, h, other)
    assert r.status_code == 402 and r.json()["detail"]["code"] == "AGENT_AREA_NOT_IN_PLAN"
    # The same account in BUYER mode uses the common buyer system
    assert c.post("/users/me/role", json={"role": "BUYER"}, headers=h).status_code == 200
    assert _reveal(c, h, other).json()["unlock_status"] == "UNLOCKED_FREE"
    assert _summary(c, h)["free_unlocks_used"] == 1


def test_conversion_never_turns_locality_access_into_credits(client):
    c, session = client
    lid = _listings(session, 1)[0]
    h, uid = _dealer_covering(c, session, lid)
    ah = fixture_admin_headers(c)
    r = c.post(f"/admin/users/{uid}/poster-type", headers=ah, json={"poster_type": "OWNER", "reason": "convert"})
    assert r.status_code == 200, r.text
    s = _summary(c, h)
    assert s["credit_balance"] == 0 and s["free_unlocks_used"] == 0 and s["unlocked_properties"] == 0


# --- 5.6 security ------------------------------------------------------------------------------


def test_no_client_control_over_entitlements(client):
    c, session = client
    lids = _listings(session, 2)
    h, uid, _ = _user(c)
    other_h, other_uid, _ = _user(c)
    assert c.post(f"/buyer/listings/{lids[0]}/contact", headers=other_h,
                  json={"user_id": uid, "credit_balance": 99, "unlocked": True}).status_code == 200
    # Unlock went to the TOKEN's user, never the payload's
    assert _counts(session, uid) == (0, 0) and _counts(session, other_uid) == (1, 1)
    assert _summary(c, h)["credit_balance"] == 0
    # No write endpoints for balances/unlocks
    for method, path in (("post", "/buyer/contact-credits"), ("put", "/buyer/contact-credits"),
                         ("post", f"/buyer/unlocks/{lids[1]}"), ("post", "/buyer/unlocks")):
        assert getattr(c, method)(path, headers=h, json={"credit_balance": 99}).status_code in (404, 405)
    assert _summary(c, h)["credit_balance"] == 0
    # Own history only; the unlock lookup never returns a phone
    assert c.get("/buyer/unlocks", headers=h).json()["total"] == 0
    assert c.get("/buyer/contact-credits/history", headers=h).json()["total"] == 0
    body = c.get(f"/buyer/unlocks/{lids[0]}", headers=other_h).json()
    assert body["unlocked"] is True and "phone" not in body
    for path in ("/buyer/contact-credits", "/buyer/unlocks", "/buyer/contact-credits/history", f"/buyer/unlocks/{lids[0]}"):
        assert c.get(path).status_code == 401
    # Another user's payment invoice is not visible
    pid = session.execute(text("SELECT MAX(id) FROM payments")).scalar()
    if pid:
        owner = session.execute(text("SELECT user_id FROM payments WHERE id=:p"), {"p": pid}).scalar()
        if owner != uid:
            assert c.get(f"/payments/{pid}/invoice", headers=h).status_code == 404
