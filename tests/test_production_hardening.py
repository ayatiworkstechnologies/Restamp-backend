"""Production hardening C1–C3 (replaces the removed OTP production override).

C1 — OTP codes never leave a production process:
- RESTAMP_OTP_DISPLAY_IN_PRODUCTION no longer exists (setting, env var and
  code path); display mode is honoured only in an explicit development env;
- production startup refuses RESTAMP_OTP_DISPLAY_MODE=1; at request time the
  service never returns a code and the auth router independently drops one;
- with no SMS delivery connected, production fails closed (503, no code);
- expiry, single use, attempt caps and request caps are unchanged.
C2 — production CORS is exactly RESTAMP_CORS_ORIGINS: listed origins get
  credentialed CORS, everything else (localhost, LAN, look-alike or
  unlisted domains) gets none; invalid lists refuse startup; development
  keeps its local defaults and localhost/LAN regex.
C3 — RESTAMP_ENV is required and alone decides production; unsafe startup
  configurations are refused with messages naming settings, never values.
No database writes persist (rollback-isolated client), no network, no SMS vendor.
"""
import logging
import re
import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import config as config_module
from app.config import is_production, is_valid_production_origin, otp_display_permitted, validate_security_config
from app.services.otp import OTPService, otp_service
from tests.conftest import captured_otp_code

APPROVED = "https://restamp-alpha.vercel.app,https://restamp-app.vercel.app,https://restamp.in,https://www.restamp.in"
NOT_CONNECTED_503 = "OTP delivery is not configured. Try again later."
STRONG = "z" * 40


def _production(monkeypatch, **overrides) -> None:
    """A valid production configuration; overrides win."""
    s = config_module.settings
    values = dict(ENV="production", JWT_SECRET=STRONG, OTP_DISPLAY_MODE=False, GOOGLE_DEV_MODE=False,
                  GOOGLE_ENABLED=False, GOOGLE_CLIENT_ID="", CORS_ORIGINS=APPROVED)
    values.update(overrides)
    for key, value in values.items():
        monkeypatch.setattr(s, key, value)


def _wrong(code: str) -> str:
    return f"{(int(code) + 1) % 10**6:06d}"


# --- C1: the production override is gone ----------------------------------------

def test_production_override_setting_no_longer_exists():
    assert not hasattr(config_module.settings, "OTP_DISPLAY_IN_PRODUCTION")
    import pathlib
    app_dir = pathlib.Path(config_module.__file__).parent
    hits = [p.name for p in app_dir.rglob("*.py") if "OTP_DISPLAY_IN_PRODUCTION" in p.read_text()]
    assert hits == []


def test_display_only_permitted_in_explicit_development(monkeypatch):
    for env, permitted in (("local", True), ("development", True), ("test", True), ("dev", True),
                           ("production", False), ("prod", False), ("staging", False), ("", False), ("demo", False)):
        monkeypatch.setattr(config_module.settings, "ENV", env)
        assert otp_display_permitted() is permitted, env


def test_production_startup_refuses_display_mode(monkeypatch, caplog):
    _production(monkeypatch, OTP_DISPLAY_MODE=True)
    with caplog.at_level(logging.WARNING, logger="restamp.config"):
        with pytest.raises(RuntimeError, match="RESTAMP_OTP_DISPLAY_MODE") as info:
            validate_security_config()
    assert STRONG not in str(info.value)


def test_production_service_never_returns_a_code_even_with_display_flag(monkeypatch):
    _production(monkeypatch, OTP_DISPLAY_MODE=True)  # forced on, bypassing startup
    inbox = {}
    svc = OTPService()
    svc.delivery = lambda phone, code: inbox.__setitem__(phone, code)
    assert svc.request_code("+919876543240") is None  # nothing returned...
    assert re.fullmatch(r"\d{6}", inbox["+919876543240"])  # ...it went to delivery only


@pytest.mark.parametrize("display", [True, False])
def test_production_without_delivery_fails_closed(monkeypatch, display):
    _production(monkeypatch, OTP_DISPLAY_MODE=display)
    svc = OTPService()
    with pytest.raises(HTTPException) as info:
        svc.request_code("+919876543241")
    assert info.value.status_code == 503 and info.value.detail == NOT_CONNECTED_503


def test_production_endpoint_never_exposes_a_code(client, monkeypatch):
    c, _ = client
    _production(monkeypatch, OTP_DISPLAY_MODE=True)
    phone = "+919876543242"
    r = c.post("/auth/otp/request", json={"phone": phone})
    assert r.status_code == 200 and set(r.json()) == {"message"}
    code = captured_otp_code(phone)  # delivered through the (test) SMS seam only
    assert code not in r.text


def test_production_router_drops_a_code_even_if_the_service_returned_one(client, monkeypatch):
    """Defence in depth: the router re-checks production on its own."""
    c, _ = client
    _production(monkeypatch)
    monkeypatch.setattr(otp_service, "request_code", lambda phone, ip=None: "424242")
    r = c.post("/auth/otp/request", json={"phone": "+919876543243"})
    assert r.status_code == 200 and r.json() == {"message": "If the number is valid, an OTP was sent."}
    assert "424242" not in r.text


def test_production_endpoint_fails_closed_without_delivery(client, monkeypatch):
    c, _ = client
    _production(monkeypatch, OTP_DISPLAY_MODE=True)
    monkeypatch.setattr(otp_service, "delivery", None)
    r = c.post("/auth/otp/request", json={"phone": "+919876543244"})
    assert r.status_code == 503 and r.json() == {"detail": NOT_CONNECTED_503}
    assert "debug_code" not in r.text


def test_development_display_mode_still_works(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(config_module.settings, "OTP_DISPLAY_MODE", True)
    monkeypatch.setattr(otp_service, "delivery", None)
    phone = "+919876543245"
    r = c.post("/auth/otp/request", json={"phone": phone})
    assert r.status_code == 200 and re.fullmatch(r"\d{6}", r.json()["debug_code"])
    assert c.post("/auth/otp/verify", json={"phone": phone, "code": r.json()["debug_code"]}).status_code == 200


def test_production_login_lifecycle_preserved(client, monkeypatch):
    """Delivered code verifies once; wrong and reused codes are refused."""
    c, _ = client
    _production(monkeypatch)
    phone = "+919876543246"
    assert c.post("/auth/otp/request", json={"phone": phone}).status_code == 200
    code = captured_otp_code(phone)
    assert c.post("/auth/otp/verify", json={"phone": phone, "code": _wrong(code)}).status_code == 401
    ok = c.post("/auth/otp/verify", json={"phone": phone, "code": code})
    assert ok.status_code == 200 and ok.json()["access_token"]
    assert c.post("/auth/otp/verify", json={"phone": phone, "code": code}).status_code == 401


def test_production_expiry_attempt_and_request_caps_preserved(monkeypatch):
    _production(monkeypatch)
    monkeypatch.setattr(config_module.settings, "OTP_TTL_SECONDS", 300)
    monkeypatch.setattr(config_module.settings, "OTP_MAX_VERIFY_ATTEMPTS", 3)
    monkeypatch.setattr(config_module.settings, "OTP_MAX_REQUESTS_PER_HOUR", 2)
    inbox = {}
    svc = OTPService()
    svc.delivery = lambda phone, code: inbox.__setitem__(phone, code)
    # expiry
    phone = "+919876543247"
    svc.request_code(phone)
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now + 301)
    assert svc.verify_code(phone, inbox[phone]) is False
    monkeypatch.setattr(time, "time", lambda: now)
    # attempt cap: the guess that reaches the cap blocks; the right code is then refused too
    phone = "+919876543248"
    svc.request_code(phone)
    for _ in range(2):
        assert svc.verify_code(phone, _wrong(inbox[phone])) is False
    with pytest.raises(HTTPException) as info:
        svc.verify_code(phone, _wrong(inbox[phone]))
    assert info.value.status_code == 429
    with pytest.raises(HTTPException) as info:
        svc.verify_code(phone, inbox[phone])
    assert info.value.status_code == 429
    # request cap per phone
    phone = "+919876543249"
    svc.request_code(phone)
    svc.request_code(phone)
    with pytest.raises(HTTPException) as info:
        svc.request_code(phone)
    assert info.value.status_code == 429


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
    _production(monkeypatch, ENV=env)
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
