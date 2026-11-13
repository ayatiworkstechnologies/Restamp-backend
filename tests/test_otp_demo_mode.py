"""No-demo regression lock: the demo OTP login and the OTP_DEBUG disclosure
path are fully removed from the codebase, and the simplified login has one
fixed testing code only.

Authentication is the fixed testing OTP (app/services/otp.py TEST_OTP),
accepted only where otp_display_permitted() is true (local/development/
dev/test or staging). Guards verified here:

- RESTAMP_ENV=demo is no longer a mode: startup refuses it unconditionally
  (regardless of environment classification, at the config gate AND at the
  real lifespan startup path);
- the removed symbols cannot return implicitly: no OTP_DEBUG /
  OTP_DEMO_MODE / OTP_DEMO_PHONES settings, no DEMO_OTPS /
  demo_login_active in the service, no OTPService class or delivery seam;
- the fixed-OTP path is the ONLY path: local/staging accepts exactly the
  fixed code, and the former predefined demo codes are never accepted —
  even with the legacy RESTAMP_OTP_* environment variables set (they are
  inert now).

No database writes persist; no network calls, no SMS.
"""
import asyncio

import pytest
from fastapi import FastAPI

from app import config as config_module
from app.config import is_production, validate_security_config
from app.services import otp as otp_module
from app.services.otp import TEST_OTP
from tests.conftest import captured_otp_code

LEGACY_DEMO_CODES = ("297569", "109745", "583214")  # former DEMO_OTPS members
REQUEST_OK = {"message": "If the number is valid, an OTP was sent.", "debug_code": TEST_OTP}


# --- the removed machinery is gone -------------------------------------------

def test_removed_symbols_and_settings_are_absent():
    for name in ("DEMO_OTPS", "demo_login_active", "OTPService"):
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
    """A leftover RESTAMP_ENV=demo is refused by name (even with a local
    database) and never boots as local development."""
    s = config_module.settings
    monkeypatch.setattr(s, "ENV", "demo")
    monkeypatch.setattr(
        s,
        "DATABASE_URL",
        "mysql+pymysql://u:p@localhost/restamp_dev?unix_socket=/tmp/mysql.sock",
    )
    # An unknown RESTAMP_ENV (demo included) is never development: request-time
    # checks fail closed as production, and startup refuses it by name.
    assert is_production() is True
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


# --- the fixed-OTP path is the ONLY path ----------------------------------------

def test_fixed_code_verifies_and_legacy_codes_fail():
    assert otp_module.verify_code("+916550009002", TEST_OTP) is True
    for legacy in LEGACY_DEMO_CODES:
        assert otp_module.verify_code("+916550009002", legacy) is False


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
    assert r.json() == REQUEST_OK  # exactly {message, debug_code: fixed}
    for wrong in ("000000", *LEGACY_DEMO_CODES):
        bad = c.post("/auth/otp/verify", json={"phone": phone, "code": wrong})
        assert bad.status_code == 401, bad.text
    ok = c.post("/auth/otp/verify", json={"phone": phone, "code": captured_otp_code(phone)})
    assert ok.status_code == 200, ok.text
    assert "access_token" in ok.json()
