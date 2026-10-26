"""No-demo regression lock: the demo OTP login and the OTP_DEBUG disclosure
path are fully removed from the codebase. Authentication is real-SMS OTP
only — a code is generated server-side with Python `secrets`, delivered
only through the (not-yet-connected) delivery seam, and in EVERY shipped
configuration the code is never returned, logged, or displayed. The single
exception is the explicitly restricted internal-testing display mode
(RESTAMP_OTP_DISPLAY_MODE="1" in a non-production-classified process;
impossible in production), which is specified and tested separately in
tests/test_otp_display_mode.py — there disclosure is part of the
deliberate, gated internal-testing contract, not a debug/demo fallback.

Guards:
- RESTAMP_ENV=demo is no longer a mode: startup refuses it unconditionally
  (regardless of environment classification, at the config gate AND at the
  real lifespan startup path);
- the removed symbols cannot return implicitly: no OTP_DEBUG /
  OTP_DEMO_MODE / OTP_DEMO_PHONES settings, no DEMO_OTPS /
  demo_login_active in the service;
- the ordinary strict path is the ONLY path: the request response is
  exactly {message}, the provider-delivered code verifies consumed-once,
  and the former predefined demo codes are never accepted — even with the
  legacy RESTAMP_OTP_* environment variables set (they are inert now).
  (RESTAMP_OTP_DISPLAY_MODE stays off throughout this suite; the display
  contract is exercised in tests/test_otp_display_mode.py.)

No database writes; no network calls (the delivery stand-in is a test-only
recording hook; no real SMS is sent).
"""
import asyncio

import pytest
from fastapi import FastAPI

from app import config as config_module
from app.config import is_production, validate_security_config
from app.services import otp as otp_module
from app.services.otp import OTPService
from tests.conftest import TestSMSInbox, captured_otp_code

LEGACY_DEMO_CODES = ("297569", "109745", "583214")  # former DEMO_OTPS members
REQUEST_200 = {"message": "If the number is valid, an OTP was sent."}


# --- the removed machinery is gone -------------------------------------------

def test_removed_symbols_and_settings_are_absent():
    for name in ("DEMO_OTPS", "demo_login_active"):
        assert not hasattr(otp_module, name), name
    for name in (
        "OTP_DEBUG", "OTP_DEMO_MODE", "OTP_DEMO_PHONES",
        "_norm_demo_phone", "demo_phone_allowlist", "demo_phone_allowed",
    ):
        assert not hasattr(config_module, name), name
        assert not hasattr(config_module.settings, name), name


# --- RESTAMP_ENV=demo is refused unconditionally --------------------------------

def test_env_demo_refused_when_production_classified(monkeypatch):
    s = config_module.settings
    monkeypatch.setattr(s, "ENV", "demo")
    monkeypatch.setattr(
        s, "DATABASE_URL", "mysql+pymysql://u:p@db.example.com/restamp_prod"
    )
    monkeypatch.setattr(s, "JWT_SECRET", "z" * 40)
    assert is_production() is True
    with pytest.raises(RuntimeError, match="RESTAMP_ENV=demo"):
        validate_security_config()


def test_env_demo_refused_even_locally(monkeypatch):
    """The refusal runs BEFORE the is_production() early-return: a leftover
    RESTAMP_ENV=demo must never silently boot as local development."""
    s = config_module.settings
    monkeypatch.setattr(s, "ENV", "demo")
    monkeypatch.setattr(
        s,
        "DATABASE_URL",
        "mysql+pymysql://u:p@localhost/restamp_dev?unix_socket=/tmp/mysql.sock",
    )
    assert is_production() is False  # sanity: this is the LOCAL classification
    with pytest.raises(RuntimeError, match="RESTAMP_ENV=demo"):
        validate_security_config()


def test_env_demo_refused_at_real_lifespan_startup(monkeypatch):
    import app.main as main_module

    s = config_module.settings
    monkeypatch.setattr(s, "ENV", "demo")
    monkeypatch.setattr(main_module, "check_db", lambda: True)

    async def _drain():
        async with main_module.lifespan(FastAPI()):
            pass

    with pytest.raises(RuntimeError, match="RESTAMP_ENV=demo"):
        asyncio.run(_drain())


# --- the strict path is the ONLY path ------------------------------------------

def test_service_never_returns_codes_and_legacy_codes_fail():
    svc = OTPService()
    inbox = TestSMSInbox()
    svc.delivery = inbox
    phone = "+916550009002"
    assert svc.request_code(phone) is None  # codes are never returned...
    code = inbox.codes[phone]  # ...but are delivered via the provider (SMS)
    for legacy in LEGACY_DEMO_CODES:
        assert svc.verify_code(phone, legacy) is False
    assert svc.verify_code(phone, code) is True  # only the delivered code
    assert svc.verify_code(phone, code) is False  # consumed once


def test_legacy_env_flags_are_inert_and_contract_preserved(client, monkeypatch):
    """RESTAMP_OTP_DEBUG/OTP_DEMO_MODE/OTP_DEMO_PHONES no longer exist as
    settings: setting them changes nothing (note the response contract)."""
    monkeypatch.setenv("RESTAMP_OTP_DEBUG", "1")
    monkeypatch.setenv("RESTAMP_OTP_DEMO_MODE", "1")
    monkeypatch.setenv("RESTAMP_OTP_DEMO_PHONES", "+916550001111")
    c, _ = client
    phone = "+916550009001"
    r = c.post("/auth/otp/request", json={"phone": phone})
    assert r.status_code == 200, r.text
    assert r.json() == REQUEST_200  # NO debug_code, ever
    for attempt, wrong in enumerate(("000000", *LEGACY_DEMO_CODES), 1):
        bad = c.post("/auth/otp/verify", json={"phone": phone, "code": wrong})
        assert bad.status_code == 401, bad.text
    ok = c.post("/auth/otp/verify", json={"phone": phone, "code": captured_otp_code(phone)})
    assert ok.status_code == 200, ok.text
    assert "access_token" in ok.json()
