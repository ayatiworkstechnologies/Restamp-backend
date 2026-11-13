"""Auth production-readiness: the simplified OTP fails closed in production
and the Google prod guards hold.

OTP endpoint tests use the rollback-isolated `client` fixture on local
restamp_dev only; Google tests are config/verifier-level. No real SMS or
Google network calls exist anywhere in this file. The delivery seam no
longer exists (the simplified login has one fixed testing OTP gated to
local/staging; see tests/test_otp_core_contract.py).
"""
import pytest
from fastapi import HTTPException

from app import config as config_module
from app.config import validate_security_config
from app.services import google as google_svc
from app.services import otp as otp_module


def _prod(monkeypatch, **overrides):
    """Pin production env with a strong JWT and quiet dev flags; overrides win."""
    monkeypatch.setattr(config_module.settings, "ENV", "production")
    monkeypatch.setattr(config_module.settings, "JWT_SECRET", "z" * 40)
    monkeypatch.setattr(config_module.settings, "GOOGLE_DEV_MODE", False)
    monkeypatch.setattr(config_module.settings, "GOOGLE_ENABLED", False)
    monkeypatch.setattr(config_module.settings, "GOOGLE_CLIENT_ID", "")
    # Production requires explicit browser origins (RESTAMP_CORS_ORIGINS).
    monkeypatch.setattr(config_module.settings, "CORS_ORIGINS", "https://restamp.in")
    for key, value in overrides.items():
        monkeypatch.setattr(config_module.settings, key, value)


# --- OTP: production fails closed (service level) --------------------------------

def test_prod_request_code_fails_closed(monkeypatch):
    _prod(monkeypatch)
    with pytest.raises(HTTPException) as info:
        otp_module.request_code("+916900001001")
    assert info.value.status_code == 503
    assert "349086" not in info.value.detail  # never leaks the testing code


def test_prod_verify_rejects_every_code(monkeypatch):
    _prod(monkeypatch)
    for code in ("349086", "000000", "999999", ""):
        assert otp_module.verify_code("+916900001002", code) is False


# --- OTP: production fails closed (endpoint level) --------------------------------

def test_prod_endpoint_503_and_verify_401_no_leak(client, monkeypatch):
    c, _ = client
    _prod(monkeypatch)
    r = c.post("/auth/otp/request", json={"phone": "+916900001005"})
    assert r.status_code == 503, r.text
    assert "349086" not in r.text and "debug_code" not in r.text
    ok = c.post("/auth/otp/verify", json={"phone": "+916900001005", "code": "349086"})
    assert ok.status_code == 401 and "access_token" not in ok.text


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
