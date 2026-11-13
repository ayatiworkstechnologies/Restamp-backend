"""The single OTP contract, end to end (one implementation, every environment).

Restamp's simplified login has ONE fixed testing code, TEST_OTP = "349086"
(app/services/otp.py). It is accepted only where otp_display_permitted()
is true — an explicit development RESTAMP_ENV (local/development/dev/test)
or staging — and returned there by /auth/otp/request as `debug_code`.
Production-classified environments (production/prod, unset/unknown, demo)
reject every code: the request endpoint fails closed with a generic 503
and verification is always 401, so the testing mechanism can never
authenticate (or disclose anything on) a real deployment. There is no
environment variable that can widen the gate.

Also locks the clean-up: no random generation/hashing/storage, no expiry,
no attempt counters or OTP rate limits, no SMS delivery seam, no
SMS8/MSG91 provider code, no internal-tester (RESTAMP_OTP_TEST_*), no
display-mode flag, no demo OTP settings.

The request/response shapes asserted here are exactly what the mobile app
(src/features/auth/api/auth.js, LoginScreen) and the website
(src/lib/api/auth.ts, useOtpLogin) send and read.

Rollback-isolated database, no network, no SMS. Documentation-style numbers.
"""
import logging
import pathlib
import re

import pytest
from fastapi.testclient import TestClient

from app import config as config_module
from app.main import create_app
from app.services import otp as otp_module
from app.services.otp import TEST_OTP, UNAVAILABLE_503
from tests.conftest import auth_header, captured_otp_code

PHONE = "+919876512340"
MESSAGE = "If the number is valid, an OTP was sent."
WRONG = "349087"  # one digit off the fixed code
STRONG = "c" * 40


def _production(monkeypatch, **overrides):
    s = config_module.settings
    values = dict(ENV="production", JWT_SECRET=STRONG, GOOGLE_DEV_MODE=False,
                  GOOGLE_ENABLED=False, GOOGLE_CLIENT_ID="", CORS_ORIGINS="https://restamp.in")
    values.update(overrides)
    for key, value in values.items():
        monkeypatch.setattr(s, key, value)


def _staging(monkeypatch, **overrides):
    """A valid staging configuration (test-site origins, strong secret)."""
    s = config_module.settings
    values = dict(ENV="staging", JWT_SECRET=STRONG, GOOGLE_DEV_MODE=False,
                  GOOGLE_ENABLED=False, GOOGLE_CLIENT_ID="",
                  CORS_ORIGINS="https://restamp-alpha.vercel.app")
    values.update(overrides)
    for key, value in values.items():
        monkeypatch.setattr(s, key, value)


# --- the fixed code, end to end (the mobile + website contract) -----------------

def test_local_request_returns_the_fixed_code(client):
    c, _ = client
    r = c.post("/auth/otp/request", json={"phone": PHONE})
    assert r.status_code == 200, r.text
    # Both frontends read exactly these two fields.
    assert r.json() == {"message": MESSAGE, "debug_code": TEST_OTP}
    assert TEST_OTP == "349086"


def test_staging_request_returns_the_fixed_code(client, monkeypatch):
    c, _ = client
    _staging(monkeypatch)
    r = c.post("/auth/otp/request", json={"phone": PHONE})
    assert r.status_code == 200, r.text
    assert r.json() == {"message": MESSAGE, "debug_code": TEST_OTP}


def test_correct_code_authenticates_the_requested_account(client):
    c, _ = client
    c.post("/auth/otp/request", json={"phone": PHONE})
    ok = c.post("/auth/otp/verify", json={"phone": PHONE, "code": TEST_OTP})
    assert ok.status_code == 200, ok.text
    body = ok.json()
    # Fields both clients read from TokenOut.
    assert {"access_token", "token_type", "expires_in", "user_id", "role"} <= set(body)
    assert body["token_type"] == "bearer" and body["role"] == "BUYER" and body["expires_in"] > 0
    # The token is a working session for the requested phone's account.
    me = c.get("/auth/me", headers=auth_header(body["access_token"]))
    assert me.status_code == 200 and me.json()["id"] == body["user_id"]
    assert me.json()["display_name"] == PHONE  # first login: display name = phone


def test_correct_code_authenticates_an_existing_account(client):
    """Second login on the same number: the SAME account, no new row."""
    c, session = client
    first = c.post("/auth/otp/verify", json={"phone": PHONE, "code": TEST_OTP}).json()
    c.post("/auth/otp/request", json={"phone": PHONE})
    second = c.post("/auth/otp/verify", json={"phone": PHONE, "code": TEST_OTP})
    assert second.status_code == 200, second.text
    assert second.json()["user_id"] == first["user_id"]
    from sqlalchemy import text as _text
    rows = session.execute(
        _text("SELECT COUNT(*) FROM auth_identities WHERE provider='phone' AND provider_identifier=:p"),
        {"p": PHONE},
    ).fetchone()[0]
    assert rows == 1  # one identity, two sessions


def test_wrong_code_rejected_with_static_detail(client):
    c, _ = client
    c.post("/auth/otp/request", json={"phone": PHONE})
    bad = c.post("/auth/otp/verify", json={"phone": PHONE, "code": WRONG})
    assert bad.status_code == 401 and bad.json() == {"detail": "Invalid code"}
    # The right code still works afterwards: no counters, no lockout.
    assert c.post("/auth/otp/verify", json={"phone": PHONE, "code": TEST_OTP}).status_code == 200


def test_verify_without_request_rejected(client):
    """There is no stored state: verification is purely the fixed compare."""
    c, _ = client
    r = c.post("/auth/otp/verify", json={"phone": PHONE, "code": TEST_OTP})
    assert r.status_code == 200  # local/staging: the fixed code needs no prior request


def test_malformed_input_rejected(client):
    c, _ = client
    assert c.post("/auth/otp/request", json={"phone": "9876512340"}).status_code == 422
    assert c.post("/auth/otp/verify", json={"phone": PHONE, "code": "12345"}).status_code == 422
    assert c.post("/auth/otp/verify", json={"phone": PHONE, "code": "34908a"}).status_code == 422


def test_otp_schemas_accept_indian_mobile_only():
    from app.schemas import OTPRequest, OTPVerify

    assert OTPRequest(phone=PHONE).phone == PHONE
    assert OTPVerify(phone=PHONE, code=TEST_OTP).code == TEST_OTP
    for bad in ("9876512340", "+911234567890", "+915987654321", "+91987654321", "not-a-phone"):
        with pytest.raises(Exception):
            OTPRequest(phone=bad)
        with pytest.raises(Exception):
            OTPVerify(phone="+15000000001", code=TEST_OTP)


# --- production -----------------------------------------------------------------

def test_production_request_fails_closed_and_returns_no_otp(client, monkeypatch):
    c, _ = client
    _production(monkeypatch)
    r = c.post("/auth/otp/request", json={"phone": PHONE})
    assert r.status_code == 503 and r.json() == {"detail": UNAVAILABLE_503}
    assert TEST_OTP not in r.text and "debug_code" not in r.text


def test_production_rejects_the_fixed_otp(client, monkeypatch):
    c, _ = client
    _production(monkeypatch)
    for code in (TEST_OTP, WRONG, "000000"):
        r = c.post("/auth/otp/verify", json={"phone": PHONE, "code": code})
        assert r.status_code == 401, (code, r.text)
        assert "access_token" not in r.text


def test_production_service_rejects_every_code(monkeypatch):
    _production(monkeypatch)
    assert otp_module.verify_code(PHONE, TEST_OTP) is False
    with pytest.raises(Exception):
        otp_module.request_code(PHONE)


def test_production_router_drops_a_code_even_if_the_service_returned_one(client, monkeypatch):
    """Defence in depth: the router re-checks the environment on its own."""
    c, _ = client
    _production(monkeypatch)
    monkeypatch.setattr(otp_module, "request_code", lambda phone: TEST_OTP)
    r = c.post("/auth/otp/request", json={"phone": PHONE})
    assert r.status_code == 200 and r.json() == {"message": MESSAGE}
    assert TEST_OTP not in r.text and "debug_code" not in r.text


def test_production_startup_refuses_the_obsolete_display_flag(monkeypatch):
    _production(monkeypatch)
    import os

    monkeypatch.setenv("RESTAMP_OTP_DISPLAY_MODE", "1")
    with pytest.raises(RuntimeError, match="RESTAMP_OTP_DISPLAY_MODE") as info:
        config_module.validate_security_config()
    assert "349086" not in str(info.value)


def test_production_startup_logs_that_otp_login_is_unavailable(monkeypatch, caplog):
    _production(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="restamp"):
        with TestClient(create_app()):
            pass
    assert "Phone OTP login is unavailable in production" in caplog.text


def test_development_startup_does_not_warn(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="restamp"):
        with TestClient(create_app()):
            pass
    assert "Phone OTP login is unavailable in production" not in caplog.text


# --- staging ---------------------------------------------------------------------

def test_staging_verifies_the_fixed_code_and_keeps_hardening(client, monkeypatch):
    c, _ = client
    _staging(monkeypatch)
    r = c.post("/auth/otp/request", json={"phone": PHONE})
    assert r.status_code == 200 and r.json()["debug_code"] == TEST_OTP
    ok = c.post("/auth/otp/verify", json={"phone": PHONE, "code": TEST_OTP})
    assert ok.status_code == 200 and ok.json()["access_token"]
    assert c.post("/auth/otp/verify", json={"phone": PHONE, "code": WRONG}).status_code == 401


def test_staging_startup_refuses_public_site_origins(monkeypatch):
    _staging(monkeypatch, CORS_ORIGINS="https://restamp.in,https://www.restamp.in")
    with pytest.raises(RuntimeError, match="public site"):
        config_module.validate_security_config()


def test_staging_startup_warns_the_testing_otp_is_active(monkeypatch, caplog):
    _staging(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="restamp.config"):
        config_module.validate_security_config()
    assert "fixed testing OTP is active" in caplog.text
    assert "349086" not in caplog.text  # the code value is never logged


# --- clean-up locks ---------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "OTP_LENGTH", "OTP_TTL_SECONDS", "OTP_MAX_REQUESTS_PER_HOUR",
    "OTP_MAX_VERIFY_ATTEMPTS", "OTP_BLOCK_SECONDS",
    "OTP_MAX_REQUESTS_PER_HOUR_PER_IP", "OTP_MAX_VERIFY_ATTEMPTS_PER_IP",
    "OTP_DISPLAY_MODE", "TRUST_PROXY",
    "OTP_TEST_DISPLAY", "OTP_TEST_PHONES", "OTP_TEST_ALLOWED_CIDRS",
    "OTP_DEMO_MODE", "OTP_DEMO_PHONES", "OTP_DEBUG", "OTP_DISPLAY_IN_PRODUCTION",
    "SMS_PROVIDER", "SMS8_API_KEY", "SMS_MSG91_AUTH_KEY",
])
def test_removed_settings_do_not_exist(name):
    assert not hasattr(config_module.settings, name)


def test_no_provider_tester_or_random_code_infrastructure_remains():
    app_dir = pathlib.Path(config_module.__file__).parent
    pattern = re.compile(r"sms8|msg91|OTP_TEST_|otp_test_display|SMS_PROVIDER", re.IGNORECASE)
    hits = [p.name for p in app_dir.rglob("*.py") if pattern.search(p.read_text())]
    assert hits == []


def test_service_has_no_random_or_hashed_code_pipeline():
    """The removed pipeline is gone from the service source itself."""
    source = pathlib.Path(otp_module.__file__).read_text()
    for marker in ("randbelow", "sha256", "hashlib", "threading", "time.time", "expires_at"):
        assert marker not in source, marker


def test_service_holds_no_state():
    """No storage, no counters, no locks: nothing to expire, consume or reset."""
    for name in ("_codes", "_requests", "_blocked_until", "_ip_requests",
                 "_ip_verify_failures", "_ip_blocked_until", "_lock",
                 "delivery", "reset", "OTPService"):
        assert not hasattr(otp_module, name), name


def test_obsolete_otp_env_vars_are_inert(monkeypatch, client):
    """The removed RESTAMP_OTP_* rate-limit/display settings change nothing."""
    monkeypatch.setenv("RESTAMP_OTP_TTL_SECONDS", "1")
    monkeypatch.setenv("RESTAMP_OTP_MAX_REQUESTS_PER_HOUR", "1")
    monkeypatch.setenv("RESTAMP_OTP_MAX_VERIFY_ATTEMPTS", "1")
    monkeypatch.setenv("RESTAMP_OTP_BLOCK_SECONDS", "1")
    monkeypatch.setenv("RESTAMP_OTP_DISPLAY_MODE", "1")  # inert in development too
    c, _ = client
    phone = "+916550009003"
    r = c.post("/auth/otp/request", json={"phone": phone})
    assert r.status_code == 200 and r.json() == {"message": MESSAGE, "debug_code": TEST_OTP}
    assert c.post("/auth/otp/verify", json={"phone": phone, "code": TEST_OTP}).status_code == 200
    # Repeated logins keep working (no consumed-once state, no burned budget).
    assert c.post("/auth/otp/verify", json={"phone": phone, "code": TEST_OTP}).status_code == 200
    for legacy in ("297569", "109745", "583214"):  # former demo codes never work
        assert c.post("/auth/otp/verify", json={"phone": phone, "code": legacy}).status_code == 401


def test_captured_otp_code_helper_is_the_fixed_code():
    assert captured_otp_code(PHONE) == TEST_OTP
