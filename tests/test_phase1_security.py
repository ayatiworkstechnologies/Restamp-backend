"""Phase 1 security regression: JWT/dev-flag guards, POST /users, OTP contract.

Unit tests (no DB) run first and never touch shared data. Client tests use
the rollback-isolated `client` fixture on local restamp_dev only. The OTP
per-IP caps and the proxy client-IP helper were removed with the simplified
OTP (no per-IP state exists anymore).
"""
import pytest
from sqlalchemy import text

from app import config as config_module
from app.config import (
    _is_local_database_url,
    is_jwt_secret_strong,
    is_production,
    validate_security_config,
)
from tests.conftest import auth_header, register_phone


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


def test_remote_database_no_longer_implies_production(monkeypatch):
    """C3: production is decided by RESTAMP_ENV alone, never by the database
    host — but a development RESTAMP_ENV against a remote database is
    refused at startup (a deployment that forgot RESTAMP_ENV=production)."""
    monkeypatch.setattr(config_module.settings, "ENV", "local")
    monkeypatch.setattr(
        config_module.settings,
        "DATABASE_URL",
        "mysql+pymysql://u:p@db.example.com/restamp_prod",
    )
    assert is_production() is False
    with pytest.raises(RuntimeError, match="RESTAMP_ENV=production"):
        validate_security_config()


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
    # Local/staging: the fixed testing code is disclosed to the client on
    # purpose (the login screen shows it); production never discloses one.
    assert r.json() == {
        "message": "If the number is valid, an OTP was sent.",
        "debug_code": "349086",
    }
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


def test_dev_env_not_fooled_by_substring_host(monkeypatch):
    """A host that merely contains "localhost" is not local: a development
    RESTAMP_ENV against it is refused at startup."""
    monkeypatch.setattr(config_module.settings, "ENV", "local")
    monkeypatch.setattr(
        config_module.settings,
        "DATABASE_URL",
        "mysql+pymysql://u:p@localhost.attacker.tld/restamp_dev",
    )
    with pytest.raises(RuntimeError, match="non-local database"):
        validate_security_config()


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
