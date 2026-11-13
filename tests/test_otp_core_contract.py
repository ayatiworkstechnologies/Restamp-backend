"""The single OTP contract, end to end (one implementation, every environment).

Restamp's backend is the only place an OTP is generated (Python `secrets`,
six digits), stored (phone-bound SHA-256 hash with a 300 s expiry) and
verified (single use). Environments differ ONLY in:
- delivery: OTPService.delivery (no SMS provider is integrated, so
  production fails closed with a clear 503 and a startup warning);
- display: RESTAMP_OTP_DISPLAY_MODE=1 returns the code as `debug_code`, and
  only in an explicit development RESTAMP_ENV. Production never returns it.

Also locks the clean-up: no SMS8/MSG91 provider code, no internal-tester
display (RESTAMP_OTP_TEST_*), no demo OTP settings remain.

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
from app.services.otp import OTPService, otp_service
from tests.conftest import auth_header, captured_otp_code

PHONE = "+919876512340"
MESSAGE = "If the number is valid, an OTP was sent."
NOT_CONNECTED_503 = "OTP delivery is not configured. Try again later."
STRONG = "c" * 40


def _production(monkeypatch, **overrides):
    s = config_module.settings
    values = dict(ENV="production", JWT_SECRET=STRONG, OTP_DISPLAY_MODE=False, GOOGLE_DEV_MODE=False,
                  GOOGLE_ENABLED=False, GOOGLE_CLIENT_ID="", CORS_ORIGINS="https://restamp.in")
    values.update(overrides)
    for key, value in values.items():
        monkeypatch.setattr(s, key, value)


def _wrong(code: str) -> str:
    return f"{(int(code) + 1) % 10**6:06d}"


# --- generation and storage -----------------------------------------------------

def test_codes_are_six_random_digits_and_stored_as_hash_only():
    svc = OTPService()
    sent = {}
    svc.delivery = lambda phone, code: sent.__setitem__(phone, code)
    codes = set()
    for _ in range(20):
        svc.reset()
        svc.request_code(PHONE)
        code = sent[PHONE]
        assert re.fullmatch(r"\d{6}", code)
        assert code not in repr(svc._codes[PHONE])  # only the hash is kept
        assert svc._codes[PHONE].expires_at > 0
        codes.add(code)
    assert len(codes) > 1  # never a fixed code


def test_generation_uses_secrets(monkeypatch):
    import app.services.otp as otp_module

    calls = []
    real = otp_module.secrets.randbelow

    def spy(n):
        calls.append(n)
        return real(n)

    monkeypatch.setattr(otp_module.secrets, "randbelow", spy)
    svc = OTPService()
    svc.delivery = lambda phone, code: None
    svc.request_code(PHONE)
    assert calls == [10**6]


# --- verification over HTTP (the mobile + website contract) -----------------------

def test_request_and_verify_shapes_used_by_mobile_and_website(client):
    c, _ = client
    r = c.post("/auth/otp/request", json={"phone": PHONE})
    assert r.status_code == 200 and r.json() == {"message": MESSAGE}  # no code without display mode
    code = captured_otp_code(PHONE)
    ok = c.post("/auth/otp/verify", json={"phone": PHONE, "code": code})
    assert ok.status_code == 200, ok.text
    body = ok.json()
    # Fields both clients read from TokenOut.
    assert {"access_token", "token_type", "expires_in", "user_id", "role"} <= set(body)
    assert body["token_type"] == "bearer" and body["role"] == "BUYER" and body["expires_in"] > 0
    # The token is a working session.
    me = c.get("/auth/me", headers=auth_header(body["access_token"]))
    assert me.status_code == 200 and me.json()["id"] == body["user_id"]


def test_wrong_code_rejected_then_right_code_still_works(client):
    c, _ = client
    c.post("/auth/otp/request", json={"phone": PHONE})
    code = captured_otp_code(PHONE)
    bad = c.post("/auth/otp/verify", json={"phone": PHONE, "code": _wrong(code)})
    assert bad.status_code == 401 and bad.json() == {"detail": "Invalid or expired code"}
    assert c.post("/auth/otp/verify", json={"phone": PHONE, "code": code}).status_code == 200


def test_expired_code_rejected(client):
    c, _ = client
    c.post("/auth/otp/request", json={"phone": PHONE})
    code = captured_otp_code(PHONE)
    assert config_module.settings.OTP_TTL_SECONDS == 300
    otp_service._codes[PHONE].expires_at = 0.0  # white-box: past its expiry
    r = c.post("/auth/otp/verify", json={"phone": PHONE, "code": code})
    assert r.status_code == 401


def test_reused_code_rejected(client):
    c, _ = client
    c.post("/auth/otp/request", json={"phone": PHONE})
    code = captured_otp_code(PHONE)
    assert c.post("/auth/otp/verify", json={"phone": PHONE, "code": code}).status_code == 200
    assert c.post("/auth/otp/verify", json={"phone": PHONE, "code": code}).status_code == 401


def test_malformed_input_rejected(client):
    c, _ = client
    assert c.post("/auth/otp/request", json={"phone": "9876512340"}).status_code == 422
    assert c.post("/auth/otp/verify", json={"phone": PHONE, "code": "12345"}).status_code == 422


# --- development display ------------------------------------------------------------

def test_development_display_returns_code_and_backend_verifies(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(config_module.settings, "OTP_DISPLAY_MODE", True)  # suite env is local
    r = c.post("/auth/otp/request", json={"phone": PHONE})
    assert r.status_code == 200 and set(r.json()) == {"message", "debug_code"}
    code = r.json()["debug_code"]
    assert re.fullmatch(r"\d{6}", code)
    assert otp_service.delivery.codes == {}  # display replaces delivery: nothing sent
    assert c.post("/auth/otp/verify", json={"phone": PHONE, "code": _wrong(code)}).status_code == 401
    assert c.post("/auth/otp/verify", json={"phone": PHONE, "code": code}).status_code == 200
    assert c.post("/auth/otp/verify", json={"phone": PHONE, "code": code}).status_code == 401


# --- production ------------------------------------------------------------------

def test_production_response_never_contains_the_code(client, monkeypatch):
    """Even with the display flag forced on (bypassing the startup refusal)
    and a delivery attached, production returns only the message."""
    c, _ = client
    _production(monkeypatch, OTP_DISPLAY_MODE=True)
    r = c.post("/auth/otp/request", json={"phone": PHONE})
    assert r.status_code == 200 and r.json() == {"message": MESSAGE}
    code = captured_otp_code(PHONE)
    assert code not in r.text
    assert c.post("/auth/otp/verify", json={"phone": PHONE, "code": code}).status_code == 200


def test_production_refuses_to_start_with_display_mode(monkeypatch):
    _production(monkeypatch, OTP_DISPLAY_MODE=True)
    with pytest.raises(RuntimeError, match="RESTAMP_OTP_DISPLAY_MODE"):
        config_module.validate_security_config()


def test_production_without_delivery_fails_clearly(client, monkeypatch):
    c, _ = client
    _production(monkeypatch)
    monkeypatch.setattr(otp_service, "delivery", None)
    r = c.post("/auth/otp/request", json={"phone": PHONE})
    assert r.status_code == 503 and r.json() == {"detail": NOT_CONNECTED_503}
    assert otp_service._codes == {}  # nothing generated, so nothing can verify


def test_production_startup_warns_when_delivery_missing(monkeypatch, caplog):
    _production(monkeypatch)
    monkeypatch.setattr(otp_service, "delivery", None)
    with caplog.at_level(logging.WARNING, logger="restamp"):
        with TestClient(create_app()):
            pass
    assert "OTP SMS delivery is not configured" in caplog.text


def test_development_startup_does_not_warn(monkeypatch, caplog):
    monkeypatch.setattr(otp_service, "delivery", None)
    with caplog.at_level(logging.WARNING, logger="restamp"):
        with TestClient(create_app()):
            pass
    assert "OTP SMS delivery is not configured" not in caplog.text


# --- clean-up locks -------------------------------------------------------------

@pytest.mark.parametrize("name", ["OTP_TEST_DISPLAY", "OTP_TEST_PHONES", "OTP_TEST_ALLOWED_CIDRS",
                                  "OTP_DEMO_MODE", "OTP_DEMO_PHONES", "OTP_DEBUG", "OTP_DISPLAY_IN_PRODUCTION",
                                  "SMS_PROVIDER", "SMS8_API_KEY", "SMS_MSG91_AUTH_KEY"])
def test_removed_settings_do_not_exist(name):
    assert not hasattr(config_module.settings, name)


def test_no_provider_or_tester_code_remains():
    app_dir = pathlib.Path(config_module.__file__).parent
    pattern = re.compile(r"sms8|msg91|OTP_TEST_|otp_test_display|SMS_PROVIDER", re.IGNORECASE)
    hits = [p.name for p in app_dir.rglob("*.py") if pattern.search(p.read_text())]
    assert hits == []
