"""RESTAMP_OTP_DISPLAY_IN_PRODUCTION: explicit demo override on production.

Contract (tested below):
- a production-classified process returns the generated code as
  "debug_code" ONLY when BOTH RESTAMP_OTP_DISPLAY_MODE=1 and
  RESTAMP_OTP_DISPLAY_IN_PRODUCTION=1 are set; startup logs a warning
  naming the setting (never a code);
- RESTAMP_OTP_DISPLAY_MODE=1 alone still refuses production startup, and
  either flag alone keeps the fail-closed 503 at request time;
- every other production hardening rule still applies;
- every OTP lifecycle rule (random 6 digits, expiry, single use, attempt and
  request caps) is unchanged; verification uses the normal endpoint and an
  existing account keeps its id and role.

No database writes persist (endpoint tests use the rollback-isolated client
fixture), no network, no SMS vendor.
"""
import logging
import re

import pytest
from fastapi import HTTPException

from app import config as config_module
from app.config import is_production, otp_display_permitted, validate_security_config
from app.models import AuthIdentity
from app.services.otp import OTPService
from tests.conftest import auth_header

PHONE = "+919876543230"
PROD_DB_URL = "mysql+pymysql://u:p@db.example.com:3306/defaultdb"
NOT_CONNECTED_503 = "OTP delivery is not configured. Try again later."


def _production(monkeypatch, display: bool = True, override: bool = True) -> None:
    s = config_module.settings
    monkeypatch.setattr(s, "ENV", "production")
    monkeypatch.setattr(s, "DATABASE_URL", PROD_DB_URL)
    monkeypatch.setattr(s, "OTP_DISPLAY_MODE", display)
    monkeypatch.setattr(s, "OTP_DISPLAY_IN_PRODUCTION", override)
    monkeypatch.setattr(s, "JWT_SECRET", "z" * 40)
    monkeypatch.setattr(s, "GOOGLE_DEV_MODE", False)
    monkeypatch.setattr(s, "CORS_ORIGINS", "https://restamp-alpha.vercel.app")


def _wrong(code: str) -> str:
    return f"{(int(code) + 1) % 10**6:06d}"


# --- startup gate -----------------------------------------------------------------

def test_override_is_off_by_default():
    assert config_module.settings.OTP_DISPLAY_IN_PRODUCTION is False


def test_production_with_both_flags_starts_and_warns(monkeypatch, caplog):
    _production(monkeypatch)
    assert is_production() is True
    assert otp_display_permitted() is True
    with caplog.at_level(logging.WARNING, logger="restamp.config"):
        validate_security_config()  # no exception
    assert "RESTAMP_OTP_DISPLAY_IN_PRODUCTION is enabled" in caplog.text


def test_display_flag_alone_still_refused_in_production(monkeypatch):
    _production(monkeypatch, display=True, override=False)
    assert otp_display_permitted() is False
    with pytest.raises(RuntimeError, match="OTP_DISPLAY_MODE"):
        validate_security_config()


def test_override_alone_starts_without_display(monkeypatch, caplog):
    _production(monkeypatch, display=False, override=True)
    with caplog.at_level(logging.WARNING, logger="restamp.config"):
        validate_security_config()
    assert "DISPLAY_IN_PRODUCTION is enabled" not in caplog.text


def test_override_keeps_other_production_hardening(monkeypatch):
    _production(monkeypatch)
    monkeypatch.setattr(config_module.settings, "JWT_SECRET", "short")
    with pytest.raises(RuntimeError, match="RESTAMP_JWT_SECRET"):
        validate_security_config()
    _production(monkeypatch)
    monkeypatch.setattr(config_module.settings, "GOOGLE_DEV_MODE", True)
    with pytest.raises(RuntimeError, match="RESTAMP_GOOGLE_DEV_MODE"):
        validate_security_config()
    _production(monkeypatch)
    monkeypatch.setattr(config_module.settings, "CORS_ORIGINS", "*")
    with pytest.raises(RuntimeError, match="RESTAMP_CORS_ORIGINS"):
        validate_security_config()


def test_override_keeps_docs_disabled(monkeypatch):
    from app.main import create_app

    _production(monkeypatch)
    app = create_app()
    assert app.docs_url is None and app.openapi_url is None


# --- service level: lifecycle unchanged ------------------------------------------

def test_production_override_issues_random_code_single_use(monkeypatch):
    _production(monkeypatch)
    svc = OTPService()
    code = svc.request_code(PHONE, ip="198.51.100.30")
    assert code and re.fullmatch(r"\d{6}", code)
    assert svc.verify_code(PHONE, _wrong(code)) is False
    assert svc.verify_code(PHONE, code) is True
    assert svc.verify_code(PHONE, code) is False  # consumed


def test_production_override_codes_are_not_predictable(monkeypatch):
    _production(monkeypatch)
    monkeypatch.setattr(config_module.settings, "OTP_MAX_REQUESTS_PER_HOUR", 100)
    svc = OTPService()
    codes = {svc.request_code(PHONE) for _ in range(20)}
    assert len(codes) > 1  # not fixed / not derived from the phone


def test_production_override_expired_code_rejected(monkeypatch):
    _production(monkeypatch)
    svc = OTPService()
    code = svc.request_code(PHONE)
    svc._codes[PHONE].expires_at = 0.0  # white-box expiry
    assert svc.verify_code(PHONE, code) is False


def test_production_override_request_rate_limit(monkeypatch):
    _production(monkeypatch)
    svc = OTPService()
    for _ in range(config_module.settings.OTP_MAX_REQUESTS_PER_HOUR):
        assert svc.request_code(PHONE)
    with pytest.raises(HTTPException) as info:
        svc.request_code(PHONE)
    assert info.value.status_code == 429


def test_production_override_wrong_attempt_cap(monkeypatch):
    _production(monkeypatch)
    svc = OTPService()
    code = svc.request_code(PHONE)
    wrong = _wrong(code)
    for _ in range(config_module.settings.OTP_MAX_VERIFY_ATTEMPTS - 1):
        assert svc.verify_code(PHONE, wrong) is False
    with pytest.raises(HTTPException) as info:
        svc.verify_code(PHONE, wrong)
    assert info.value.status_code == 429
    with pytest.raises(HTTPException):  # blocked phone refuses even the right code
        svc.verify_code(PHONE, code)


def test_production_override_never_logs_the_code(monkeypatch, caplog):
    _production(monkeypatch)
    svc = OTPService()
    with caplog.at_level(logging.DEBUG):
        code = svc.request_code(PHONE)
        svc.verify_code(PHONE, code)
    assert code not in caplog.text


@pytest.mark.parametrize("display,override", [(True, False), (False, True), (False, False)])
def test_production_without_both_flags_fails_closed(monkeypatch, display, override):
    _production(monkeypatch, display=display, override=override)
    svc = OTPService()
    with pytest.raises(HTTPException) as info:
        svc.request_code(PHONE)
    assert info.value.status_code == 503
    assert info.value.detail == NOT_CONNECTED_503


# --- endpoint level (rollback-isolated client) -----------------------------------

def _request_and_verify(c, phone: str) -> dict:
    r = c.post("/auth/otp/request", json={"phone": phone})
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"message", "debug_code"}
    code = r.json()["debug_code"]
    assert re.fullmatch(r"\d{6}", code)
    ok = c.post("/auth/otp/verify", json={"phone": phone, "code": code})
    assert ok.status_code == 200, ok.text
    return ok.json()


def test_endpoint_flow_preserves_existing_account_and_role(client, monkeypatch):
    c, session = client
    _production(monkeypatch)
    phone = "+919876543231"

    first = _request_and_verify(c, phone)
    assert first["role"] == "BUYER"  # new phone registers once with the default role
    switched = c.post(
        "/users/me/role", json={"role": "OWNER"}, headers=auth_header(first["access_token"])
    )
    assert switched.status_code == 200, switched.text

    second = _request_and_verify(c, phone)
    assert second["user_id"] == first["user_id"]  # same account
    assert second["role"] == "OWNER"  # login does not reset the role
    me = c.get("/auth/me", headers=auth_header(second["access_token"]))
    assert me.status_code == 200 and me.json()["id"] == first["user_id"]
    identities = (
        session.query(AuthIdentity)
        .filter(AuthIdentity.provider == "phone", AuthIdentity.provider_identifier == phone)
        .count()
    )
    assert identities == 1  # no duplicate registration


def test_endpoint_rejects_wrong_and_reused_codes(client, monkeypatch):
    c, _ = client
    _production(monkeypatch)
    phone = "+919876543232"
    code = c.post("/auth/otp/request", json={"phone": phone}).json()["debug_code"]
    assert c.post(
        "/auth/otp/verify", json={"phone": phone, "code": _wrong(code)}
    ).status_code == 401
    assert c.post("/auth/otp/verify", json={"phone": phone, "code": code}).status_code == 200
    assert c.post("/auth/otp/verify", json={"phone": phone, "code": code}).status_code == 401


def test_endpoint_without_override_stays_503_without_code(client, monkeypatch):
    from app.services.otp import otp_service

    c, _ = client
    _production(monkeypatch, display=True, override=False)
    monkeypatch.setattr(otp_service, "delivery", None)
    r = c.post("/auth/otp/request", json={"phone": PHONE})
    assert r.status_code == 503
    assert "debug_code" not in r.text
