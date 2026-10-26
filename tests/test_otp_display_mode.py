"""Temporary internal-testing OTP display mode + Indian-mobile-only input.

Production contract (fail closed, tested below):
- the display mode is impossible in production: the startup gate refuses
  RESTAMP_OTP_DISPLAY_MODE there, and the service re-checks is_production()
  at request time, so no code can ever leave via a response (or a log);
- with display inactive and delivery unconnected, requests still fail
  closed with the generic 503 (nothing generated, nothing disclosed).

Internal-testing contract (local / non-production-classified only):
- when RESTAMP_OTP_DISPLAY_MODE is explicitly "1", the backend-selected
  code is returned by `request_code` and included in the API response as
  "debug_code" — the ONLY leak channel by design; no SMS is sent (the
  delivery seam is bypassed), no code is logged;
- every lifecycle rule is unchanged in display mode: rate limits,
  wrong-code attempts, expiry, single-use verification.

Input contract: both OTP endpoints accept Indian mobiles only
(+91 followed by exactly 10 digits starting 6-9); other numbers 422.

No database writes (the endpoint tests use the rollback-isolated client
fixture), no network, no SMS vendor anywhere.
"""
import logging
import re

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app import config as config_module
from app.config import is_production, validate_security_config
from app.schemas import INDIAN_MOBILE_PATTERN, OTPRequest, OTPVerify
from app.services.otp import OTPService
from tests.conftest import TestSMSInbox

PHONE = "+919876543210"
REQUEST_MESSAGE = "If the number is valid, an OTP was sent."
NOT_CONNECTED_503 = "OTP delivery is not configured. Try again later."


def _enable_display(monkeypatch) -> None:
    monkeypatch.setattr(config_module.settings, "OTP_DISPLAY_MODE", True)


# --- Indian mobiles only (schema gate) -----------------------------------------

def test_otp_schemas_accept_indian_mobile_only():
    assert re.fullmatch(INDIAN_MOBILE_PATTERN, "+919876543210")
    assert OTPRequest(phone=PHONE).phone == PHONE
    assert OTPVerify(phone=PHONE, code="123456").code == "123456"
    for bad in (
        "+15000000001",      # non-Indian E.164
        "+915000000001",     # Indian number must start 6-9
        "+91876543210",      # only 9 digits after +91
        "+9198765432101",    # 11 digits after +91
        "9876543210",        # missing +91 prefix
        "+910987654321",     # 10 digits but starts 0
        "+91 98765 43210",   # spacing is not accepted by the strict gate
    ):
        with pytest.raises(ValidationError):
            OTPRequest(phone=bad)
    with pytest.raises(ValidationError):
        OTPVerify(phone="+15000000001", code="123456")


# --- display mode: service level ------------------------------------------------

def test_display_mode_off_by_default_fails_closed_without_delivery():
    assert config_module.settings.OTP_DISPLAY_MODE is False  # suite default posture
    svc = OTPService()
    with pytest.raises(HTTPException) as info:
        svc.request_code(PHONE)
    assert info.value.status_code == 503
    assert info.value.detail == NOT_CONNECTED_503


def test_display_mode_returns_backend_selected_code_without_delivery(monkeypatch):
    _enable_display(monkeypatch)  # suite env is development: never production
    svc = OTPService()
    code = svc.request_code(PHONE, ip="198.51.100.7")
    assert code and re.fullmatch(r"\d{6}", code)  # `secrets`-selected, 6 digits
    assert svc.verify_code(PHONE, code) is True    # single use
    assert svc.verify_code(PHONE, code) is False
    assert svc.verify_code(PHONE, "000000") is False  # wrong code rejected


def test_display_mode_does_not_touch_the_delivery_seam(monkeypatch):
    """Display substitutes delivery: no SMS path is invoked (and no
    code can appear anywhere beyond the response)."""
    _enable_display(monkeypatch)
    svc = OTPService()
    inbox = TestSMSInbox()
    svc.delivery = inbox
    code = svc.request_code(PHONE)
    assert code
    assert inbox.codes == {}  # nothing was "sent" in display mode


def test_display_mode_rate_limits_unchanged(monkeypatch):
    _enable_display(monkeypatch)
    svc = OTPService()
    for _ in range(5):
        assert svc.request_code(PHONE)
    with pytest.raises(HTTPException) as info:
        svc.request_code(PHONE)
    assert info.value.status_code == 429


def test_display_mode_wrong_attempts_still_bounded(monkeypatch):
    _enable_display(monkeypatch)
    svc = OTPService()
    code = svc.request_code(PHONE)
    for _ in range(4):
        assert svc.verify_code(PHONE, "000000") is False
    with pytest.raises(HTTPException) as info:  # 5th wrong attempt trips the cap
        svc.verify_code(PHONE, "000000")
    assert info.value.status_code == 429
    with pytest.raises(HTTPException):  # blocked phone refuses even the right code
        svc.verify_code(PHONE, code)


def test_display_mode_expired_code_never_verifies(monkeypatch):
    _enable_display(monkeypatch)
    svc = OTPService()
    code = svc.request_code(PHONE)
    svc._codes[PHONE].expires_at = 0.0  # white-box expiry (local test only)
    assert svc.verify_code(PHONE, code) is False


def test_display_mode_never_logs_the_code(monkeypatch, caplog):
    _enable_display(monkeypatch)
    svc = OTPService()
    code = svc.request_code(PHONE)
    with caplog.at_level(logging.DEBUG):
        svc.verify_code(PHONE, code)
    joined = "\n".join(rec.getMessage() for rec in caplog.records)
    assert code not in joined   # display lives ONLY in the response contract
    assert PHONE not in joined  # the phone never leaks into logs either


# --- display mode: endpoint level (rollback-isolated client) --------------------

def test_display_endpoint_returns_debug_code_and_verifies_once(client, monkeypatch):
    from app.services.otp import otp_service

    _enable_display(monkeypatch)
    c, _ = client
    phone = "+919876543211"
    r = c.post("/auth/otp/request", json={"phone": phone})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"message", "debug_code"}  # the ONLY disclosure channel
    assert body["message"] == REQUEST_MESSAGE
    code = body["debug_code"]
    assert re.fullmatch(r"\d{6}", code)
    assert otp_service.delivery.codes == {}  # fixture inbox untouched: no SMS path
    ok = c.post("/auth/otp/verify", json={"phone": phone, "code": code})
    assert ok.status_code == 200, ok.text
    assert "access_token" in ok.json()
    assert c.post(
        "/auth/otp/verify", json={"phone": phone, "code": code}
    ).status_code == 401  # consumed once


def test_display_endpoint_accepts_indian_numbers_only(client, monkeypatch):
    _enable_display(monkeypatch)
    c, _ = client
    r = c.post("/auth/otp/request", json={"phone": "+15000000001"})
    assert r.status_code == 422  # non-Indian: rejected before anything else


def test_display_endpoint_503_fails_closed_without_display_or_delivery(
    client, monkeypatch
):
    """Display off (default) + delivery unconnected = the production posture:
    generic 503, constant contract, no code anywhere in the exchange."""
    monkeypatch.setattr(config_module.settings, "OTP_DISPLAY_MODE", False)
    from app.services.otp import otp_service

    monkeypatch.setattr(otp_service, "delivery", None)
    c, _ = client
    r = c.post("/auth/otp/request", json={"phone": PHONE})
    assert r.status_code == 503, r.text
    assert r.json() == {"detail": NOT_CONNECTED_503}
    assert "debug_code" not in r.text


# --- production non-disclosure (the core fail-closed guarantee) -----------------

def test_production_gate_refuses_display_mode(monkeypatch):
    s = config_module.settings
    monkeypatch.setattr(s, "ENV", "production")
    monkeypatch.setattr(s, "JWT_SECRET", "z" * 40)
    monkeypatch.setattr(s, "OTP_DISPLAY_MODE", True)
    with pytest.raises(RuntimeError, match="OTP_DISPLAY_MODE"):
        validate_security_config()


def test_production_service_swallows_display_even_if_flag_left_on(monkeypatch):
    """The second, independent barrier: even with the flag left enabled, a
    production-classified process must never return a code — it falls back
    to the delivery seam exactly as if display were off."""
    s = config_module.settings
    monkeypatch.setattr(s, "ENV", "production")
    monkeypatch.setattr(s, "DATABASE_URL", "mysql+pymysql://u:p@db.example.com/db")
    monkeypatch.setattr(s, "OTP_DISPLAY_MODE", True)
    assert is_production() is True
    svc = OTPService()
    inbox = TestSMSInbox()
    svc.delivery = inbox
    assert svc.request_code(PHONE) is None           # no code in the return...
    assert svc.verify_code(PHONE, inbox.codes[PHONE]) is True  # ...delivery works
