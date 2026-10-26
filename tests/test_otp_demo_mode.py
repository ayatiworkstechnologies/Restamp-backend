"""Tests for the dedicated demo OTP login (RESTAMP_ENV=demo + RESTAMP_OTP_DEMO_MODE=1).

Each demo OTP request SELECTS one predefined code (DEMO_OTPS), stores it with
the normal lifecycle (expiry, attempt caps, single use, rate limits, per-IP
caps), and returns it for display (debug_code in the existing response
contract). Verification then follows the ordinary strict path: ONLY the
selected code works for that phone — never the other predefined codes.

Boundaries tested here:
- outside RESTAMP_ENV=demo the flag is refused by the startup gate and no
  demo-code path exists in verification;
- a deployed demo service (RESTAMP_ENV=demo + remote DATABASE_URL, which
  is_production() classifies as production BY DESIGN) still enforces the full
  production hardening (strong JWT, no OTP_DEBUG, no CORS '*');
- a production-classified demo REFUSES startup without a non-empty
  RESTAMP_OTP_DEMO_PHONES allowlist (private test phones); with one, demo
  issuance is exact-match only and every other phone fails closed with no
  OTP state and no rate-limit budget consumed.

No SMS is sent anywhere; the provider is never involved in the demo path.
"""
import logging

import pytest
from fastapi import HTTPException

from app import config as config_module
from app.config import validate_security_config
from app.services import otp as otp_module
from app.services.otp import DEMO_OTPS, DisabledOTPProvider, OTPService

ALL_DEMO = sorted(DEMO_OTPS)
assert len(ALL_DEMO) == 10


def _demo_on(monkeypatch, **overrides):
    """Local demo: env=demo + flag=1 (isolated OTPService tests)."""
    monkeypatch.setattr(config_module.settings, "ENV", "demo")
    monkeypatch.setattr(config_module.settings, "OTP_DEMO_MODE", True)
    for key, value in overrides.items():
        monkeypatch.setattr(config_module.settings, key, value)


def _deployed(monkeypatch, env, **overrides):
    """Deployed-like env: remote DATABASE_URL so is_production() is True."""
    s = config_module.settings
    monkeypatch.setattr(s, "ENV", env)
    monkeypatch.setattr(s, "DATABASE_URL", "mysql+pymysql://user:pw@remote-host/db")
    monkeypatch.setattr(s, "JWT_SECRET", "z" * 40)
    monkeypatch.setattr(s, "OTP_DEBUG", False)
    monkeypatch.setattr(s, "OTP_DEMO_MODE", False)
    monkeypatch.setattr(s, "GOOGLE_DEV_MODE", False)
    monkeypatch.setattr(s, "GOOGLE_ENABLED", False)
    monkeypatch.setattr(s, "GOOGLE_CLIENT_ID", "")
    monkeypatch.setattr(s, "CORS_ORIGINS", "")
    monkeypatch.setattr(s, "SMS_MODE", "disabled")
    for key, value in overrides.items():
        monkeypatch.setattr(s, key, value)


# --- selection, storage, strict verification ---------------------------------

def test_demo_request_selects_and_returns_predefined_code(monkeypatch):
    _demo_on(monkeypatch, OTP_DEBUG=False)  # returned even with OTP_DEBUG off
    svc = OTPService(provider=DisabledOTPProvider())
    code = svc.request_code("+10000003001", ip="203.0.113.30")
    assert code in DEMO_OTPS
    entry = svc._codes["+10000003001"]
    assert entry is not None
    assert entry.code_hash == svc._hash("+10000003001", code)  # stored selected
    assert ["+10000003001"] == list(svc._requests)


def test_selected_code_verifies(monkeypatch):
    _demo_on(monkeypatch)
    svc = OTPService(provider=DisabledOTPProvider())
    selected = svc.request_code("+10000003002")
    assert svc.verify_code("+10000003002", selected) is True


def test_repeated_selection_follows_the_same_rule(monkeypatch):
    """Whatever code gets selected on a NEW request, exactly that one works."""
    _demo_on(monkeypatch)
    for i in range(3):
        svc = OTPService(provider=DisabledOTPProvider())
        phone = f"+10000003010{i}"
        selected = svc.request_code(phone)
        assert selected in DEMO_OTPS
        assert svc.verify_code(phone, selected) is True


def test_every_other_predefined_code_is_rejected(monkeypatch):
    """The core demo rule: only the backend-selected code. All OTHER codes
    from the predefined set (and non-predefined codes) must fail. The verify
    attempt cap is raised here so the whole candidate list can be swept on
    one live entry (a dedicated test covers the cap itself)."""
    _demo_on(monkeypatch, OTP_MAX_VERIFY_ATTEMPTS=20)
    svc = OTPService(provider=DisabledOTPProvider())
    selected = svc.request_code("+10000003020")
    wrongs = [c for c in ALL_DEMO if c != selected] + ["111111", "999999"]
    results = [svc.verify_code("+10000003020", c) for c in wrongs]
    assert results == [False] * len(wrongs)
    assert svc.verify_code("+10000003020", selected) is True


def test_non_selected_code_fails_on_endpoint(client, monkeypatch):
    monkeypatch.setattr(config_module.settings, "ENV", "demo")
    monkeypatch.setattr(config_module.settings, "OTP_DEMO_MODE", True)
    c, _ = client
    phone = "+10000003060"
    r = c.post("/auth/otp/request", json={"phone": phone})
    assert r.status_code == 200, r.text
    selected = r.json().get("debug_code")
    assert selected in DEMO_OTPS
    wrong = next(c for c in ALL_DEMO if c != selected)
    r2 = c.post("/auth/otp/verify", json={"phone": phone, "code": wrong})
    assert r2.status_code == 401, r2.text
    assert r2.json() == {"detail": "Invalid or expired code"}
    r3 = c.post("/auth/otp/verify", json={"phone": phone, "code": selected})
    assert r3.status_code == 200, r3.text
    assert "access_token" in r3.json()


def test_demo_verify_endpoint_returns_selected_code(client, monkeypatch):
    monkeypatch.setattr(config_module.settings, "ENV", "demo")
    monkeypatch.setattr(config_module.settings, "OTP_DEMO_MODE", True)
    c, _ = client
    phone = "+10000003050"
    r = c.post("/auth/otp/request", json={"phone": phone})
    assert r.status_code == 200, r.text
    selected = r.json().get("debug_code")
    assert selected in DEMO_OTPS  # backend-selected, shown in the response
    r2 = c.post("/auth/otp/verify", json={"phone": phone, "code": selected})
    assert r2.status_code == 200, r2.text
    assert "access_token" in r2.json()
    # Single use: another verify on the same phone/code fails (401).
    r3 = c.post("/auth/otp/verify", json={"phone": phone, "code": selected})
    assert r3.status_code == 401, r3.text


# --- expiry, consumption, attempt caps, throttle ------------------------------

def test_expired_demo_code_rejected(monkeypatch):
    _demo_on(monkeypatch, OTP_TTL_SECONDS=-1)  # already expired at birth
    svc = OTPService(provider=DisabledOTPProvider())
    code = svc.request_code("+10000003201")
    assert code in DEMO_OTPS
    assert svc.verify_code("+10000003201", code) is False


def test_wrong_attempts_cap_and_generic_block(monkeypatch):
    _demo_on(monkeypatch)
    svc = OTPService(provider=DisabledOTPProvider())
    selected = svc.request_code("+10000003203")
    wrong = next(c for c in ALL_DEMO if c != selected)
    for _ in range(4):
        assert svc.verify_code("+10000003203", wrong) is False
    # Fifth wrong attempt trips the attempt cap: explicit 429.
    with pytest.raises(HTTPException) as info:
        svc.verify_code("+10000003203", wrong)
    assert info.value.status_code == 429
    assert "Too many" in info.value.detail
    # Afterwards the phone is blocked: even the CORRECT selected code gets 429.
    with pytest.raises(HTTPException) as info2:
        svc.verify_code("+10000003203", selected)
    assert info2.value.status_code == 429


def test_demo_request_rate_limit_per_phone(monkeypatch):
    _demo_on(monkeypatch, OTP_MAX_REQUESTS_PER_HOUR=2)
    svc = OTPService(provider=DisabledOTPProvider())
    svc.request_code("+10000003241")
    svc.request_code("+10000003241")
    with pytest.raises(HTTPException) as info:
        svc.request_code("+10000003241")
    assert info.value.status_code == 429
    assert info.value.detail == "Too many OTP requests. Try again later."


def test_demo_request_rate_limit_per_ip(monkeypatch):
    _demo_on(monkeypatch, OTP_MAX_REQUESTS_PER_HOUR_PER_IP=2)
    svc = OTPService(provider=DisabledOTPProvider())
    svc.request_code("+10000003251", ip="198.51.100.25")
    svc.request_code("+10000003252", ip="198.51.100.25")
    with pytest.raises(HTTPException) as info:
        svc.request_code("+10000003253", ip="198.51.100.25")
    assert info.value.status_code == 429


def test_demo_no_log_disclosure(monkeypatch, caplog):
    _demo_on(monkeypatch)
    svc = OTPService(provider=DisabledOTPProvider())
    with caplog.at_level(logging.DEBUG):
        code = svc.request_code("+10000003261", ip="203.0.113.26")
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert code not in joined
    assert "+10000003261" not in joined
    assert code in DEMO_OTPS


# --- boundary: activation switches and production refusal --------------------

def test_demo_login_requires_env_and_flag(monkeypatch):
    # Both switches are required; either alone leaves demo OFF.
    monkeypatch.setattr(config_module.settings, "ENV", "demo")
    monkeypatch.setattr(config_module.settings, "OTP_DEMO_MODE", False)
    assert otp_module.demo_login_active() is False

    monkeypatch.setattr(config_module.settings, "ENV", "local")
    monkeypatch.setattr(config_module.settings, "OTP_DEMO_MODE", True)
    assert otp_module.demo_login_active() is False

    # Staging remains production: it never gets demo behavior.
    monkeypatch.setattr(config_module.settings, "ENV", "staging")
    assert otp_module.demo_login_active() is False
    _deployed(monkeypatch, "staging", OTP_DEMO_MODE=True)
    assert config_module.is_production() is True
    with pytest.raises(RuntimeError, match="RESTAMP_OTP_DEMO_MODE"):
        validate_security_config()


def test_demo_inactive_verification_has_no_shortcut(monkeypatch):
    """Without the demo switches, predefined codes are just ordinary codes:
    nothing may accept them (the OTP_DEBUG debug_code path is separate)."""
    monkeypatch.setattr(config_module.settings, "ENV", "local")
    monkeypatch.setattr(config_module.settings, "OTP_DEMO_MODE", False)
    # This sweep checks the ABSENCE of any demo shortcut, not the attempt
    # cap (covered separately): raise the cap so all 10 codes can be tried.
    monkeypatch.setattr(config_module.settings, "OTP_MAX_VERIFY_ATTEMPTS", 20)
    svc = OTPService()
    real = svc.request_code("+10000003271")  # OTP_DEBUG=1 in test env
    assert real is not None
    for c in ALL_DEMO:
        assert svc.verify_code("+10000003271", c) is False
    _demo_on(monkeypatch)
    svc2 = OTPService(provider=DisabledOTPProvider())
    code = svc2.request_code("+10000003272")
    assert code in DEMO_OTPS
    assert otp_module.demo_login_active() is True


def test_demo_env_gets_refusing_provider(monkeypatch):
    """Startup on a deployed demo resolves DisabledOTPProvider: no SMS and no
    env-var surprises (the demo path itself never touches the provider)."""
    _deployed(monkeypatch, "demo", OTP_DEMO_MODE=True)
    assert otp_module.demo_login_active() is True
    assert isinstance(otp_module.get_otp_provider(), DisabledOTPProvider)


def test_deployed_demo_service_keeps_full_production_hardening(monkeypatch):
    """A production-classified demo deployment must still fail on weak JWT,
    OTP_DEBUG, CORS '*' or Google dev mode — demo is not a weaker prod."""
    _deployed(monkeypatch, "demo", OTP_DEMO_MODE=True, OTP_DEMO_PHONES="+15550000001")
    validate_security_config()  # demo + flag + allowlist passes with a strong JWT
    monkeypatch.setattr(config_module.settings, "JWT_SECRET", "short")
    with pytest.raises(RuntimeError, match="RESTAMP_JWT_SECRET"):
        validate_security_config()
    _deployed(monkeypatch, "demo", OTP_DEMO_MODE=True, OTP_DEBUG=True)
    with pytest.raises(RuntimeError, match="RESTAMP_OTP_DEBUG"):
        validate_security_config()
    _deployed(monkeypatch, "demo", OTP_DEMO_MODE=True, CORS_ORIGINS="*")
    with pytest.raises(RuntimeError, match="RESTAMP_CORS_ORIGINS"):
        validate_security_config()
    _deployed(monkeypatch, "demo", OTP_DEMO_MODE=True, GOOGLE_DEV_MODE=True)
    with pytest.raises(RuntimeError, match="RESTAMP_GOOGLE_DEV_MODE"):
        validate_security_config()


def test_production_and_staging_refuse_demo_flag(monkeypatch):
    _deployed(monkeypatch, "production", OTP_DEMO_MODE=True)
    with pytest.raises(RuntimeError, match="fixed demo codes require"):
        validate_security_config()
    _deployed(monkeypatch, "staging", OTP_DEMO_MODE=True)
    with pytest.raises(RuntimeError, match="RESTAMP_OTP_DEMO_MODE"):
        validate_security_config()


# --- demo-phone allowlist (RESTAMP_OTP_DEMO_PHONES) ---------------------------

def test_local_demo_without_allowlist_keeps_any_phone(monkeypatch):
    """Empty allowlist (default) outside production classification: demo
    issuance is unchanged for ANY phone and demo_phone_allowed is True."""
    _demo_on(monkeypatch)  # no OTP_DEMO_PHONES configured
    assert config_module.demo_phone_allowed("+15550006001") is True
    svc = OTPService(provider=DisabledOTPProvider())
    assert svc.request_code("+15550006001") in DEMO_OTPS


def test_demo_allowlist_restricts_local_demo_issuance(monkeypatch):
    """Configured allowlist: ONLY listed phones take the demo path. An
    unlisted phone fails closed (503), leaves NO OTP state and consumes NO
    rate-limit budget (six consecutive 503s never trip the 5/hour cap)."""
    _demo_on(monkeypatch, OTP_DEMO_PHONES="+15550003001, +1 555 000 3002")
    svc = OTPService(provider=DisabledOTPProvider())
    code1 = svc.request_code("+15550003001")
    assert code1 in DEMO_OTPS
    assert svc.verify_code("+15550003001", code1) is True  # single use intact
    assert svc.request_code("+15550003002") in DEMO_OTPS  # spaced entry matches
    for _ in range(6):
        with pytest.raises(HTTPException) as info:
            svc.request_code("+15550003003")
        assert info.value.status_code == 503
        assert info.value.detail == "Failed to send code. Try again later."
    assert "+15550003003" not in svc._codes  # no OTP state was left behind
    assert "+15550003003" not in svc._requests  # no rate budget was consumed
    assert svc.request_code("+15550003001") in DEMO_OTPS  # listed unaffected


def test_demo_allowlist_entries_are_normalized(monkeypatch):
    """Entries normalize to exact E.164 (spaces/commas/stray pluses
    tolerated, digits-less entries dropped). Malformed entries can never
    widen or wildcard the list, and a production-classified demo with an
    effectively-empty allowlist cannot boot (previous tests cover the
    local any-phone behavior for an empty allowlist)."""
    _demo_on(monkeypatch, OTP_DEMO_PHONES=" +1 555 000 7001 , 15550007002, x ")
    svc = OTPService(provider=DisabledOTPProvider())
    assert svc.request_code("+15550007001") in DEMO_OTPS
    assert svc.request_code("+15550007002") in DEMO_OTPS
    for junk in ("not-a-phone", "  ", "x"):
        _demo_on(monkeypatch, OTP_DEMO_PHONES=junk)
        # Malformed entries are DROPPED, never normalized into a match-all.
        assert config_module.demo_phone_allowlist() == frozenset()
    # ...and where it matters (production-classified demo), an effectively
    # empty allowlist refuses startup instead of opening the demo up:
    _deployed(monkeypatch, "demo", OTP_DEMO_MODE=True, OTP_DEMO_PHONES="x, ")
    with pytest.raises(RuntimeError, match="RESTAMP_OTP_DEMO_PHONES"):
        validate_security_config()


def test_deployed_demo_requires_non_empty_allowlist(monkeypatch):
    """Production-classified demo without the allowlist REFUSES startup."""
    _deployed(monkeypatch, "demo", OTP_DEMO_MODE=True)
    with pytest.raises(RuntimeError, match="RESTAMP_OTP_DEMO_PHONES"):
        validate_security_config()


def test_non_allowlisted_phone_fails_closed_on_deployed_demo(client, monkeypatch):
    """Deployed demo with the allowlist: the listed phone completes the full
    e2e demo login; EVERY unlisted request is a pre-accounting 503 (no OTP
    state, no rate budget) and verification itself never succeeds for it."""
    _deployed(monkeypatch, "demo", OTP_DEMO_MODE=True, OTP_DEMO_PHONES="+15550004001")
    validate_security_config()  # allowlist present: boots cleanly
    c, _ = client
    listed = "+15550004001"
    r = c.post("/auth/otp/request", json={"phone": listed})
    assert r.status_code == 200, r.text
    selected = r.json().get("debug_code")
    assert selected in DEMO_OTPS
    wrong = next(x for x in ALL_DEMO if x != selected)
    r_wrong = c.post("/auth/otp/verify", json={"phone": listed, "code": wrong})
    assert r_wrong.status_code == 401, r_wrong.text
    r_ok = c.post("/auth/otp/verify", json={"phone": listed, "code": selected})
    assert r_ok.status_code == 200, r_ok.text
    assert "access_token" in r_ok.json()
    unlisted = "+15550004002"
    for _ in range(6):
        ru = c.post("/auth/otp/request", json={"phone": unlisted})
        assert ru.status_code == 503, ru.text
        assert ru.json() == {
            "detail": "OTP delivery is not configured. Try again later."
        }
    assert otp_module.otp_service._codes.get(unlisted) is None  # no OTP state
    rv = c.post("/auth/otp/verify", json={"phone": unlisted, "code": "999999"})
    assert rv.status_code == 401, rv.text


def test_demo_disabled_with_allowlist_changes_nothing(monkeypatch):
    """The allowlist alone NEVER enables demo: with the flag off the phone
    walks the ordinary (dev/debug) path — no predefined-code issuance."""
    _demo_on(monkeypatch, OTP_DEMO_MODE=False, OTP_DEMO_PHONES="+15550005001")
    assert otp_module.demo_login_active() is False
    monkeypatch.setattr(config_module.settings, "OTP_LENGTH", 4)
    svc = OTPService()
    real = svc.request_code("+15550005001")  # ordinary 4-digit code (debug tests)
    assert real is not None and len(real) == 4
    # A 4-digit ordinary code can never equal a 6-digit predefined code; the
    # cap is raised (suite precedent) so the whole candidate list is swept.
    monkeypatch.setattr(config_module.settings, "OTP_MAX_VERIFY_ATTEMPTS", 20)
    assert all(svc.verify_code("+15550005001", x) is False for x in ALL_DEMO)
