"""Phase 1 — backend-authoritative account capabilities.

Covered:
- Registration as Buyer / Owner / Dealer (and the DEALER alias): one user id,
  poster type + starting mode set, NO posting plan granted.
- The declared account type never changes an existing account.
- Capability activation through a plan (admin assignment, payment bypass)
  keeps the SAME user id and records the poster type.
- Owner XOR Dealer: activation of the other poster type's plan, a mode switch
  into the other type, and a legacy mode row are all refused; nothing written.
- Client manipulation: extra request fields cannot set the poster type.
- Buyer capability in every mode (BUYER, OWNER, BROKER), incl. writes, and
  owner-mode buyer activity recorded as BUYER (never OWNER).
- Expired poster plan: poster type kept, status INACTIVE, buyer access kept.
- Stale JWT role claim never authorizes: authorization follows the DB.
Rollback-isolated (conftest client fixture).
"""
from sqlalchemy import text

from tests.conftest import auth_header, captured_otp_code, register_phone
from tests.test_admin import _admin
from tests.test_viewed_properties import _seed_listing

_n = {"i": 0}


def _phone():
    _n["i"] += 1
    return f"+916881{_n['i']:06d}"


def _verify(c, phone, account_type=None):
    assert c.post("/auth/otp/request", json={"phone": phone}).status_code == 200
    body = {"phone": phone, "code": captured_otp_code(phone)}
    if account_type is not None:
        body["account_type"] = account_type
    return c.post("/auth/otp/verify", json=body)


def _register(c, account_type=None):
    r = _verify(c, _phone(), account_type)
    assert r.status_code == 200, r.text
    tok = r.json()
    return auth_header(tok["access_token"]), tok["user_id"], tok


def _caps(c, h):
    r = c.get("/users/me/capabilities", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


_plan_n = {"i": 0}


def _plan(c, ah, role):
    _plan_n["i"] += 1
    body = {"code": f"TEST_P1_{role[:2]}_{_plan_n['i']:04d}", "role": role, "name": f"P1 {role} {_plan_n['i']}",
            "price_paise": 49900, "duration_days": 30}
    if role == "OWNER":
        body["listing_limit"] = 5
    if role == "BROKER":
        body["location_limit"] = 3
    r = c.post("/admin/plans", json=body, headers=ah)
    assert r.status_code == 201, r.text
    return r.json()["code"]


def _assign(c, ah, uid, code):
    return c.post(f"/admin/users/{uid}/subscription", headers=ah, json={"plan_code": code, "reason": "phase1 test"})


def _poster_type(session, uid):
    return session.execute(text("SELECT poster_type FROM users WHERE id=:u"), {"u": uid}).scalar()


def _sub_count(session, uid):
    return session.execute(text("SELECT COUNT(*) FROM subscriptions WHERE user_id=:u"), {"u": uid}).scalar()


# --- registration -----------------------------------------------------------


def test_registration_buyer_owner_dealer(client):
    c, _s = client
    h, _uid, tok = _register(c)
    caps = _caps(c, h)
    assert tok["role"] == "BUYER"
    assert caps["buyer"] is True and caps["poster_type"] is None
    assert caps["poster_plan_status"] == "NONE"
    assert caps["allowed_modes"] == ["BUYER", "OWNER", "BROKER"]

    for account_type, poster in (("OWNER", "OWNER"), ("BROKER", "BROKER"), ("DEALER", "BROKER")):
        h, _uid, tok = _register(c, account_type)
        caps = _caps(c, h)
        assert tok["role"] == poster  # starting experience
        assert caps["buyer"] is True
        assert caps["poster_type"] == poster
        assert caps["owner"] is (poster == "OWNER") and caps["dealer"] is (poster == "BROKER")
        # Registration grants NO posting plan.
        assert caps["poster_plan_status"] == "INACTIVE" and caps["poster_plan"] is None
        assert caps["allowed_modes"] == ["BUYER", poster]


def test_registration_account_type_is_validated(client):
    c, _s = client
    assert _verify(c, _phone(), "ADMIN").status_code == 422
    assert _verify(c, _phone(), "owner ").status_code == 422


def test_account_type_never_changes_an_existing_account(client):
    c, session = client
    phone = _phone()
    first = register_phone(c, phone)
    r = _verify(c, phone, "OWNER")
    assert r.status_code == 200
    assert r.json()["user_id"] == first["user_id"]  # same identity, no new account
    assert r.json()["role"] == "BUYER"
    assert _poster_type(session, first["user_id"]) is None
    n = session.execute(text("SELECT COUNT(*) FROM auth_identities WHERE provider='phone' AND provider_identifier=:p"),
                        {"p": phone}).scalar()
    assert n == 1


def test_me_includes_capabilities(client):
    c, _s = client
    h, uid, _ = _register(c, "OWNER")
    me = c.get("/auth/me", headers=h).json()
    assert me["id"] == uid and me["capabilities"]["poster_type"] == "OWNER"
    assert c.get("/users/me/capabilities").status_code == 401


# --- capability activation ----------------------------------------------------


def test_buyer_acquires_owner_capability_same_user_id(client):
    c, session = client
    ah, _ = _admin(c, session)
    h, uid, _ = _register(c)
    code = _plan(c, ah, "OWNER")
    r = _assign(c, ah, uid, code)
    assert r.status_code == 200, r.text
    assert r.json()["user_id"] == uid
    caps = _caps(c, h)
    assert caps["poster_type"] == "OWNER" and caps["poster_plan_status"] == "ACTIVE"
    assert caps["poster_plan"]["plan_code"] == code
    assert caps["buyer"] is True
    assert c.get("/auth/me", headers=h).json()["id"] == uid


def test_buyer_acquires_dealer_capability_same_user_id(client):
    c, session = client
    ah, _ = _admin(c, session)
    h, uid, _ = _register(c)
    code = _plan(c, ah, "BROKER")
    assert _assign(c, ah, uid, code).status_code == 200
    caps = _caps(c, h)
    assert caps["poster_type"] == "BROKER" and caps["dealer"] is True
    assert caps["poster_plan_status"] == "ACTIVE"
    assert caps["allowed_modes"] == ["BUYER", "BROKER"]


def test_bypass_activation_claims_poster_type(client):
    c, session = client
    ah, _ = _admin(c, session)
    code = _plan(c, ah, "OWNER")
    h, uid, _ = _register(c)
    assert c.post("/users/me/role", json={"role": "OWNER"}, headers=h).status_code == 200
    assert _poster_type(session, uid) is None  # the mode alone grants nothing
    r = c.post("/subscriptions/bypass", headers=h, json={"plan_code": code})
    assert r.status_code == 200, r.text
    assert _poster_type(session, uid) == "OWNER"


def test_buyer_plan_bypass_allowed_in_any_mode(client):
    c, session = client
    ah, _ = _admin(c, session)
    code = _plan(c, ah, "BUYER")
    h, uid, _ = _register(c, "OWNER")  # OWNER mode
    r = c.post("/subscriptions/bypass", headers=h, json={"plan_code": code})
    assert r.status_code == 200, r.text
    assert _poster_type(session, uid) == "OWNER"  # buyer plan never touches poster type


# --- Owner XOR Dealer -----------------------------------------------------------


def test_owner_cannot_acquire_dealer_plan(client):
    c, session = client
    ah, _ = _admin(c, session)
    _h, uid, _ = _register(c)
    assert _assign(c, ah, uid, _plan(c, ah, "OWNER")).status_code == 200
    before = _sub_count(session, uid)
    r = _assign(c, ah, uid, _plan(c, ah, "BROKER"))
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "POSTER_TYPE_CONFLICT"
    assert _sub_count(session, uid) == before
    assert _poster_type(session, uid) == "OWNER"


def test_dealer_can_add_owner_plan_via_bypass(client):
    """Dealer first (Sales Hub), then an Owner add-on plan (My Properties)."""
    c, session = client
    ah, _ = _admin(c, session)
    code = _plan(c, ah, "OWNER")
    h, uid, _ = _register(c, "DEALER")
    r = c.post("/users/me/role", json={"role": "OWNER"}, headers=h)
    assert r.status_code == 200, r.text
    r = c.post("/subscriptions/bypass", headers=h, json={"plan_code": code})
    assert r.status_code == 200, r.text
    assert _sub_count(session, uid) == 1
    assert _poster_type(session, uid) == "BROKER"  # still a Dealer account
    caps = c.get("/users/me/capabilities", headers=h).json()
    assert caps["owner_addon_plan"]["plan_code"] == code and caps["can_post_owner"] is True
    assert "OWNER" in caps["allowed_modes"] and "BROKER" in caps["allowed_modes"]


def test_mode_switching_rules(client):
    c, session = client
    oh, ouid, _ = _register(c, "OWNER")
    assert c.post("/users/me/role", json={"role": "BROKER"}, headers=oh).status_code == 409
    assert c.post("/users/me/role", json={"role": "BUYER"}, headers=oh).status_code == 200
    assert c.post("/users/me/role", json={"role": "OWNER"}, headers=oh).status_code == 200
    assert _poster_type(session, ouid) == "OWNER"

    dh, duid, _ = _register(c, "BROKER")
    assert c.post("/users/me/role", json={"role": "OWNER"}, headers=dh).status_code == 409
    assert c.post("/users/me/role", json={"role": "BUYER"}, headers=dh).status_code == 200
    assert _poster_type(session, duid) == "BROKER"

    # A Buyer-only account may enter either mode (to browse/buy a plan) but
    # the mode never becomes a capability.
    bh, buid, _ = _register(c)
    assert c.post("/users/me/role", json={"role": "BROKER"}, headers=bh).status_code == 200
    assert c.post("/users/me/role", json={"role": "OWNER"}, headers=bh).status_code == 200
    caps = _caps(c, bh)
    assert caps["poster_type"] is None and caps["owner"] is False and caps["active_mode"] == "OWNER"
    assert c.post("/users/me/role", json={"role": "ADMIN"}, headers=bh).status_code == 422


def test_client_cannot_set_capabilities_through_payloads(client):
    c, session = client
    h, uid, _ = _register(c)
    c.post("/users/me/role", json={"role": "BUYER", "poster_type": "OWNER", "capabilities": {"owner": True}},
           headers=h)
    c.patch("/users/me", json={"display_name": "Manip", "poster_type": "BROKER"}, headers=h)
    assert _poster_type(session, uid) is None
    assert _caps(c, h)["poster_type"] is None


def test_poster_mode_refused_on_conflicting_legacy_row(client):
    c, session = client
    h, uid, _ = _register(c, "DEALER")
    session.execute(text("UPDATE user_current_role SET role='OWNER' WHERE user_id=:u"), {"u": uid})
    session.flush()
    assert c.get("/owner/listings", headers=h).status_code == 403
    oh, ouid, _ = _register(c, "OWNER")
    session.execute(text("UPDATE user_current_role SET role='BROKER' WHERE user_id=:u"), {"u": ouid})
    session.flush()
    assert c.get("/broker/profile", headers=oh).status_code == 403


# --- Buyer capability in every mode -------------------------------------------

_BUYER_READS = ("/buyer/enquiries", "/buyer/saved", "/buyer/visits", "/buyer/plan", "/buyer/viewed",
                "/buyer/call-events")


def test_buyer_reads_work_in_every_mode(client):
    c, _s = client
    for account_type in (None, "OWNER", "DEALER"):
        h, _uid, _ = _register(c, account_type)
        for path in _BUYER_READS:
            r = c.get(path, headers=h)
            assert r.status_code == 200, (account_type, path, r.text)


def test_owner_mode_buyer_writes_recorded_as_buyer(client):
    c, session = client
    lid = _seed_listing(session, title="P1 owner-mode buyer")
    h, uid, _ = _register(c, "OWNER")
    assert c.get("/users/me/role", headers=h).json()["role"] == "OWNER"
    assert c.post(f"/buyer/saved/{lid}", headers=h).status_code == 201
    r = c.post("/buyer/enquiries", headers=h, json={"property_listing_id": lid, "message": "Is it available?"})
    assert r.status_code == 201, r.text
    role = session.execute(text("SELECT source_role FROM enquiries WHERE buyer_user_id=:u"), {"u": uid}).scalar()
    assert role == "BUYER"
    r = c.post(f"/buyer/listings/{lid}/contact", headers=h)
    assert r.status_code == 200, r.text


def test_dealer_mode_buyer_writes_keep_agent_area_gate(client):
    c, session = client
    lid = _seed_listing(session, title="P1 dealer-mode buyer")
    h, _uid, _ = _register(c, "DEALER")
    # Agent-mode listing actions keep the existing area gate (separate
    # entitlement, unchanged): outside the dealer's areas -> 402, not 403.
    assert c.post(f"/buyer/saved/{lid}", headers=h).status_code == 402
    r = c.post("/buyer/enquiries", headers=h, json={"property_listing_id": lid, "message": "Hi"})
    assert r.status_code == 402
    # The same account in BUYER mode acts as a plain buyer.
    assert c.post("/users/me/role", json={"role": "BUYER"}, headers=h).status_code == 200
    assert c.post(f"/buyer/saved/{lid}", headers=h).status_code == 201


def test_owner_endpoints_still_need_owner_mode(client):
    c, _s = client
    h, _uid, _ = _register(c)
    assert c.get("/owner/listings", headers=h).status_code == 403
    assert c.get("/broker/profile", headers=h).status_code == 403


# --- expiry and session consistency ----------------------------------------------


def test_expired_owner_plan_keeps_identity_and_buyer_access(client):
    c, session = client
    ah, _ = _admin(c, session)
    h, uid, _ = _register(c)
    assert _assign(c, ah, uid, _plan(c, ah, "OWNER")).status_code == 200
    session.execute(text("UPDATE subscriptions SET end_date = NOW() - INTERVAL 1 DAY WHERE user_id=:u"), {"u": uid})
    session.flush()
    caps = _caps(c, h)
    assert caps["poster_type"] == "OWNER" and caps["poster_plan_status"] == "INACTIVE"
    assert caps["buyer"] is True
    for path in _BUYER_READS:
        assert c.get(path, headers=h).status_code == 200
    assert c.post("/users/me/role", json={"role": "OWNER"}, headers=h).status_code == 200
    assert c.get("/owner/listings", headers=h).status_code == 200  # read access kept
    assert c.get("/auth/me", headers=h).json()["id"] == uid


def test_stale_jwt_role_claim_never_authorizes(client):
    c, _s = client
    h, _uid, tok = _register(c, "OWNER")
    assert tok["role"] == "OWNER"
    assert c.get("/owner/listings", headers=h).status_code == 200
    assert c.post("/users/me/role", json={"role": "BUYER"}, headers=h).status_code == 200
    # Same token (role claim still OWNER): the DB mode decides.
    assert c.get("/owner/listings", headers=h).status_code == 403
    assert c.get("/buyer/enquiries", headers=h).status_code == 200
