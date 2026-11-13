"""Production fixed-OTP opt-in (RESTAMP_PRODUCTION_FIXED_OTP=1).

Owner decision: with no SMS provider, production may ACCEPT the fixed code
349086 behind an explicit opt-in, while:
- the flag is enabled only by the exact value "1";
- production never returns the code (request -> 200 without debug_code);
- admin accounts cannot sign in with it (same 401 as a wrong code);
- the session is marked amr=fixed_otp and is refused on every admin path,
  on reading other users, and on Google linking — even if the account is
  made admin after the session was issued;
- JWT validation, suspension checks, roles, and every production startup
  check are unchanged; local/staging behaviour is unchanged.

Endpoint tests use the rollback-isolated `client` fixture (local test DB
only). Production is simulated with monkeypatch on the shared settings
object, exactly like tests/test_auth_production.py. No SMS, no Google
network calls.
"""
from __future__ import annotations

import secrets as _secrets
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app import config as config_module
from app.config import (
    fixed_otp_in_production,
    fixed_otp_permitted,
    otp_display_permitted,
    validate_security_config,
)
from app.security import FIXED_OTP_AMR, create_access_token
from app.services import google as google_svc
from app.services import otp as otp_module
from tests.conftest import auth_header, register_phone

CODE = "349086"
PROD_SECRET = "z" * 40


def _prod(monkeypatch, flag="1", **overrides):
    """Pin a valid production config with the opt-in set to `flag`."""
    monkeypatch.setattr(config_module.settings, "ENV", "production")
    monkeypatch.setattr(config_module.settings, "JWT_SECRET", PROD_SECRET)
    monkeypatch.setattr(config_module.settings, "GOOGLE_DEV_MODE", False)
    monkeypatch.setattr(config_module.settings, "GOOGLE_ENABLED", False)
    monkeypatch.setattr(config_module.settings, "GOOGLE_CLIENT_ID", "")
    monkeypatch.setattr(
        config_module.settings, "CORS_ORIGINS", "https://restamp-app.vercel.app"
    )
    monkeypatch.setattr(config_module.settings, "PRODUCTION_FIXED_OTP", flag)
    monkeypatch.delenv("RESTAMP_OTP_DISPLAY_MODE", raising=False)
    for key, value in overrides.items():
        monkeypatch.setattr(config_module.settings, key, value)


def _phone() -> str:
    # Random tail: never collides with persisted rows in a reused test DB.
    return "+9167" + "".join(_secrets.choice("0123456789") for _ in range(8))


def _claims(token: str) -> dict:
    return jwt.decode(token, config_module.settings.JWT_SECRET, algorithms=["HS256"])


def _local_user(c) -> tuple[int, str]:
    """Create an ordinary user through the normal local OTP flow."""
    phone = _phone()
    return register_phone(c, phone)["user_id"], phone


def _grant_admin(session, user_id: int, role: str = "SUPER_ADMIN") -> None:
    session.execute(
        text("INSERT INTO admin_accounts (user_id, admin_role) VALUES (:u, :r)"),
        {"u": user_id, "r": role},
    )
    session.flush()


def _fixed_otp_login(c, phone: str):
    return c.post("/auth/otp/verify", json={"phone": phone, "code": CODE})


class _FakeVerifier:
    def __init__(self, subject: str):
        self.subject = subject

    def verify(self, _token: str) -> str:
        return self.subject


# --- flag parsing -------------------------------------------------------------

@pytest.mark.parametrize(
    "value", ["", "0", "true", "TRUE", "yes", "on", "enabled", " 1", "1 ", "01", "2"]
)
def test_only_exact_one_enables_the_flag(monkeypatch, value):
    _prod(monkeypatch, flag=value)
    assert fixed_otp_in_production() is False
    assert fixed_otp_permitted() is False
    with pytest.raises(HTTPException) as info:
        otp_module.request_code(_phone())
    assert info.value.status_code == 503
    assert otp_module.verify_code(_phone(), CODE) is False


def test_exact_one_enables_acceptance_but_not_display(monkeypatch):
    _prod(monkeypatch, flag="1")
    assert fixed_otp_in_production() is True
    assert fixed_otp_permitted() is True
    assert otp_display_permitted() is False  # unchanged: production never displays


@pytest.mark.parametrize("env", ["local", "development", "dev", "test", "staging"])
def test_flag_is_a_no_op_outside_production(monkeypatch, env):
    monkeypatch.setattr(config_module.settings, "ENV", env)
    monkeypatch.setattr(config_module.settings, "PRODUCTION_FIXED_OTP", "1")
    assert fixed_otp_in_production() is False
    assert otp_display_permitted() is True  # existing local/staging gate
    assert otp_module.request_code(_phone()) == CODE


def test_prod_alias_env_also_opts_in(monkeypatch):
    _prod(monkeypatch, flag="1", ENV="prod")
    assert fixed_otp_in_production() is True


# --- service level ------------------------------------------------------------

def test_service_accepts_only_the_fixed_code(monkeypatch):
    _prod(monkeypatch)
    assert otp_module.request_code(_phone()) is None  # accepted, never displayed
    assert otp_module.verify_code(_phone(), CODE) is True
    assert otp_module.verify_code(_phone(), f" {CODE} ") is True  # existing strip
    for bad in ("", " ", "000000", "349087", "3490860", "34908", "Ł49086"):
        assert otp_module.verify_code(_phone(), bad) is False, bad


# --- endpoint: flag disabled keeps today's production behaviour ----------------

def test_flag_disabled_keeps_503_and_401(client, monkeypatch):
    c, _ = client
    _prod(monkeypatch, flag="0")
    phone = _phone()
    r = c.post("/auth/otp/request", json={"phone": phone})
    assert r.status_code == 503, r.text
    assert CODE not in r.text and "debug_code" not in r.text
    v = _fixed_otp_login(c, phone)
    assert v.status_code == 401 and "access_token" not in v.text


# --- endpoint: flag enabled ---------------------------------------------------

def test_request_returns_200_without_debug_code(client, monkeypatch):
    c, _ = client
    _prod(monkeypatch)
    r = c.post("/auth/otp/request", json={"phone": _phone()})
    assert r.status_code == 200, r.text
    assert r.json() == {"message": "If the number is valid, an OTP was sent."}
    assert CODE not in r.text and "debug_code" not in r.text


def test_request_still_validates_phone(client, monkeypatch):
    c, _ = client
    _prod(monkeypatch)
    r = c.post("/auth/otp/request", json={"phone": "+15550001111"})
    assert r.status_code == 422


def test_new_user_registers_as_buyer_with_fixed_otp_session(client, monkeypatch):
    c, _ = client
    _prod(monkeypatch)
    phone = _phone()
    r = _fixed_otp_login(c, phone)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["role"] == "BUYER"
    claims = _claims(body["access_token"])
    assert claims["amr"] == FIXED_OTP_AMR
    assert claims["type"] == "access" and claims["sub"] == str(body["user_id"])
    me = c.get("/auth/me", headers=auth_header(body["access_token"]))
    assert me.status_code == 200 and me.json()["id"] == body["user_id"]
    assert me.json()["is_admin"] is False


def test_existing_ordinary_user_signs_in_to_same_account(client, monkeypatch):
    c, _ = client
    uid, phone = _local_user(c)
    _prod(monkeypatch)
    r = _fixed_otp_login(c, phone)
    assert r.status_code == 200, r.text
    assert r.json()["user_id"] == uid
    assert _claims(r.json()["access_token"])["amr"] == FIXED_OTP_AMR


def test_wrong_codes_are_rejected(client, monkeypatch):
    c, _ = client
    _prod(monkeypatch)
    # Includes Arabic-Indic digits for 349086: the schema's \d admits them,
    # and they must be a clean 401 (or 422), never a 500.
    for bad in ("٣٤٩٠٨٦",):
        r = c.post("/auth/otp/verify", json={"phone": _phone(), "code": bad})
        assert r.status_code in (401, 422), r.text
    for bad in ("000000", "349087", "123456"):
        r = c.post("/auth/otp/verify", json={"phone": _phone(), "code": bad})
        assert r.status_code == 401 and r.json() == {"detail": "Invalid code"}


def test_ordinary_session_keeps_role_rules(client, monkeypatch):
    c, _ = client
    _prod(monkeypatch)
    h = auth_header(_fixed_otp_login(c, _phone()).json()["access_token"])
    # BUYER cannot use owner endpoints: role authorization is unchanged.
    assert c.get("/owner/listings", headers=h).status_code == 403


# --- admin accounts can never use the fixed OTP --------------------------------

def test_admin_account_cannot_sign_in(client, monkeypatch):
    c, session = client
    uid, phone = _local_user(c)
    _grant_admin(session, uid)
    before = session.execute(text("SELECT COUNT(*) FROM admin_accounts")).scalar()
    _prod(monkeypatch)
    r = _fixed_otp_login(c, phone)
    wrong = c.post("/auth/otp/verify", json={"phone": phone, "code": "000000"})
    assert r.status_code == 401 and "access_token" not in r.text
    # Indistinguishable from a wrong code: the number's admin status never leaks.
    assert r.json() == wrong.json() == {"detail": "Invalid code"}
    after = session.execute(text("SELECT COUNT(*) FROM admin_accounts")).scalar()
    assert after == before


@pytest.mark.parametrize("admin_role", ["SUPER_ADMIN", "SUPPORT_SPECIALIST"])
def test_any_admin_role_is_refused(client, monkeypatch, admin_role):
    c, session = client
    uid, phone = _local_user(c)
    _grant_admin(session, uid, admin_role)
    _prod(monkeypatch)
    assert _fixed_otp_login(c, phone).status_code == 401


def test_fixed_otp_session_never_reaches_admin_even_after_grant(client, monkeypatch):
    c, session = client
    _prod(monkeypatch)
    tok = _fixed_otp_login(c, _phone()).json()
    h = auth_header(tok["access_token"])
    _grant_admin(session, tok["user_id"])  # promoted while the session is live
    for path in ("/admin/me", "/admin/roles", "/admin/users", "/admin/dashboard",
                 "/admin/enquiries", "/admin/audit-logs", "/admin/staff"):
        r = c.get(path, headers=h)
        assert r.status_code == 403, (path, r.text)
        assert r.json() == {"detail": "Forbidden"}, path
    r = c.post("/admin/staff", json={"user_id": tok["user_id"], "admin_role": "SUPER_ADMIN"},
               headers=h)
    assert r.status_code == 403


def test_fixed_otp_session_cannot_read_other_users(client, monkeypatch):
    c, session = client
    other_uid, _ = _local_user(c)
    _prod(monkeypatch)
    tok = _fixed_otp_login(c, _phone()).json()
    h = auth_header(tok["access_token"])
    _grant_admin(session, tok["user_id"])
    assert c.get(f"/users/{other_uid}", headers=h).status_code == 403
    assert c.get(f"/users/{tok['user_id']}", headers=h).status_code == 200  # self is fine


def test_fixed_otp_session_cannot_link_google(client, monkeypatch):
    c, _ = client
    _prod(monkeypatch)

    def _must_not_verify():
        raise AssertionError("Google verification must not run for fixed-OTP sessions")

    monkeypatch.setattr(google_svc, "get_verifier", _must_not_verify)
    h = auth_header(_fixed_otp_login(c, _phone()).json()["access_token"])
    r = c.post("/auth/google/link", json={"id_token": "anything"}, headers=h)
    assert r.status_code == 403 and r.json() == {"detail": "Forbidden"}


# --- ordinary (non-fixed-OTP) sessions keep their behaviour ---------------------

def test_normal_admin_session_still_works_with_flag_on(client, monkeypatch):
    c, session = client
    uid, _ = _local_user(c)
    other_uid, _ = _local_user(c)
    _grant_admin(session, uid)
    _prod(monkeypatch)
    token, _ = create_access_token(uid, "BUYER")  # no amr: e.g. a Google login
    h = auth_header(token)
    assert c.get("/admin/me", headers=h).status_code == 200
    assert c.get("/admin/users", headers=h).status_code == 200
    assert c.get(f"/users/{other_uid}", headers=h).status_code == 200


def test_google_link_and_login_unchanged_for_normal_sessions(client, monkeypatch):
    c, session = client
    uid, _ = _local_user(c)
    _grant_admin(session, uid)
    _prod(monkeypatch)
    subject = "google-sub-" + _secrets.token_hex(6)
    monkeypatch.setattr(google_svc, "get_verifier", lambda: _FakeVerifier(subject))
    token, _ = create_access_token(uid, "BUYER")
    r = c.post("/auth/google/link", json={"id_token": "t"}, headers=auth_header(token))
    assert r.status_code == 200, r.text
    assert "google" in r.json()["providers"]
    g = c.post("/auth/google", json={"id_token": "t"})
    assert g.status_code == 200, g.text
    assert "amr" not in _claims(g.json()["access_token"])
    assert c.get("/admin/me", headers=auth_header(g.json()["access_token"])).status_code == 200


# --- token and account checks are unchanged ----------------------------------

def test_suspended_account_cannot_sign_in_or_act(client, monkeypatch):
    c, session = client
    uid, phone = _local_user(c)
    _prod(monkeypatch)
    live = _fixed_otp_login(c, phone).json()["access_token"]
    session.execute(text("UPDATE users SET status='suspended' WHERE id=:u"), {"u": uid})
    session.flush()
    r = _fixed_otp_login(c, phone)
    assert r.status_code == 403 and r.json() == {"detail": "Account disabled"}
    assert c.get("/auth/me", headers=auth_header(live)).status_code == 403


def _forge(claims: dict, secret: str = PROD_SECRET) -> str:
    return jwt.encode(claims, secret, algorithm="HS256")


def test_expired_malformed_and_tampered_tokens_rejected(client, monkeypatch):
    c, session = client
    uid, _ = _local_user(c)
    _grant_admin(session, uid)
    _prod(monkeypatch)
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    future = datetime.now(timezone.utc) + timedelta(minutes=10)
    expired = _forge({"sub": str(uid), "type": "access", "exp": past})
    r = c.get("/admin/me", headers=auth_header(expired))
    assert r.status_code == 401 and r.json() == {"detail": "Token expired"}
    for bad in ("not-a-jwt", "a.b.c", ""):
        assert c.get("/admin/me", headers=auth_header(bad)).status_code == 401
    # Stripping amr from a fixed-OTP token requires re-signing: wrong key -> 401.
    stripped = _forge({"sub": str(uid), "type": "access", "exp": future}, secret="x" * 40)
    assert c.get("/admin/me", headers=auth_header(stripped)).status_code == 401
    # Wrong token type is still refused.
    wrong_type = _forge({"sub": str(uid), "type": "refresh", "exp": future})
    assert c.get("/admin/me", headers=auth_header(wrong_type)).status_code == 401
    # No token at all.
    assert c.get("/admin/me").status_code == 401


@pytest.mark.parametrize("amr", [["fixed_otp"], {"m": 1}, 1, "", "otp"])
def test_any_amr_marker_is_restricted_fail_closed(client, monkeypatch, amr):
    c, session = client
    uid, _ = _local_user(c)
    _grant_admin(session, uid)
    _prod(monkeypatch)
    future = datetime.now(timezone.utc) + timedelta(minutes=10)
    tok = _forge({"sub": str(uid), "type": "access", "exp": future, "amr": amr})
    assert c.get("/admin/me", headers=auth_header(tok)).status_code == 403


# --- startup validation is not weakened ----------------------------------------

def test_valid_prod_config_with_flag_passes_and_warns(monkeypatch, caplog):
    _prod(monkeypatch)
    with caplog.at_level("WARNING", logger="restamp.config"):
        validate_security_config()
    assert any("RESTAMP_PRODUCTION_FIXED_OTP=1" in m for m in caplog.messages)
    assert not any(CODE in m for m in caplog.messages)  # never logs the code


def test_flag_does_not_bypass_weak_jwt(monkeypatch):
    _prod(monkeypatch, JWT_SECRET="short")
    with pytest.raises(RuntimeError, match="RESTAMP_JWT_SECRET"):
        validate_security_config()


@pytest.mark.parametrize("cors", ["*", "", "http://restamp-app.vercel.app",
                                  "https://localhost:3000"])
def test_flag_does_not_bypass_cors_rules(monkeypatch, cors):
    _prod(monkeypatch, CORS_ORIGINS=cors)
    with pytest.raises(RuntimeError, match="RESTAMP_CORS_ORIGINS"):
        validate_security_config()


def test_flag_does_not_bypass_google_dev_mode(monkeypatch):
    _prod(monkeypatch, GOOGLE_DEV_MODE=True)
    with pytest.raises(RuntimeError, match="RESTAMP_GOOGLE_DEV_MODE"):
        validate_security_config()


def test_flag_does_not_bypass_obsolete_display_flag(monkeypatch):
    _prod(monkeypatch)
    monkeypatch.setenv("RESTAMP_OTP_DISPLAY_MODE", "1")
    with pytest.raises(RuntimeError, match="RESTAMP_OTP_DISPLAY_MODE"):
        validate_security_config()


def test_flag_does_not_bypass_missing_database_url(monkeypatch):
    _prod(monkeypatch, DATABASE_URL="")
    with pytest.raises(RuntimeError, match="RESTAMP_DATABASE_URL"):
        validate_security_config()


# --- local and staging behaviour unchanged ---------------------------------------

def test_local_flow_unchanged_and_unmarked(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(config_module.settings, "PRODUCTION_FIXED_OTP", "1")
    phone = _phone()
    r = c.post("/auth/otp/request", json={"phone": phone})
    assert r.status_code == 200 and r.json()["debug_code"] == CODE
    tok = _fixed_otp_login(c, phone).json()["access_token"]
    assert "amr" not in _claims(tok)


def test_staging_flow_unchanged_and_unmarked(client, monkeypatch):
    c, session = client
    uid, phone = _local_user(c)
    _grant_admin(session, uid)
    _prod(monkeypatch, flag="0", ENV="staging")
    r = c.post("/auth/otp/request", json={"phone": phone})
    assert r.status_code == 200 and r.json()["debug_code"] == CODE
    v = _fixed_otp_login(c, phone)  # staging keeps its existing admin behaviour
    assert v.status_code == 200 and "amr" not in _claims(v.json()["access_token"])
