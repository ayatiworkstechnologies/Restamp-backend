"""Restricted internal-tester OTP display (RESTAMP_OTP_TEST_DISPLAY).

Contract (tested below, production configuration throughout unless noted):
- off by default; with it off nothing changes (production still 503, no code);
- a code is returned ONLY when the switch is on AND the phone is in the
  server-configured RESTAMP_OTP_TEST_PHONES AND the connection's client IP is
  inside RESTAMP_OTP_TEST_ALLOWED_CIDRS — all three from server config;
- nothing the client sends (body fields, query params, a spoofed
  X-Forwarded-For) can satisfy those conditions;
- every OTP rule is unchanged for testers: six digits, single use, expiry,
  wrong-attempt cap, rate limits; the code is never logged;
- production startup refuses an incomplete or over-broad configuration and
  still refuses RESTAMP_OTP_DISPLAY_MODE=1.

The client IP is set with Starlette's TestClient(client=(host, port)).
Documentation ranges only (RFC 5737): 198.51.100.0/24 is the "office",
203.0.113.0/24 is "the internet". Rollback-isolated DB, no network, no SMS.
"""
import logging
import re

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import config as config_module
from app.config import otp_test_display_permitted, validate_security_config
from app.services.otp import OTPService, otp_service

STRONG = "q" * 40
TESTER = "+919876500001"
OTHER = "+919876500002"
OFFICE_IP = "198.51.100.25"
OUTSIDE_IP = "203.0.113.5"
OFFICE_NET = "198.51.100.0/24"
MESSAGE = "If the number is valid, an OTP was sent."


def _production(monkeypatch, **overrides) -> None:
    s = config_module.settings
    values = dict(ENV="production", JWT_SECRET=STRONG, OTP_DISPLAY_MODE=False, GOOGLE_DEV_MODE=False,
                  GOOGLE_ENABLED=False, GOOGLE_CLIENT_ID="", CORS_ORIGINS="https://restamp.in",
                  TRUST_PROXY=False, OTP_TEST_DISPLAY=False, OTP_TEST_PHONES="", OTP_TEST_ALLOWED_CIDRS="")
    values.update(overrides)
    for key, value in values.items():
        monkeypatch.setattr(s, key, value)


def _tester_config(monkeypatch, **overrides) -> None:
    values = dict(OTP_TEST_DISPLAY=True, OTP_TEST_PHONES=TESTER, OTP_TEST_ALLOWED_CIDRS=OFFICE_NET)
    values.update(overrides)
    _production(monkeypatch, **values)


def _from(c, ip: str) -> TestClient:
    """A client for the same (rollback-isolated) app, connecting from `ip`."""
    return TestClient(c.app, client=(ip, 50000))


# --- defaults ---------------------------------------------------------------------

def test_off_by_default():
    s = config_module.settings
    assert s.OTP_TEST_DISPLAY is False and s.OTP_TEST_PHONES == "" and s.OTP_TEST_ALLOWED_CIDRS == ""
    assert not otp_test_display_permitted(TESTER, OFFICE_IP)


@pytest.mark.parametrize("missing", ["OTP_TEST_DISPLAY", "OTP_TEST_PHONES", "OTP_TEST_ALLOWED_CIDRS"])
def test_any_missing_setting_disables_it(monkeypatch, missing):
    _tester_config(monkeypatch, **{missing: False if missing == "OTP_TEST_DISPLAY" else ""})
    assert not otp_test_display_permitted(TESTER, OFFICE_IP)


def test_production_without_config_still_503_and_no_code(client, monkeypatch):
    c, _ = client
    _production(monkeypatch)
    monkeypatch.setattr(otp_service, "delivery", None)
    r = _from(c, OFFICE_IP).post("/auth/otp/request", json={"phone": TESTER})
    assert r.status_code == 503 and "debug_code" not in r.text


# --- authorization ----------------------------------------------------------------

def test_authorized_tester_receives_code_and_logs_in_once(client, monkeypatch):
    c, _ = client
    _tester_config(monkeypatch)
    monkeypatch.setattr(otp_service, "delivery", None)  # no SMS provider, as in production
    tc = _from(c, OFFICE_IP)
    r = tc.post("/auth/otp/request", json={"phone": TESTER})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"message", "debug_code"} and body["message"] == MESSAGE
    code = body["debug_code"]
    assert re.fullmatch(r"\d{6}", code)
    ok = tc.post("/auth/otp/verify", json={"phone": TESTER, "code": code})
    assert ok.status_code == 200 and "access_token" in ok.json()
    assert tc.post("/auth/otp/verify", json={"phone": TESTER, "code": code}).status_code == 401  # single use


def test_unlisted_phone_on_office_network_gets_no_code(client, monkeypatch):
    c, _ = client
    _tester_config(monkeypatch)
    monkeypatch.setattr(otp_service, "delivery", None)
    r = _from(c, OFFICE_IP).post("/auth/otp/request", json={"phone": OTHER})
    assert r.status_code == 503 and "debug_code" not in r.text


def test_listed_phone_from_outside_network_gets_no_code(client, monkeypatch):
    c, _ = client
    _tester_config(monkeypatch)
    monkeypatch.setattr(otp_service, "delivery", None)
    r = _from(c, OUTSIDE_IP).post("/auth/otp/request", json={"phone": TESTER})
    assert r.status_code == 503 and "debug_code" not in r.text


def test_ordinary_user_response_unchanged_when_delivery_exists(client, monkeypatch):
    """With a real delivery connected, non-testers get the plain message and an SMS."""
    c, _ = client
    _tester_config(monkeypatch)
    r = _from(c, OUTSIDE_IP).post("/auth/otp/request", json={"phone": TESTER})
    assert r.status_code == 200 and r.json() == {"message": MESSAGE}
    assert otp_service.delivery.codes.get(TESTER)  # went through the SMS seam instead


def test_request_fields_cannot_enable_it(client, monkeypatch):
    c, _ = client
    _tester_config(monkeypatch)
    monkeypatch.setattr(otp_service, "delivery", None)
    tc = _from(c, OUTSIDE_IP)
    attempts = [
        tc.post("/auth/otp/request", json={"phone": OTHER, "debug": True, "test_display": 1, "display": "1"}),
        tc.post("/auth/otp/request?debug=1&display=1&test=1", json={"phone": OTHER}),
        tc.post("/auth/otp/request", json={"phone": OTHER},
                headers={"X-Restamp-Tester": "1", "X-Debug": "1", "X-Real-IP": OFFICE_IP}),
    ]
    for r in attempts:
        assert r.status_code in (422, 503) and "debug_code" not in r.text


def test_spoofed_forwarded_for_is_ignored_without_trusted_proxy(client, monkeypatch):
    c, _ = client
    _tester_config(monkeypatch, TRUST_PROXY=False)
    monkeypatch.setattr(otp_service, "delivery", None)
    r = _from(c, OUTSIDE_IP).post("/auth/otp/request", json={"phone": TESTER},
                                  headers={"X-Forwarded-For": OFFICE_IP})
    assert r.status_code == 503 and "debug_code" not in r.text


def test_spoofed_forwarded_for_is_ignored_behind_trusted_proxy(client, monkeypatch):
    """Behind the proxy only the rightmost (proxy-appended) entry counts: a
    client-supplied office address earlier in the chain is ignored."""
    c, _ = client
    _tester_config(monkeypatch, TRUST_PROXY=True)
    monkeypatch.setattr(otp_service, "delivery", None)
    proxy = _from(c, "192.0.2.10")
    r = proxy.post("/auth/otp/request", json={"phone": TESTER},
                   headers={"X-Forwarded-For": f"{OFFICE_IP}, {OUTSIDE_IP}"})
    assert r.status_code == 503 and "debug_code" not in r.text
    ok = proxy.post("/auth/otp/request", json={"phone": TESTER},
                    headers={"X-Forwarded-For": f"{OUTSIDE_IP}, {OFFICE_IP}"})
    assert ok.status_code == 200 and re.fullmatch(r"\d{6}", ok.json()["debug_code"])


def test_loopback_and_over_broad_networks_never_qualify(monkeypatch):
    for nets, ip in (("127.0.0.0/8", "127.0.0.1"), ("::1/128", "::1"), ("0.0.0.0/0", OUTSIDE_IP),
                     ("203.0.0.0/8", OUTSIDE_IP), ("2001:db8::/32", "2001:db8::1")):
        _tester_config(monkeypatch, OTP_TEST_ALLOWED_CIDRS=nets)
        assert not otp_test_display_permitted(TESTER, ip), nets


def test_ipv6_office_range_works(monkeypatch):
    _tester_config(monkeypatch, OTP_TEST_ALLOWED_CIDRS="2001:db8:1234::/48")
    assert otp_test_display_permitted(TESTER, "2001:db8:1234::7")
    assert not otp_test_display_permitted(TESTER, "2001:db8:9999::7")


# --- OTP rules unchanged for testers ----------------------------------------------

def test_tester_rate_limit_unchanged(monkeypatch):
    _tester_config(monkeypatch)
    svc = OTPService()
    for _ in range(config_module.settings.OTP_MAX_REQUESTS_PER_HOUR):
        assert svc.request_code(TESTER, ip=OFFICE_IP)
    with pytest.raises(HTTPException) as info:
        svc.request_code(TESTER, ip=OFFICE_IP)
    assert info.value.status_code == 429


def test_tester_wrong_attempts_bounded(monkeypatch):
    _tester_config(monkeypatch)
    svc = OTPService()
    code = svc.request_code(TESTER, ip=OFFICE_IP)
    wrong = f"{(int(code) + 1) % 10**6:06d}"
    for _ in range(config_module.settings.OTP_MAX_VERIFY_ATTEMPTS - 1):
        assert svc.verify_code(TESTER, wrong, ip=OFFICE_IP) is False
    with pytest.raises(HTTPException) as info:
        svc.verify_code(TESTER, wrong, ip=OFFICE_IP)
    assert info.value.status_code == 429
    with pytest.raises(HTTPException):
        svc.verify_code(TESTER, code, ip=OFFICE_IP)


def test_tester_code_expires(monkeypatch):
    _tester_config(monkeypatch)
    svc = OTPService()
    code = svc.request_code(TESTER, ip=OFFICE_IP)
    assert config_module.settings.OTP_TTL_SECONDS == 300
    svc._codes[TESTER].expires_at = 0.0  # white-box expiry
    assert svc.verify_code(TESTER, code, ip=OFFICE_IP) is False


def test_tester_codes_are_random_and_hash_stored(monkeypatch):
    _tester_config(monkeypatch)
    svc = OTPService()
    codes = set()
    for _ in range(5):
        svc.reset()
        code = svc.request_code(TESTER, ip=OFFICE_IP)
        assert code not in repr(svc._codes[TESTER])  # only the hash is kept
        codes.add(code)
    assert len(codes) > 1  # not a fixed code


def test_code_and_phone_never_logged(client, monkeypatch, caplog):
    c, _ = client
    _tester_config(monkeypatch)
    monkeypatch.setattr(otp_service, "delivery", None)
    tc = _from(c, OFFICE_IP)
    with caplog.at_level(logging.DEBUG):
        code = tc.post("/auth/otp/request", json={"phone": TESTER}).json()["debug_code"]
        tc.post("/auth/otp/verify", json={"phone": TESTER, "code": code})
        validate_security_config()
    joined = "\n".join(rec.getMessage() for rec in caplog.records)
    assert code not in joined and TESTER not in joined and OFFICE_NET not in joined


# --- production startup validation -------------------------------------------------

def test_production_boots_with_valid_tester_config_and_warns(monkeypatch, caplog):
    _tester_config(monkeypatch, OTP_TEST_PHONES=f"{TESTER},{OTHER}", OTP_TEST_ALLOWED_CIDRS=f"{OFFICE_NET},2001:db8:1::/48")
    with caplog.at_level(logging.WARNING):
        validate_security_config()
    assert "RESTAMP_OTP_TEST_DISPLAY is enabled" in caplog.text and "2 staff" in caplog.text


@pytest.mark.parametrize("phones,nets", [
    ("", OFFICE_NET),                          # no phones
    ("+15005550006", OFFICE_NET),              # not an Indian mobile
    (f"{TESTER},garbage", OFFICE_NET),         # one invalid entry
    (",".join(f"+9198765{i:05d}" for i in range(21)), OFFICE_NET),  # too many
    (TESTER, ""),                              # no networks
    (TESTER, "not-a-cidr"),
    (TESTER, f"{OFFICE_NET},nonsense"),
    (TESTER, "0.0.0.0/0"),
    (TESTER, "::/0"),
    (TESTER, "127.0.0.0/8"),
    (TESTER, "::1/128"),
    (TESTER, "198.51.0.0/16"),                 # broader than /24
    (TESTER, "2001:db8::/32"),                 # broader than /48
])
def test_production_refuses_incomplete_or_broad_config(monkeypatch, phones, nets):
    _tester_config(monkeypatch, OTP_TEST_PHONES=phones, OTP_TEST_ALLOWED_CIDRS=nets)
    with pytest.raises(RuntimeError) as info:
        validate_security_config()
    assert TESTER not in str(info.value) and "+15005550006" not in str(info.value)


def test_production_still_refuses_public_display_mode(monkeypatch):
    _tester_config(monkeypatch, OTP_DISPLAY_MODE=True)
    with pytest.raises(RuntimeError):
        validate_security_config()


def test_no_production_override_reintroduced():
    assert not hasattr(config_module.settings, "OTP_DISPLAY_IN_PRODUCTION")
