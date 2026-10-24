"""MSG91 production SMS provider: selection, startup validation, delivery.

No database and no network: the HTTP call is replaced with a fake, so no real
SMS is ever sent. Sentinel secrets are used so leaks are easy to detect.
"""
import logging

import httpx
import pytest
from fastapi import HTTPException

from app import config as config_module
from app.config import validate_security_config
from app.services import otp as otp_module
from app.services.otp import DevOTPProvider, Msg91OTPProvider, OTPService

AUTH_KEY = "SENTINEL-AUTHKEY-9f3a"
TEMPLATE_ID = "SENTINEL-TEMPLATE-77"
PHONE = "+919876543210"


def _prod(monkeypatch, **overrides):
    """Production env with a strong JWT, dev flags off, msg91 fully configured."""
    s = config_module.settings
    monkeypatch.setattr(s, "ENV", "production")
    monkeypatch.setattr(s, "JWT_SECRET", "z" * 40)
    monkeypatch.setattr(s, "OTP_DEBUG", False)
    monkeypatch.setattr(s, "OTP_DEMO_MODE", False)
    monkeypatch.setattr(s, "GOOGLE_DEV_MODE", False)
    monkeypatch.setattr(s, "GOOGLE_ENABLED", False)
    monkeypatch.setattr(s, "GOOGLE_CLIENT_ID", "")
    monkeypatch.setattr(s, "CORS_ORIGINS", "")
    monkeypatch.setattr(s, "SMS_PROVIDER", "msg91")
    monkeypatch.setattr(s, "SMS_MSG91_AUTH_KEY", AUTH_KEY)
    monkeypatch.setattr(s, "SMS_MSG91_TEMPLATE_ID", TEMPLATE_ID)
    for key, value in overrides.items():
        monkeypatch.setattr(s, key, value)


class FakePost:
    """Stand-in for httpx.post that records calls and returns/raises on cue."""

    def __init__(self, status=200, json_body=None, text=None, raises=None):
        self.status = status
        self.json_body = {"type": "success", "message": "req123"} if json_body is None else json_body
        self.text = text
        self.raises = raises
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.raises is not None:
            raise self.raises
        request = httpx.Request("POST", url)
        if self.text is not None:
            return httpx.Response(self.status, text=self.text, request=request)
        return httpx.Response(self.status, json=self.json_body, request=request)


def _service(monkeypatch, fake):
    _prod(monkeypatch)
    monkeypatch.setattr(otp_module.httpx, "post", fake)
    return OTPService(provider=Msg91OTPProvider())


def _assert_no_leak(text, code=None):
    for secret in (AUTH_KEY, TEMPLATE_ID, PHONE, PHONE.lstrip("+")):
        assert secret not in text
    if code:
        assert code not in text


# --- selection & startup validation -----------------------------------------

def test_msg91_is_registered_and_selected_case_insensitively(monkeypatch):
    assert otp_module.PRODUCTION_SMS_PROVIDERS["msg91"] is Msg91OTPProvider
    _prod(monkeypatch, SMS_PROVIDER="  MSG91 ")
    assert otp_module.is_sms_configured() is True
    validate_security_config()  # must not raise
    assert isinstance(otp_module.get_otp_provider(), Msg91OTPProvider)


def test_unknown_provider_still_rejected(monkeypatch):
    _prod(monkeypatch, SMS_PROVIDER="not-a-vendor")
    with pytest.raises(RuntimeError, match="RESTAMP_SMS_PROVIDER"):
        validate_security_config()


def test_empty_provider_still_rejected_in_production(monkeypatch):
    _prod(monkeypatch, SMS_PROVIDER="")
    with pytest.raises(RuntimeError, match="RESTAMP_SMS_PROVIDER"):
        validate_security_config()
    with pytest.raises(RuntimeError, match="RESTAMP_SMS_PROVIDER"):
        otp_module.get_otp_provider()


@pytest.mark.parametrize(
    "field,env_name",
    [
        ("SMS_MSG91_AUTH_KEY", "RESTAMP_SMS_MSG91_AUTH_KEY"),
        ("SMS_MSG91_TEMPLATE_ID", "RESTAMP_SMS_MSG91_TEMPLATE_ID"),
    ],
)
@pytest.mark.parametrize("blank", ["", "   "])
def test_missing_credential_fails_startup_naming_only_the_setting(
    monkeypatch, field, env_name, blank
):
    _prod(monkeypatch, **{field: blank})
    with pytest.raises(RuntimeError) as info:
        otp_module.get_otp_provider()
    message = str(info.value)
    assert env_name in message
    assert AUTH_KEY not in message and TEMPLATE_ID not in message


def test_lifespan_fails_fast_when_msg91_credentials_missing(monkeypatch):
    import asyncio

    import app.main as main_module
    from fastapi import FastAPI

    _prod(monkeypatch, SMS_MSG91_AUTH_KEY="")
    monkeypatch.setattr(main_module, "check_db", lambda: True)

    async def _drain(cm):
        async with cm:
            pass

    with pytest.raises(RuntimeError, match="RESTAMP_SMS_MSG91_AUTH_KEY"):
        asyncio.run(_drain(main_module.lifespan(FastAPI())))


def test_production_never_accepts_demo_or_debug_even_with_msg91(monkeypatch):
    _prod(monkeypatch, OTP_DEMO_MODE=True)
    with pytest.raises(RuntimeError, match="RESTAMP_OTP_DEMO_MODE"):
        validate_security_config()
    _prod(monkeypatch, OTP_DEBUG=True)
    with pytest.raises(RuntimeError, match="RESTAMP_OTP_DEBUG"):
        validate_security_config()


def test_dev_and_test_behavior_unchanged(monkeypatch):
    # Outside production the dev provider is used even if msg91 is selected
    # (and even with no credentials), so local work never sends real SMS.
    s = config_module.settings
    monkeypatch.setattr(s, "ENV", "local")
    monkeypatch.setattr(s, "DATABASE_URL", "mysql+pymysql://u:p@localhost/restamp")
    monkeypatch.setattr(s, "SMS_PROVIDER", "msg91")
    monkeypatch.setattr(s, "SMS_MSG91_AUTH_KEY", "")
    monkeypatch.setattr(s, "SMS_MSG91_TEMPLATE_ID", "")
    assert isinstance(otp_module.get_otp_provider(), DevOTPProvider)


# --- delivery ----------------------------------------------------------------

def test_success_sends_our_code_with_header_auth_and_verifies(monkeypatch):
    fake = FakePost()
    svc = _service(monkeypatch, fake)
    code = svc.request_code(PHONE)
    assert code is None  # OTP_DEBUG off: the code is never returned
    assert len(fake.calls) == 1
    url, kwargs = fake.calls[0]
    assert url == "https://control.msg91.com/api/v5/otp"
    assert kwargs["params"]["mobile"] == PHONE.lstrip("+")  # no '+'
    assert kwargs["params"]["template_id"] == TEMPLATE_ID
    assert len(kwargs["params"]["otp"]) == 6 and kwargs["params"]["otp"].isdigit()
    assert kwargs["headers"]["authkey"] == AUTH_KEY
    assert AUTH_KEY not in str(kwargs["params"])  # key is header-only, never in URL
    assert kwargs["follow_redirects"] is False
    # The code that was sent is the one that verifies (and only once).
    sent = kwargs["params"]["otp"]
    assert svc.verify_code(PHONE, sent) is True
    assert svc.verify_code(PHONE, sent) is False


def test_demo_codes_do_not_verify_in_production(monkeypatch):
    svc = _service(monkeypatch, FakePost())
    svc.request_code(PHONE)
    for demo in sorted(otp_module.DEMO_OTPS)[:3]:
        assert svc.verify_code(PHONE, demo) is False


@pytest.mark.parametrize(
    "fake",
    [
        FakePost(status=200, json_body={"type": "error", "message": "Template not found", "code": "311"}),
        FakePost(status=401, json_body={"type": "error", "message": "Authentication failure"}),
        FakePost(status=500, text="<html>upstream error</html>"),
        FakePost(status=200, text="not json at all"),
        FakePost(status=200, json_body=["unexpected"]),
        FakePost(status=200, json_body={"message": "no type field"}),
        FakePost(raises=httpx.ConnectTimeout("slow")),
        FakePost(raises=httpx.ConnectError("down")),
    ],
    ids=["200-error", "401", "500-html", "200-nonjson", "200-list", "200-notype", "timeout", "connect"],
)
def test_vendor_or_transport_failure_is_generic_503_and_rolls_back(monkeypatch, caplog, fake):
    svc = _service(monkeypatch, fake)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(HTTPException) as info:
        svc.request_code(PHONE)
    assert info.value.status_code == 503
    assert info.value.detail == "Failed to send code. Try again later."
    # No live-but-undelivered code, and the retry budget is not burned.
    assert PHONE not in svc._codes
    assert svc._requests.get(PHONE) in (None, [])
    sent = fake.calls[0][1]["params"]["otp"]
    assert svc.verify_code(PHONE, sent) is False
    _assert_no_leak(caplog.text + str(info.value.detail), code=sent)


def test_success_logs_nothing_sensitive(monkeypatch, caplog):
    fake = FakePost()
    svc = _service(monkeypatch, fake)
    caplog.set_level(logging.DEBUG)
    svc.request_code(PHONE)
    sent = fake.calls[0][1]["params"]["otp"]
    _assert_no_leak(caplog.text, code=sent)


def test_invalid_phone_never_reaches_the_vendor(monkeypatch):
    fake = FakePost()
    _prod(monkeypatch)
    monkeypatch.setattr(otp_module.httpx, "post", fake)
    with pytest.raises(RuntimeError):
        Msg91OTPProvider().send("+91abc", "123456")
    assert fake.calls == []


def test_http_client_loggers_cannot_emit_request_urls():
    # httpx logs full URLs (incl. phone + OTP query params) at INFO.
    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING
