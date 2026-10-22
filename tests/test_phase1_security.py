"""Phase 1 security regression: JWT/dev-flag guards, OTP per-IP caps, POST /users.

Unit tests (no DB) run first and never touch shared data. Client tests use
the rollback-isolated `client` fixture on local restamp_dev only.
"""
import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app import config as config_module
from app.config import (
    is_jwt_secret_strong,
    is_production,
    validate_security_config,
)
from app.services.otp import OTPService
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
    monkeypatch.setattr(config_module.settings, "OTP_DEBUG", True)
    monkeypatch.setattr(config_module.settings, "OTP_DEMO_MODE", True)
    monkeypatch.setattr(config_module.settings, "GOOGLE_DEV_MODE", True)
    validate_security_config()  # no-op locally


def test_validate_fails_prod_weak_jwt(monkeypatch):
    monkeypatch.setattr(config_module.settings, "ENV", "production")
    monkeypatch.setattr(config_module.settings, "JWT_SECRET", "short")
    monkeypatch.setattr(config_module.settings, "OTP_DEBUG", False)
    monkeypatch.setattr(config_module.settings, "OTP_DEMO_MODE", False)
    monkeypatch.setattr(config_module.settings, "GOOGLE_DEV_MODE", False)
    with pytest.raises(RuntimeError, match="RESTAMP_JWT_SECRET"):
        validate_security_config()


def test_validate_fails_prod_dev_flags(monkeypatch):
    monkeypatch.setattr(config_module.settings, "ENV", "production")
    monkeypatch.setattr(config_module.settings, "JWT_SECRET", "z" * 40)
    for flag in ("OTP_DEBUG", "OTP_DEMO_MODE", "GOOGLE_DEV_MODE"):
        monkeypatch.setattr(config_module.settings, "OTP_DEBUG", False)
        monkeypatch.setattr(config_module.settings, "OTP_DEMO_MODE", False)
        monkeypatch.setattr(config_module.settings, "GOOGLE_DEV_MODE", False)
        monkeypatch.setattr(config_module.settings, flag, True)
        with pytest.raises(RuntimeError, match="RESTAMP_" + flag.split("_")[0]):
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
    phone = "+15900000101"
    for _ in range(5):
        svc.request_code(phone)
    with pytest.raises(HTTPException) as e:
        svc.request_code(phone)
    assert e.value.status_code == 429


def test_otp_per_ip_request_cap_across_phones():
    svc = OTPService()
    ip = "10.9.9.1"
    for i in range(30):
        svc.request_code(f"+1590010{i:04d}", ip=ip)
    with pytest.raises(HTTPException) as e:
        svc.request_code("+15900999999", ip=ip)
    assert e.value.status_code == 429
    # Different IP unaffected.
    svc.request_code("+15900999998", ip="10.9.9.2")


def test_otp_phone_rotation_still_bounded_by_ip(monkeypatch):
    svc = OTPService()
    monkeypatch.setattr(config_module.settings, "OTP_MAX_VERIFY_ATTEMPTS_PER_IP", 5)
    ip = "10.9.9.3"
    tripped = False
    for i in range(6):
        phone = f"+1590020{i:04d}"
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
    ip = "10.9.9.4"
    for i in range(30):
        svc.request_code(f"+1590030{i:04d}", ip=ip)
    svc.reset()
    svc.request_code("+15900999997", ip=ip)  # no longer blocked


def test_otp_backward_compat_no_ip():
    svc = OTPService()
    code = svc.request_code("+15900000102")
    assert code is not None  # OTP_DEBUG=1 in test env
    assert svc.verify_code("+15900000102", code) is True


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
    phone = "+10000000301"
    tok = register_phone(c, phone)
    h = auth_header(tok["access_token"])
    me = c.get("/auth/me", headers=h).json()
    assert me["display_name"] == phone
    r = c.patch("/users/me", json={"display_name": "  First Time  "}, headers=h)
    assert r.status_code == 200
    assert r.json()["display_name"] == "First Time"


def test_owner_broker_self_serve_preserved(client):
    c, _ = client
    tok = register_phone(c, "+10000000302")
    h = auth_header(tok["access_token"])
    assert c.post("/users/me/role", json={"role": "OWNER"}, headers=h).status_code == 200
    assert c.post("/users/me/role", json={"role": "BROKER"}, headers=h).status_code == 200
    assert c.post("/users/me/role", json={"role": "ADMIN"}, headers=h).status_code == 422


def test_otp_request_verify_contract_preserved(client):
    c, _ = client
    r = c.post("/auth/otp/request", json={"phone": "+10000000303"})
    assert r.status_code == 200
    assert r.json()["message"] == "If the number is valid, an OTP was sent."
    assert "debug_code" in r.json()  # test env OTP_DEBUG=1
    bad = c.post("/auth/otp/verify", json={"phone": "+10000000303", "code": "000000"})
    assert bad.status_code == 401


# --- Reliability audit: startup fail-closed (no DB writes) -------------------

def test_cors_wildcard_rejected_in_production(monkeypatch):
    from app import config as config_module
    from app.services import otp as otp_module
    monkeypatch.setattr(config_module.settings, "ENV", "production")
    monkeypatch.setattr(config_module.settings, "JWT_SECRET", "z" * 40)
    monkeypatch.setattr(config_module.settings, "OTP_DEBUG", False)
    monkeypatch.setattr(config_module.settings, "OTP_DEMO_MODE", False)
    monkeypatch.setattr(config_module.settings, "GOOGLE_DEV_MODE", False)
    monkeypatch.setattr(config_module.settings, "GOOGLE_ENABLED", False)
    monkeypatch.setattr(config_module.settings, "SMS_PROVIDER", "fake-vendor")
    monkeypatch.setitem(otp_module.PRODUCTION_SMS_PROVIDERS,
                        "fake-vendor", otp_module.DevOTPProvider)
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
