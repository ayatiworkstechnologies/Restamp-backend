"""Focused tests for ProductionGoogleVerifier (Phase 1, Google Sign-In).

No DB, no network: the JWKS client is stubbed; RSA round-trip tests need the
`cryptography` package and skip cleanly where it is absent.
"""
import types
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import HTTPException

from app import config as config_module
from app.services import google as google_svc

AUD = "test-client-id.apps.googleusercontent.com"
ISS = "https://accounts.google.com"


@pytest.fixture()
def verifier(monkeypatch):
    monkeypatch.setattr(config_module.settings, "GOOGLE_CLIENT_ID", AUD)
    return google_svc.ProductionGoogleVerifier()


def _stub_jwks(monkeypatch, key):
    fake = types.SimpleNamespace(
        get_signing_key_from_jwt=lambda token: types.SimpleNamespace(key=key)
    )
    monkeypatch.setattr(google_svc, "_jwks_client", fake)


def test_malformed_token_is_401(verifier):
    with pytest.raises(HTTPException) as e:
        verifier.verify("not-a-jwt")
    assert e.value.status_code == 401


def test_empty_token_is_401(verifier):
    for bad in ("", None):
        with pytest.raises(HTTPException) as e:
            verifier.verify(bad)
        assert e.value.status_code == 401


def test_decode_wiring_audience_issuer_algorithm(verifier, monkeypatch):
    """The verifier must pass aud/iss/RS256/required-claims to jwt.decode."""
    _stub_jwks(monkeypatch, key="unused")
    seen = {}

    def fake_decode(token, key, algorithms=None, audience=None, issuer=None, options=None):
        seen.update(algorithms=algorithms, audience=audience, issuer=issuer, options=options)
        return {"sub": "google-sub-123"}

    monkeypatch.setattr(jwt, "decode", fake_decode)
    assert verifier.verify("header.payload.sig") == "google-sub-123"
    assert seen["algorithms"] == ["RS256"]
    assert seen["audience"] == AUD
    assert set(seen["issuer"]) == {"accounts.google.com", "https://accounts.google.com"}
    for claim in ("exp", "iss", "aud", "sub"):
        assert claim in seen["options"]["require"]


def _rsa_keypair():
    crypto = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.rsa")
    return crypto.generate_private_key(public_exponent=65537, key_size=2048)


def _rsa_token(key, **claims):
    now = datetime.now(timezone.utc)
    payload = {"iss": ISS, "aud": AUD, "sub": "google-sub-1",
               "exp": now + timedelta(minutes=5), "iat": now}
    payload.update(claims)
    return jwt.encode(payload, key, algorithm="RS256", headers={"kid": "t1"})


def test_rsa_valid_token_returns_sub(verifier, monkeypatch):
    key = _rsa_keypair()
    _stub_jwks(monkeypatch, key.public_key())
    assert verifier.verify(_rsa_token(key)) == "google-sub-1"


def test_rsa_expired_is_401(verifier, monkeypatch):
    key = _rsa_keypair()
    _stub_jwks(monkeypatch, key.public_key())
    token = _rsa_token(key, exp=datetime.now(timezone.utc) - timedelta(minutes=5))
    with pytest.raises(HTTPException) as e:
        verifier.verify(token)
    assert e.value.status_code == 401


def test_rsa_wrong_audience_is_401(verifier, monkeypatch):
    key = _rsa_keypair()
    _stub_jwks(monkeypatch, key.public_key())
    with pytest.raises(HTTPException) as e:
        verifier.verify(_rsa_token(key, aud="other-client"))
    assert e.value.status_code == 401


def test_rsa_wrong_issuer_is_401(verifier, monkeypatch):
    key = _rsa_keypair()
    _stub_jwks(monkeypatch, key.public_key())
    with pytest.raises(HTTPException) as e:
        verifier.verify(_rsa_token(key, iss="https://evil.example.com"))
    assert e.value.status_code == 401


def test_rsa_tampered_signature_is_401(verifier, monkeypatch):
    key = _rsa_keypair()
    _stub_jwks(monkeypatch, key.public_key())
    other = _rsa_keypair()
    _stub_jwks(monkeypatch, other.public_key())
    with pytest.raises(HTTPException) as e:
        verifier.verify(_rsa_token(key))
    assert e.value.status_code == 401


# ---------------------------------------------------------------------------
# POST /auth/google/link (phone-session JWT + verified Google ID token)
# Uses dev-mode "dev:<sub>" tokens (no CLIENT_ID in tests); production
# verification path is covered by the RSA tests above.
# ---------------------------------------------------------------------------

from sqlalchemy import text as _sa_text
from tests.conftest import auth_header as _auth_header


def _phone_user(c, session, tag: str) -> str:
    """Create a phone-registered user directly (no OTP flow dependency) and
    return a Bearer JWT for it, mirroring otp_verify's registration."""
    from app.security import create_access_token

    uid = session.execute(_sa_text(
        "INSERT INTO users (display_name) VALUES (:n)"), {"n": f"Link User {tag}"}
    ).lastrowid
    session.execute(_sa_text(
        "INSERT INTO auth_identities (user_id, provider, provider_identifier) "
        "VALUES (:u, 'phone', :p)"), {"u": uid, "p": f"+100000009{tag}"})
    session.execute(_sa_text(
        "INSERT INTO user_current_role (user_id, role) VALUES (:u, 'BUYER')"),
        {"u": uid})
    token, _ = create_access_token(uid, "BUYER")
    return token


def test_link_happy_path_then_google_login_works(client):
    c, session = client
    token = _phone_user(c, session, "01")
    r = c.post("/auth/google/link", json={"id_token": "dev:g-link-1"},
               headers=_auth_header(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert "google" in body["providers"]
    # The same Google identity now logs in directly.
    r = c.post("/auth/google", json={"id_token": "dev:g-link-1"})
    assert r.status_code == 200, r.text
    assert r.json()["user_id"] == body["id"]


def test_link_invalid_token_401(client):
    c, session = client
    token = _phone_user(c, session, "02")
    r = c.post("/auth/google/link", json={"id_token": "garbage"},
               headers=_auth_header(token))
    assert r.status_code == 401, r.text


def test_link_unauthenticated_401(client):
    c, session = client
    r = c.post("/auth/google/link", json={"id_token": "dev:g-link-x"})
    assert r.status_code == 401, r.text


def test_link_idempotent_same_user(client):
    c, session = client
    token = _phone_user(c, session, "03")
    for _ in range(2):
        r = c.post("/auth/google/link", json={"id_token": "dev:g-link-2"},
                   headers=_auth_header(token))
        assert r.status_code == 200, r.text
    n = session.execute(_sa_text(
        "SELECT COUNT(*) FROM auth_identities WHERE provider='google' "
        # Dev verifier strips the "dev:" prefix; the stored subject is g-link-2.
        "AND provider_identifier='g-link-2'")).fetchone()[0]
    assert n == 1


def test_link_cross_user_409(client):
    c, session = client
    token_a = _phone_user(c, session, "04")
    token_b = _phone_user(c, session, "05")
    r = c.post("/auth/google/link", json={"id_token": "dev:g-link-3"},
               headers=_auth_header(token_a))
    assert r.status_code == 200, r.text
    r = c.post("/auth/google/link", json={"id_token": "dev:g-link-3"},
               headers=_auth_header(token_b))
    assert r.status_code == 409, r.text
    me = c.get("/auth/me", headers=_auth_header(token_b)).json()
    assert "google" not in me["providers"]


def test_me_providers_contains_google_after_link(client):
    c, session = client
    token = _phone_user(c, session, "06")
    before = c.get("/auth/me", headers=_auth_header(token)).json()["providers"]
    assert "google" not in before
    c.post("/auth/google/link", json={"id_token": "dev:g-link-4"},
           headers=_auth_header(token))
    after = c.get("/auth/me", headers=_auth_header(token)).json()["providers"]
    assert "google" in after
