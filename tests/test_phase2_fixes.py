"""Phase 2 remaining fixes — admin-only verification, drafts, Dealer listing
locations, and the manual-review refund workflow. Real enforcement (no
bypass anywhere in the suite).
"""
from sqlalchemy import text

from app.services import payments as payments_svc
from tests.conftest import (
    admin_verify,
    auth_header,
    captured_otp_code,
    fixture_admin_headers,
    fresh_pincode,
    grant_test_posting_plan,
    make_postcode,
)
from tests.test_admin import _admin
from tests.test_payments import (  # noqa: F401  (pytest fixtures)
    FakeRazorpay,
    _Resp,
    _checkout_sig,
    _webhook,
    razorpay,
)

_n = {"i": 0, "l": 0}


def _phone():
    _n["i"] += 1
    return f"+916883{_n['i']:06d}"


def _register(c, account_type=None):
    phone = _phone()
    assert c.post("/auth/otp/request", json={"phone": phone}).status_code == 200
    body = {"phone": phone, "code": captured_otp_code(phone)}
    if account_type:
        body["account_type"] = account_type
    r = c.post("/auth/otp/verify", json=body)
    assert r.status_code == 200, r.text
    return auth_header(r.json()["access_token"]), r.json()["user_id"]


def _poster(c, account_type="OWNER"):
    h, uid = _register(c, account_type)
    grant_test_posting_plan(c, h, "BROKER" if account_type == "DEALER" else account_type)
    return h, uid


def _post(c, h, **extra):
    _n["l"] += 1
    r = c.post("/owner/listings", headers=h, json={
        "title": f"P2F listing {_n['l']}", "transaction_type": "BUY", "property_type": "APARTMENT",
        "price_rupees": 4500000, "locality": f"P2F Nagar {_n['l']}", "city": "Chennai",
        "pincode": fresh_pincode(c), **extra})
    assert r.status_code == 201, r.text
    return r.json()["listing_id"]


def _vstatus(session, lid):
    return session.execute(text("SELECT verification_status FROM property_listings WHERE id=:l"), {"l": lid}).scalar()


# --- admin-only verification ---------------------------------------------------------


def test_owner_and_dealer_cannot_self_verify(client):
    c, session = client
    for account_type in ("OWNER", "DEALER"):
        h, _ = _poster(c, account_type)
        lid = _post(c, h)
        r = c.post(f"/owner/listings/{lid}/verify", headers=h)
        assert r.status_code == 403 and r.json()["detail"]["code"] == "ADMIN_VERIFICATION_REQUIRED"
        assert _vstatus(session, lid) == "PENDING"
        assert c.get(f"/buyer/listings/{lid}").status_code == 404


def test_verification_auth_matrix(client):
    c, session = client
    h, _ = _poster(c)
    lid = _post(c, h)
    assert c.post(f"/owner/listings/{lid}/verify").status_code == 401
    assert c.post(f"/admin/listings/{lid}/verify").status_code == 401
    bh, _ = _register(c)
    assert c.post(f"/admin/listings/{lid}/verify", headers=bh).status_code == 403  # non-admin
    assert c.post(f"/admin/listings/{lid}/verify", headers=h).status_code == 403   # its own poster
    r = admin_verify(c, lid)
    assert r.status_code == 200 and r.json()["verification_status"] == "VERIFIED"
    hist = session.execute(text("SELECT COUNT(*) FROM verifications WHERE property_listing_id=:l"), {"l": lid}).scalar()
    assert hist >= 1  # verification history kept


def test_admin_without_approve_permission_cannot_verify(client):
    c, session = client
    h, _ = _poster(c)
    lid = _post(c, h)
    for role in ("SUPPORT_SPECIALIST",):  # the only admin role without properties.approve
        rh, _ = _admin(c, session, role)
        r = c.post(f"/admin/listings/{lid}/verify", headers=rh)
        assert r.status_code == 403, (role, r.text)
    assert _vstatus(session, lid) == "PENDING"


def test_other_apis_cannot_set_verification(client):
    c, session = client
    h, _ = _poster(c)
    lid = _post(c, h)
    # Update payload carrying verification fields: ignored, never applied.
    r = c.patch(f"/owner/listings/{lid}", headers=h,
                json={"title": "Edited", "verification_status": "VERIFIED", "listing_status": "AVAILABLE"})
    assert r.status_code == 200, r.text
    assert _vstatus(session, lid) == "PENDING"
    # Create payload carrying it: ignored, lands PENDING.
    lid2 = _post(c, h, verification_status="VERIFIED")
    assert _vstatus(session, lid2) == "PENDING"
    # Submit only moves to PENDING; status route cannot touch verification.
    assert c.post(f"/owner/listings/{lid}/submit", headers=h).status_code == 200
    # (listing_status routes are idempotent on AVAILABLE; none changes verification)
    c.post(f"/owner/listings/{lid}/status", headers=h, json={"status": "AVAILABLE"})
    c.post(f"/owner/listings/{lid}/publish", headers=h)
    assert _vstatus(session, lid) == "PENDING"
    assert c.get(f"/buyer/listings/{lid}").status_code == 404
    # Drafts cannot carry a verification state into a listing.
    r = c.post("/owner/drafts", headers=h, json={"title": "D", "form_data": {"verification_status": "VERIFIED"}})
    assert r.status_code == 201
    assert _vstatus(session, lid) == "PENDING"


# --- drafts ----------------------------------------------------------------------------


def test_drafts_without_plan_private_and_publish_gated(client):
    c, session = client
    ah, _ = _admin(c, session)
    h, uid = _register(c, "OWNER")  # no plan
    r = c.post("/owner/drafts", headers=h, json={"title": "My flat", "current_step": 2, "form_data": {"bedrooms": 2}})
    assert r.status_code == 201, r.text
    did = r.json()["id"]
    assert c.put(f"/owner/drafts/{did}", headers=h, json={"title": "My flat 2", "current_step": 3,
                                                          "form_data": {"bedrooms": 3}}).status_code == 200
    assert c.get(f"/owner/drafts/{did}", headers=h).status_code == 200
    assert any(d["id"] == did for d in c.get("/owner/drafts", headers=h).json()["items"])
    # Private: another poster gets 404, a buyer-mode account gets 403, guests 401.
    oh, _ = _register(c, "OWNER")
    assert c.get(f"/owner/drafts/{did}", headers=oh).status_code == 404
    assert c.put(f"/owner/drafts/{did}", headers=oh, json={"title": "x"}).status_code in (404, 422)
    bh, _ = _register(c)
    assert c.get(f"/owner/drafts/{did}", headers=bh).status_code == 403
    assert c.get(f"/owner/drafts/{did}").status_code == 401
    # Never public: drafts are not listings.
    found = c.get("/buyer/listings", params={"q": "My flat 2"}).json()
    assert all(i.get("title") != "My flat 2" for i in found.get("items", []))
    # Publishing (creating the listing) needs the paid plan.
    r = c.post("/owner/listings", headers=h, json={"title": "My flat 2", "transaction_type": "BUY",
                                                  "property_type": "APARTMENT", "price_rupees": 100000,
                                                  "locality": "Draft Nagar", "city": "Chennai",
                                                  "pincode": fresh_pincode(c)})
    assert r.status_code == 402 and r.json()["detail"]["code"] == "POSTER_PLAN_REQUIRED"
    # Expired account keeps its drafts but still cannot publish.
    plan = c.post("/admin/plans", headers=ah, json={"code": "TEST_P2F_OWNER_1", "role": "OWNER", "name": "P2F",
                                                    "price_paise": 100, "duration_days": 30}).json()["code"]
    assert c.post(f"/admin/users/{uid}/subscription", headers=ah,
                  json={"plan_code": plan, "reason": "t"}).status_code == 200
    session.execute(text("UPDATE subscriptions SET end_date = NOW() - INTERVAL 1 DAY WHERE user_id=:u"), {"u": uid})
    session.flush()
    assert c.get(f"/owner/drafts/{did}", headers=h).status_code == 200
    assert c.post("/owner/listings", headers=h, json={"title": "x", "transaction_type": "BUY",
                                                     "property_type": "APARTMENT", "price_rupees": 1,
                                                     "locality": "Draft Nagar 2", "city": "Chennai",
                                                     "pincode": fresh_pincode(c)}).status_code == 402


# --- Dealer listing locations ------------------------------------------------------------


def test_dealer_listing_location_not_limited_by_locality_access(client):
    c, session = client
    h, uid = _poster(c, "DEALER")
    covered = make_postcode(session, "-p2f-cov")
    session.execute(text("INSERT INTO broker_postcode_access_current (broker_user_id, postcode_id, granted_by) "
                         "VALUES (:u, :p, :u)"), {"u": uid, "p": covered})
    session.flush()
    lid = _post(c, h)  # a fresh pincode the dealer does NOT cover
    assert c.get(f"/owner/listings/{lid}", headers=h).status_code == 200
    # Posting did not grant or change locality access.
    rows = session.execute(text("SELECT postcode_id FROM broker_postcode_access_current WHERE broker_user_id=:u"),
                           {"u": uid}).fetchall()
    assert [r[0] for r in rows] == [covered]


# --- manual-review refunds ---------------------------------------------------------------


class RefundGateway(FakeRazorpay):
    """FakeRazorpay + refund endpoints with a scripted outcome."""

    def __init__(self):
        super().__init__()
        self.refund_mode = "processed"  # processed | pending | error | unreachable
        self.refunds: list[dict] = []

    async def post(self, url, json=None):
        if url.endswith("/refund"):
            self.calls.append(("POST", url, json))
            if self.refund_mode == "unreachable":
                import httpx
                raise httpx.ConnectError("down")
            if self.refund_mode == "error":
                return _Resp(400, {"error": {"code": "BAD_REQUEST_ERROR", "description": "x"}})
            rf = {"id": f"rfnd_{len(self.refunds) + 1}", "status": self.refund_mode, "amount": json["amount"],
                  "notes": json.get("notes") or {}}
            self.refunds.append(rf)
            return _Resp(200, rf)
        return await super().post(url, json=json)

    async def get(self, url):
        if url.endswith("/refunds"):
            self.calls.append(("GET", url, None))
            return _Resp(200, {"items": list(self.refunds)})
        return await super().get(url)


import pytest  # noqa: E402


@pytest.fixture
def gw(monkeypatch):
    fake = RefundGateway()
    monkeypatch.setattr(payments_svc, "_client", lambda: fake)
    return fake


def _flagged_payment(c, session, gw):
    """A real manual-review payment: checkout an Owner plan, become a Dealer,
    then verify (Phase 2 conflict path)."""
    ah, _ = _admin(c, session)
    _n["l"] += 1
    oc = c.post("/admin/plans", headers=ah, json={"code": f"TEST_P2F_OW_{_n['l']:04d}", "role": "OWNER",
                                                  "name": "P2F owner", "price_paise": 99900,
                                                  "duration_days": 30}).json()["code"]
    bc = c.post("/admin/plans", headers=ah, json={"code": f"TEST_P2F_BR_{_n['l']:04d}", "role": "BROKER",
                                                  "name": "P2F dealer", "price_paise": 1000, "duration_days": 30,
                                                  "location_limit": 1}).json()["code"]
    h, uid = _register(c)
    co = c.post("/payments/checkout", headers=h, json={"kind": "owner", "code": oc}).json()
    assert c.post(f"/admin/users/{uid}/subscription", headers=ah, json={"plan_code": bc, "reason": "t"}).status_code == 200
    r = c.post("/payments/verify", headers=h, json={"razorpay_order_id": co["razorpay_order_id"],
                                                    "razorpay_payment_id": f"pay_P2F{_n['l']}",
                                                    "razorpay_signature": _checkout_sig(co["razorpay_order_id"],
                                                                                        f"pay_P2F{_n['l']}")})
    assert r.json()["manual_review"] is True
    return ah, h, uid, co["payment_id"]


def _subs_for(session, pid):
    return session.execute(text("SELECT COUNT(*) FROM subscriptions WHERE payment_id=:p"), {"p": pid}).scalar()


def test_review_listing_and_refund_permissions(client, razorpay, gw):
    c, session = client
    ah, h, uid, pid = _flagged_payment(c, session, gw)
    # View: admins with reports.view; not users, not guests.
    r = c.get("/admin/payments/reviews", headers=ah)
    assert r.status_code == 200
    item = [i for i in r.json()["items"] if i["payment_id"] == pid][0]
    assert item["status"] == "SUCCESS" and item["review_status"] == "PENDING"
    assert item["intended_poster_type"] == "OWNER" and item["refund"] is None
    assert c.get("/admin/payments/reviews", headers=h).status_code == 403
    assert c.get("/admin/payments/reviews").status_code == 401
    # Refund: only settings.configure_platform admins.
    for role in ("OPERATIONS_MANAGER", "LISTING_REVIEWER", "SUPPORT_SPECIALIST"):
        rh, _ = _admin(c, session, role)
        assert c.post(f"/admin/payments/{pid}/refund", headers=rh, json={"reason": "x"}).status_code == 403
    assert c.post(f"/admin/payments/{pid}/refund", headers=h, json={"reason": "x"}).status_code == 403
    assert c.post(f"/admin/payments/{pid}/refund", headers=ah, json={}).status_code == 422
    assert gw.refunds == []


def test_refund_processed_recorded_and_duplicates_prevented(client, razorpay, gw):
    c, session = client
    ah, _h, uid, pid = _flagged_payment(c, session, gw)
    _admin_id = session.execute(text("SELECT user_id FROM admin_accounts ORDER BY granted_at DESC LIMIT 1")).scalar()
    r = c.post(f"/admin/payments/{pid}/refund", headers=ah, json={"reason": "Poster type conflict"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "PROCESSED" and out["provider_refund_id"] == "rfnd_1"
    assert out["amount_paise"] == 99900 and out["intended_poster_type"] == "OWNER"
    assert out["original_payment_status"] == "SUCCESS" and out["review_reason"] == "POSTER_TYPE_CONFLICT"
    assert out["admin_reason"] == "Poster type conflict" and out["processed_at"] is not None
    pay = session.execute(text("SELECT status, activation_review_status FROM payments WHERE id=:p"), {"p": pid}).fetchone()
    assert tuple(pay) == ("SUCCESS", "RESOLVED")  # original payment preserved
    # Duplicate request: refused, no second gateway call
    r = c.post(f"/admin/payments/{pid}/refund", headers=ah, json={"reason": "again"})
    assert r.status_code == 409
    assert len([x for x in gw.calls if x[1].endswith("/refund")]) == 1
    # Repeated webhook: no change, no duplicate
    for _ in range(2):
        assert _webhook(c, {"event": "refund.processed", "payload": {"refund": {"entity": {
            "id": "rfnd_1", "status": "processed", "payment_id": f"pay_P2F{_n['l']}"}}}}).status_code == 200
    assert session.execute(text("SELECT COUNT(*) FROM payment_refunds WHERE payment_id=:p"), {"p": pid}).scalar() == 1
    assert _subs_for(session, pid) == 0  # never activates anything
    assert session.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='PAYMENT_REFUND_REQUESTED' "
                                "AND entity=:e"), {"e": f"payment:{pid}"}).scalar() == 1


def test_refund_pending_until_webhook(client, razorpay, gw):
    c, session = client
    gw.refund_mode = "pending"
    ah, _h, _uid, pid = _flagged_payment(c, session, gw)
    r = c.post(f"/admin/payments/{pid}/refund", headers=ah, json={"reason": "conflict"})
    assert r.status_code == 200 and r.json()["status"] == "PENDING"
    assert session.execute(text("SELECT activation_review_status FROM payments WHERE id=:p"), {"p": pid}).scalar() == "PENDING"
    assert c.post(f"/admin/payments/{pid}/refund", headers=ah, json={"reason": "again"}).status_code == 409
    assert _webhook(c, {"event": "refund.processed", "payload": {"refund": {"entity": {"id": "rfnd_1"}}}}).status_code == 200
    row = session.execute(text("SELECT status, processed_at FROM payment_refunds WHERE payment_id=:p"), {"p": pid}).fetchone()
    assert row[0] == "PROCESSED" and row[1] is not None
    # Unknown refund id: ignored
    assert _webhook(c, {"event": "refund.processed", "payload": {"refund": {"entity": {"id": "rfnd_x"}}}}).json()["ignored"]


def test_refund_gateway_failure_recorded_and_safe_retry(client, razorpay, gw):
    c, session = client
    gw.refund_mode = "unreachable"
    ah, _h, _uid, pid = _flagged_payment(c, session, gw)
    r = c.post(f"/admin/payments/{pid}/refund", headers=ah, json={"reason": "conflict"})
    assert r.status_code == 502 and r.json()["detail"]["failure_code"] == "GATEWAY_UNREACHABLE"
    row = session.execute(text("SELECT status, attempts, processed_at FROM payment_refunds WHERE payment_id=:p"),
                          {"p": pid}).fetchone()
    assert tuple(row) == ("FAILED", 1, None)  # recorded, never marked successful
    gw.refund_mode = "error"
    r = c.post(f"/admin/payments/{pid}/refund", headers=ah, json={"reason": "retry"})
    assert r.status_code == 502 and r.json()["detail"]["failure_code"] == "BAD_REQUEST_ERROR"
    # The gateway processed a refund whose answer was lost: the retry adopts it, no 2nd refund.
    gw.refunds.append({"id": "rfnd_lost", "status": "processed",
                       "notes": {"restamp_refund_id": str(r.json()["detail"]["refund_id"])}})
    gw.refund_mode = "processed"
    r = c.post(f"/admin/payments/{pid}/refund", headers=ah, json={"reason": "retry 2"})
    assert r.status_code == 200 and r.json()["status"] == "PROCESSED" and r.json()["provider_refund_id"] == "rfnd_lost"
    assert r.json()["attempts"] == 3
    assert len(gw.refunds) == 1
    assert _subs_for(session, pid) == 0


def test_refund_refused_for_payments_not_under_review(client, razorpay, gw):
    c, session = client
    ah, _ = _admin(c, session)
    h, uid = _register(c)
    co = c.post("/payments/checkout", headers=h, json={"kind": "buyer", "code": "RESTAMP_BUYER_PLUS"}).json()
    r = c.post(f"/admin/payments/{co['payment_id']}/refund", headers=ah, json={"reason": "x"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "PAYMENT_NOT_UNDER_REVIEW"
    assert c.post("/admin/payments/999999999/refund", headers=ah, json={"reason": "x"}).status_code == 404
    assert gw.refunds == []
