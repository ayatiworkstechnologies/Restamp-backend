"""Auth production-readiness: OTP delivery seam + Google prod guards.

Service-level tests use isolated OTPService instances (no DB). Endpoint
tests use the rollback-isolated `client` fixture on local restamp_dev only.
Delivery failures are simulated with a test-only delivery hook; no real SMS
or Google network calls exist anywhere in this file.
"""
import pytest
from fastapi import HTTPException

from app import config as config_module
from app.config import validate_security_config
from app.services import google as google_svc
from app.services.otp import OTPService


class RecordingDelivery:
    """Test-only delivery hook: it records every delivery and can raise on
    cue. Installed as OTPService.delivery — the seam production code leaves
    deliberately unconnected (see app/services/otp.py)."""

    def __init__(self, calls=None, fail_with=None):
        self.calls = calls if calls is not None else []
        self.fail_with = fail_with

    def __call__(self, phone, code):
        self.calls.append((phone, code))
        if self.fail_with is not None:
            raise self.fail_with


def _prod(monkeypatch, **overrides):
    """Pin production env with a strong JWT and quiet dev flags; overrides win."""
    monkeypatch.setattr(config_module.settings, "ENV", "production")
    monkeypatch.setattr(config_module.settings, "JWT_SECRET", "z" * 40)
    monkeypatch.setattr(config_module.settings, "GOOGLE_DEV_MODE", False)
    monkeypatch.setattr(config_module.settings, "GOOGLE_ENABLED", False)
    monkeypatch.setattr(config_module.settings, "GOOGLE_CLIENT_ID", "")
    monkeypatch.setattr(config_module.settings, "CORS_ORIGINS", "")
    for key, value in overrides.items():
        monkeypatch.setattr(config_module.settings, key, value)


# --- OTP delivery: mocked success / failure / rollback ------------------------

def test_delivery_success_delivers_and_keeps_state():
    calls = []
    svc = OTPService()
    svc.delivery = RecordingDelivery(calls)
    assert svc.request_code("+916900001001") is None  # codes are never returned
    assert len(calls) == 1
    phone_sent, code = calls[0]  # the code exists only inside the delivery
    assert phone_sent == "+916900001001"
    assert svc.verify_code("+916900001001", code) is True


def test_delivery_failure_is_503_generic_with_rollback():
    calls = []
    svc = OTPService()
    svc.delivery = RecordingDelivery(calls, fail_with=RuntimeError("boom"))
    with pytest.raises(HTTPException) as info:
        svc.request_code("+916900001002")
    assert info.value.status_code == 503
    assert info.value.detail == "Failed to send code. Try again later."
    # Failed delivery leaves no live code and burns no rate-limit budget:
    assert "+916900001002" not in svc._codes
    assert "+916900001002" not in svc._requests
    # Immediate retry with a working delivery succeeds (no false 429).
    svc.delivery = RecordingDelivery(calls)
    assert svc.request_code("+916900001002") is None  # codes are never returned
    assert calls[-1][0] == "+916900001002"  # delivery happened on the retry


def test_delivery_http_exception_passes_through_after_rollback():
    calls = []
    svc = OTPService()
    svc.delivery = RecordingDelivery(
        calls, fail_with=HTTPException(status_code=429, detail="Slow down"))
    with pytest.raises(HTTPException) as info:
        svc.request_code("+916900001003")
    assert info.value.status_code == 429
    assert "+916900001003" not in svc._codes


def test_delivery_failure_never_leaks_secrets():
    calls = []
    svc = OTPService()
    svc.delivery = RecordingDelivery(
        calls, fail_with=RuntimeError("VENDOR-SECRET-abc code=123456"))
    with pytest.raises(HTTPException) as info:
        svc.request_code("+916900001004")
    body = f"{info.value.status_code} {info.value.detail}"
    assert "VENDOR-SECRET-abc" not in body
    assert "123456" not in body
    assert "+916900001004" not in info.value.detail


def test_endpoint_503_on_delivery_failure_no_leak(client, monkeypatch):
    from app.services.otp import otp_service
    monkeypatch.setattr(
        otp_service, "delivery",
        RecordingDelivery([], fail_with=RuntimeError("VENDOR-SECRET-xyz")),
    )
    c, _ = client
    r = c.post("/auth/otp/request", json={"phone": "+916900001005"})
    assert r.status_code == 503, r.text
    assert r.json() == {"detail": "Failed to send code. Try again later."}
    assert "VENDOR-SECRET-xyz" not in r.text


# --- Google: production guards ------------------------------------------------

def test_prod_google_enabled_requires_client_id(monkeypatch):
    _prod(monkeypatch, GOOGLE_ENABLED=True)
    with pytest.raises(RuntimeError, match="RESTAMP_GOOGLE_CLIENT_ID"):
        validate_security_config()


def test_prod_google_enabled_with_client_id_passes(monkeypatch):
    _prod(monkeypatch, GOOGLE_ENABLED=True, GOOGLE_CLIENT_ID="prod-client-id")
    validate_security_config()


def test_prod_never_returns_dev_verifier(monkeypatch):
    _prod(monkeypatch, GOOGLE_DEV_MODE=True)  # even if the flag is forced on...
    assert isinstance(google_svc.get_verifier(), google_svc.DisabledGoogleVerifier)
    with pytest.raises(HTTPException) as info:
        google_svc.get_verifier().verify("dev:anyone")
    assert info.value.status_code == 501  # ...synthetic identities still refused


def test_prod_disabled_google_rejects_real_looking_tokens(monkeypatch):
    _prod(monkeypatch)
    with pytest.raises(HTTPException) as info:
        google_svc.get_verifier().verify("header.payload.sig")
    assert info.value.status_code == 501


def test_prod_client_id_selects_production_verifier(monkeypatch):
    _prod(monkeypatch, GOOGLE_CLIENT_ID="prod-client-id")
    assert isinstance(google_svc.get_verifier(), google_svc.ProductionGoogleVerifier)
    # A synthetic dev token is not valid input to the crypto verifier.
    with pytest.raises(HTTPException) as info:
        google_svc.get_verifier().verify("dev:anyone")
    assert info.value.status_code == 401
