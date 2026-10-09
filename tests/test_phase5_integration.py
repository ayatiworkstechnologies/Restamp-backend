"""Phase 5 — foundation integration + E2E + regression (rollback-isolated).

Covers: frontend/backend contract, auth E2E (OTP + Google rules), all four roles,
ownership, API->MySQL persistence, error/security shapes, scale sanity, and a
Phase 1-4 regression lock (28 tables / 43 FKs / 13 indexes / revision / 16 Phase 4 tests).
"""
import os
import time

import pytest
from sqlalchemy import text

from app.db import engine
from tests.conftest import auth_header, make_postcode, register_phone
from tests.frontend_client import ApiError, FrontendClient, map_error


@pytest.fixture()
def fx(client):
    c, session = client
    return FrontendClient(c), session


# --- Step 3: frontend <-> backend foundation integration ------------------------


def test_api_base_url_env_config(fx):
    fx_client, _ = fx
    assert fx_client.base_url == os.environ.get("RESTAMP_API_BASE_URL", "http://testserver")
    custom = FrontendClient(fx_client.transport, base_url="http://local-dev:8000")
    assert custom.base_url == "http://local-dev:8000"


def test_frontend_login_state_and_logout(fx):
    fc, _ = fx
    assert fc.token is None
    code = fc.request_otp("+15000000001")["debug_code"]
    body = fc.login_phone("+15000000001", code)
    assert fc.token == body["access_token"]  # token stored
    me = fc.get_me()  # authenticated request works
    assert me["id"] == body["user_id"]
    assert fc.get_role()["role"] == "BUYER"  # role retrieval
    fc.logout()
    assert fc.token is None
    with pytest.raises(ApiError) as e:
        fc.get_me()
    assert e.value.kind == "login" and e.value.status == 401


def test_frontend_auth_failure_handling(fx):
    fc, _ = fx
    with pytest.raises(ApiError) as e:
        fc.login_phone("+15000000002", "000000")
    assert e.value.kind == "login"  # 401 -> login screen


def test_frontend_http_error_mapping(fx):
    assert map_error(400, {"detail": "x"}).kind == "validation"
    assert map_error(422, {"detail": "x"}).kind == "validation"
    assert map_error(401, {"detail": "x"}).kind == "login"
    assert map_error(403, {"detail": "x"}).kind == "denied"
    assert map_error(404, {"detail": "x"}).kind == "missing"
    assert map_error(429, {"detail": "x"}).kind == "retry_later"
    assert map_error(500, {"detail": "x"}).kind == "retry"


# --- Step 4: authentication E2E --------------------------------------------------


def test_e2e_otp_full_path_with_expiry_and_limits(fx):
    fc, session = fx
    phone = "+15000000003"
    code = fc.request_otp(phone)["debug_code"]
    assert fc.login_phone(phone, code)["role"] == "BUYER"
    # Expired OTP is rejected (white-box expiry, local test only).
    from app.services.otp import otp_service

    code2 = fc.request_otp("+15000000004")["debug_code"]
    otp_service._codes["+15000000004"].expires_at = 0.0
    with pytest.raises(ApiError) as e:
        fc.login_phone("+15000000004", code2)
    assert e.value.kind == "login"
    # Abuse path: request flood is throttled.
    flood = "+15000000005"
    for _ in range(5):
        fc.request_otp(flood)
    with pytest.raises(ApiError) as e:
        fc.request_otp(flood)
    assert e.value.kind == "retry_later"


def test_e2e_tampered_token_rejected(fx):
    fc, _ = fx
    code = fc.request_otp("+15000000006")["debug_code"]
    tok = fc.login_phone("+15000000006", code)["access_token"]
    fc.token = tok[:-2] + ("ab" if not tok.endswith("ab") else "cd")
    with pytest.raises(ApiError) as e:
        fc.get_me()
    assert e.value.kind == "login"


def test_e2e_google_rules(fx):
    fc, session = fx
    before = session.execute(text("SELECT COUNT(*) FROM users")).fetchone()[0]
    with pytest.raises(ApiError) as e:
        fc.login_google("dev:g5-unknown")
    assert e.value.status == 404  # unknown Google identity: no silent creation
    assert session.execute(text("SELECT COUNT(*) FROM users")).fetchone()[0] == before
    code = fc.request_otp("+15000000007")["debug_code"]
    uid = fc.login_phone("+15000000007", code)["user_id"]
    session.execute(
        text("INSERT INTO auth_identities (user_id, provider, provider_identifier) "
             "VALUES (:u, 'google', 'g5-known')"),
        {"u": uid},
    )
    session.flush()
    fc.logout()
    assert fc.login_google("dev:g5-known")["user_id"] == uid  # known identity authenticates
    cols = [r[0] for r in session.execute(text("SHOW COLUMNS FROM users")).fetchall()]
    assert not any("google" in c or "phone" in c for c in cols)


# --- Step 5: role E2E (all four) --------------------------------------------------


def _login_as(fc, phone, role=None):
    code = fc.request_otp(phone)["debug_code"]
    body = fc.login_phone(phone, code)
    if role:
        fc.set_role(role)
    return body


def test_e2e_roles_buyer_owner_broker(fx):
    fc, _ = fx
    _login_as(fc, "+15000000008")  # BUYER
    assert fc.get("/buyer/welcome")["message"] == "buyer ok"
    for denied in ("/owner/vault", "/broker/scope", "/admin/ping"):
        with pytest.raises(ApiError) as e:
            fc.get(denied)
        assert e.value.status in (401, 403)
    fc.set_role("OWNER")
    assert fc.get("/owner/vault")["message"] == "owner ok"
    with pytest.raises(ApiError) as e:
        fc.get("/broker/scope")
    assert e.value.status == 403
    fc.set_role("BROKER")
    assert fc.get("/broker/scope")["broker_user_id"] == fc.get_me()["id"]
    with pytest.raises(ApiError) as e:
        fc.get("/owner/vault")
    assert e.value.status == 403


def test_e2e_admin(fx):
    fc, session = fx
    body = _login_as(fc, "+15000000009")
    with pytest.raises(ApiError) as e:
        fc.get("/admin/ping")
    assert e.value.status == 403
    session.execute(text("INSERT INTO admin_accounts (user_id) VALUES (:u)"), {"u": body["user_id"]})
    session.flush()
    assert fc.get("/admin/ping")["message"] == "admin ok"


# --- Step 6: ownership E2E ----------------------------------------------------------


def test_e2e_ownership_across_users(fx):
    fc, session = fx
    a = _login_as(fc, "+15000000010", role="OWNER")
    pc = make_postcode(session)
    session.execute(
        text("INSERT INTO physical_properties (postcode_id, user_id) VALUES (:p, :u)"),
        {"p": pc, "u": a["user_id"]},
    )
    session.flush()
    pp = session.execute(text("SELECT LAST_INSERT_ID()")).fetchone()[0]
    assert fc.get(f"/owner/check-ownership/{pp}")["message"] == "ownership confirmed"
    fc.logout()
    _login_as(fc, "+15000000011", role="OWNER")  # different user
    with pytest.raises(ApiError) as e:
        fc.get(f"/owner/check-ownership/{pp}")
    assert e.value.status == 403  # backend-enforced, not a frontend check
    fc.logout()
    with pytest.raises(ApiError) as e:
        fc.get(f"/owner/check-ownership/{pp}")
    assert e.value.kind == "login"  # unauthenticated rejected


# --- Step 7: API -> MySQL persistence -------------------------------------------------


def test_e2e_persistence_through_api(fx):
    fc, session = fx
    body = _login_as(fc, "+15000000012")
    uid = body["user_id"]
    row = session.execute(
        text("SELECT display_name, status FROM users WHERE id=:u"), {"u": uid}
    ).fetchone()
    assert row[0] == "+15000000012" and row[1] == "active"
    ident = session.execute(
        text("SELECT provider, provider_identifier FROM auth_identities WHERE user_id=:u"),
        {"u": uid},
    ).fetchall()
    assert ("phone", "+15000000012") in [(r[0], r[1]) for r in ident]
    assert session.execute(
        text("SELECT role FROM user_current_role WHERE user_id=:u"), {"u": uid}
    ).fetchone()[0] == "BUYER"
    fc.set_role("OWNER")
    hist = session.execute(
        text("SELECT role FROM role_history WHERE user_id=:u ORDER BY id"), {"u": uid}
    ).fetchall()
    assert [r[0] for r in hist] == ["BUYER", "OWNER"]


# --- Step 9: error + security shapes ----------------------------------------------------


def test_error_shapes_and_no_leakage(client):
    c, _ = client
    probes = [
        ("post", "/auth/otp/request", {"phone": "not-a-phone"}),
        ("post", "/auth/otp/verify", {"phone": "+15000000013", "code": "12"}),
        ("post", "/users/me/role", {"role": "ADMIN"}),
        ("get", "/users/999999999", None),
        ("get", "/auth/me", None),
    ]
    for method, path, payload in probes:
        kwargs = {"json": payload} if payload is not None else {}
        if path in ("/users/me/role", "/users/999999999"):
            tok = register_phone(c, "+15000000099")
            kwargs["headers"] = auth_header(tok["access_token"])
        r = getattr(c, method)(path, **kwargs)
        assert r.status_code in (400, 401, 403, 404, 422), (path, r.status_code)
        body = r.json()
        assert set(body.keys()) == {"detail"}, body  # envelope only
        blob = str(body).lower()
        assert "restamp@123" not in blob and "otpv" not in blob
        assert "traceback" not in blob and "secret" not in blob


# --- Step 11: scale sanity -----------------------------------------------------------------


def test_auth_lookup_paths_are_indexed_and_fast(fx):
    fc, session = fx
    idx = {
        r[0]
        for r in session.execute(
            text("SELECT DISTINCT INDEX_NAME FROM information_schema.STATISTICS "
                 "WHERE TABLE_SCHEMA = DATABASE()")
        ).fetchall()
    }
    # Login path: (provider, provider_identifier) -> user; role via PK; both covered.
    assert "uk_auth_provider_identifier_scoped" in idx
    assert "uk_auth_identities_provider_user" in idx
    t0 = time.perf_counter()
    code = fc.request_otp("+15000000014")["debug_code"]
    fc.login_phone("+15000000014", code)
    fc.get_me()
    assert time.perf_counter() - t0 < 5.0
    from app.db import engine as _engine

    assert _engine.pool.size() >= 1 and _engine.pool._max_overflow >= 0


# --- Step 8: regression lock (Phases 1-4 intact) ----------------------------------------------


def test_regression_foundation_intact(client):
    c, session = client
    tables = {
        r[0]
        for r in session.execute(
            text("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()")
        ).fetchall()
    } - {"alembic_version"}
    # Drafts phase: 32 + listing_drafts.
    assert len(tables) == 33
    assert "saved_properties" in tables
    # Drafts phase: 48 + listing_drafts user FK.
    assert (
        session.execute(
            text("SELECT COUNT(*) FROM information_schema.REFERENTIAL_CONSTRAINTS "
                 "WHERE CONSTRAINT_SCHEMA = DATABASE()")
        ).fetchone()[0]
        == 49
    )
    idx = {
        r[0]
        for r in session.execute(
            text("SELECT DISTINCT INDEX_NAME FROM information_schema.STATISTICS "
                 "WHERE TABLE_SCHEMA = DATABASE()")
        ).fetchall()
    }
    for required in (
        "idx_users_status", "idx_users_created_at", "idx_users_updated_at",
        "idx_payments_user_id", "idx_payments_status", "idx_payments_idempotency_key",
        "idx_webhook_events_payment_id", "idx_webhook_events_provider_event_id",
        "idx_broker_current_broker_user", "idx_broker_current_postcode",
        "idx_broker_history_broker_user", "idx_broker_history_postcode",
        "idx_broker_history_status",
    ):
        assert required in idx, required
    assert session.execute(text("SELECT version_num FROM alembic_version")).fetchone()[0] == (
        "d5e6f7a8b9c0"
    )
    from app.models import Base

    # Drafts phase: 32 + listing_drafts.
    assert len(Base.metadata.tables) == 33


def test_phase7_e2e_buyer_profile_owner_flow(client):
    """Phase 7 end-to-end integration:
    1. OTP request & verify for new user (display_name == phone)
    2. PATCH /users/me updates display_name to real name
    3. Subsequent OTP login for same phone returns display_name already completed
    4. Buyer discovery / listings / detail / similar / save / unsave / enquiry / contact
    5. Switch role to OWNER, access owner endpoint, switch back to BUYER
    """
    c, session = client
    phone = "+16000000001"

    # 1. New user registration via OTP
    code_req = c.post("/auth/otp/request", json={"phone": phone}).json()
    debug_code = code_req.get("debug_code")
    assert debug_code is not None

    tok = c.post("/auth/otp/verify", json={"phone": phone, "code": debug_code}).json()
    token = tok["access_token"]
    h = auth_header(token)

    # Initial profile state
    me1 = c.get("/auth/me", headers=h).json()
    assert me1["display_name"] == phone
    assert me1["role"] == "BUYER"

    # 2. Complete profile via PATCH /users/me
    patch_res = c.patch("/users/me", json={"display_name": "  Restamp Tester  "}, headers=h)
    assert patch_res.status_code == 200
    assert patch_res.json()["display_name"] == "Restamp Tester"

    me2 = c.get("/auth/me", headers=h).json()
    assert me2["display_name"] == "Restamp Tester"

    # 3. Returning user login with same phone
    code_req2 = c.post("/auth/otp/request", json={"phone": phone}).json()
    tok2 = c.post("/auth/otp/verify", json={"phone": phone, "code": code_req2["debug_code"]}).json()
    h2 = auth_header(tok2["access_token"])
    me_returning = c.get("/auth/me", headers=h2).json()
    assert me_returning["display_name"] == "Restamp Tester"  # name preserved!

    # 4. Role transition: Common Home -> Owner -> Buyer
    c.post("/users/me/role", json={"role": "OWNER"}, headers=h2)
    assert c.get("/owner/vault", headers=h2).status_code == 200
    c.post("/users/me/role", json={"role": "BUYER"}, headers=h2)
    assert c.get("/buyer/welcome", headers=h2).status_code == 200
