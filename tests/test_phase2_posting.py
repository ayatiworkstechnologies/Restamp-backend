"""Phase 2 — subscription lifecycle and posting enforcement (real gate: this
module is NOT in conftest's legacy allowlist).

Covered: Owner/Dealer posting without, with expired and with active paid
plans; listing limits; Dealer free trial never authorizes posting (incl.
expired trial and bypass attempts); read access, buyer access and public
visibility after expiry; renewal and upgrade on the same account; payment
bypass; admin Owner<->Dealer conversion (restrictions, audit, listings kept,
Dealer locality grants disabled with history kept); Razorpay owner plans:
verify, bad signature, duplicate confirmation, superseded-payment replay,
poster-type conflict -> manual review (payment kept SUCCESS, no plan, retries
and webhook replays idempotent).
"""
import json

from sqlalchemy import text

from tests.conftest import admin_verify, auth_header, captured_otp_code, fresh_pincode, make_postcode
from tests.test_admin import _admin
from tests.test_payments import (  # noqa: F401  (pytest fixtures)
    FakeRazorpay,
    _captured_payload,
    _checkout_sig,
    _webhook,
    fake_rzp,
    razorpay,
)

_n = {"i": 0, "p": 0, "l": 0}


def _phone():
    _n["i"] += 1
    return f"+916882{_n['i']:06d}"


def _register(c, account_type=None):
    phone = _phone()
    assert c.post("/auth/otp/request", json={"phone": phone}).status_code == 200
    body = {"phone": phone, "code": captured_otp_code(phone)}
    if account_type:
        body["account_type"] = account_type
    r = c.post("/auth/otp/verify", json=body)
    assert r.status_code == 200, r.text
    return auth_header(r.json()["access_token"]), r.json()["user_id"]


def _plan(c, ah, role, listing_limit=None, **kw):
    _n["p"] += 1
    body = {"code": f"TEST_P2_{role[:2]}_{_n['p']:04d}", "role": role, "name": f"P2 {role} {_n['p']}",
            "price_paise": 99900, "duration_days": 30, **kw}
    if listing_limit is not None:
        body["listing_limit"] = listing_limit
    if role == "BROKER":
        body.setdefault("location_limit", 3)
    r = c.post("/admin/plans", json=body, headers=ah)
    assert r.status_code == 201, r.text
    return r.json()["code"]


def _assign(c, ah, uid, code):
    return c.post(f"/admin/users/{uid}/subscription", headers=ah, json={"plan_code": code, "reason": "p2 test"})


def _post(c, h):
    _n["l"] += 1
    return c.post("/owner/listings", headers=h, json={
        "title": f"P2 listing {_n['l']}", "transaction_type": "BUY", "property_type": "APARTMENT",
        "price_rupees": 4500000, "locality": f"P2 Nagar {_n['l']}", "city": "Chennai",
        "pincode": fresh_pincode(c)})


def _expire(session, uid):
    session.execute(text("UPDATE subscriptions SET end_date = NOW() - INTERVAL 1 DAY WHERE user_id=:u"), {"u": uid})
    session.flush()


def _caps(c, h):
    return c.get("/users/me/capabilities", headers=h).json()


def _plan_required(r, status_=None):
    assert r.status_code == 402, r.text
    d = r.json()["detail"]
    assert d["code"] == "POSTER_PLAN_REQUIRED"
    if status_:
        assert d["poster_plan_status"] == status_


# --- Owner -----------------------------------------------------------------------


def test_owner_posting_without_a_plan(client):
    c, _s = client
    h, _uid = _register(c, "OWNER")
    _plan_required(_post(c, h), "INACTIVE")
    assert c.get("/owner/listings", headers=h).status_code == 200  # reading is never paid
    # A Buyer-only account in OWNER mode is refused the same way.
    bh, _ = _register(c)
    c.post("/users/me/role", json={"role": "OWNER"}, headers=bh)
    _plan_required(_post(c, bh), "NONE")


def test_owner_posting_with_active_plan_and_limit(client):
    c, session = client
    ah, _ = _admin(c, session)
    h, uid = _register(c, "OWNER")
    assert _assign(c, ah, uid, _plan(c, ah, "OWNER", listing_limit=1)).status_code == 200
    caps = _caps(c, h)
    assert caps["poster_plan_status"] == "ACTIVE" and caps["can_post"] is True
    assert _post(c, h).status_code == 201
    r = _post(c, h)
    assert r.status_code == 402 and r.json()["detail"]["code"] == "OWNER_PLAN_LIMIT"


def test_owner_expired_plan_blocks_paid_ops_keeps_everything_else(client):
    c, session = client
    ah, _ = _admin(c, session)
    h, uid = _register(c, "OWNER")
    assert _assign(c, ah, uid, _plan(c, ah, "OWNER", listing_limit=5)).status_code == 200
    lid = _post(c, h).json()["listing_id"]
    assert admin_verify(c, lid).status_code == 200
    _expire(session, uid)
    assert _caps(c, h)["poster_plan_status"] == "INACTIVE"
    # Restricted operations
    _plan_required(_post(c, h), "INACTIVE")
    _plan_required(c.patch(f"/owner/listings/{lid}", headers=h, json={"title": "New title"}))
    _plan_required(c.post(f"/owner/listings/{lid}/submit", headers=h))
    _plan_required(c.post(f"/owner/listings/{lid}/photos", headers=h,
                          files={"file": ("a.jpg", b"\xff\xd8\xff\xe0" + b"0" * 64, "image/jpeg")}))
    # Still accessible to the owner, still public, nothing changed by expiry.
    assert c.get(f"/owner/listings/{lid}", headers=h).status_code == 200
    assert any(i["listing_id"] == lid for i in c.get("/owner/listings", headers=h).json()["items"])
    assert c.get(f"/buyer/listings/{lid}").status_code == 200
    row = session.execute(text("SELECT verification_status, listing_status, deleted_at FROM property_listings "
                               "WHERE id=:l"), {"l": lid}).fetchone()
    assert tuple(row) == ("VERIFIED", "AVAILABLE", None)
    assert c.get("/owner/leads", headers=h).status_code == 200
    assert c.get("/owner/visits", headers=h).status_code == 200
    # Closing is allowed without a plan; re-publishing is a paid operation.
    assert c.post(f"/owner/listings/{lid}/archive", headers=h).status_code == 200
    _plan_required(c.post(f"/owner/listings/{lid}/publish", headers=h))
    _plan_required(c.post(f"/owner/listings/{lid}/status", headers=h, json={"status": "AVAILABLE"}))
    # Buyer functionality after expiry
    for path in ("/buyer/enquiries", "/buyer/saved", "/buyer/visits", "/buyer/plan"):
        assert c.get(path, headers=h).status_code == 200


def test_renewal_and_upgrade_same_account(client):
    c, session = client
    ah, _ = _admin(c, session)
    h, uid = _register(c, "OWNER")
    basic = _plan(c, ah, "OWNER", listing_limit=1)
    pro = _plan(c, ah, "OWNER", listing_limit=3)
    assert _assign(c, ah, uid, basic).status_code == 200
    lid = _post(c, h).json()["listing_id"]
    _expire(session, uid)
    _plan_required(_post(c, h))
    # Renewal (same plan) after expiry
    r = _assign(c, ah, uid, basic)
    assert r.status_code == 200 and r.json()["user_id"] == uid
    assert _caps(c, h)["poster_plan"]["plan_code"] == basic
    assert _post(c, h).status_code == 402  # limit 1 still enforced (1 listing exists)
    # Upgrade while active: previous ends (history kept), new limit applies
    assert _assign(c, ah, uid, pro).status_code == 200
    rows = session.execute(text("SELECT p.code, s.status FROM subscriptions s JOIN plans p ON p.id=s.plan_id "
                                "WHERE s.user_id=:u ORDER BY s.id"), {"u": uid}).fetchall()
    # Expired original, ended renewal, active upgrade: full history, one current plan.
    assert [tuple(r) for r in rows] == [(basic, "EXPIRED"), (basic, "EXPIRED"), (pro, "ACTIVE")]
    assert _post(c, h).status_code == 201
    assert c.get(f"/owner/listings/{lid}", headers=h).status_code == 200
    assert session.execute(text("SELECT COUNT(*) FROM users WHERE id=:u"), {"u": uid}).scalar() == 1


def test_payment_bypass_grants_posting(client):
    c, session = client
    ah, _ = _admin(c, session)
    code = _plan(c, ah, "OWNER", listing_limit=2)
    h, uid = _register(c, "OWNER")
    _plan_required(_post(c, h))
    r = c.post("/subscriptions/bypass", headers=h, json={"plan_code": code})
    assert r.status_code == 200, r.text
    assert _post(c, h).status_code == 201


# --- Dealer -----------------------------------------------------------------------


def test_dealer_posting_without_expired_and_active_plan(client):
    c, session = client
    ah, _ = _admin(c, session)
    h, uid = _register(c, "DEALER")
    assert c.get("/users/me/role", headers=h).json()["role"] == "BROKER"
    _plan_required(_post(c, h), "INACTIVE")
    assert _assign(c, ah, uid, _plan(c, ah, "BROKER", listing_limit=1)).status_code == 200
    r = _post(c, h)
    assert r.status_code == 201, r.text
    lid = r.json()["listing_id"]
    assert c.get(f"/owner/listings/{lid}", headers=h).status_code == 200
    r = _post(c, h)
    assert r.status_code == 402 and r.json()["detail"]["code"] == "OWNER_PLAN_LIMIT"  # Dealer plan's limit
    _expire(session, uid)
    _plan_required(_post(c, h), "INACTIVE")
    _plan_required(c.patch(f"/owner/listings/{lid}", headers=h, json={"title": "X"}))
    assert c.get(f"/owner/listings/{lid}", headers=h).status_code == 200
    # Dealer in BUYER mode cannot post either (mode must be the poster type).
    c.post("/users/me/role", json={"role": "BUYER"}, headers=h)
    assert c.post("/owner/listings", headers=h, json={}).status_code == 403


def test_dealer_trial_never_authorizes_posting(client):
    c, session = client
    ah, _ = _admin(c, session)
    h, uid = _register(c, "DEALER")
    # The real trial plan row, assigned by an admin (no payment).
    r = _assign(c, ah, uid, "RESTAMP_AGENT_TRIAL")
    assert r.status_code == 200, r.text
    caps = _caps(c, h)
    assert caps["poster_plan_status"] == "TRIAL" and caps["can_post"] is False
    assert caps["poster_plan"]["is_trial"] is True
    _plan_required(_post(c, h), "TRIAL")
    # Bypass attempts: entering Owner mode, buying the trial via bypass/checkout.
    assert c.post("/users/me/role", json={"role": "OWNER"}, headers=h).status_code == 409
    assert c.post("/subscriptions/bypass", headers=h, json={"plan_code": "RESTAMP_AGENT_TRIAL"}).status_code == 404
    # A FREE_TRIAL-provider subscription (the self-serve trial's shape) is a trial too.
    session.execute(text("UPDATE payments p JOIN subscriptions s ON s.payment_id=p.id SET p.provider='FREE_TRIAL' "
                         "WHERE s.user_id=:u"), {"u": uid})
    session.execute(text("UPDATE subscriptions SET source=NULL WHERE user_id=:u"), {"u": uid})
    session.flush()
    _plan_required(_post(c, h), "TRIAL")
    # Expired trial -> INACTIVE, still no posting.
    _expire(session, uid)
    assert _caps(c, h)["poster_plan_status"] == "INACTIVE"
    _plan_required(_post(c, h), "INACTIVE")


def test_dealer_listing_leads_reach_the_dealer(client):
    c, session = client
    ah, _ = _admin(c, session)
    h, uid = _register(c, "DEALER")
    assert _assign(c, ah, uid, _plan(c, ah, "BROKER", listing_limit=5)).status_code == 200
    lid = _post(c, h).json()["listing_id"]
    assert admin_verify(c, lid).status_code == 200
    bh, _b = _register(c)
    assert c.post("/buyer/enquiries", headers=bh, json={"property_listing_id": lid, "message": "Hi"}).status_code == 201
    leads = c.get("/owner/leads", headers=h).json()["items"]
    assert len(leads) == 1


# --- Conversion -------------------------------------------------------------------


def test_conversion_rules_audit_and_listings_kept(client):
    c, session = client
    ah, admin_id = _admin(c, session)
    h, uid = _register(c, "OWNER")
    assert _assign(c, ah, uid, _plan(c, ah, "OWNER", listing_limit=5)).status_code == 200
    lid = _post(c, h).json()["listing_id"]
    url = f"/admin/users/{uid}/poster-type"
    # Authorization and validation
    assert c.post(url, headers=h, json={"poster_type": "DEALER", "reason": "x"}).status_code == 403
    assert c.post(url, json={"poster_type": "DEALER", "reason": "x"}).status_code == 401
    assert c.post(url, headers=ah, json={"poster_type": "DEALER"}).status_code == 422
    assert c.post(url, headers=ah, json={"poster_type": "DEALER", "reason": "   "}).status_code == 422
    # Refused while the Owner subscription is active
    r = c.post(url, headers=ah, json={"poster_type": "DEALER", "reason": "Customer request"})
    assert r.status_code == 409
    _expire(session, uid)
    assert c.post(url, headers=ah, json={"poster_type": "OWNER", "reason": "same"}).status_code == 409
    r = c.post(url, headers=ah, json={"poster_type": "DEALER", "reason": "Customer request"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["old_poster_type"] == "OWNER" and out["new_poster_type"] == "BROKER" and out["converted_by"] == admin_id
    audit = session.execute(text("SELECT user_id, before_json, after_json FROM audit_logs "
                                 "WHERE action='POSTER_TYPE_CONVERTED' AND entity=:e"), {"e": f"user:{uid}"}).fetchone()
    assert audit[0] == admin_id
    after = json.loads(audit[2]) if isinstance(audit[2], str) else audit[2]
    assert after["reason"] == "Customer request" and after["poster_type"] == "BROKER"
    # Same user, mode reset to BUYER, subscriptions untouched (no cancel/refund)
    assert c.get("/users/me/role", headers=h).json()["role"] == "BUYER"
    assert session.execute(text("SELECT COUNT(*) FROM subscriptions WHERE user_id=:u"), {"u": uid}).scalar() == 1
    # Listings kept and visible to the account in its new mode; no paid ops until a Dealer plan.
    assert c.post("/users/me/role", json={"role": "OWNER"}, headers=h).status_code == 409
    assert c.post("/users/me/role", json={"role": "BROKER"}, headers=h).status_code == 200
    assert c.get(f"/owner/listings/{lid}", headers=h).status_code == 200
    _plan_required(c.patch(f"/owner/listings/{lid}", headers=h, json={"title": "Y"}))
    status_row = session.execute(text("SELECT user_id, verification_status FROM property_listings WHERE id=:l"),
                                 {"l": lid}).fetchone()
    assert status_row[0] == uid
    assert _assign(c, ah, uid, _plan(c, ah, "BROKER", listing_limit=5)).status_code == 200
    assert c.patch(f"/owner/listings/{lid}", headers=h, json={"title": "Dealer title"}).status_code == 200


def test_conversion_dealer_to_owner_disables_locality_access(client):
    c, session = client
    ah, admin_id = _admin(c, session)
    h, uid = _register(c, "DEALER")
    pc = make_postcode(session, "-p2conv")
    session.execute(text("INSERT INTO broker_postcode_access_current (broker_user_id, postcode_id, granted_by) "
                         "VALUES (:u, :p, :u)"), {"u": uid, "p": pc})
    session.execute(text("INSERT INTO broker_postcode_access_history (broker_user_id, postcode_id, granted_by, "
                         "status) VALUES (:u, :p, :u, 'ACTIVE')"), {"u": uid, "p": pc})
    session.flush()
    r = c.post(f"/admin/users/{uid}/poster-type", headers=ah,
               json={"poster_type": "OWNER", "reason": "Now an owner"})
    assert r.status_code == 200, r.text
    assert r.json()["revoked_locality_grants"] == 1
    assert session.execute(text("SELECT COUNT(*) FROM broker_postcode_access_current WHERE broker_user_id=:u"),
                           {"u": uid}).scalar() == 0
    hist = session.execute(text("SELECT status, revoked_by FROM broker_postcode_access_history "
                                "WHERE broker_user_id=:u"), {"u": uid}).fetchall()
    assert [tuple(x) for x in hist] == [("REVOKED", admin_id)]  # record kept, not deleted
    assert c.get("/broker/profile", headers=h).status_code == 403


def test_conversion_refused_during_trial(client):
    c, session = client
    ah, _ = _admin(c, session)
    _h, uid = _register(c, "DEALER")
    assert _assign(c, ah, uid, "RESTAMP_AGENT_TRIAL").status_code == 200
    r = c.post(f"/admin/users/{uid}/poster-type", headers=ah, json={"poster_type": "OWNER", "reason": "r"})
    assert r.status_code == 409


# --- Razorpay owner plans ----------------------------------------------------------


def _verify(c, h, order_id, pay_id, sig=None):
    return c.post("/payments/verify", headers=h, json={
        "razorpay_order_id": order_id, "razorpay_payment_id": pay_id,
        "razorpay_signature": sig or _checkout_sig(order_id, pay_id)})


def _owner_checkout(c, h, code):
    r = c.post("/payments/checkout", headers=h, json={"kind": "owner", "code": code})
    assert r.status_code == 200, r.text
    return r.json()


def test_razorpay_owner_plan_verify_duplicate_and_bad_signature(client, razorpay, fake_rzp):
    c, session = client
    ah, _ = _admin(c, session)
    code = _plan(c, ah, "OWNER", listing_limit=2)
    h, uid = _register(c, "OWNER")
    co = _owner_checkout(c, h, code)
    assert co["plan"]["kind"] == "owner" and co["amount_paise"] == 99900
    # Bad signature: 400, payment stays INITIATED, nothing activated
    assert _verify(c, h, co["razorpay_order_id"], "pay_P2A", "0" * 64).status_code == 400
    assert session.execute(text("SELECT status FROM payments WHERE id=:p"), {"p": co["payment_id"]}).scalar() == "INITIATED"
    _plan_required(_post(c, h))
    r = _verify(c, h, co["razorpay_order_id"], "pay_P2A")
    assert r.status_code == 200 and r.json()["activated"] is True, r.text
    assert _post(c, h).status_code == 201
    # Duplicate confirmation (verify + webhook replay): no second subscription
    r = _verify(c, h, co["razorpay_order_id"], "pay_P2A")
    assert r.status_code == 200 and r.json()["activated"] is False
    assert _webhook(c, _captured_payload(co["razorpay_order_id"], 99900, "pay_P2A"), "evt_p2a").status_code == 200
    assert session.execute(text("SELECT COUNT(*) FROM subscriptions WHERE payment_id=:p"),
                           {"p": co["payment_id"]}).scalar() == 1
    src = session.execute(text("SELECT source FROM subscriptions WHERE payment_id=:p"), {"p": co["payment_id"]}).scalar()
    assert src == "PAYMENT"


def test_superseded_payment_replay_never_reactivates(client, razorpay, fake_rzp):
    c, session = client
    ah, _ = _admin(c, session)
    code = _plan(c, ah, "OWNER", listing_limit=2)
    h, uid = _register(c, "OWNER")
    co = _owner_checkout(c, h, code)
    assert _verify(c, h, co["razorpay_order_id"], "pay_P2B").json()["activated"] is True
    assert _assign(c, ah, uid, _plan(c, ah, "OWNER", listing_limit=9)).status_code == 200  # upgrade replaces it
    r = _verify(c, h, co["razorpay_order_id"], "pay_P2B")
    assert r.status_code == 200 and r.json()["activated"] is False
    assert session.execute(text("SELECT COUNT(*) FROM subscriptions WHERE user_id=:u AND status='ACTIVE'"),
                           {"u": uid}).scalar() == 1


def test_checkout_refused_for_incompatible_poster_type(client, razorpay, fake_rzp):
    c, session = client
    ah, _ = _admin(c, session)
    code = _plan(c, ah, "BROKER")
    h, _uid = _register(c, "OWNER")  # an Owner account can never become a Dealer
    r = c.post("/payments/checkout", headers=h, json={"kind": "agent", "code": code})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "POSTER_TYPE_CONFLICT"
    assert fake_rzp.calls == []  # no order, no money
    r = c.post("/payments/checkout", headers=h, json={"kind": "agent", "code": "RESTAMP_AGENT_TRIAL"})
    assert r.status_code == 404  # the trial is never purchasable


def test_poster_type_conflict_after_checkout_goes_to_manual_review(client, razorpay, fake_rzp):
    c, session = client
    ah, _ = _admin(c, session)
    owner_code = _plan(c, ah, "OWNER", listing_limit=2)
    h, uid = _register(c)  # Buyer only: checkout allowed
    co = _owner_checkout(c, h, owner_code)
    # Meanwhile the account becomes a Dealer.
    assert _assign(c, ah, uid, _plan(c, ah, "BROKER")).status_code == 200
    r = _verify(c, h, co["razorpay_order_id"], "pay_P2C")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["activated"] is False and body["manual_review"] is True
    assert body["review_reason"] == "POSTER_TYPE_CONFLICT" and body["status"] == "SUCCESS"
    pay = session.execute(text("SELECT status, provider_payment_id, paid_at, activation_review_status "
                               "FROM payments WHERE id=:p"), {"p": co["payment_id"]}).fetchone()
    assert pay[0] == "SUCCESS" and pay[1] == "pay_P2C" and pay[2] is not None and pay[3] == "PENDING"
    assert session.execute(text("SELECT COUNT(*) FROM subscriptions WHERE payment_id=:p"),
                           {"p": co["payment_id"]}).scalar() == 0
    assert session.execute(text("SELECT poster_type FROM users WHERE id=:u"), {"u": uid}).scalar() == "BROKER"
    # Retries and webhook replay: same answer, nothing created
    r2 = _verify(c, h, co["razorpay_order_id"], "pay_P2C")
    assert r2.json()["manual_review"] is True and r2.json()["activated"] is False
    assert _webhook(c, _captured_payload(co["razorpay_order_id"], 99900, "pay_P2C"), "evt_p2c").status_code == 200
    assert session.execute(text("SELECT COUNT(*) FROM subscriptions WHERE payment_id=:p"),
                           {"p": co["payment_id"]}).scalar() == 0
    assert session.execute(text("SELECT COUNT(*) FROM payments WHERE user_id=:u AND provider='razorpay'"),
                           {"u": uid}).scalar() == 1
    # No Owner posting right was granted
    assert c.post("/users/me/role", json={"role": "OWNER"}, headers=h).status_code == 409
