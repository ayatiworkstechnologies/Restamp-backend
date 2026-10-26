"""Phase 1 security regression: JWT/dev-flag guards, OTP per-IP caps, POST /users.

Unit tests (no DB) run first and never touch shared data. Client tests use
the rollback-isolated `client` fixture on local restamp_dev only.
"""
import pytest
from fastapi import HTTPException, Request
from sqlalchemy import text

from app import config as config_module
from app.config import (
    _is_local_database_url,
    is_jwt_secret_strong,
    is_production,
    validate_security_config,
)
from app.routers.auth import _client_ip
from app.services.otp import OTPService
from tests.conftest import TestSMSInbox, auth_header, register_phone


# --- Unit: production detection (no DB) -------------------------------------

def test_is_production_local_by_default():
    assert config_module.settings.DATABASE_URL is not None
    # Default local env + local DB URL => not production.
    assert is_production() is False


def test_is_production_explicit_env(monkeypatch):
    monkeypatch.setattr(config_module.settings, "ENV", "production")
    assert is_production() is True
    monkeypatch.setattr(config_module.settings, "ENV", "staging")
    assert is_production() is True
    monkeypatch.setattr(config_module.settings, "ENV", "local")
    assert is_production() is False


def test_is_production_remote_url_triggers(monkeypatch):
    monkeypatch.setattr(config_module.settings, "ENV", "local")
    monkeypatch.setattr(
        config_module.settings,
        "DATABASE_URL",
        "mysql+pymysql://u:p@db.example.com/restamp_prod",
    )
    assert is_production() is True


# --- Unit: JWT strength (no DB, no secrets in output) -----------------------

def test_jwt_weak_secrets_rejected():
    for weak in ["", "short", "x" * 31, "restamp-local-dev-secret-change-me",
                 "change-me-to-a-long-random-secret", "phase4-test-secret"]:
        assert is_jwt_secret_strong(weak) is False


def test_jwt_strong_secret_accepted():
    assert is_jwt_secret_strong("a" * 32) is True
    assert is_jwt_secret_strong("r4nd0m-L0ng-s3cure-value-xyz-1234567890") is True


def test_validate_passes_local_with_dev_flags(monkeypatch):
    monkeypatch.setattr(config_module.settings, "ENV", "local")
    monkeypatch.setattr(config_module.settings, "JWT_SECRET", "short")
    monkeypatch.setattr(config_module.settings, "GOOGLE_DEV_MODE", True)
    validate_security_config()  # no-op locally


def test_validate_fails_prod_weak_jwt(monkeypatch):
    monkeypatch.setattr(config_module.settings, "ENV", "production")
    monkeypatch.setattr(config_module.settings, "JWT_SECRET", "short")
    monkeypatch.setattr(config_module.settings, "GOOGLE_DEV_MODE", False)
    with pytest.raises(RuntimeError, match="RESTAMP_JWT_SECRET"):
        validate_security_config()


def test_validate_fails_prod_dev_flags(monkeypatch):
    monkeypatch.setattr(config_module.settings, "ENV", "production")
    monkeypatch.setattr(config_module.settings, "JWT_SECRET", "z" * 40)
    monkeypatch.setattr(config_module.settings, "GOOGLE_DEV_MODE", True)
    with pytest.raises(RuntimeError, match="RESTAMP_GOOGLE"):
        validate_security_config()


def test_validate_errors_never_contain_secret(monkeypatch):
    sentinel = "s3nsitive-marker-" + "q" * 20
    monkeypatch.setattr(config_module.settings, "ENV", "production")
    monkeypatch.setattr(config_module.settings, "JWT_SECRET", sentinel[:10])  # weak
    with pytest.raises(RuntimeError) as info:
        validate_security_config()
    assert sentinel[:10] not in str(info.value)


# --- Unit: OTP per-phone preserved + per-IP added (no DB) -------------------

def test_otp_per_phone_cap_preserved():
    svc = OTPService()
    svc.delivery = TestSMSInbox()
    phone = "+916900000101"
    for _ in range(5):
        svc.request_code(phone)
    with pytest.raises(HTTPException) as e:
        svc.request_code(phone)
    assert e.value.status_code == 429


def test_otp_per_ip_request_cap_across_phones():
    svc = OTPService()
    svc.delivery = TestSMSInbox()
    ip = "10.9.9.1"
    for i in range(30):
        svc.request_code(f"+91690010{i:04d}", ip=ip)
    with pytest.raises(HTTPException) as e:
        svc.request_code("+916900999999", ip=ip)
    assert e.value.status_code == 429
    # Different IP unaffected.
    svc.request_code("+916900999998", ip="10.9.9.2")


def test_otp_phone_rotation_still_bounded_by_ip(monkeypatch):
    svc = OTPService()
    svc.delivery = TestSMSInbox()
    monkeypatch.setattr(config_module.settings, "OTP_MAX_VERIFY_ATTEMPTS_PER_IP", 5)
    ip = "10.9.9.3"
    tripped = False
    for i in range(6):
        phone = f"+91690020{i:04d}"
        svc.request_code(phone, ip=ip)
        try:
            svc.verify_code(phone, "000000", ip=ip)
        except HTTPException as e:
            assert e.status_code == 429
            tripped = True
            break
    assert tripped is True


def test_otp_reset_clears_ip_state():
    svc = OTPService()
    svc.delivery = TestSMSInbox()
    ip = "10.9.9.4"
    for i in range(30):
        svc.request_code(f"+91690030{i:04d}", ip=ip)
    svc.reset()
    svc.request_code("+916900999997", ip=ip)  # no longer blocked


def test_otp_backward_compat_no_ip():
    svc = OTPService()
    inbox = TestSMSInbox()
    svc.delivery = inbox
    assert svc.request_code("+916900000102") is None  # codes are never returned
    code = inbox.codes["+916900000102"]  # read the SMS via the test inbox
    assert svc.verify_code("+916900000102", code) is True


# --- Integration: POST /users disabled, OTP + profile preserved (restamp_dev rollback) ---

def test_post_users_disabled_no_creation(client):
    c, session = client
    before = session.execute(text("SELECT COUNT(*) FROM users")).fetchone()[0]
    r = c.post("/users", json={"display_name": "Abuser"})
    assert r.status_code == 410, r.text
    assert "OTP" in r.json()["detail"]
    after = session.execute(text("SELECT COUNT(*) FROM users")).fetchone()[0]
    assert before == after


def test_otp_registration_and_profile_setup_still_work(client):
    c, _ = client
    phone = "+916000000301"
    tok = register_phone(c, phone)
    h = auth_header(tok["access_token"])
    me = c.get("/auth/me", headers=h).json()
    assert me["display_name"] == phone
    r = c.patch("/users/me", json={"display_name": "  First Time  "}, headers=h)
    assert r.status_code == 200
    assert r.json()["display_name"] == "First Time"


def test_owner_broker_self_serve_preserved(client):
    c, _ = client
    tok = register_phone(c, "+916000000302")
    h = auth_header(tok["access_token"])
    assert c.post("/users/me/role", json={"role": "OWNER"}, headers=h).status_code == 200
    assert c.post("/users/me/role", json={"role": "BROKER"}, headers=h).status_code == 200
    assert c.post("/users/me/role", json={"role": "ADMIN"}, headers=h).status_code == 422


def test_otp_request_verify_contract_preserved(client):
    c, _ = client
    r = c.post("/auth/otp/request", json={"phone": "+916000000303"})
    assert r.status_code == 200
    # Constant body, zero disclosure: no debug_code can exist in responses.
    assert r.json() == {"message": "If the number is valid, an OTP was sent."}
    bad = c.post("/auth/otp/verify", json={"phone": "+916000000303", "code": "000000"})
    assert bad.status_code == 401


# --- Reliability audit: startup fail-closed (no DB writes) -------------------

def test_cors_wildcard_rejected_in_production(monkeypatch):
    from app import config as config_module
    monkeypatch.setattr(config_module.settings, "ENV", "production")
    monkeypatch.setattr(config_module.settings, "JWT_SECRET", "z" * 40)
    monkeypatch.setattr(config_module.settings, "GOOGLE_DEV_MODE", False)
    monkeypatch.setattr(config_module.settings, "GOOGLE_ENABLED", False)
    monkeypatch.setattr(config_module.settings, "CORS_ORIGINS", "https://app.example.com, *")
    with pytest.raises(RuntimeError, match="CORS_ORIGINS"):
        validate_security_config()
    monkeypatch.setattr(config_module.settings, "CORS_ORIGINS", "https://app.example.com")
    validate_security_config()  # exact origins pass


def test_lifespan_fails_when_db_unreachable(monkeypatch):
    """Startup serves no traffic without a database (no DB touched)."""
    import asyncio
    import app.main as main_module
    from fastapi import FastAPI
    monkeypatch.setattr(main_module, "check_db", lambda: False)
    app = FastAPI()

    async def _drain():
        async with main_module.lifespan(app):
            pass

    with pytest.raises(RuntimeError, match="Database unreachable"):
        asyncio.run(_drain())


# --- Phase 1 hardening batch: proxy client IP, env detection, docs, media ----

def _client_ip_request(peer: str | None, xff: bytes | None = None) -> Request:
    """Build a Request from a raw ASGI scope (no server, no DB)."""
    headers = [(b"host", b"restamp-backend.onrender.com")]
    if xff is not None:
        headers.append((b"x-forwarded-for", xff))
    scope: dict = {"type": "http", "headers": headers}
    if peer is not None:
        scope["client"] = (peer, 51234)
    return Request(scope)


# Unchanged default semantics: the socket peer is the per-IP key; the
# header is ignored unless RESTAMP_TRUST_PROXY=1.
def test_client_ip_default_mode_ignores_forwarded(monkeypatch):
    monkeypatch.setattr(config_module.settings, "TRUST_PROXY", False)
    r = _client_ip_request("203.0.113.7", b"198.51.100.9, 10.0.0.5")
    assert _client_ip(r) == "203.0.113.7"


def test_client_ip_trusted_proxy_uses_rightmost_hop(monkeypatch):
    monkeypatch.setattr(config_module.settings, "TRUST_PROXY", True)
    r = _client_ip_request("203.0.113.1", b"198.51.100.9, 10.0.0.5")
    # Rightmost = appended by the trusted platform proxy (unspoofable by
    # the client itself); earlier entries are attacker-visible and ignored.
    assert _client_ip(r) == "10.0.0.5"


def test_client_ip_trusted_proxy_single_hop(monkeypatch):
    monkeypatch.setattr(config_module.settings, "TRUST_PROXY", True)
    r = _client_ip_request("203.0.113.1", b"198.51.100.9")
    assert _client_ip(r) == "198.51.100.9"


def test_client_ip_trusted_proxy_without_header_falls_back(monkeypatch):
    monkeypatch.setattr(config_module.settings, "TRUST_PROXY", True)
    assert _client_ip(_client_ip_request("203.0.113.1")) == "203.0.113.1"


def test_client_ip_trusted_proxy_loopback_peer_ignores_header(monkeypatch):
    monkeypatch.setattr(config_module.settings, "TRUST_PROXY", True)
    r = _client_ip_request("127.0.0.1", b"8.8.8.8")
    assert _client_ip(r) == "127.0.0.1"


def test_client_ip_trusted_proxy_blank_forwarded_entries(monkeypatch):
    monkeypatch.setattr(config_module.settings, "TRUST_PROXY", True)
    assert _client_ip(_client_ip_request("203.0.113.1", b"   ")) == "203.0.113.1"
    assert _client_ip(_client_ip_request("203.0.113.1", b"  , ")) == "203.0.113.1"


def test_client_ip_missing_client_is_none(monkeypatch):
    monkeypatch.setattr(config_module.settings, "TRUST_PROXY", False)
    assert _client_ip(_client_ip_request(None)) is None
    assert _client_ip(None) is None


# Strict URL parsing: a loopback SUBSTRING can no longer make a remote host
# classify as local (which would skip production hardening entirely).
def test_local_db_url_detection():
    assert _is_local_database_url(
        "mysql+pymysql://u:p@localhost/restamp_dev?unix_socket=/tmp/mysql.sock"
    )
    assert _is_local_database_url("mysql+pymysql://u:p@localhost/restamp")
    assert _is_local_database_url("mysql+pymysql://u:p@127.0.0.1:3306/restamp")
    assert _is_local_database_url("postgresql://u:p@[::1]:5432/db")
    assert not _is_local_database_url(
        "mysql+pymysql://u:p@localhost.attacker.tld/restamp_dev"
    )
    assert not _is_local_database_url("mysql+pymysql://u:p@db.example.com/restamp")
    assert not _is_local_database_url("")
    assert not _is_local_database_url("this is not a url")


def test_is_production_not_fooled_by_substring_host(monkeypatch):
    monkeypatch.setattr(config_module.settings, "ENV", "local")
    monkeypatch.setattr(
        config_module.settings,
        "DATABASE_URL",
        "mysql+pymysql://u:p@localhost.attacker.tld/restamp_dev",
    )
    assert is_production() is True


# Interactive API docs enumerate the full public route surface: they must
# not exist in production (locally unchanged).
def test_prod_docs_disabled(monkeypatch):
    import app.main as main_module
    monkeypatch.setattr(config_module.settings, "ENV", "production")
    ap = main_module.create_app()
    assert ap.docs_url is None
    assert ap.redoc_url is None
    assert ap.openapi_url is None


def test_local_docs_available():
    import app.main as main_module
    assert main_module.app.docs_url == "/docs"
    assert main_module.app.openapi_url == "/openapi.json"


# Missing Cloudinary in production warns loudly at startup but stays a
# non-fatal decision (fail-fast would be a behavior change needing approval).
def _prod_static_media_settings(monkeypatch, cloud_name: str) -> None:
    monkeypatch.setattr(config_module.settings, "ENV", "production")
    monkeypatch.setattr(config_module.settings, "JWT_SECRET", "z" * 40)
    monkeypatch.setattr(config_module.settings, "GOOGLE_DEV_MODE", False)
    monkeypatch.setattr(config_module.settings, "GOOGLE_ENABLED", False)
    monkeypatch.setattr(config_module.settings, "CORS_ORIGINS", "https://app.example.com")
    monkeypatch.setattr(config_module.settings, "CLOUDINARY_CLOUD_NAME", cloud_name)


def test_prod_cloudinary_absence_warns_not_raises(monkeypatch, caplog):
    _prod_static_media_settings(monkeypatch, "")
    with caplog.at_level("WARNING", logger="restamp.config"):
        validate_security_config()  # warning, never an exception
    assert "CLOUDINARY_CLOUD_NAME" in caplog.text


def test_prod_cloudinary_configured_silent(monkeypatch, caplog):
    _prod_static_media_settings(monkeypatch, "restamp-cloud")
    with caplog.at_level("WARNING", logger="restamp.config"):
        validate_security_config()
    assert "CLOUDINARY_CLOUD_NAME" not in caplog.text


# /media serves client-declared content types: nosniff must ride on every
# response from that prefix (including errors), and only on that prefix.
def test_media_responses_carry_nosniff(client):
    c, _ = client
    r = c.get("/media/no-such-file.jpg")
    assert r.status_code == 404
    assert r.headers.get("x-content-type-options") == "nosniff"


def test_non_media_responses_lack_nosniff(client):
    c, _ = client
    r = c.get("/health")
    assert r.status_code == 200
    assert "x-content-type-options" not in r.headers
