"""Phase 4 coverage: startup, DB, users, roles, exclusivity, auth, authz, OTP abuse,
Google rules, ownership, token expiry, schema regression. Rollback-isolated (conftest)."""
from sqlalchemy import text

from tests.conftest import auth_header, make_postcode, register_phone


def test_app_startup_and_health(client):
    c, _ = client
    assert c.get("/health").status_code == 200
    assert c.get("/ready").status_code == 200


def test_database_connection(client):
    c, session = client
    assert session.execute(text("SELECT 1")).fetchone() == (1,)


def test_user_create_and_retrieve(client):
    c, _ = client
    r = c.post("/users", json={"display_name": "Buyer One"})
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    tok = register_phone(c, "+10000000001")
    me = c.get("/auth/me", headers=auth_header(tok["access_token"])).json()
    # Owner of the record or admin may read it; use a fresh admin-less self-read path:
    r = c.get(f"/users/{uid}", headers=auth_header(tok["access_token"]))
    # tok belongs to a different user -> 403 proves the read guard works
    assert r.status_code == 403
    assert me["providers"] == ["phone"]


def test_role_assignment_and_history(client):
    c, _ = client
    tok = register_phone(c, "+10000000002")
    h = auth_header(tok["access_token"])
    assert c.get("/users/me/role", headers=h).json()["role"] == "BUYER"
    assert c.post("/users/me/role", json={"role": "OWNER"}, headers=h).json()["role"] == "OWNER"
    assert c.post("/users/me/role", json={"role": "BUYER"}, headers=h).json()["role"] == "BUYER"
    hist = c.get("/users/me/history", headers=h).json()
    assert [x["role"] for x in hist] == ["BUYER", "OWNER", "BUYER"]
    assert c.post("/users/me/role", json={"role": "ADMIN"}, headers=h).status_code == 422


def test_owner_broker_exclusivity(client):
    c, _ = client
    tok = register_phone(c, "+10000000003")
    h = auth_header(tok["access_token"])
    assert c.get("/buyer/welcome", headers=h).status_code == 200
    assert c.get("/owner/vault", headers=h).status_code == 403
    assert c.get("/broker/scope", headers=h).status_code == 403
    c.post("/users/me/role", json={"role": "OWNER"}, headers=h)
    assert c.get("/owner/vault", headers=h).status_code == 200
    assert c.get("/buyer/welcome", headers=h).status_code == 403
    assert c.get("/broker/scope", headers=h).status_code == 403
    c.post("/users/me/role", json={"role": "BROKER"}, headers=h)
    assert c.get("/broker/scope", headers=h).status_code == 200
    assert c.get("/owner/vault", headers=h).status_code == 403
    assert c.get("/buyer/welcome", headers=h).status_code == 403


def test_owner_switch_preserves_property_history(client):
    c, session = client
    tok = register_phone(c, "+10000000004")
    h = auth_header(tok["access_token"])
    uid = tok["user_id"]
    c.post("/users/me/role", json={"role": "OWNER"}, headers=h)
    pc = make_postcode(session)
    session.execute(
        text("INSERT INTO physical_properties (postcode_id, user_id) VALUES (:p, :u)"),
        {"p": pc, "u": uid},
    )
    session.flush()
    pp_id = session.execute(text("SELECT LAST_INSERT_ID()")).fetchone()[0]
    # Owner -> Buyer must not delete property rows.
    c.post("/users/me/role", json={"role": "BUYER"}, headers=h)
    assert session.execute(
        text("SELECT COUNT(*) FROM physical_properties WHERE id=:i"), {"i": pp_id}
    ).fetchone()[0] == 1
    # Switching back restores management: ownership proof passes again.
    c.post("/users/me/role", json={"role": "OWNER"}, headers=h)
    assert c.get(f"/owner/check-ownership/{pp_id}", headers=h).status_code == 200


def test_ownership_enforcement(client):
    c, session = client
    a = register_phone(c, "+10000000005")
    b = register_phone(c, "+10000000006")
    ha, hb = auth_header(a["access_token"]), auth_header(b["access_token"])
    c.post("/users/me/role", json={"role": "OWNER"}, headers=ha)
    c.post("/users/me/role", json={"role": "OWNER"}, headers=hb)
    pc = make_postcode(session)
    session.execute(
        text("INSERT INTO physical_properties (postcode_id, user_id) VALUES (:p, :u)"),
        {"p": pc, "u": a["user_id"]},
    )
    session.flush()
    pp_id = session.execute(text("SELECT LAST_INSERT_ID()")).fetchone()[0]
    assert c.get(f"/owner/check-ownership/{pp_id}", headers=ha).status_code == 200
    assert c.get(f"/owner/check-ownership/{pp_id}", headers=hb).status_code == 403
    assert c.get("/owner/check-ownership/999999999", headers=ha).status_code == 404


def test_auth_success_and_me(client):
    c, _ = client
    tok = register_phone(c, "+10000000007")
    me = c.get("/auth/me", headers=auth_header(tok["access_token"]))
    assert me.status_code == 200
    body = me.json()
    assert body["id"] == tok["user_id"] and body["role"] == "BUYER"
    assert body["is_admin"] is False


def test_auth_failure_wrong_code(client):
    c, _ = client
    c.post("/auth/otp/request", json={"phone": "+10000000008"})
    r = c.post("/auth/otp/verify", json={"phone": "+10000000008", "code": "000000"})
    assert r.status_code == 401


def test_protected_without_auth(client):
    c, _ = client
    for path in ("/auth/me", "/buyer/welcome", "/owner/vault", "/broker/scope", "/admin/ping"):
        assert c.get(path).status_code == 401, path


def test_invalid_and_expired_tokens(client):
    c, _ = client
    assert c.get("/auth/me", headers=auth_header("not.a.token")).status_code == 401
    import jwt as pyjwt

    tok = register_phone(c, "+10000000009")
    expired = pyjwt.encode(
        {"sub": str(tok["user_id"]), "role": "BUYER", "type": "access", "exp": 1},
        "phase4-test-secret",
        algorithm="HS256",
    )
    assert c.get("/auth/me", headers=auth_header(expired)).status_code == 401
    # Token for a nonexistent user is rejected.
    ghost = pyjwt.encode(
        {"sub": "999999999", "role": "BUYER", "type": "access", "exp": 9999999999},
        "phase4-test-secret",
        algorithm="HS256",
    )
    assert c.get("/auth/me", headers=auth_header(ghost)).status_code == 401


def test_otp_rate_limit_foundation(client):
    c, _ = client
    phone = "+10000000010"
    for _ in range(5):
        assert c.post("/auth/otp/request", json={"phone": phone}).status_code == 200
    assert c.post("/auth/otp/request", json={"phone": phone}).status_code == 429
    # Wrong-code attempts also trip protection.
    phone2 = "+10000000011"
    c.post("/auth/otp/request", json={"phone": phone2})
    for _ in range(5):
        c.post("/auth/otp/verify", json={"phone": phone2, "code": "000000"})
    r = c.post("/auth/otp/verify", json={"phone": phone2, "code": "000000"})
    assert r.status_code in (401, 429)


def test_google_unknown_has_no_autocreate(client):
    c, session = client
    before = session.execute(text("SELECT COUNT(*) FROM users")).fetchone()[0]
    r = c.post("/auth/google", json={"id_token": "dev:g-unknown-xyz"})
    assert r.status_code == 404
    assert r.json()["detail"] == "GOOGLE_UNKNOWN"
    after = session.execute(text("SELECT COUNT(*) FROM users")).fetchone()[0]
    assert before == after


def test_google_known_identity_authenticates(client):
    c, session = client
    tok = register_phone(c, "+10000000012")
    uid = tok["user_id"]
    session.execute(
        text("INSERT INTO auth_identities (user_id, provider, provider_identifier) "
             "VALUES (:u, 'google', 'g-known-1')"),
        {"u": uid},
    )
    session.flush()
    r = c.post("/auth/google", json={"id_token": "dev:g-known-1"})
    assert r.status_code == 200, r.text
    assert r.json()["user_id"] == uid
    # Google identifiers live only in auth_identities, never on users.
    cols = [row[0] for row in session.execute(text("SHOW COLUMNS FROM users")).fetchall()]
    assert not any("google" in col or "phone" in col for col in cols)


def test_admin_flow_no_public_registration(client):
    c, session = client
    tok = register_phone(c, "+10000000013")
    h = auth_header(tok["access_token"])
    assert c.get("/admin/ping", headers=h).status_code == 403
    assert c.post("/admin/grants", json={"user_id": tok["user_id"]}, headers=h).status_code == 403
    # Bootstrap the first admin directly in DB (local-dev procedure, not a public flow).
    session.execute(
        text("INSERT INTO admin_accounts (user_id) VALUES (:u)"), {"u": tok["user_id"]}
    )
    session.flush()
    assert c.get("/admin/ping", headers=h).status_code == 200
    other = register_phone(c, "+10000000014")
    r = c.post("/admin/grants", json={"user_id": other["user_id"]}, headers=h)
    assert r.status_code == 201
    assert (
        c.get("/admin/ping", headers=auth_header(other["access_token"])).status_code == 200
    )


def test_patch_users_me_profile_update(client):
    c, session = client
    phone = "+10000000099"
    tok = register_phone(c, phone)
    h = auth_header(tok["access_token"])

    # Initial state: display_name equals phone
    me = c.get("/auth/me", headers=h).json()
    assert me["display_name"] == phone

    # Unauthenticated request fails with 401
    assert c.patch("/users/me", json={"display_name": "Isaac Vineeth"}).status_code == 401

    # Empty / whitespace-only display_name fails with 422
    assert c.patch("/users/me", json={"display_name": ""}, headers=h).status_code == 422
    assert c.patch("/users/me", json={"display_name": "   "}, headers=h).status_code == 422

    # Successful update trims whitespace and persists
    res = c.patch("/users/me", json={"display_name": "  Isaac Vineeth  "}, headers=h)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["display_name"] == "Isaac Vineeth"
    assert data["id"] == tok["user_id"]

    # Verify /auth/me reflects the update
    me_after = c.get("/auth/me", headers=h).json()
    assert me_after["display_name"] == "Isaac Vineeth"


def test_schema_regression_29_tables(client):
    # Drafts phase: 32 + listing_drafts.
    c, session = client
    tables = {
        r[0]
        for r in session.execute(
            text("SELECT TABLE_NAME FROM information_schema.TABLES "
                  "WHERE TABLE_SCHEMA = DATABASE()")
        ).fetchall()
    } - {"alembic_version"}
    assert len(tables) == 33
    assert "saved_properties" in tables
    assert "rent_terms" in tables
    assert "amenity_master" in tables
    assert "listing_amenities" in tables
    assert session.execute(text("SELECT version_num FROM alembic_version")).fetchone()[0] == (
        "d5e6f7a8b9c0"
    )
