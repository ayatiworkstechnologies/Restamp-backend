"""Production hardening C1–C3 (fixed-OTP edition).

C1 — the fixed testing OTP never works in a production process:
- one fixed code (app/services/otp.py TEST_OTP) is accepted only where
  otp_display_permitted() is true (explicit development env or staging);
- production startup refuses the obsolete RESTAMP_OTP_DISPLAY_MODE flag;
  at request time the service fails closed (503 / verify=False) and the
  auth router independently drops any code from a response;
- the removed random-code pipeline (generation, hashing, expiry, attempt
  caps, request caps, delivery seam) is gone: the corresponding settings
  and code paths no longer exist.
C2 — production CORS is exactly RESTAMP_CORS_ORIGINS: listed origins get
  credentialed CORS, everything else (localhost, LAN, look-alike or
  unlisted domains) gets none; invalid lists refuse startup; development
  keeps its local defaults and localhost/LAN regex; staging refuses to
  serve the public site while the testing OTP is active.
C3 — RESTAMP_ENV is required and alone decides production; unsafe startup
  configurations are refused with messages naming settings, never values.
No database writes persist (rollback-isolated client), no network, no SMS vendor.
"""
import logging

import pytest
from fastapi.testclient import TestClient

from app import config as config_module
from app.config import is_production, is_staging, is_valid_production_origin, otp_display_permitted, validate_security_config

APPROVED = "https://restamp-alpha.vercel.app,https://restamp-app.vercel.app,https://restamp.in,https://www.restamp.in"
STRONG = "z" * 40


def _production(monkeypatch, **overrides) -> None:
    """A valid production configuration; overrides win."""
    s = config_module.settings
    values = dict(ENV="production", JWT_SECRET=STRONG, GOOGLE_DEV_MODE=False,
                  GOOGLE_ENABLED=False, GOOGLE_CLIENT_ID="", CORS_ORIGINS=APPROVED)
    values.update(overrides)
    for key, value in values.items():
        monkeypatch.setattr(s, key, value)


# --- C1: the fixed testing OTP is environment-gated ------------------------------

def test_production_override_setting_no_longer_exists():
    assert not hasattr(config_module.settings, "OTP_DISPLAY_IN_PRODUCTION")
    import pathlib
    app_dir = pathlib.Path(config_module.__file__).parent
    hits = [p.name for p in app_dir.rglob("*.py") if "OTP_DISPLAY_IN_PRODUCTION" in p.read_text()]
    assert hits == []


def test_display_only_permitted_in_explicit_development_or_staging(monkeypatch):
    for env, permitted in (("local", True), ("development", True), ("test", True), ("dev", True),
                           ("staging", True), ("production", False), ("prod", False),
                           ("", False), ("demo", False)):
        monkeypatch.setattr(config_module.settings, "ENV", env)
        assert otp_display_permitted() is permitted, env


def test_staging_is_production_classified_but_display_permitted(monkeypatch):
    monkeypatch.setattr(config_module.settings, "ENV", "staging")
    assert is_staging() is True and is_production() is True
    assert otp_display_permitted() is True


def test_production_startup_refuses_the_obsolete_display_flag(monkeypatch, caplog):
    _production(monkeypatch)
    monkeypatch.setenv("RESTAMP_OTP_DISPLAY_MODE", "1")
    with caplog.at_level(logging.WARNING, logger="restamp.config"):
        with pytest.raises(RuntimeError, match="RESTAMP_OTP_DISPLAY_MODE") as info:
            validate_security_config()
    assert STRONG not in str(info.value)


def test_production_still_boots_without_the_obsolete_flag(monkeypatch):
    _production(monkeypatch)
    monkeypatch.setenv("RESTAMP_OTP_DISPLAY_MODE", "0")
    validate_security_config()


def test_staging_startup_refuses_public_site_origins(monkeypatch):
    _production(monkeypatch, ENV="staging")
    with pytest.raises(RuntimeError, match="public site"):
        validate_security_config()


def test_staging_startup_passes_with_test_site_origins(monkeypatch, caplog):
    _production(monkeypatch, ENV="staging",
                CORS_ORIGINS="https://restamp-alpha.vercel.app,https://restamp-app.vercel.app")
    with caplog.at_level(logging.WARNING, logger="restamp.config"):
        validate_security_config()
    assert "fixed testing OTP is active" in caplog.text


# --- C2: production CORS ----------------------------------------------------------

def _prod_app(monkeypatch, **overrides) -> TestClient:
    import app.main as main_module
    _production(monkeypatch, **overrides)
    return TestClient(main_module.create_app())  # no lifespan: CORS needs no DB


def _preflight(tc: TestClient, origin: str):
    return tc.options("/health", headers={"Origin": origin, "Access-Control-Request-Method": "GET",
                                           "Access-Control-Request-Headers": "authorization,content-type"})


@pytest.mark.parametrize("origin", APPROVED.split(","))
def test_production_cors_allows_each_configured_origin_with_credentials(monkeypatch, origin):
    tc = _prod_app(monkeypatch)
    pre = _preflight(tc, origin)
    assert pre.status_code == 200
    assert pre.headers["access-control-allow-origin"] == origin  # echoed, never "*"
    assert pre.headers["access-control-allow-credentials"] == "true"
    assert "authorization" in pre.headers["access-control-allow-headers"].lower()
    get = tc.get("/health", headers={"Origin": origin})
    assert get.headers["access-control-allow-origin"] == origin
    assert get.headers["access-control-allow-credentials"] == "true"


@pytest.mark.parametrize("origin", [
    "http://localhost:3000", "http://localhost:8081", "http://127.0.0.1:8081", "http://192.168.1.10:8081",
    "http://10.0.0.5:3000", "http://172.16.4.2:19006", "https://evil.example", "https://restamp.in.evil.example",
    "https://evilrestamp.in", "http://restamp.in", "https://restamp-web.vercel.app", "null",
])
def test_production_cors_rejects_unlisted_origins(monkeypatch, origin):
    tc = _prod_app(monkeypatch)
    pre = _preflight(tc, origin)
    assert pre.status_code == 400
    assert "access-control-allow-origin" not in pre.headers
    assert "access-control-allow-credentials" not in pre.headers
    get = tc.get("/health", headers={"Origin": origin})
    assert get.status_code == 200  # the request itself works (e.g. curl, native apps)...
    assert "access-control-allow-origin" not in get.headers  # ...but browsers get no CORS grant
    assert "access-control-allow-credentials" not in get.headers


def test_production_cors_uses_only_the_configured_list(monkeypatch):
    tc = _prod_app(monkeypatch, CORS_ORIGINS="https://restamp.in")
    assert _preflight(tc, "https://restamp.in").status_code == 200
    assert _preflight(tc, "https://www.restamp.in").status_code == 400  # not configured -> not allowed


def test_development_cors_keeps_local_defaults_and_lan(monkeypatch):
    import app.main as main_module
    monkeypatch.setattr(config_module.settings, "ENV", "local")
    monkeypatch.setattr(config_module.settings, "CORS_ORIGINS", "")
    tc = TestClient(main_module.create_app())
    for origin in ("http://localhost:8081", "http://localhost:3000", "http://192.168.1.10:8081"):
        pre = _preflight(tc, origin)
        assert pre.status_code == 200 and pre.headers["access-control-allow-origin"] == origin, origin
    assert _preflight(tc, "https://evil.example").status_code == 400


@pytest.mark.parametrize("raw", [
    "",                                              # missing
    "*",                                             # wildcard
    "https://restamp.in,*",
    "https://*.vercel.app",
    "http://restamp.in",                             # not https
    "https://restamp.in/",                           # trailing slash
    "https://restamp.in/app",                        # path
    "https://localhost",                             # localhost
    "https://192.168.1.10",                          # private LAN
    "https://10.1.2.3",
    "https://RESTAMP.in",                            # never matches a browser origin
    "https://user@restamp.in",                       # credentials in URL
])
def test_production_startup_refuses_unsafe_cors(monkeypatch, raw):
    _production(monkeypatch, CORS_ORIGINS=raw)
    with pytest.raises(RuntimeError, match="RESTAMP_CORS_ORIGINS"):
        validate_security_config()


def test_approved_origins_validate():
    assert all(is_valid_production_origin(o) for o in APPROVED.split(","))
    assert is_valid_production_origin("https://restamp.in:8443")


# --- C3: explicit production configuration --------------------------------------

@pytest.mark.parametrize("env", ["", "  ", "prodution", "live", "demo"])
def test_startup_refuses_unset_or_unknown_env(monkeypatch, env):
    monkeypatch.setattr(config_module.settings, "ENV", env)
    with pytest.raises(RuntimeError, match="RESTAMP_ENV"):
        validate_security_config()
    assert is_production() is True  # request-time checks fail closed meanwhile


@pytest.mark.parametrize("env", ["local", "development", "dev", "test", "LOCAL"])
def test_development_envs_with_local_db_start(monkeypatch, env):
    monkeypatch.setattr(config_module.settings, "ENV", env)
    validate_security_config()
    assert is_production() is False


@pytest.mark.parametrize("env", ["production", "prod", "staging", "Production"])
def test_production_is_decided_by_env_even_with_a_local_database(monkeypatch, env):
    """The single-server deployment (MySQL on localhost) is production when
    RESTAMP_ENV says so — the database host plays no part."""
    # Staging must never list the public site (the fixed testing OTP is
    # active there), so its valid origin list is the test-site pair.
    origins = ("https://restamp-alpha.vercel.app,https://restamp-app.vercel.app"
               if env.lower() == "staging" else APPROVED)
    _production(monkeypatch, ENV=env, CORS_ORIGINS=origins)
    monkeypatch.setattr(config_module.settings, "DATABASE_URL",
                        "mysql+pymysql://u:p@localhost/restamp?unix_socket=/var/run/mysqld/mysqld.sock")
    assert is_production() is True
    validate_security_config()


def test_production_refuses_placeholder_database_url(monkeypatch):
    _production(monkeypatch, DATABASE_URL="mysql+pymysql://user:pass@localhost/restamp")
    with pytest.raises(RuntimeError, match="RESTAMP_DATABASE_URL"):
        validate_security_config()


@pytest.mark.parametrize("secret", ["", "short", "restamp-local-dev-secret-change-me", "x" * 31,
                                    "my-password-is-very-long-indeed-1234567"])
def test_production_refuses_weak_jwt_secret(monkeypatch, secret):
    _production(monkeypatch, JWT_SECRET=secret)
    with pytest.raises(RuntimeError, match="RESTAMP_JWT_SECRET") as info:
        validate_security_config()
    if secret:
        assert secret not in str(info.value)  # never echo the value


def test_production_refuses_google_dev_flags(monkeypatch):
    _production(monkeypatch, GOOGLE_DEV_MODE=True)
    with pytest.raises(RuntimeError, match="RESTAMP_GOOGLE_DEV_MODE"):
        validate_security_config()
    _production(monkeypatch, GOOGLE_ENABLED=True, GOOGLE_CLIENT_ID="  ")
    with pytest.raises(RuntimeError, match="RESTAMP_GOOGLE_CLIENT_ID"):
        validate_security_config()


def test_production_docs_hidden_and_development_docs_available(monkeypatch):
    import app.main as main_module
    _production(monkeypatch)
    prod = main_module.create_app()
    assert prod.docs_url is None and prod.redoc_url is None and prod.openapi_url is None
    monkeypatch.setattr(config_module.settings, "ENV", "local")
    dev = main_module.create_app()
    assert dev.docs_url == "/docs" and dev.openapi_url == "/openapi.json"


def test_real_startup_refuses_unsafe_production_config(monkeypatch):
    """The lifespan gate runs on real startup (TestClient context)."""
    import app.main as main_module
    _production(monkeypatch, CORS_ORIGINS="")
    with pytest.raises(RuntimeError, match="RESTAMP_CORS_ORIGINS"):
        with TestClient(main_module.create_app()):
            pass
