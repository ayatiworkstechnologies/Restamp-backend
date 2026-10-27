"""Restricted test-only login (/auth/test-login) contract and guards.

Verified here (no SMS provider in use, and NO OTP code is ever generated,
stored, returned, or logged by this mechanism):

- disabled by default in EVERY configuration — inactive replies are
  byte-identical to FastAPI's unknown-route 404 ({"detail": "Not Found"});
- active requires BOTH RESTAMP_TEST_LOGIN_ENABLED=1 AND a non-empty
  RESTAMP_TEST_LOGIN_KEY (enabled-without-key stays inactive, fail closed);
- the key arrives via the X-Test-Login-Key header and is compared in
  constant time; wrong or missing keys get a generic 401 with NO
  phone-existence oracle;
- the request body is parsed by hand AFTER the activation guard: a
  disabled endpoint answers the exact unknown-route 404 for EVERY body
  shape (valid, schema-invalid, or unparseable — never a 422), and an
  active one answers malformed bodies with the SAME generic 401 as a
  wrong key (never a field-level validation error, and malformed
  requests never consume rate budget);
- the phone must be on the explicit RESTAMP_TEST_LOGIN_ALLOWED_PHONES
  allowlist: fail closed when the list is EMPTY (every phone rejected),
  any phone outside the list is rejected with a generic 403 EVEN WITH a
  valid key, the list is the ONLY permission to register an unknown test
  number, and membership is checked AFTER the key comparison (never an
  unauthenticated oracle) and BEFORE any database access;
- attempts are rate limited per IP and per phone BEFORE the key check
  (wrong-key attempts consume budget and trip the same caps), blocking
  for OTP_BLOCK_SECONDS after the cap;
- registration is a SEPARATE feature flag: existing allowlisted user rows
  (any verified_by) log in unchanged and are never recreated, while an
  allowlisted UNKNOWN phone creates a new BUYER (verified_by="test_login")
  ONLY when RESTAMP_TEST_LOGIN_ALLOW_REGISTRATION=1 — with the flag off
  (the default) the attempt gets the SAME generic 401 body as a wrong key
  and not a single row is created; token issuance is identical to
  /auth/otp/verify (TokenOut);
- production defaults stay safe: the flag-off state boots and answers
  404; flag-on with a missing/weak key REFUSES startup; malformed
  allowed-phone entries REFUSE production boot; an empty allowlist boots
  but warns and rejects every attempt at request time;
- independence from the OTP display mode in BOTH directions, and the
  OTP service state is never touched by a test login.

Endpoint tests use the rollback-isolated `client` fixture on the local
restamp_dev database; unit tests use isolated services/settings only.
No network, no SMS, no secrets printed.
"""
import logging
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import text as sqltext

from app import config as config_module
from app.config import (
    invalid_test_login_phones,
    is_production,
    parse_test_login_suffix_until,
    validate_security_config,
    _split_test_login_phones,
)
from app.services import test_login as test_login_module
from app.services import users_roles
from app.services.otp import otp_service
from app.services.test_login import test_login_service

KEY = "t" * 40
REQUEST_404 = {"detail": "Not Found"}
UNAUTHORIZED = {"detail": "Unauthorized"}
FORBIDDEN = {"detail": "Forbidden"}
TOO_MANY = {"detail": "Too many attempts. Try again later."}
_seq = {"n": 0}


def _phone() -> str:
    _seq["n"] += 1
    return f"+919850000{_seq['n']:03d}"


def _enable(monkeypatch, key: str | None = KEY) -> None:
    monkeypatch.setattr(config_module.settings, "TEST_LOGIN_ENABLED", True)
    monkeypatch.setattr(config_module.settings, "TEST_LOGIN_KEY", key or "")


def _disable(monkeypatch) -> None:
    monkeypatch.setattr(config_module.settings, "TEST_LOGIN_ENABLED", False)
    monkeypatch.setattr(config_module.settings, "TEST_LOGIN_KEY", "")


def _allow(monkeypatch, phones) -> None:
    monkeypatch.setattr(
        config_module.settings, "TEST_LOGIN_ALLOWED_PHONES", tuple(phones)
    )


def _allow_registration(monkeypatch, on: bool = True) -> None:
    monkeypatch.setattr(
        config_module.settings, "TEST_LOGIN_ALLOW_REGISTRATION", on
    )


def _login(c, phone: str, key: str | None = KEY):
    headers = {} if key is None else {"X-Test-Login-Key": key}
    return c.post("/auth/test-login", json={"phone": phone}, headers=headers)


def _seed_phone_user(session, phone: str, verified_by: str) -> int:
    """Create an existing phone user directly (as a prior real login would
    have); returns the user id."""
    user = users_roles.create_user(session, display_name=phone)
    users_roles.link_identity(
        session, user.id, "phone", phone, verified_by=verified_by
    )
    session.commit()
    return user.id


def _identity_count(session, phone: str) -> int:
    return session.execute(
        sqltext(
            "SELECT COUNT(*) FROM auth_identities WHERE provider='phone' "
            "AND provider_identifier=:p"
        ),
        {"p": phone},
    ).fetchone()[0]


def _prod(monkeypatch, **overrides):
    """Pin production env with a strong JWT and quiet dev flags."""
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


# --- defaults: disabled and indistinguishable from a missing route ----------

def test_disabled_by_default_answers_unknown_route_404(client, monkeypatch):
    _disable(monkeypatch)
    c, _ = client
    r = _login(c, _phone())
    assert r.status_code == 404
    assert r.json() == c.get("/definitely-not-a-real-route").json()
    assert r.json() == REQUEST_404


def test_enabled_without_key_stays_inactive(client, monkeypatch):
    _enable(monkeypatch, key="")
    c, _ = client
    assert test_login_service.is_active() is False
    r = _login(c, _phone())
    assert r.status_code == 404
    assert r.json() == REQUEST_404


# --- request-shape guards: disabled = exact 404 for ANY body; active = generic 401 ---

def test_disabled_endpoint_404_even_for_malformed_bodies(client, monkeypatch):
    """When inactive, EVERY body — valid, schema-invalid, or unparseable —
    gets the exact unknown-route 404 (never FastAPI's 422), so malformed
    probes cannot reveal that the route exists. The body is not even read:
    the activation guard runs first."""
    _disable(monkeypatch)
    c, _ = client
    reference = c.get("/definitely-not-a-real-route")
    assert reference.status_code == 404
    for raw in (
        b'{"phone": "+919850000001"}',  # a perfectly valid body
        b'{"phone": "123"}',            # schema-invalid phone
        b'{"other": 1}',                # missing phone field
        b"[1, 2, 3]",                   # valid JSON, wrong shape
        b"not json at all",             # unparseable bytes
        b"",                            # no body at all
    ):
        r = c.post(
            "/auth/test-login",
            content=raw,
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 404, raw
        assert r.content == reference.content  # byte-identical, any shape
        assert r.json() == REQUEST_404


def test_enabled_malformed_body_is_generic_401(client, monkeypatch):
    """When active, EVERY malformed/unparseable/wrong-shaped body gets the
    SAME byte-identical generic 401 as a wrong key — never a field-level
    422 — so request shape cannot probe whether the feature is on, and no
    schema detail ever leaves the server.
    """
    _enable(monkeypatch)
    c, _ = client
    wrong = _login(c, _phone(), key="w" * 40)
    assert wrong.status_code == 401
    for raw in (
        b'{"phone": "123"}',      # wrong phone format
        b'{"other": 1}',          # missing phone field
        b"[1, 2, 3]",             # valid JSON, wrong shape
        b'"a bare string"',       # valid JSON, wrong shape
        b"not json at all",       # unparseable bytes
        b"",                      # no body at all
    ):
        r = c.post(
            "/auth/test-login",
            content=raw,
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 401, raw
        assert r.content == wrong.content  # indistinguishable from bad key
        assert r.json() == UNAUTHORIZED


def test_malformed_bodies_never_burn_rate_budget(client, monkeypatch):
    """Deliberate ordering: body parsing runs BEFORE the attempt counter,
    so unauthenticated malformed-body garbage cannot consume the per-IP
    budget (and, being uniformly 401, learns nothing either)."""
    _enable(monkeypatch)
    monkeypatch.setattr(
        config_module.settings, "TEST_LOGIN_MAX_PER_HOUR_PER_IP", 1
    )
    monkeypatch.setattr(
        config_module.settings, "TEST_LOGIN_MAX_PER_HOUR_PER_PHONE", 99
    )
    c, _ = client
    for _ in range(5):
        r = c.post(
            "/auth/test-login",
            content=b"garbage",
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 401
    # The single budget slot is untouched: a real attempt still succeeds.
    phone = _phone()
    _allow(monkeypatch, (phone,))
    _allow_registration(monkeypatch)
    assert _login(c, phone).status_code == 200


# --- the allowlist: fail closed, exact, not an oracle ------------------------

def test_active_with_empty_allowlist_fails_closed_rejects_login(
    client, monkeypatch
):
    """Enabled + key + EMPTY allowlist: the endpoint stays active but
    rejects EVERY phone (fail closed), while it never regresses to 404
    (that would suggest inactivity, hiding the misconfiguration)."""
    _enable(monkeypatch)
    _allow(monkeypatch, ())  # default posture: an empty allowlist
    c, _ = client
    assert test_login_service.is_active() is True
    first = _login(c, _phone())
    second = _login(c, _phone())
    assert first.status_code == second.status_code == 403
    assert first.json() == second.json() == FORBIDDEN


def test_disallowed_phone_with_valid_key_rejected_no_side_effects(
    client, monkeypatch
):
    """An existing user whose phone is NOT allowlisted and a completely
    unknown phone are rejected IDENTICALLY (both 403) even with a valid
    key — no existence oracle, no rows created or mutated."""
    _enable(monkeypatch)
    c, session = client
    seeded = _phone()
    seeded_id = _seed_phone_user(session, seeded, verified_by="otp")
    unknown = "+919859000999"
    _allow(monkeypatch, (_phone(),))  # neither seeded nor unknown present
    for attempt_phone in (seeded, unknown):
        r = _login(c, attempt_phone)
        assert r.status_code == 403
        assert r.json() == FORBIDDEN
        assert attempt_phone not in r.text
    # Nothing changed on the DB side for either phone:
    assert _identity_count(session, seeded) == 1
    assert _identity_count(session, unknown) == 0
    # And the seeded user is untouched (verified_by preserved):
    row = session.execute(
        sqltext(
            "SELECT i.verified_by FROM auth_identities i "
            "WHERE i.provider='phone' AND i.provider_identifier=:p"
        ),
        {"p": seeded},
    ).fetchone()
    assert row[0] == "otp"


def test_phone_with_valid_key_attempts_do_not_create_users(client, monkeypatch):
    """A valid key can NEVER turn a disallowed phone into an account:
    exactly the allowlisted registration is possible."""
    _enable(monkeypatch)
    c, session = client
    _allow(monkeypatch, ("+916000000000",))
    r = _login(c, "+919870000111")  # well-formed but NOT allowlisted
    assert r.status_code == 403
    assert _identity_count(session, "+919870000111") == 0
    assert otp_service._codes == {}


def test_wrong_key_on_allowed_phone_is_still_401(
    client, monkeypatch
):
    """Ordering contract: the key comparison precedes the allowlist, so a
    wrong key cannot probe allowlist membership (same 401 for any phone)."""
    _enable(monkeypatch)
    c, _ = client
    allowed = _phone()
    _allow(monkeypatch, (allowed,))
    r = _login(c, allowed, key="w" * 40)
    assert r.status_code == 401
    assert r.json() == UNAUTHORIZED


# --- registration: a separate, default-off feature flag ----------------------

def test_unknown_allowlisted_phone_registration_off_rejected_no_rows(
    client, monkeypatch
):
    """REQUIRED default posture: valid key + allowlisted phone +
    RESTAMP_TEST_LOGIN_ALLOW_REGISTRATION=0. An EXISTING allowlisted
    account would log in, but an UNKNOWN phone must NOT create one:
    rejected with the same generic 401 as a wrong key, zero rows, no OTP
    state, and no oracle in the response."""
    _enable(monkeypatch)
    _allow_registration(monkeypatch, False)
    c, session = client
    phone = _phone()
    _allow(monkeypatch, (phone,))
    first = _login(c, phone)
    assert first.status_code == 401
    assert first.json() == UNAUTHORIZED
    assert phone not in first.text
    # Retrying cannot change the outcome or smuggle state in:
    second = _login(c, phone)
    assert second.status_code == 401
    assert second.json() == UNAUTHORIZED
    assert _identity_count(session, phone) == 0
    assert otp_service._codes == {}
    assert otp_service.delivery.codes == {}


def test_registration_off_401_indistinguishable_from_bad_key(
    client, monkeypatch
):
    """No account-existence oracle: with registration off, a VALID key on
    an UNKNOWN allowlisted phone and a WRONG key on a REGISTERED
    allowlisted phone produce byte-identical generic 401 responses."""
    _enable(monkeypatch)
    _allow_registration(monkeypatch, False)
    c, session = client
    known = _phone()
    _seed_phone_user(session, known, verified_by="otp")
    unknown = _phone()
    _allow(monkeypatch, (known, unknown))
    unknown_valid_key = _login(c, unknown)  # key VALID, phone not registered
    known_wrong_key = _login(c, known, key="w" * 40)  # registered, key WRONG
    assert unknown_valid_key.status_code == known_wrong_key.status_code == 401
    assert unknown_valid_key.content == known_wrong_key.content
    assert unknown_valid_key.json() == known_wrong_key.json() == UNAUTHORIZED
    # And nothing was created for the unknown phone:
    assert _identity_count(session, unknown) == 0


def test_registration_on_registers_unknown_allowlisted_phone(
    client, monkeypatch
):
    """Flag "1" + allowlisted unknown phone: the ONLY state in which the
    endpoint creates an account — registered as BUYER with the audit
    marker verified_by="test_login"."""
    _enable(monkeypatch)
    _allow_registration(monkeypatch, True)
    c, session = client
    phone = _phone()
    _allow(monkeypatch, (phone,))
    r = _login(c, phone)
    assert r.status_code == 200
    assert r.json()["role"] == "BUYER"
    row = session.execute(
        sqltext(
            "SELECT i.verified_by FROM auth_identities i "
            "WHERE i.provider='phone' AND i.provider_identifier=:p"
        ),
        {"p": phone},
    ).fetchone()
    assert row[0] == "test_login"


def test_registration_on_cannot_rescue_disallowed_phone(
    client, monkeypatch
):
    """The registration flag does NOT bypass the allowlist: with the flag
    "1" and a valid key, a phone outside the list still gets the generic
    403 and no rows."""
    _enable(monkeypatch)
    _allow_registration(monkeypatch, True)
    c, session = client
    _allow(monkeypatch, (_phone(),))  # allowlist holds some OTHER phone
    outsider = "+919870000222"
    r = _login(c, outsider)
    assert r.status_code == 403
    assert r.json() == FORBIDDEN
    assert _identity_count(session, outsider) == 0


# --- activation requires flag + key (allowlist is a runtime guard) ----------

def test_flag_on_key_on_and_allowlist_is_active(client, monkeypatch):
    _enable(monkeypatch)
    phone = _phone()
    _allow(monkeypatch, (phone,))
    _allow_registration(monkeypatch)  # unknown phone registers here
    c, _ = client
    try:
        assert test_login_service.is_active() is True
        assert _login(c, phone).status_code == 200
    finally:
        test_login_service.reset()


# --- authorization: constant-time key, generic 401, no existence oracle -----

def test_wrong_and_missing_keys_401_generic_without_oracle(client, monkeypatch):
    _enable(monkeypatch)
    c, session = client
    known = _phone()
    unknown = "+919859000999"
    for attempt_phone in (known, unknown):
        missing = _login(c, attempt_phone, key=None)
        wrong = _login(c, attempt_phone, key="w" * 40)
        assert missing.status_code == wrong.status_code == 401
        assert missing.json() == wrong.json() == UNAUTHORIZED
        assert attempt_phone not in missing.text
        assert attempt_phone not in wrong.text
        # A wrong key must not create users or identities (no side effects):
        assert _identity_count(session, attempt_phone) == 0


def test_key_comparison_strips_configured_value_only(client, monkeypatch):
    """Whitespace in the DASHBOARD value is tolerated; the candidate must
    match exactly (no automatic trimming of what clients send)."""
    monkeypatch.setattr(config_module.settings, "TEST_LOGIN_ENABLED", True)
    monkeypatch.setattr(config_module.settings, "TEST_LOGIN_KEY", f"  {KEY}  ")
    c, _ = client
    phone = _phone()
    _allow(monkeypatch, (phone,))
    _allow_registration(monkeypatch)
    assert _login(c, phone, key=KEY).status_code == 200
    assert _login(c, _phone(), key=f" {KEY} ").status_code == 401


# --- token issuance, registration, and audit trail ---------------------------

def test_authorized_login_registers_and_issues_token(client, monkeypatch):
    _enable(monkeypatch)
    c, session = client
    phone = _phone()
    _allow(monkeypatch, (phone,))
    _allow_registration(monkeypatch)  # registration ON: unknown phone may join
    r = _login(c, phone)
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {
        "access_token", "token_type", "expires_in", "user_id", "role"
    }
    assert body["role"] == "BUYER"
    # The registered identity is auditable as a TEST login (not a real OTP):
    row = session.execute(
        sqltext(
            "SELECT u.display_name, i.verified_by FROM auth_identities i "
            "JOIN users u ON u.id = i.user_id "
            "WHERE i.provider='phone' AND i.provider_identifier=:p"
        ),
        {"p": phone},
    ).fetchone()
    assert row[0] == phone
    assert row[1] == "test_login"
    # A repeat login reuses the SAME user (no duplicate identities):
    r2 = _login(c, phone)
    assert r2.status_code == 200
    assert r2.json()["user_id"] == body["user_id"]
    assert _identity_count(session, phone) == 1


def test_existing_allowlisted_user_logs_in_without_recreating(
    client, monkeypatch
):
    """An EXISTING phone user (e.g. verified by a real OTP earlier) logs in
    on the allowlist without any new identity or audit-trail overwrite —
    with REGISTRATION DISABLED (the default posture): the flag can only
    ever permit NEW accounts, never rewrite existing rows."""
    _enable(monkeypatch)
    c, session = client
    phone = _phone()
    seeded_id = _seed_phone_user(session, phone, verified_by="otp")
    _allow(monkeypatch, (phone,))
    _allow_registration(monkeypatch, False)  # the default posture, pinned
    r = _login(c, phone)
    assert r.status_code == 200
    assert r.json()["role"] == "BUYER"
    assert r.json()["user_id"] == seeded_id
    assert _identity_count(session, phone) == 1
    row = session.execute(
        sqltext(
            "SELECT i.verified_by FROM auth_identities i "
            "WHERE i.provider='phone' AND i.provider_identifier=:p"
        ),
        {"p": phone},
    ).fetchone()
    assert row[0] == "otp"  # the existing audit trail was NOT rewritten


def test_authorized_login_generates_no_otp_state(client, monkeypatch):
    """The flow must never create OTP state: no code exists for the phone
    even after a successful test login (nothing can leak — nothing exists)."""
    _enable(monkeypatch)
    c, _ = client
    phone = _phone()
    _allow(monkeypatch, (phone,))
    _allow_registration(monkeypatch)
    assert _login(c, phone).status_code == 200
    assert otp_service._codes == {}
    assert otp_service.delivery.codes == {}  # fixture inbox untouched
    # The strict OTP path is unchanged and independent: with the fixture
    # stand-in removed, the public OTP endpoints still fail closed.
    monkeypatch.setattr(otp_service, "delivery", None)
    otp_req = c.post("/auth/otp/request", json={"phone": _phone()})
    assert otp_req.status_code == 503
    verify = c.post(
        "/auth/otp/verify", json={"phone": phone, "code": "000000"}
    )
    assert verify.status_code == 401
    assert otp_service._codes == {}


def test_display_mode_independence_both_directions(client, monkeypatch):
    """Enabling one mechanism never enables the other (separate flags &
    state): display mode ON does not activate test login; test login ON
    does not activate the display channel."""
    # Direction 1: display ON, test login OFF → /auth/test-login stays 404.
    _disable(monkeypatch)
    monkeypatch.setattr(config_module.settings, "OTP_DISPLAY_MODE", True)
    c, _ = client
    assert _login(c, _phone()).status_code == 404
    # Direction 2: test login ON, display OFF → login via test-login works
    # while the OTP request path still never returns a code.
    monkeypatch.setattr(config_module.settings, "OTP_DISPLAY_MODE", False)
    _enable(monkeypatch)
    phone = _phone()
    _allow(monkeypatch, (phone,))
    _allow_registration(monkeypatch)
    assert _login(c, phone).status_code == 200
    monkeypatch.setattr(otp_service, "delivery", None)
    sent = c.post("/auth/otp/request", json={"phone": phone})
    assert sent.status_code == 503
    assert "debug_code" not in sent.text
    assert otp_service._codes == {}


# --- rate limiting: per-IP and per-phone caps, block windows ------------------

def test_per_ip_attempt_cap_blocks_before_key_check(client, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(
        config_module.settings, "TEST_LOGIN_MAX_PER_HOUR_PER_IP", 3
    )
    monkeypatch.setattr(
        config_module.settings, "TEST_LOGIN_MAX_PER_HOUR_PER_PHONE", 99
    )
    c, _ = client
    for _ in range(3):
        # Each failed attempt (missing key) burns IP budget:
        assert _login(c, _phone(), key=None).status_code == 401
    # 4th attempt from the same client IP is blocked (even with the right key):
    r = _login(c, _phone())
    assert r.status_code == 429
    assert r.json() == TOO_MANY
    # And blocked attempts happen BEFORE the key comparison: a wrong key gets
    # the same 429, not a 401 (no distinction between blocked callers).
    wrong = _login(c, _phone(), key="w" * 40)
    assert wrong.status_code == 429


def test_per_phone_attempt_cap_blocks(client, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(
        config_module.settings, "TEST_LOGIN_MAX_PER_HOUR_PER_PHONE", 2
    )
    monkeypatch.setattr(
        config_module.settings, "TEST_LOGIN_MAX_PER_HOUR_PER_IP", 99
    )
    c, _ = client
    phone = _phone()
    other = _phone()
    _allow(monkeypatch, (phone, other))
    _allow_registration(monkeypatch)  # unknown phones register here
    assert _login(c, phone).status_code == 200
    assert _login(c, phone).status_code == 200
    r = _login(c, phone)
    assert r.status_code == 429
    # A DIFFERENT phone is unaffected by the blocked phone (its own window):
    assert _login(c, other).status_code == 200
    assert r.json() == TOO_MANY


# --- leak-proof behavior ------------------------------------------------------

def test_no_key_or_phone_ever_logged(client, monkeypatch, caplog):
    _enable(monkeypatch)
    c, _ = client
    phone = _phone()
    other = "+919859000999"  # not allowlisted
    _allow(monkeypatch, (phone,))
    _allow_registration(monkeypatch)
    with caplog.at_level(logging.DEBUG):
        assert _login(c, phone, key=None).status_code == 401
        assert _login(c, phone, key="w" * 40).status_code == 401
        assert _login(c, phone).status_code == 200
        assert _login(c, other).status_code == 403
    joined = "\n".join(rec.getMessage() for rec in caplog.records)
    assert KEY not in joined
    assert phone not in joined
    assert phone.lstrip("+") not in joined
    assert other not in joined


# --- service-level units: allowlist semantics --------------------------------

def test_service_require_phone_allowed_semantics(monkeypatch):
    """Fail closed on empty; exact membership; accepts tuple/set/list."""
    svc = test_login_module.TestLoginService()
    monkeypatch.setattr(
        config_module.settings, "TEST_LOGIN_ALLOWED_PHONES", ()
    )
    with pytest.raises(HTTPException) as e:
        svc.require_phone_allowed(_phone())
    assert e.value.status_code == 403
    allowed = _phone()
    for representation in ((allowed,), {allowed}, [allowed]):
        monkeypatch.setattr(
            config_module.settings, "TEST_LOGIN_ALLOWED_PHONES", representation
        )
        svc.require_phone_allowed(allowed)  # no raise
        with pytest.raises(HTTPException) as other:
            svc.require_phone_allowed(_phone())  # exact match only
        assert other.value.status_code == 403


def test_service_registration_gate_semantics(monkeypatch):
    """Existing identities pass regardless of the flag; an unknown phone
    passes ONLY with RESTAMP_TEST_LOGIN_ALLOW_REGISTRATION=1 — otherwise
    the SAME generic 401 as a bad key (no new status, no new detail, no
    oracle)."""
    svc = test_login_module.TestLoginService()
    existing = object()  # stands in for any loaded identity row
    svc.require_registration_allowed(existing)  # never trips, flag-agnostic
    monkeypatch.setattr(
        config_module.settings, "TEST_LOGIN_ALLOW_REGISTRATION", False
    )
    svc.require_registration_allowed(existing)  # still passes
    with pytest.raises(HTTPException) as e:
        svc.require_registration_allowed(None)
    assert e.value.status_code == 401
    assert e.value.detail == "Unauthorized"
    # Flag on: an UNKNOWN phone passes through (the router then registers it
    # — and only the allowlist check before this guard constrained it).
    monkeypatch.setattr(
        config_module.settings, "TEST_LOGIN_ALLOW_REGISTRATION", True
    )
    svc.require_registration_allowed(None)  # no raise


# --- service-level units: exact caps, time windows, reset ---------------------

def test_service_ip_window_and_old_timestamps_are_trimmed(monkeypatch):
    monkeypatch.setattr(
        config_module.settings, "TEST_LOGIN_MAX_PER_HOUR_PER_IP", 3
    )
    monkeypatch.setattr(
        config_module.settings, "TEST_LOGIN_MAX_PER_HOUR_PER_PHONE", 99
    )
    monkeypatch.setattr(config_module.settings, "TEST_LOGIN_ENABLED", True)
    monkeypatch.setattr(config_module.settings, "TEST_LOGIN_KEY", KEY)
    svc = test_login_module.TestLoginService()
    # Two stale (older than an hour) IP attempts must not count:
    stale = time.time() - 7200
    svc._ip_requests["203.0.113.9"] = [stale, stale]
    for _ in range(3):
        svc.record_attempt(ip="203.0.113.9", phone=_phone())
    with pytest.raises(HTTPException) as e:
        svc.record_attempt(ip="203.0.113.9", phone=_phone())
    assert e.value.status_code == 429
    # A blocked IP refuses everything; other IPs are unaffected:
    with pytest.raises(HTTPException) as blocked:
        svc.record_attempt(ip="203.0.113.9", phone=_phone())
    assert blocked.value.status_code == 429
    svc.record_attempt(ip="198.51.100.7", phone=_phone())
    # A phone from a blocked IP with no IP given (None) is still allowed:
    svc.record_attempt(ip=None, phone=_phone())


def test_service_phone_window_blocks_only_that_phone(monkeypatch):
    monkeypatch.setattr(
        config_module.settings, "TEST_LOGIN_MAX_PER_HOUR_PER_PHONE", 2
    )
    monkeypatch.setattr(
        config_module.settings, "TEST_LOGIN_MAX_PER_HOUR_PER_IP", 99
    )
    svc = test_login_module.TestLoginService()
    phone = _phone()
    svc.record_attempt(ip=None, phone=phone)
    svc.record_attempt(ip=None, phone=phone)
    with pytest.raises(HTTPException) as e:
        svc.record_attempt(ip=None, phone=phone)
    assert e.value.status_code == 429
    svc.record_attempt(ip=None, phone=_phone())  # other phone unaffected
    # White-box: the block key is namespaced so it can never collide with IPs.
    assert f"phone::{phone}" in svc._blocked_until


def test_service_reset_clears_all_state(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(
        config_module.settings, "TEST_LOGIN_MAX_PER_HOUR_PER_IP", 1
    )
    svc = test_login_module.TestLoginService()
    svc.record_attempt(ip="203.0.113.10", phone=_phone())
    with pytest.raises(HTTPException):
        svc.record_attempt(ip="203.0.113.10", phone=_phone())
    svc.reset()
    assert svc._ip_requests == {}
    assert svc._phone_requests == {}
    assert svc._blocked_until == {}
    # And the IP can attempt again after the reset:
    svc.record_attempt(ip="203.0.113.10", phone=_phone())


# --- config parsing/validator units -------------------------------------------

def test_split_invalid_test_login_phones_parsers():
    allowed = "+919876543210"
    other = "+919876543211"
    assert _split_test_login_phones(
        f"  {allowed} ,,{other} , ,"
    ) == (allowed, other)
    assert _split_test_login_phones("") == ()
    assert _split_test_login_phones(None) == ()
    assert _split_test_login_phones("   ") == ()
    # Never-matchable entries are flagged for the gate (and never printed):
    flagged = invalid_test_login_phones(
        (allowed, "9876543210", "+15000000001", "+91876543210", other)
    )
    assert flagged == ("9876543210", "+15000000001", "+91876543210")


# --- production gate: strengthened, never bypassed ----------------------------

def test_prod_disabled_default_passes_gate(monkeypatch):
    """Production default (flag off — even with a junk key present) boots."""
    _prod(
        monkeypatch,
        TEST_LOGIN_ENABLED=False,
        TEST_LOGIN_KEY="junk-key-that-is-not-strong",
    )
    assert is_production() is True
    validate_security_config()  # must not raise


def test_prod_enabled_requires_strong_key(monkeypatch):
    _prod(monkeypatch, TEST_LOGIN_ENABLED=True, TEST_LOGIN_KEY="")
    assert is_production() is True
    with pytest.raises(RuntimeError, match="RESTAMP_TEST_LOGIN_KEY"):
        validate_security_config()


def test_prod_enabled_weak_key_refuses_boot(monkeypatch):
    _prod(monkeypatch, TEST_LOGIN_ENABLED=True, TEST_LOGIN_KEY="short-key")
    with pytest.raises(RuntimeError) as e:
        validate_security_config()
    assert "RESTAMP_TEST_LOGIN_KEY" in str(e.value)
    assert "short-key" not in str(e.value)


def test_prod_enabled_strong_key_empty_allowlist_boots_with_warning(
    monkeypatch, caplog
):
    """Enabled + strong key + EMPTY allowlist boots (fail-CLOSED at request
    time: every attempt is rejected), loud in the logs."""
    _prod(monkeypatch, TEST_LOGIN_ENABLED=True, TEST_LOGIN_KEY=KEY)
    _allow(monkeypatch, ())
    assert is_production() is True
    with caplog.at_level(logging.INFO):
        validate_security_config()  # must not raise
    joined = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "RESTAMP_TEST_LOGIN_ALLOWED_PHONES" in joined
    assert "EMPTY" in joined


def test_prod_enabled_strong_key_valid_allowlist_passes(monkeypatch, caplog):
    _prod(monkeypatch, TEST_LOGIN_ENABLED=True, TEST_LOGIN_KEY=KEY)
    _allow(monkeypatch, ("+919876543210",))
    assert is_production() is True
    with caplog.at_level(logging.INFO):
        validate_security_config()  # must not raise
    # No allowlist warning (other unrelated production warnings may exist):
    joined = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "RESTAMP_TEST_LOGIN_ALLOWED_PHONES" not in joined


def test_prod_enabled_malformed_allowlist_entry_refuses_boot(monkeypatch):
    _prod(monkeypatch, TEST_LOGIN_ENABLED=True, TEST_LOGIN_KEY=KEY)
    _allow(monkeypatch, ("+919876543210", "9876543210"))  # 2nd: no +91 prefix
    with pytest.raises(RuntimeError) as e:
        validate_security_config()
    assert "RESTAMP_TEST_LOGIN_ALLOWED_PHONES" in str(e.value)
    # The offending entry is never echoed:
    assert "9876543210" not in str(e.value)


def test_prod_allowlist_ignored_when_disabled_with_junk_key(monkeypatch):
    """A malformed allowlist entry with the feature DISABLED must not break
    production boot (the dormant config has no runtime effect)."""
    _prod(
        monkeypatch,
        TEST_LOGIN_ENABLED=False,
        TEST_LOGIN_KEY="junk-key-that-is-not-strong",
    )
    _allow(monkeypatch, ("9876543210",))
    validate_security_config()  # must not raise


def test_local_enabled_without_key_warns_not_raises(monkeypatch, caplog):
    _enable(monkeypatch, key="")
    assert is_production() is False  # suite posture: local classification
    with caplog.at_level(logging.INFO):
        validate_security_config()  # must not raise locally
    joined = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "RESTAMP_TEST_LOGIN_KEY" in joined
    assert "INACTIVE" in joined


def test_display_mode_gate_still_refused_with_test_login_enabled(monkeypatch):
    """The display-mode production refusal is untouched by this feature."""
    _prod(
        monkeypatch,
        TEST_LOGIN_ENABLED=True,
        TEST_LOGIN_KEY=KEY,
        OTP_DISPLAY_MODE=True,
    )
    with pytest.raises(RuntimeError, match="OTP_DISPLAY_MODE"):
        validate_security_config()


def test_auth_gate_still_applies_with_test_login_enabled(monkeypatch):
    """The JWT-secret production refusal is untouched by this feature."""
    _prod(monkeypatch, TEST_LOGIN_ENABLED=True, TEST_LOGIN_KEY=KEY)
    monkeypatch.setattr(config_module.settings, "JWT_SECRET", "weak")
    with pytest.raises(RuntimeError, match="RESTAMP_JWT_SECRET"):
        validate_security_config()


# --- end-to-end integration with the real token side --------------------------

def test_test_login_token_actually_authenticates(client, monkeypatch):
    """The issued access_token is a real session: /auth/me accepts it and
    reports the phone identity (same as the /auth/otp/verify flow)."""
    _enable(monkeypatch)
    c, _ = client
    phone = _phone()
    _allow(monkeypatch, (phone,))
    _allow_registration(monkeypatch)
    body = _login(c, phone).json()
    me = c.get(
        "/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert me.status_code == 200
    me_body = me.json()
    assert me_body["providers"] == ["phone"]
    assert me_body["display_name"] == phone
    assert me_body["role"] == "BUYER"


# --- "existing" mode: accounts-only demo login (no allowlist, no registration) ---

def _set_mode(monkeypatch, value: str) -> None:
    monkeypatch.setattr(config_module.settings, "TEST_LOGIN_MODE", value)


def test_existing_mode_logs_in_existing_account_without_allowlist(
    client, monkeypatch
):
    """Any valid Indian mobile that ALREADY has a Restamp account may log
    in; the allowlist must be irrelevant in this mode and the seeded
    identity is reused exactly as stored (verified_by never rewritten)."""
    _enable(monkeypatch)
    _set_mode(monkeypatch, "existing")
    _allow(monkeypatch, ())  # empty allowlist MUST NOT block existing accounts
    c, session = client
    phone = _phone()
    uid = _seed_phone_user(session, phone, verified_by="otp")
    r = _login(c, phone)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"access_token", "token_type", "expires_in", "user_id", "role"}
    assert body["user_id"] == uid
    assert body["role"] == "BUYER"
    verified_by = session.execute(
        sqltext(
            "SELECT verified_by FROM auth_identities WHERE provider='phone' "
            "AND provider_identifier=:p"
        ),
        {"p": phone},
    ).fetchone()[0]
    assert verified_by == "otp"  # untouched
    assert _identity_count(session, phone) == 1  # no duplicates


def test_existing_mode_unknown_phone_clear_404_zero_rows(client, monkeypatch):
    """Unknown phone + valid key → the explicit 404 message, and not a
    single row is created or modified."""
    _enable(monkeypatch)
    _set_mode(monkeypatch, "existing")
    c, session = client
    phone = _phone()
    before_ident = session.execute(sqltext("SELECT COUNT(*) FROM auth_identities")).fetchone()[0]
    before_users = session.execute(sqltext("SELECT COUNT(*) FROM users")).fetchone()[0]
    r = _login(c, phone)
    assert r.status_code == 404
    assert r.json() == {"detail": "No account found for this phone number."}
    assert _identity_count(session, phone) == 0
    after_ident = session.execute(sqltext("SELECT COUNT(*) FROM auth_identities")).fetchone()[0]
    after_users = session.execute(sqltext("SELECT COUNT(*) FROM users")).fetchone()[0]
    assert after_ident == before_ident
    assert after_users == before_users


def test_existing_mode_never_registers_even_with_flag_on(client, monkeypatch):
    """Mode semantics beat the registration flag: 'existing' + flag "1"
    still refuses unknown phones without creating anything."""
    _enable(monkeypatch)
    _set_mode(monkeypatch, "existing")
    _allow_registration(monkeypatch)
    c, session = client
    phone = _phone()
    before = session.execute(sqltext("SELECT COUNT(*) FROM users")).fetchone()[0]
    r = _login(c, phone)
    assert r.status_code == 404
    assert r.json() == {"detail": "No account found for this phone number."}
    after = session.execute(sqltext("SELECT COUNT(*) FROM users")).fetchone()[0]
    assert after == before


def test_existing_mode_key_precedes_account_lookup(client, monkeypatch):
    """The no-account message is key-gated: wrong or missing keys get the
    byte-identical generic 401 and NEVER the account-existence text."""
    _enable(monkeypatch)
    _set_mode(monkeypatch, "existing")
    c, _ = client
    phone = _phone()
    wrong = _login(c, phone, key="w" * 40)
    missing = _login(c, phone, key=None)
    assert wrong.status_code == missing.status_code == 401
    assert wrong.content == missing.content  # indistinguishable
    assert wrong.json() == UNAUTHORIZED
    assert b"No account" not in wrong.content


def test_existing_mode_rate_limits_still_apply(client, monkeypatch):
    _enable(monkeypatch)
    _set_mode(monkeypatch, "existing")
    monkeypatch.setattr(
        config_module.settings, "TEST_LOGIN_MAX_PER_HOUR_PER_PHONE", 1
    )
    monkeypatch.setattr(
        config_module.settings, "TEST_LOGIN_MAX_PER_HOUR_PER_IP", 99
    )
    c, session = client
    phone = _phone()
    _seed_phone_user(session, phone, verified_by="otp")
    assert _login(c, phone).status_code == 200  # 1 successful attempt counts
    r = _login(c, phone)
    assert r.status_code == 429
    assert r.json() == TOO_MANY


def test_default_allowlist_mode_unchanged_after_mode_switch(client, monkeypatch):
    """Switching the mode back to "allowlist" restores the original
    fail-closed semantics EXACTLY: empty allowlist rejects every phone
    (even an existing account) with the generic 403."""
    _enable(monkeypatch)
    _set_mode(monkeypatch, "allowlist")
    _allow(monkeypatch, ())
    c, session = client
    phone = _phone()
    _seed_phone_user(session, phone, verified_by="otp")
    r = _login(c, phone)
    assert r.status_code == 403
    assert r.json() == FORBIDDEN


def test_prod_existing_mode_with_registration_refuses_boot(monkeypatch):
    _prod(
        monkeypatch,
        TEST_LOGIN_ENABLED=True,
        TEST_LOGIN_KEY=KEY,
        TEST_LOGIN_MODE="existing",
        TEST_LOGIN_ALLOW_REGISTRATION=True,
    )
    assert is_production() is True
    with pytest.raises(RuntimeError) as e:
        validate_security_config()
    assert "RESTAMP_TEST_LOGIN_MODE" in str(e.value)
    assert "RESTAMP_TEST_LOGIN_ALLOW_REGISTRATION" in str(e.value)


def test_prod_existing_mode_boots_and_ignores_allowlist(monkeypatch, caplog):
    """'existing' + strong key + empty/malformed allowlist boots: the
    allowlist is inert in this mode, so the allowlist warning is absent."""
    _prod(
        monkeypatch,
        TEST_LOGIN_ENABLED=True,
        TEST_LOGIN_KEY=KEY,
        TEST_LOGIN_MODE="existing",
        TEST_LOGIN_ALLOW_REGISTRATION=False,
    )
    _set_mode(monkeypatch, "existing")
    _allow(monkeypatch, ("9876543210",))  # malformed — but ignored here
    with caplog.at_level(logging.INFO):
        validate_security_config()  # must not raise
    joined = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "RESTAMP_TEST_LOGIN_ALLOWED_PHONES" not in joined
    assert "RESTAMP_TEST_LOGIN_MODE" not in joined


def test_prod_unknown_mode_refuses_boot(monkeypatch):
    _prod(
        monkeypatch,
        TEST_LOGIN_ENABLED=True,
        TEST_LOGIN_KEY=KEY,
        TEST_LOGIN_MODE="phone-book",
    )
    with pytest.raises(RuntimeError) as e:
        validate_security_config()
    assert "RESTAMP_TEST_LOGIN_MODE" in str(e.value)


def test_prod_switching_back_to_allowlist_revalidates_allowlist(monkeypatch):
    """Mode drift safety: after returning to 'allowlist' mode, a stale
    malformed allowlist entry refuses production boot again."""
    _prod(monkeypatch, TEST_LOGIN_ENABLED=True, TEST_LOGIN_KEY=KEY)
    _set_mode(monkeypatch, "allowlist")
    _allow(monkeypatch, ("9876543210",))
    with pytest.raises(RuntimeError) as e:
        validate_security_config()
    assert "RESTAMP_TEST_LOGIN_ALLOWED_PHONES" in str(e.value)
    assert "9876543210" not in str(e.value)  # entry never echoed


def test_local_unknown_mode_stays_inactive_exact_404(client, monkeypatch):
    """Defense in depth: an unknown mode value keeps the endpoint fully
    inactive even outside production (exact unknown-route 404)."""
    _enable(monkeypatch)
    _set_mode(monkeypatch, "phone-book")
    c, _ = client
    assert test_login_service.is_active() is False
    r = _login(c, _phone())
    assert r.status_code == 404
    assert r.json() == REQUEST_404


# --- "suffix" mode: last-six-digit demo code (presenter convenience, -------
#     expiry-bounded; the code is the phone's own predictable suffix, so
#     it is NEVER a secret — every guard here exists to bound exactly that)

def _future_deadline(hours: float) -> str:
    """ISO-8601 UTC deadline string at now+hours (second precision)."""
    return (
        datetime.now(timezone.utc) + timedelta(hours=hours)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def _set_suffix_deadline(monkeypatch, value: str) -> None:
    monkeypatch.setattr(
        config_module.settings, "TEST_LOGIN_SUFFIX_UNTIL", value
    )


def _enable_suffix(monkeypatch, deadline: str | None = None) -> None:
    """Suffix mode ON with a str-key and a future window (default 1h)."""
    _enable(monkeypatch)
    _set_mode(monkeypatch, "suffix")
    _set_suffix_deadline(
        monkeypatch,
        _future_deadline(1.0) if deadline is None else deadline,
    )


def _suffix_login(c, phone: str, code: str):
    """The suffix-mode request shape: {"phone", "code"} and NO header."""
    return c.post("/auth/test-login", json={"phone": phone, "code": code})


def test_suffix_mode_logs_in_existing_account_with_leading_zero_suffix(
    client, monkeypatch
):
    """The demo code is the LAST SIX DIGITS of the phone as a string —
    leading zeros preserved (the exact working example: +917530006460 →
    "006460"). The X-Test-Login-Key header is NOT required, and even a
    garbage header cannot interfere with this mode. TokenOut is identical
    to /auth/otp/verify and the stored identity is reused untouched."""
    _enable_suffix(monkeypatch)
    c, session = client
    phone = "+917530006460"
    # Reuse a pre-existing local row for this exact example phone when one
    # exists (the dev database is shared across sessions); otherwise seed
    # it. Either way the SUFFIX must be the authorization.
    pre = users_roles.find_user_by_identity(session, "phone", phone)
    uid = pre.id if pre is not None else _seed_phone_user(
        session, phone, verified_by="otp"
    )
    r = _suffix_login(c, phone, "006460")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {
        "access_token",
        "token_type",
        "expires_in",
        "user_id",
        "role",
    }
    assert body["user_id"] == uid
    assert body["role"] == "BUYER"
    # A wrong key-looking header must be irrelevant in this mode: the
    # suffix code alone authorizes, no X-Test-Login-Key is consulted.
    r2 = c.post(
        "/auth/test-login",
        json={"phone": phone, "code": "006460"},
        headers={"X-Test-Login-Key": "w" * 40},
    )
    assert r2.status_code == 200
    assert _identity_count(session, phone) >= 1  # never duplicated


def test_suffix_mode_generated_phone_suffix_has_leading_zeros(
    client, monkeypatch
):
    """Second leading-zero proof on a GENERATED phone: the suffix of the
    standard test phone always starts with zeros, the six-digit string
    suffix authenticates, and the classic int()-coercion variant (the
    zero-DROPPED value, left-padded with a different digit) is rejected."""
    _enable_suffix(monkeypatch)
    c, session = client
    phone = _phone()
    _seed_phone_user(session, phone, verified_by="otp")
    suffix = phone[-6:]
    assert suffix.startswith("000")  # leading zeros by construction
    assert _suffix_login(c, phone, suffix).status_code == 200
    dropped = suffix.lstrip("0")  # what an int() round-trip would produce
    assert dropped
    r = _suffix_login(c, phone, dropped.rjust(6, "9"))
    assert r.status_code == 401
    assert r.json() == UNAUTHORIZED


def test_suffix_mode_bad_code_shapes_are_generic_401(client, monkeypatch):
    """Every non-exact-suffix value — dropped zero, short, long,
    surrounding whitespace, non-digit content, simply wrong — is the ONE
    generic 401, byte-identical for all of them, and never creates or
    reveals rows."""
    _enable_suffix(monkeypatch)
    c, session = client
    phone = _phone()
    _seed_phone_user(session, phone, verified_by="otp")
    suffix = phone[-6:]
    before_users = session.execute(
        sqltext("SELECT COUNT(*) FROM users")
    ).fetchone()[0]
    codes = (
        suffix.lstrip("0") or "0",  # dropped leading zeros
        suffix[1:],                 # five digits
        suffix + "0",               # seven digits
        " " + suffix,               # leading space
        suffix + " ",               # trailing space
        "abcdefgh",
        "0O" + suffix[-4:],         # letter-O homoglyph
        "9" * 6,                    # valid shape, wrong value
        "",                         # empty
    )
    contents = set()
    for code in codes:
        r = _suffix_login(c, phone, code)
        assert r.status_code == 401, repr(code)
        assert r.json() == UNAUTHORIZED
        assert b"No account" not in r.content
        contents.add(r.content)
    assert len(contents) == 1  # byte-identical rejections
    after_users = session.execute(
        sqltext("SELECT COUNT(*) FROM users")
    ).fetchone()[0]
    assert after_users == before_users
    assert _identity_count(session, phone) == 1  # seeded row untouched


def test_suffix_mode_unknown_phone_explicit_404_zero_rows(
    client, monkeypatch
):
    """Correct suffix + phone with NO Restamp account → the explicit
    no-account message and not a single row created or modified."""
    _enable_suffix(monkeypatch)
    c, session = client
    phone = _phone()
    before_ident = session.execute(
        sqltext("SELECT COUNT(*) FROM auth_identities")
    ).fetchone()[0]
    before_users = session.execute(
        sqltext("SELECT COUNT(*) FROM users")
    ).fetchone()[0]
    r = _suffix_login(c, phone, phone[-6:])
    assert r.status_code == 404
    assert r.json() == {"detail": "No account found for this phone number."}
    assert _identity_count(session, phone) == 0
    assert session.execute(
        sqltext("SELECT COUNT(*) FROM auth_identities")
    ).fetchone()[0] == before_ident
    assert session.execute(
        sqltext("SELECT COUNT(*) FROM users")
    ).fetchone()[0] == before_users


def test_suffix_mode_wrong_code_never_reveals_account(client, monkeypatch):
    """The code check runs BEFORE the account lookup: a wrong code on a
    phone WITH an account is byte-identical to a wrong code on a
    never-seen phone — no account-existence oracle through failures."""
    _enable_suffix(monkeypatch)
    c, _ = client
    existing = _phone()
    wrong_existing = "1" * 6 if existing[-6:] != "111111" else "2" * 6
    wrong_unknown = "3" * 6
    r_a = _suffix_login(c, existing, wrong_existing)
    r_b = _suffix_login(c, _phone(), wrong_unknown)
    assert r_a.status_code == r_b.status_code == 401
    assert r_a.content == r_b.content
    assert r_a.json() == UNAUTHORIZED
    assert b"No account" not in r_a.content


def test_suffix_mode_never_registers_even_with_flag_on(client, monkeypatch):
    """Mode semantics beat the registration flag: "suffix" + flag "1"
    still refuses unknown phones with ZERO rows written (the suffix path
    never INSERTs; production boots are additionally refused)."""
    _enable_suffix(monkeypatch)
    _allow_registration(monkeypatch)
    c, session = client
    phone = _phone()
    before = session.execute(sqltext("SELECT COUNT(*) FROM users")).fetchone()[0]
    r = _suffix_login(c, phone, phone[-6:])
    assert r.status_code == 404
    assert r.json() == {"detail": "No account found for this phone number."}
    assert _identity_count(session, phone) == 0
    assert session.execute(
        sqltext("SELECT COUNT(*) FROM users")
    ).fetchone()[0] == before


def test_suffix_mode_inactive_without_deadline(client, monkeypatch):
    """No RESTAMP_TEST_LOGIN_SUFFIX_UNTIL → the mode flag alone cannot
    activate the endpoint: exact 404 even for a plausible well-shaped
    request (fail closed)."""
    _enable(monkeypatch)
    _set_mode(monkeypatch, "suffix")
    _set_suffix_deadline(monkeypatch, "")
    c, _ = client
    assert test_login_service.is_active() is False
    phone = _phone()
    r = _suffix_login(c, phone, phone[-6:])
    assert r.status_code == 404
    assert r.json() == REQUEST_404


def test_suffix_mode_self_disarms_when_deadline_passes(client, monkeypatch):
    """A past deadline deactivates the endpoint on EVERY request (exact
    404) — the demo window ends by itself, with no redeploy needed."""
    _enable(monkeypatch)
    _set_mode(monkeypatch, "suffix")
    _set_suffix_deadline(monkeypatch, "2020-01-01T00:00:00Z")
    c, _ = client
    assert test_login_service.is_active() is False
    phone = _phone()
    r = _suffix_login(c, phone, phone[-6:])
    assert r.status_code == 404
    assert r.json() == REQUEST_404


def test_suffix_mode_invalid_phone_generic_401_zero_rows(
    client, monkeypatch
):
    """A schema-invalid phone is the SAME generic 401 (never a field-level
    422, never the no-account 404) and writes nothing."""
    _enable_suffix(monkeypatch)
    c, session = client
    before_users = session.execute(
        sqltext("SELECT COUNT(*) FROM users")
    ).fetchone()[0]
    r = _suffix_login(c, "+91123", "001123")
    assert r.status_code == 401
    assert r.json() == UNAUTHORIZED
    assert session.execute(
        sqltext("SELECT COUNT(*) FROM users")
    ).fetchone()[0] == before_users


def test_suffix_mode_rate_limits_still_apply(client, monkeypatch):
    """The per-phone budget is consumed by suffix attempts exactly like
    keyed attempts: one successful login trips a cap of 1 on the next."""
    _enable_suffix(monkeypatch)
    monkeypatch.setattr(
        config_module.settings, "TEST_LOGIN_MAX_PER_HOUR_PER_PHONE", 1
    )
    monkeypatch.setattr(
        config_module.settings, "TEST_LOGIN_MAX_PER_HOUR_PER_IP", 99
    )
    c, session = client
    phone = _phone()
    _seed_phone_user(session, phone, verified_by="otp")
    assert _suffix_login(c, phone, phone[-6:]).status_code == 200
    r = _suffix_login(c, phone, phone[-6:])
    assert r.status_code == 429
    assert r.json() == TOO_MANY


def test_suffix_mode_wrong_codes_consume_shared_per_ip_budget(
    client, monkeypatch
):
    """Per-IP sharing: wrong-code suffix attempts (which can never leak
    account state) STILL consume the IP budget just like wrong keys, so
    brute-forcing the six digits is bounded by the same caps."""
    _enable_suffix(monkeypatch)
    monkeypatch.setattr(
        config_module.settings, "TEST_LOGIN_MAX_PER_HOUR_PER_IP", 3
    )
    monkeypatch.setattr(
        config_module.settings, "TEST_LOGIN_MAX_PER_HOUR_PER_PHONE", 99
    )
    c, session = client
    p1, p2, p3, p4 = _phone(), _phone(), _phone(), _phone()
    for p in (p1, p2, p3, p4):
        _seed_phone_user(session, p, verified_by="otp")
    wrong = "9" * 6
    for p in (p1, p2, p3):
        assert _suffix_login(c, p, wrong).status_code == 401
    # Fourth attempt from the same IP — even with a CORRECT code — is
    # blocked by the shared per-IP budget.
    r = _suffix_login(c, p4, p4[-6:])
    assert r.status_code == 429
    assert r.json() == TOO_MANY


def test_suffix_deadline_parser_accepts_z_naive_offset(monkeypatch):
    """config.parse_test_login_suffix_until: 'Z', naive (assumed UTC) and
    explicit offsets all normalize to the SAME aware UTC moment; garbage
    and empties return None (fail closed)."""
    expected = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert parse_test_login_suffix_until("2026-01-02T03:04:05Z") == expected
    assert parse_test_login_suffix_until("2026-01-02T03:04:05") == expected
    assert parse_test_login_suffix_until("2026-01-02T05:04:05+02:00") == expected
    assert parse_test_login_suffix_until("2026-01-02 03:04:05") == expected
    assert parse_test_login_suffix_until("") is None
    assert parse_test_login_suffix_until("   ") is None
    assert parse_test_login_suffix_until("not a date") is None
    assert parse_test_login_suffix_until(None) is None


def test_require_suffix_code_unit_contract():
    """Service-level contract of the suffix comparison: exact string
    suffix (leading zeros) passes; every deviation is the generic 401."""
    from fastapi import HTTPException

    def attempt(phone: str, code: str):
        try:
            test_login_service.require_suffix_code(phone, code)
            return None
        except HTTPException as e:
            return e

    assert attempt("+917530006460", "006460") is None
    for bad in ("6460", "0006460", "106460", " 006460", "006460 ", "abcdef",
                "٠٠٦٤٦٠", ""):
        e = attempt("+917530006460", bad)
        assert e is not None and e.status_code == 401, repr(bad)


def test_prod_suffix_mode_requires_deadline(monkeypatch):
    _prod(
        monkeypatch,
        TEST_LOGIN_ENABLED=True,
        TEST_LOGIN_KEY=KEY,
        TEST_LOGIN_MODE="suffix",
        TEST_LOGIN_ALLOW_REGISTRATION=False,
        TEST_LOGIN_SUFFIX_UNTIL="",
    )
    assert is_production() is True
    with pytest.raises(RuntimeError) as e:
        validate_security_config()
    assert "RESTAMP_TEST_LOGIN_SUFFIX_UNTIL" in str(e.value)


def test_prod_suffix_mode_refuses_past_deadline(monkeypatch):
    _prod(
        monkeypatch,
        TEST_LOGIN_ENABLED=True,
        TEST_LOGIN_KEY=KEY,
        TEST_LOGIN_MODE="suffix",
        TEST_LOGIN_ALLOW_REGISTRATION=False,
        TEST_LOGIN_SUFFIX_UNTIL="2020-01-01T00:00:00Z",
    )
    with pytest.raises(RuntimeError) as e:
        validate_security_config()
    assert "RESTAMP_TEST_LOGIN_SUFFIX_UNTIL" in str(e.value)
    assert "future" in str(e.value)


def test_prod_suffix_mode_refuses_more_than_24h_ahead(monkeypatch):
    _prod(
        monkeypatch,
        TEST_LOGIN_ENABLED=True,
        TEST_LOGIN_KEY=KEY,
        TEST_LOGIN_MODE="suffix",
        TEST_LOGIN_ALLOW_REGISTRATION=False,
        TEST_LOGIN_SUFFIX_UNTIL=_future_deadline(25.0),
    )
    with pytest.raises(RuntimeError) as e:
        validate_security_config()
    assert "RESTAMP_TEST_LOGIN_SUFFIX_UNTIL" in str(e.value)
    assert "24" in str(e.value)


def test_prod_suffix_mode_refuses_registration(monkeypatch):
    _prod(
        monkeypatch,
        TEST_LOGIN_ENABLED=True,
        TEST_LOGIN_KEY=KEY,
        TEST_LOGIN_MODE="suffix",
        TEST_LOGIN_ALLOW_REGISTRATION=True,
        TEST_LOGIN_SUFFIX_UNTIL=_future_deadline(1.0),
    )
    with pytest.raises(RuntimeError) as e:
        validate_security_config()
    assert "RESTAMP_TEST_LOGIN_MODE" in str(e.value)
    assert "RESTAMP_TEST_LOGIN_ALLOW_REGISTRATION" in str(e.value)


def test_prod_suffix_mode_boots_and_activates_with_valid_window(monkeypatch):
    """A future (≤ 24h) deadline + registration off boots cleanly in
    production and ACTIVATES the endpoint (deadline in the future)."""
    _prod(
        monkeypatch,
        TEST_LOGIN_ENABLED=True,
        TEST_LOGIN_KEY=KEY,
        TEST_LOGIN_MODE="suffix",
        TEST_LOGIN_ALLOW_REGISTRATION=False,
        TEST_LOGIN_SUFFIX_UNTIL=_future_deadline(1.0),
        TEST_LOGIN_ALLOWED_PHONES=(),  # inert in suffix mode, must not warn
    )
    validate_security_config()  # must not raise
    assert test_login_service.is_active() is True
    assert test_login_service.mode() == "suffix"
