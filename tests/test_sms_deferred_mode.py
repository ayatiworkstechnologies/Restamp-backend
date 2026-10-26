"""Explicit SMS-deferred mode (RESTAMP_SMS_MODE=disabled).

Production must START without SMS while the MSG91 business prerequisites are
pending: OTP requests fail closed with the generic unconfigured-503 BEFORE any
code exists (no state created, no retry budget burned), verification keeps
every rule with no bypass, and all other production gates (dev flags, Google,
CORS) stay enforced in every mode.

No database writes (the endpoint test uses the same rollback-isolated TestClient
pattern as test_auth_production.py), no network, no real SMS.
"""
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import config as config_module
from app.config import validate_security_config
from app.services import otp as otp_module
from app.services.otp import (
    DEMO_OTPS,
    DevOTPProvider,
    DisabledOTPProvider,
    Msg91OTPProvider,
    OTPService,
)

AUTH_KEY = "SENTINEL-AUTHKEY-9f3a"
TEMPLATE_ID = "SENTINEL-TEMPLATE-77"
PHONE = "+919876543210"
GENERIC_REQUEST_503 = "OTP delivery is not configured. Try again later."


def _prod(monkeypatch, **overrides):
    """Production env with a strong JWT and all dev flags off; overrides win."""
    s = config_module.settings
    monkeypatch.setattr(s, "ENV", "production")
    monkeypatch.setattr(s, "JWT_SECRET", "z" * 40)
    monkeypatch.setattr(s, "OTP_DEBUG", False)
    monkeypatch.setattr(s, "OTP_DEMO_MODE", False)
    monkeypatch.setattr(s, "GOOGLE_DEV_MODE", False)
    monkeypatch.setattr(s, "GOOGLE_ENABLED", False)
    monkeypatch.setattr(s, "GOOGLE_CLIENT_ID", "")
    monkeypatch.setattr(s, "CORS_ORIGINS", "")
    monkeypatch.setattr(s, "SMS_MODE", "provider")
    monkeypatch.setattr(s, "SMS_PROVIDER", "")
    for key, value in overrides.items():
        monkeypatch.setattr(s, key, value)


class RecordingProvider(otp_module.OTPProvider):
    """Zero-arg-constructible mock vendor (the shape get_otp_provider needs)."""

    def __init__(self, calls=None):
        self.calls = calls if calls is not None else []

    def send(self, phone, code):
        self.calls.append((phone, code))


# --- deferred startup ----------------------------------------------------------

def test_prod_startup_passes_in_deferred_mode_without_provider(monkeypatch, caplog):
    """The core fix: production starts with RESTAMP_SMS_MODE=disabled and NO
    provider configured — the media-style warn-not-fail trade-off, loudly."""
    _prod(monkeypatch, SMS_MODE="disabled")
    validate_security_config()  # must not raise
    texts = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "RESTAMP_SMS_MODE=disabled" in texts
    assert "phone-OTP login is unavailable" in texts


def test_registry_stays_pure_under_deferred_mode(monkeypatch):
    """Deferred mode must not register or select anything new: only msg91 is a
    selectable provider, and DisabledOTPProvider is never in the registry."""
    _prod(monkeypatch, SMS_MODE="disabled")
    assert set(otp_module.PRODUCTION_SMS_PROVIDERS) == {"msg91"}
    assert DisabledOTPProvider not in otp_module.PRODUCTION_SMS_PROVIDERS.values()
    assert not otp_module.is_sms_configured()  # "" names no provider
    assert otp_module.is_sms_disabled() is True


def test_prod_unknown_sms_mode_fails_gate_and_provider_resolution(monkeypatch):
    """A typo'd mode must fail LOUDLY at startup, never silently disable/enable
    SMS checks; get_otp_provider() refuses it too (defense in depth)."""
    for bad in ("disable", "providers", "off", "disabledX"):
        _prod(monkeypatch, SMS_MODE=bad)
        with pytest.raises(RuntimeError, match="RESTAMP_SMS_MODE"):
            validate_security_config()
        with pytest.raises(RuntimeError, match="RESTAMP_SMS_MODE"):
            otp_module.get_otp_provider()
    assert otp_module.sms_mode_error() is not None  # for the unprefixed gate


# --- deferred request path: fail closed, nothing generated ---------------------

def test_request_fails_closed_before_generation(monkeypatch):
    """In the deferred mode the request dies at the provider guard: no OTP
    entry, no rate windows touched, no retry budget burned (look-before the
    window append and code generation)."""
    _prod(monkeypatch, SMS_MODE="disabled")
    svc = OTPService(provider=DisabledOTPProvider())
    with pytest.raises(HTTPException) as info:
        svc.request_code(PHONE, ip="203.0.113.9")
    assert info.value.status_code == 503
    assert info.value.detail == GENERIC_REQUEST_503
    # Nothing was generated or recorded anywhere:
    assert svc._codes == {}
    assert svc._requests == {}
    assert svc._ip_requests == {}
    assert svc._blocked_until == {}
    assert svc._ip_blocked_until == {}
    assert svc._ip_verify_failures == {}


def test_request_failure_is_generic_and_discloses_nothing(monkeypatch, caplog):
    _prod(monkeypatch, SMS_MODE="disabled")
    svc = OTPService(provider=DisabledOTPProvider())
    with caplog.at_level("DEBUG"):
        with pytest.raises(HTTPException) as info:
            svc.request_code(PHONE, ip="203.0.113.9")
    body = f"{info.value.status_code} {info.value.detail}"
    assert PHONE not in body
    joined = "\n".join(rec.getMessage() for rec in caplog.records)
    assert PHONE not in joined
    assert "msg91" not in info.value.detail


def test_delayed_reactivation_on_same_service_still_delivers(monkeypatch):
    """After a deferred-mode failure leaves cleanly no state, flipping the mode
    to a recording provider must deliver normally (same service instance)."""
    _prod(monkeypatch, SMS_MODE="disabled")
    svc = OTPService(provider=DisabledOTPProvider())
    with pytest.raises(HTTPException):
        svc.request_code(PHONE, ip="203.0.113.9")
    _prod(monkeypatch, SMS_MODE="provider", SMS_PROVIDER="startup-vendor")
    monkeypatch.setitem(
        otp_module.PRODUCTION_SMS_PROVIDERS, "startup-vendor", RecordingProvider)
    calls = []
    svc.provider = RecordingProvider(calls)
    assert svc.request_code(PHONE, ip="203.0.113.9") is None  # OTP_DEBUG off
    assert len(calls) == 1 and calls[0][0] == PHONE
    assert PHONE in svc._requests


# --- deferred mode cannot weaken verification or other gates -------------------

def test_verify_has_no_bypass_in_deferred_mode(monkeypatch):
    """No code can exist in the deferred mode, so every predictable/demo code
    must be rejected with False — and no state is poisoned by the attempts."""
    _prod(monkeypatch, SMS_MODE="disabled")
    svc = OTPService(provider=DisabledOTPProvider())
    for code in ("000000", "123456", "111111", *sorted(DEMO_OTPS)):
        assert svc.verify_code(PHONE, code) is False
    assert svc._codes == {}
    assert svc._blocked_until == {}
    assert svc._ip_blocked_until == {}


def test_deferred_mode_cannot_weaken_dev_flag_gates(monkeypatch):
    """Startup dev-flag prohibitions run in EVERY mode."""
    for flag in ("OTP_DEBUG", "OTP_DEMO_MODE"):
        _prod(monkeypatch, SMS_MODE="disabled", **{flag: True})
        with pytest.raises(RuntimeError, match=f"RESTAMP_{flag}"):
            validate_security_config()
    _prod(monkeypatch, SMS_MODE="disabled", GOOGLE_DEV_MODE=True)
    with pytest.raises(RuntimeError, match="GOOGLE"):
        validate_security_config()


@pytest.mark.parametrize(
    "name",
    ["demo", "mock", "console", "logging", "dev", "fake-vendor", "", "CONSOLE"],
)
def test_provider_mode_rejects_non_registered_names(monkeypatch, name):
    """In the ordinary provider mode the registry remains the only gate:
    dev/mock/console/vendor names are not real providers."""
    _prod(monkeypatch, SMS_MODE="provider", SMS_PROVIDER=name)
    with pytest.raises(RuntimeError, match="RESTAMP_SMS_PROVIDER"):
        validate_security_config()


@pytest.mark.parametrize(
    "provider_cls, mode",
    [
        (DevOTPProvider, "provider"),   # dev delivery refused (existing rule)
        (DisabledOTPProvider, "disabled"),  # deferred delivery refused
    ],
)
def test_prod_refuses_unsafe_providers_at_request_time(monkeypatch, provider_cls, mode):
    _prod(monkeypatch, SMS_MODE=mode)
    svc = OTPService(provider=provider_cls())
    with pytest.raises(HTTPException) as info:
        svc.request_code(PHONE)
    assert info.value.status_code == 503
    assert info.value.detail == GENERIC_REQUEST_503
    assert svc._codes == {}


# --- re-activation and unaffected local dev ------------------------------------

def test_msg91_reactivation_still_selects_msg91_provider(monkeypatch):
    """Flipping the mode back with full MSG91 configuration activates the
    deferred provider with no code change."""
    _prod(
        monkeypatch,
        SMS_MODE="provider",
        SMS_PROVIDER="msg91",
        SMS_MSG91_AUTH_KEY=AUTH_KEY,
        SMS_MSG91_TEMPLATE_ID=TEMPLATE_ID,
    )
    assert otp_module.is_sms_configured() is True
    assert otp_module.sms_mode_error() is None
    validate_security_config()
    provider = otp_module.get_otp_provider()
    assert isinstance(provider, Msg91OTPProvider)


def test_deferred_mode_ignores_incomplete_msg91_config(monkeypatch):
    """Deferred mode suppresses only the provider requirement — an incomplete
    msg91 config neither fails startup nor tries to construct the vendor."""
    _prod(monkeypatch, SMS_MODE="disabled", SMS_PROVIDER="msg91")
    validate_security_config()  # credentials irrelevant here
    provider = otp_module.get_otp_provider()
    assert isinstance(provider, DisabledOTPProvider)


def test_local_dev_still_uses_dev_provider_even_when_disabled(monkeypatch):
    """The disabled mode is a production concern: local development keeps the
    dev provider so tests/local flows never change."""
    s = config_module.settings
    monkeypatch.setattr(s, "ENV", "development")
    monkeypatch.setattr(s, "SMS_MODE", "disabled")
    assert isinstance(otp_module.get_otp_provider(), DevOTPProvider)
    assert otp_module.is_sms_disabled() is True  # raw mode, but prod-only gate


# --- real startup path installs the deferred provider --------------------------

def test_lifespan_installs_deferred_provider_and_endpoints_fail_closed(monkeypatch):
    """Production lifespan (create_app) must install DisabledOTPProvider on the
    singleton; both OTP endpoints then fail closed with generic errors and no
    disclosure. Restore the singleton afterwards so other tests stay isolated."""
    import app.main as main_module
    from app.services.otp import DevOTPProvider, otp_service

    _prod(monkeypatch, SMS_MODE="disabled")
    monkeypatch.setattr(main_module, "check_db", lambda: True)
    otp_service.reset()
    try:
        with TestClient(main_module.create_app()) as c:
            assert isinstance(otp_service.provider, DisabledOTPProvider)
            r = c.post("/auth/otp/request", json={"phone": PHONE})
            assert r.status_code == 503, r.text
            assert r.json() == {"detail": GENERIC_REQUEST_503}
            assert PHONE not in r.text
            r2 = c.post("/auth/otp/verify", json={"phone": PHONE, "code": "123456"})
            assert r2.status_code == 401, r2.text
            assert r2.json() == {"detail": "Invalid or expired code"}
    finally:
        otp_service.provider = DevOTPProvider()
        otp_service.reset()
