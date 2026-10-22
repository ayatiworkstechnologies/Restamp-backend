"""Auth production-readiness: configurable SMS delivery + Google prod guards.

Service-level tests use isolated OTPService instances (no DB). Endpoint
tests use the rollback-isolated `client` fixture on local restamp_dev only.
Provider failures are simulated with mocks; no real SMS or Google network
calls exist anywhere in this file.
"""
import pytest
from fastapi import HTTPException

from app import config as config_module
from app.config import validate_security_config
from app.services import google as google_svc
from app.services import otp as otp_module
from app.services.otp import OTPService


class RecordingProvider(otp_module.OTPProvider):
    """Zero-arg-constructible mock vendor (the shape get_otp_provider needs)."""

    def __init__(self, calls=None, fail_with=None):
        self.calls = calls if calls is not None else []
        self.fail_with = fail_with

    def send(self, phone, code):
        self.calls.append((phone, code))
        if self.fail_with is not None:
            raise self.fail_with


def _prod(monkeypatch, **overrides):
    """Pin production env with a strong JWT and quiet dev flags; overrides win."""
    monkeypatch.setattr(config_module.settings, "ENV", "production")
    monkeypatch.setattr(config_module.settings, "JWT_SECRET", "z" * 40)
    monkeypatch.setattr(config_module.settings, "OTP_DEBUG", False)
    monkeypatch.setattr(config_module.settings, "OTP_DEMO_MODE", False)
    monkeypatch.setattr(config_module.settings, "GOOGLE_DEV_MODE", False)
    monkeypatch.setattr(config_module.settings, "GOOGLE_ENABLED", False)
    monkeypatch.setattr(config_module.settings, "GOOGLE_CLIENT_ID", "")
    monkeypatch.setattr(config_module.settings, "CORS_ORIGINS", "")
    monkeypatch.setattr(config_module.settings, "SMS_PROVIDER", "")
    for key, value in overrides.items():
        monkeypatch.setattr(config_module.settings, key, value)


# --- SMS: mocked provider success / failure / rollback -----------------------

def test_provider_success_delivers_and_keeps_state():
    calls = []
    svc = OTPService(provider=RecordingProvider(calls))
    code = svc.request_code("+15900001001")
    assert code is not None  # OTP_DEBUG=1 in test env
    assert calls == [("+15900001001", code)]
    assert svc.verify_code("+15900001001", code) is True


def test_provider_failure_is_503_generic_with_rollback():
    calls = []
    svc = OTPService(provider=RecordingProvider(calls, fail_with=RuntimeError("boom")))
    with pytest.raises(HTTPException) as info:
        svc.request_code("+15900001002")
    assert info.value.status_code == 503
    assert info.value.detail == "Failed to send code. Try again later."
    # Failed delivery leaves no live code and burns no rate-limit budget:
    assert "+15900001002" not in svc._codes
    assert "+15900001002" not in svc._requests
    # Immediate retry with a working provider succeeds (no false 429).
    svc.provider = RecordingProvider(calls)
    assert svc.request_code("+15900001002") is not None


def test_provider_http_exception_passes_through_after_rollback():
    calls = []
    svc = OTPService(provider=RecordingProvider(
        calls, fail_with=HTTPException(status_code=429, detail="Slow down")))
    with pytest.raises(HTTPException) as info:
        svc.request_code("+15900001003")
    assert info.value.status_code == 429
    assert "+15900001003" not in svc._codes


def test_provider_failure_never_leaks_secrets():
    calls = []
    svc = OTPService(provider=RecordingProvider(
        calls, fail_with=RuntimeError("VENDOR-SECRET-abc code=123456")))
    with pytest.raises(HTTPException) as info:
        svc.request_code("+15900001004")
    body = f"{info.value.status_code} {info.value.detail}"
    assert "VENDOR-SECRET-abc" not in body
    assert "123456" not in body
    assert "+15900001004" not in info.value.detail


def test_endpoint_503_on_delivery_failure_no_leak(client, monkeypatch):
    from app.services.otp import otp_service
    monkeypatch.setattr(
        otp_service, "provider",
        RecordingProvider([], fail_with=RuntimeError("VENDOR-SECRET-xyz")),
    )
    c, _ = client
    r = c.post("/auth/otp/request", json={"phone": "+15900001005"})
    assert r.status_code == 503, r.text
    assert r.json() == {"detail": "Failed to send code. Try again later."}
    assert "VENDOR-SECRET-xyz" not in r.text


# --- SMS: production configuration -------------------------------------------

def test_prod_requires_configured_sms_provider(monkeypatch):
    _prod(monkeypatch)
    with pytest.raises(RuntimeError, match="RESTAMP_SMS_PROVIDER"):
        validate_security_config()
    with pytest.raises(RuntimeError, match="RESTAMP_SMS_PROVIDER"):
        otp_module.get_otp_provider()


def test_prod_unknown_provider_name_rejected(monkeypatch):
    _prod(monkeypatch, SMS_PROVIDER="acme-telco")
    with pytest.raises(RuntimeError, match="RESTAMP_SMS_PROVIDER"):
        validate_security_config()


def test_prod_dev_provider_rejected_at_request_time(monkeypatch):
    _prod(monkeypatch)
    svc = OTPService()  # default DevOTPProvider
    with pytest.raises(HTTPException) as info:
        svc.request_code("+15900001006")
    assert info.value.status_code == 503
    assert "+15900001006" not in svc._codes  # nothing stored


def test_registered_provider_satisfies_prod_validation(monkeypatch):
    _prod(monkeypatch, SMS_PROVIDER="fake-vendor")
    monkeypatch.setitem(
        otp_module.PRODUCTION_SMS_PROVIDERS, "fake-vendor", RecordingProvider)
    assert otp_module.is_sms_configured() is True
    validate_security_config()  # no SMS error (Google disabled by default)
    provider = otp_module.get_otp_provider()
    assert isinstance(provider, RecordingProvider)


def test_lifespan_fails_without_sms_in_prod(monkeypatch):
    import asyncio
    import app.main as main_module
    from fastapi import FastAPI
    _prod(monkeypatch)
    monkeypatch.setattr(main_module, "check_db", lambda: True)
    with pytest.raises(RuntimeError, match="SMS_PROVIDER"):
        asyncio.run(_drain(main_module.lifespan(FastAPI())))


def test_lifespan_installs_configured_provider_for_requests(monkeypatch):
    """Production startup must install the validated provider in the singleton.

    Constructing a provider and discarding it leaves the dev provider behind,
    so the request path fails closed even though startup succeeded.
    """
    from fastapi.testclient import TestClient
    import app.main as main_module
    from app.services.otp import DevOTPProvider, otp_service

    calls = []

    class StartupProvider(otp_module.OTPProvider):
        def send(self, phone, code):
            calls.append((phone, code))

    phone = "+15900001007"
    _prod(monkeypatch, SMS_PROVIDER="startup-vendor")
    monkeypatch.setitem(
        otp_module.PRODUCTION_SMS_PROVIDERS, "startup-vendor", StartupProvider)
    monkeypatch.setattr(main_module, "check_db", lambda: True)
    otp_service.reset()
    try:
        with TestClient(main_module.create_app()) as c:
            assert isinstance(otp_service.provider, StartupProvider)
            r = c.post("/auth/otp/request", json={"phone": phone})
            assert r.status_code == 200, r.text
            assert "debug_code" not in r.json()
            assert len(calls) == 1 and calls[0][0] == phone
    finally:
        otp_service.provider = DevOTPProvider()
        otp_service.reset()


async def _drain(cm):
    async with cm:
        pass


# --- Google: production guards ------------------------------------------------

def test_prod_google_enabled_requires_client_id(monkeypatch):
    _prod(monkeypatch, GOOGLE_ENABLED=True, SMS_PROVIDER="fake-vendor")
    monkeypatch.setitem(
        otp_module.PRODUCTION_SMS_PROVIDERS, "fake-vendor", RecordingProvider)
    with pytest.raises(RuntimeError, match="RESTAMP_GOOGLE_CLIENT_ID"):
        validate_security_config()


def test_prod_google_enabled_with_client_id_passes(monkeypatch):
    _prod(monkeypatch, GOOGLE_ENABLED=True, GOOGLE_CLIENT_ID="prod-client-id")
    monkeypatch.setitem(
        otp_module.PRODUCTION_SMS_PROVIDERS, "fake-vendor", RecordingProvider)
    monkeypatch.setattr(config_module.settings, "SMS_PROVIDER", "fake-vendor")
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
