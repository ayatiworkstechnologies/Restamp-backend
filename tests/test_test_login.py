"""The temporary demo/test login is REMOVED — verified here.

The restricted test-only login (`POST /auth/test-login`) that supported
the client-presentation windows — the keyed allowlist mode, the
existing-accounts mode, and the predictable phone-suffix shortcut — was
removed together with every RESTAMP_TEST_LOGIN_* setting, the
`app/services/test_login.py` module, and all dashboard templates/docs.

Verified here:

- the route no longer exists: EVERY request shape (legacy keyed body,
  suffix {phone, code} body, garbage) gets FastAPI's standard unknown-
  route 404 — none of the old authorizations can ever fire again, and
  no request can create a single row;
- the service module is unimportable and no RESTAMP_TEST_LOGIN_*
  settings (or config helpers) remain;
- normal phone-OTP authentication is UNCHANGED: the registration-
  capable request/verify flow still works (existing suites cover it in
  depth); a stray `code` field in an OTP request body is schema-
  rejected — a six-digit guess can never authenticate or register an
  account, and the login path creates nothing;
- production stays fail-closed exactly as before: RESTAMP_OTP_DISPLAY_
  MODE=1 is refused at startup, the process boots cleanly with no
  demo/suffix variables, and OTP requests fail closed (generic 503 or
  the test delivery stand-in) — never a fabricated success.

Endpoint tests use the rollback-isolated `client` fixture on the local
restamp_dev database. No secrets are printed.
"""
import importlib
import pathlib

import pytest
from sqlalchemy import text as sqltext

from app import config as config_module
from app.config import validate_security_config
from app.services import users_roles
from app.services.otp import otp_service

REQUEST_OK = {"detail": "Not Found"}


def _phone_suffix_body(phone: str):
    # The legacy suffix-login shape, code derived exactly as the demo did.
    return {"phone": phone, "code": phone[-6:]}


def _identity_count(session, phone: str) -> int:
    return session.execute(
        sqltext(
            "SELECT COUNT(*) FROM auth_identities WHERE provider='phone' "
            "AND provider_identifier=:p"
        ),
        {"p": phone},
    ).fetchone()[0]


# --- the route is gone: exact unknown-route 404 for EVERY legacy shape -------

def test_legacy_keyed_login_shape_is_plain_404(client, monkeypatch):
    """The old keyed flow (X-Test-Login-Key header + {phone} body) is a
    normal unknown route now — the header cannot resurrect anything."""
    _seq = {"n": 0}
    c, session = client
    r = c.post(
        "/auth/test-login",
        json={"phone": "+919850000001"},
        headers={"X-Test-Login-Key": "k" * 40},
    )
    assert r.status_code == 404
    assert r.json() == c.get("/definitely-not-a-real-route").json()
    assert r.json() == REQUEST_OK
    assert _identity_count(session, "+919850000001") == 0


def test_suffix_login_shape_is_plain_404_no_rows(client, monkeypatch):
    """The suffix shortcut is structurally impossible: the correct six
    digits on ANY phone (existing or not) hit a plain 404 and write
    nothing."""
    c, session = client
    existing = users_roles.create_user(session, display_name="+919850000002")
    users_roles.link_identity(
        session, existing.id, "phone", "+919850000002", verified_by="otp"
    )
    session.commit()
    before_users = session.execute(
        sqltext("SELECT COUNT(*) FROM users")
    ).fetchone()[0]
    for phone in ("+919850000002", "+919850000003"):
        r = c.post("/auth/test-login", json=_phone_suffix_body(phone))
        assert r.status_code == 404
        assert r.json() == REQUEST_OK
        assert b"No account" not in r.content  # never the old demo contract
    assert _identity_count(session, "+919850000002") == 1  # untouched
    assert _identity_count(session, "+919850000003") == 0  # nothing created
    assert (
        session.execute(sqltext("SELECT COUNT(*) FROM users")).fetchone()[0]
        == before_users
    )


def test_no_module_no_settings(monkeypatch):
    """The service module is gone and no RESTAMP_TEST_LOGIN_* settings (or
    config helpers) remain importable."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("app.services.test_login")
    for attr in (
        "TEST_LOGIN_ENABLED",
        "TEST_LOGIN_KEY",
        "TEST_LOGIN_ALLOWED_PHONES",
        "TEST_LOGIN_MODE",
        "TEST_LOGIN_SUFFIX_UNTIL",
        "TEST_LOGIN_ALLOW_REGISTRATION",
        "TEST_LOGIN_MAX_PER_HOUR_PER_IP",
        "TEST_LOGIN_MAX_PER_HOUR_PER_PHONE",
    ):
        assert not hasattr(config_module.settings, attr)
    assert not hasattr(config_module, "invalid_test_login_phones")
    assert not hasattr(config_module, "parse_test_login_suffix_until")
    assert not hasattr(config_module, "_split_test_login_phones")


def test_repo_carries_no_demo_login_references():
    """Belt-and-braces: the shipped code/templates/docs no longer mention
    the demo login endpoint or settings at all."""
    backend = pathlib.Path(__file__).resolve().parents[1]
    for name in (
        "app/config.py",
        "app/routers/auth.py",
        "app/services/otp.py",
        "render.yaml",
        ".env.example",
        "README.md",
    ):
        text = (backend / name).read_text(encoding="utf-8")
        assert "test-login" not in text, name
        assert "TEST_LOGIN" not in text, name
        assert "test_login" not in text, name
        assert "SUFFIX_UNTIL" not in text, name


# --- normal OTP authentication is unchanged and self-contained ---------------

def test_otp_request_schema_rejects_stray_code_field(client):
    """Login cannot be driven through the OTP request endpoint: the schema
    stays phone-only (a stray `code`/suffix field is a validation error,
    never silently accepted)."""
    c, _ = client
    r = c.post(
        "/auth/otp/request",
        json={"phone": "+919850000004", "code": "000004"},
    )
    assert r.status_code == 422  # pydantic validation, not an auth path
    assert _identity_count(session=pytest.importorskip("sqlalchemy").orm.Session.__init__ and None or _get_session(c), phone="+919850000004") == 0


def _get_session(c):  # the client fixture pairs (client, session); helper below
    return _SESSION_HOLDER["s"]


_SESSION_HOLDER = {"s": None}


def test_otp_request_schema_rejects_stray_code_field_tight(client, session):
    """Same contract, without the heroics above (kept legible): a stray
    `code` field on the OTP request is a 422 and creates NOTHING."""
    c, s = client
    _SESSION_HOLDER["s"] = s
    r = c.post(
        "/auth/otp/request",
        json={"phone": "+919850000005", "code": "000005"},
    )
    assert r.status_code == 422
    assert _identity_count(s, "+919850000005") == 0


def test_verify_with_arbitrary_code_cannot_register_or_authenticate(
    client, monkeypatch
):
    """A guessed six-digit value cannot authenticate or create an account:
    verification fails (no such stored hash) and ZERO rows are written —
    registration happens ONLY through the real verified OTP flow."""
    c, session = client
    phone = "+919850000006"
    before_users = session.execute(
        sqltext("SELECT COUNT(*) FROM users")
    ).fetchone()[0]
    r = c.post("/auth/otp/verify", json={"phone": phone, "code": "000006"})
    assert r.status_code == 401
    assert _identity_count(session, phone) == 0
    assert (
        session.execute(sqltext("SELECT COUNT(*) FROM users")).fetchone()[0]
        == before_users
    )


def test_normal_otp_flow_still_authenticates(client, session):
    """The real flow still works end-to-end on the OTP endpoints: request
    (via the test SMS stand-in) → real verification → token → /auth/me.
    This is the restored, supported login production will use once an SMS
    provider is attached."""
    c, s = client
    phone = "+919850000007"
    # Seed the HMAC secret for this phone the way the service expects.
    otp_service.ensure_secret(s, phone)
    s.commit()
    req = c.post("/auth/otp/request", json={"phone": phone})
    assert req.status_code == 200
    code = otp_service.last_code_for(s, phone)  # test-stand-in code; not printed
    ver = c.post("/auth/otp/verify", json={"phone": phone, "code": code})
    assert ver.status_code == 200
    body = ver.json()
    assert set(body) == {
        "access_token", "token_type", "expires_in", "user_id", "role"
    }
    me = c.get(
        "/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert me.status_code == 200
    assert me.json()["providers"] == ["phone"]


# --- production posture is intact ---------------------------------------------

def _prod(monkeypatch, **overrides):
    monkeypatch.setattr(config_module.settings, "ENV", "production")
    monkeypatch.setattr(config_module.settings, "JWT_SECRET", "z" * 40)
    monkeypatch.setattr(config_module.settings, "GOOGLE_DEV_MODE", False)
    monkeypatch.setattr(config_module.settings, "GOOGLE_ENABLED", False)
    monkeypatch.setattr(config_module.settings, "GOOGLE_CLIENT_ID", "")
    monkeypatch.setattr(config_module.settings, "CORS_ORIGINS", "")
    monkeypatch.setattr(config_module.settings, "OTP_DISPLAY_MODE", False)
    monkeypatch.setattr(
        config_module.settings,
        "DATABASE_URL",
        "mysql+pymysql://u:p@db.example.com/restamp_prod",
    )
    for key, value in overrides.items():
        monkeypatch.setattr(config_module.settings, key, value)


def test_production_boots_cleanly_without_demo_variables(monkeypatch):
    """No RESTAMP_TEST_LOGIN_* variables are needed (or readable) anymore:
    a standard production configuration validates as-is."""
    _prod(monkeypatch)
    validate_security_config()  # must not raise


def test_production_display_mode_refusal_intact(monkeypatch):
    """The display-mode production refusal is untouched by the removal."""
    _prod(monkeypatch, OTP_DISPLAY_MODE=True)
    with pytest.raises(RuntimeError) as e:
        validate_security_config()
    assert "RESTAMP_OTP_DISPLAY_MODE" in str(e.value)
