"""The OTP delivery seam: intentionally NOT connected in production code.

The OTP service is provider-independent (Python `secrets`-based generation,
strict hash-based verification). This module locks in the NOT-CONNECTED
contract:
- with no delivery attached, OTP requests fail closed with the generic 503
  BEFORE any rate accounting or code generation (no state, no burned
  budget, no disclosure), and verification accepts nothing;
- a delivery that raises is rolled back exactly like a failed vendor send
  would be (generic 503, no live-but-undelivered code);
- once a delivery is attached (tests do — the test-only stand-in emulates
  the phone's SMS), the delivered code is the only acceptable one, and no
  error or log line ever carries the phone, the code, or a sentinel secret.

No database writes (the endpoint test uses the same rollback-isolated
TestClient pattern), no network, and no SMS vendor: nothing here could send
a real message, and none of this code is a delivery implementation.
"""
import logging

import pytest
from fastapi import HTTPException

from app import config as config_module
from app.config import validate_security_config
from app.services.otp import OTPService
from tests.conftest import TestSMSInbox

PHONE = "+919876543210"
NOT_CONNECTED_503 = "OTP delivery is not configured. Try again later."
GENERIC_SEND_503 = "Failed to send code. Try again later."
SENTINEL = "VENDOR-SENTINEL-4c7a"


class RecordingDelivery:
    """Test-only delivery hook: records deliveries; raises on cue."""

    def __init__(self, calls=None, fail_with=None):
        self.calls = calls if calls is not None else []
        self.fail_with = fail_with

    def __call__(self, phone, code):
        self.calls.append((phone, code))
        if self.fail_with is not None:
            raise self.fail_with


# --- not connected: fail closed before anything exists -------------------------

def test_request_fails_closed_before_generation():
    """No delivery connected (the deliberate default): the request dies at
    the delivery guard — no OTP entry, no rate windows touched, no retry
    budget burned, and nothing discloses the phone."""
    svc = OTPService()
    with pytest.raises(HTTPException) as info:
        svc.request_code(PHONE, ip="203.0.113.9")
    assert info.value.status_code == 503
    assert info.value.detail == NOT_CONNECTED_503
    assert PHONE not in info.value.detail
    # Nothing was generated or recorded anywhere:
    assert svc._codes == {}
    assert svc._requests == {}
    assert svc._ip_requests == {}
    assert svc._blocked_until == {}
    assert svc._ip_blocked_until == {}
    assert svc._ip_verify_failures == {}
    # Repeating the request behaves identically (no budget was consumed).
    with pytest.raises(HTTPException) as info2:
        svc.request_code(PHONE, ip="203.0.113.9")
    assert info2.value.detail == NOT_CONNECTED_503
    assert svc._requests == {}


def test_verify_has_no_bypass_without_delivery():
    """No code can exist without delivery, so every predictable code is
    rejected with False and the failures poison no state."""
    svc = OTPService()
    for code in ("000000", "123456", "111111", "297569", "109745", "583214"):
        assert svc.verify_code(PHONE, code) is False
    assert svc._codes == {}
    assert svc._blocked_until == {}
    assert svc._ip_blocked_until == {}


def test_sms_env_vars_are_inert(monkeypatch):
    """There is no SMS configuration anymore: junk values for the removed
    env vars cannot weaken, enable, or misconfigure production startup."""
    s = config_module.settings
    monkeypatch.setattr(s, "ENV", "production")
    monkeypatch.setattr(s, "JWT_SECRET", "z" * 40)
    monkeypatch.setattr(s, "GOOGLE_DEV_MODE", False)
    monkeypatch.setattr(s, "GOOGLE_ENABLED", False)
    monkeypatch.setattr(s, "GOOGLE_CLIENT_ID", "")
    monkeypatch.setattr(s, "CORS_ORIGINS", "")
    validate_security_config()  # must not raise


# --- connected delivery: lifecycle preserved -----------------------------------

def test_successful_delivery_verifies_consumed_once():
    """With a delivery attached, the DELIVERED code verifies exactly once
    and the service never discloses it. (Test-only delivery stand-in:
    emulates the phone's SMS — not a production mechanism.)"""
    svc = OTPService()
    inbox = TestSMSInbox()
    svc.delivery = inbox
    assert svc.request_code(PHONE) is None
    code = inbox.codes[PHONE]
    assert svc.verify_code(PHONE, code) is True
    assert svc.verify_code(PHONE, code) is False  # single use
    assert svc.verify_code(PHONE, "000000") is False  # wrong stays wrong


def test_delivery_failure_is_generic_503_with_rollback(caplog):
    """A raising delivery is rolled back: no live-but-undelivered code, no
    burned budget, and the error/logs carry no phone, code, or sentinel."""
    svc = OTPService()
    svc.delivery = RecordingDelivery(fail_with=RuntimeError(SENTINEL))
    caplog.set_level(logging.DEBUG)
    with pytest.raises(HTTPException) as info:
        svc.request_code(PHONE, ip="203.0.113.9")
    assert info.value.status_code == 503
    assert info.value.detail == GENERIC_SEND_503
    # No state survived the failure, and the budget is not burned:
    assert PHONE not in svc._codes
    assert svc._requests.get(PHONE) in (None, [])
    assert svc._ip_requests.get("203.0.113.9") in (None, [])
    joined = "\n".join(rec.getMessage() for rec in caplog.records)
    for secret in (SENTINEL, PHONE, PHONE.lstrip("+")):
        assert secret not in joined
        assert secret not in info.value.detail


def test_delivery_http_exception_propagates_after_rollback():
    """A delivery raising HTTPException passes through — after the same
    rollback (no code left behind, budget intact)."""
    svc = OTPService()
    svc.delivery = RecordingDelivery(
        fail_with=HTTPException(status_code=429, detail="Slow down"))
    with pytest.raises(HTTPException) as info:
        svc.request_code(PHONE)
    assert info.value.status_code == 429
    assert PHONE not in svc._codes


# --- endpoint-level: the deployed surface discloses nothing --------------------

def test_endpoint_fails_closed_when_delivery_not_connected(client, monkeypatch):
    """With the fixture's test inbox removed (delivery back to None — the
    production default), the HTTP endpoint fails closed with the generic
    error and the message-only contract; no phone or code appears anywhere."""
    from app.services.otp import otp_service

    monkeypatch.setattr(otp_service, "delivery", None)
    c, _ = client
    r = c.post("/auth/otp/request", json={"phone": PHONE})
    assert r.status_code == 503, r.text
    assert r.json() == {"detail": NOT_CONNECTED_503}
    assert PHONE not in r.text
