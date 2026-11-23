"""Production CORS opt-in for local frontends (RESTAMP_CORS_ALLOW_LOCALHOST_IN_PRODUCTION).

Default off. When the exact value "1" is set, a production-classified backend
additionally allows EXACTLY http://localhost:3000 and http://localhost:8081
(config.PRODUCTION_LOCAL_DEV_ORIGINS). No other port, no 127.0.0.1, no LAN,
no external origin, never a regex/wildcard; RESTAMP_CORS_DEV_ORIGINS cannot
widen production. The configured https origins behave exactly as before.
"""
from __future__ import annotations

import importlib.util
import logging

import pytest

from app import config as config_module
from app.config import PRODUCTION_LOCAL_DEV_ORIGINS, validate_security_config
from tests.test_production_hardening import APPROVED, _preflight, _prod_app, _production

LOCAL = ["http://localhost:3000", "http://localhost:8081"]
REJECTED_EVEN_WHEN_ENABLED = [
    "http://localhost:4000",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:8081",
    "https://localhost:3000",
    "http://localhost",
    "http://localhost:30000",
    "http://localhost:3000.evil.example",
    "http://192.168.1.10:3000",
    "https://evil.example",
    "null",
]


def _assert_allowed(tc, origin):
    pre = _preflight(tc, origin)
    assert pre.status_code == 200, origin
    assert pre.headers["access-control-allow-origin"] == origin  # echoed, never "*"
    assert pre.headers["access-control-allow-credentials"] == "true"
    assert "authorization" in pre.headers["access-control-allow-headers"].lower()
    assert "GET" in pre.headers["access-control-allow-methods"]
    get = tc.get("/health", headers={"Origin": origin})
    assert get.headers["access-control-allow-origin"] == origin


def _assert_rejected(tc, origin):
    pre = _preflight(tc, origin)
    assert pre.status_code == 400, origin
    assert "access-control-allow-origin" not in pre.headers
    assert "access-control-allow-credentials" not in pre.headers
    get = tc.get("/health", headers={"Origin": origin})
    assert "access-control-allow-origin" not in get.headers
    assert "access-control-allow-credentials" not in get.headers


def _fresh_flag() -> bool:
    """Evaluate the flag from the CURRENT environment: Settings reads the
    environment once at class definition, so load an isolated copy of the
    config module (never replaces the app's live settings)."""
    spec = importlib.util.spec_from_file_location("_restamp_config_probe", config_module.__file__)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Settings.CORS_ALLOW_LOCALHOST_IN_PRODUCTION


def test_fixed_local_origin_set_is_exactly_the_approved_pair():
    assert PRODUCTION_LOCAL_DEV_ORIGINS == ("http://localhost:3000", "http://localhost:8081")


def test_opt_in_defaults_to_disabled(monkeypatch):
    monkeypatch.delenv("RESTAMP_CORS_ALLOW_LOCALHOST_IN_PRODUCTION", raising=False)
    assert _fresh_flag() is False


@pytest.mark.parametrize("origin", LOCAL)
def test_production_local_origins_rejected_when_opt_in_disabled(monkeypatch, origin):
    tc = _prod_app(monkeypatch, CORS_ALLOW_LOCALHOST_IN_PRODUCTION=False)
    _assert_rejected(tc, origin)


@pytest.mark.parametrize("origin", LOCAL)
def test_production_local_origins_allowed_when_opt_in_enabled(monkeypatch, origin):
    tc = _prod_app(monkeypatch, CORS_ALLOW_LOCALHOST_IN_PRODUCTION=True)
    _assert_allowed(tc, origin)


@pytest.mark.parametrize("origin", REJECTED_EVEN_WHEN_ENABLED)
def test_production_other_origins_rejected_when_opt_in_enabled(monkeypatch, origin):
    tc = _prod_app(monkeypatch, CORS_ALLOW_LOCALHOST_IN_PRODUCTION=True)
    _assert_rejected(tc, origin)


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("origin", APPROVED.split(","))
def test_production_https_origins_unchanged(monkeypatch, origin, enabled):
    tc = _prod_app(monkeypatch, CORS_ALLOW_LOCALHOST_IN_PRODUCTION=enabled)
    _assert_allowed(tc, origin)


def test_dev_origins_setting_cannot_widen_production(monkeypatch):
    tc = _prod_app(
        monkeypatch,
        CORS_ALLOW_LOCALHOST_IN_PRODUCTION=True,
        DEV_CORS_ORIGINS="http://localhost:4000,http://127.0.0.1:3000,https://evil.example",
    )
    for origin in ("http://localhost:4000", "http://127.0.0.1:3000", "https://evil.example"):
        _assert_rejected(tc, origin)
    for origin in LOCAL:
        _assert_allowed(tc, origin)


def test_middleware_receives_exact_list_without_regex(monkeypatch):
    import app.main as main_module

    _production(monkeypatch, CORS_ALLOW_LOCALHOST_IN_PRODUCTION=True, CORS_ORIGINS="https://restamp.in")
    assert main_module._cors_settings() == {
        "allow_origins": ["https://restamp.in", "http://localhost:3000", "http://localhost:8081"],
        "allow_origin_regex": None,
    }
    monkeypatch.setattr(config_module.settings, "CORS_ALLOW_LOCALHOST_IN_PRODUCTION", False)
    assert main_module._cors_settings() == {"allow_origins": ["https://restamp.in"], "allow_origin_regex": None}


@pytest.mark.parametrize("raw", ["true", "yes", " 1", "1 ", "on", "0", ""])
def test_only_exact_one_enables_the_opt_in(monkeypatch, raw):
    monkeypatch.setenv("RESTAMP_CORS_ALLOW_LOCALHOST_IN_PRODUCTION", raw)
    assert _fresh_flag() is False


def test_exact_one_enables_the_opt_in(monkeypatch):
    monkeypatch.setenv("RESTAMP_CORS_ALLOW_LOCALHOST_IN_PRODUCTION", "1")
    assert _fresh_flag() is True


def test_production_startup_passes_and_warns_when_enabled(monkeypatch, caplog):
    _production(monkeypatch, CORS_ALLOW_LOCALHOST_IN_PRODUCTION=True)
    with caplog.at_level(logging.WARNING, logger="restamp.config"):
        validate_security_config()  # used to crash (NameError) with the flag on
    assert "RESTAMP_CORS_ALLOW_LOCALHOST_IN_PRODUCTION=1" in caplog.text
    assert "http://localhost:3000" in caplog.text and "http://localhost:8081" in caplog.text


def test_production_validation_still_refuses_localhost_in_cors_origins(monkeypatch):
    # The opt-in never relaxes RESTAMP_CORS_ORIGINS validation.
    _production(monkeypatch, CORS_ALLOW_LOCALHOST_IN_PRODUCTION=True,
                CORS_ORIGINS="https://restamp.in,http://localhost:3000")
    with pytest.raises(RuntimeError, match="RESTAMP_CORS_ORIGINS"):
        validate_security_config()


def test_production_validation_still_refuses_wildcard_with_opt_in(monkeypatch):
    _production(monkeypatch, CORS_ALLOW_LOCALHOST_IN_PRODUCTION=True, CORS_ORIGINS="*")
    with pytest.raises(RuntimeError, match="RESTAMP_CORS_ORIGINS"):
        validate_security_config()


def test_stray_dev_origins_warned_and_ignored_in_production(monkeypatch, caplog):
    _production(monkeypatch, DEV_CORS_ORIGINS="http://localhost:4000")
    with caplog.at_level(logging.WARNING, logger="restamp.config"):
        validate_security_config()
    assert "RESTAMP_CORS_DEV_ORIGINS is set but ignored in production" in caplog.text
    assert "localhost:4000" not in caplog.text  # setting name only, never its value
