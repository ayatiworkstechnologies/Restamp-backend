"""Regression tests for the OTP lockout fix (no DB; unit-level OTPService).

The old lockout: every code request (including ones that ended in a
successful login, and every resend) stayed in the phone's rolling one-hour
window; hitting the cap also set a phone block that refused verification of
the code already delivered, and was re-armed on each retry while the window
was still full, so a phone stayed locked for well over an hour.

Now: request caps (per phone, per IP) are rolling windows that reject only the
excess request; a successful verification clears the phone's request window;
wrong codes trip a single fixed OTP_BLOCK_SECONDS block that retries never
extend. Codes are read from the test SMS inbox (the delivery seam), never
from an API response.
"""
from __future__ import annotations

import pathlib
import types

import pytest
from fastapi import HTTPException

from app import config as config_module
from app.services import otp as otp_module
from app.services.otp import OTPService
from tests.conftest import TestSMSInbox

PHONE = "+916911000001"
IP = "10.20.30.40"


class Clock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(otp_module, "time", types.SimpleNamespace(time=c.time))
    return c


@pytest.fixture
def svc():
    s = OTPService()
    s.delivery = TestSMSInbox()
    return s


def settings():
    return config_module.settings


def wrong(code: str) -> str:
    return f"{(int(code) + 1) % 10**len(code):0{len(code)}d}"


def login(svc: OTPService, phone: str = PHONE, ip: str | None = IP) -> None:
    svc.request_code(phone, ip=ip)
    assert svc.verify_code(phone, svc.delivery.codes[phone], ip=ip) is True


def assert_429(fn, *args, **kwargs):
    with pytest.raises(HTTPException) as e:
        fn(*args, **kwargs)
    assert e.value.status_code == 429
    return e.value


# 1. Legitimate repeated use -----------------------------------------------------

def test_new_defaults_are_the_documented_values():
    s = settings()
    assert (s.OTP_TTL_SECONDS, s.OTP_MAX_REQUESTS_PER_HOUR, s.OTP_MAX_VERIFY_ATTEMPTS, s.OTP_BLOCK_SECONDS) == (300, 10, 5, 600)
    assert (s.OTP_MAX_REQUESTS_PER_HOUR_PER_IP, s.OTP_MAX_VERIFY_ATTEMPTS_PER_IP) == (60, 30)


def test_render_production_config_matches_new_limits():
    render = (pathlib.Path(__file__).resolve().parents[1] / "render.yaml").read_text()
    for key, value in (
        ("RESTAMP_OTP_TTL_SECONDS", "300"),
        ("RESTAMP_OTP_MAX_REQUESTS_PER_HOUR", "10"),
        ("RESTAMP_OTP_MAX_VERIFY_ATTEMPTS", "5"),
        ("RESTAMP_OTP_BLOCK_SECONDS", "600"),
    ):
        assert f'- key: {key}\n        value: "{value}"' in render, key


def test_many_successful_logins_in_an_hour_never_lock_the_phone(clock, svc):
    # More successful logins than the hourly cap (old cap was 5 in total).
    for _ in range(settings().OTP_MAX_REQUESTS_PER_HOUR * 2):
        login(svc)
        clock.advance(60)


def test_resends_then_login_then_more_logins(clock, svc):
    # A user who needs a few resends, logs in, and later logs in again.
    for _ in range(3):
        svc.request_code(PHONE, ip=IP)  # resends
        clock.advance(30)
    assert svc.verify_code(PHONE, svc.delivery.codes[PHONE], ip=IP) is True
    for _ in range(settings().OTP_MAX_REQUESTS_PER_HOUR):
        login(svc)


def test_success_clears_phone_window_but_keeps_ip_window(clock, svc):
    login(svc)
    assert PHONE not in svc._requests
    assert len(svc._ip_requests[IP]) == 1


# 2/6. Temporary, self-recovering, not re-armed -------------------------------------

def test_request_cap_rejects_excess_but_delivered_code_still_works(clock, svc):
    for _ in range(settings().OTP_MAX_REQUESTS_PER_HOUR):
        svc.request_code(PHONE, ip=IP)
        clock.advance(10)
    last_code = svc.delivery.codes[PHONE]
    assert_429(svc.request_code, PHONE, ip=IP)
    # The cap set no block: the code the user already has still logs them in.
    assert svc.verify_code(PHONE, last_code, ip=IP) is True
    # ...and that success frees the phone immediately.
    svc.request_code(PHONE, ip=IP)


def test_request_cap_recovers_on_rolling_window_and_retries_do_not_extend_it(clock, svc):
    start = clock.now
    for _ in range(settings().OTP_MAX_REQUESTS_PER_HOUR):
        svc.request_code(PHONE, ip=IP)
    # Keep retrying during the hour: each is refused, none extends anything.
    # (The old code re-armed a 15-minute block on these retries; the one at
    # +3590 would have locked the phone until ~+4490.)
    for _ in range(6):
        clock.advance(500)
        assert_429(svc.request_code, PHONE, ip=IP)
    clock.now = start + 3590
    assert_429(svc.request_code, PHONE, ip=IP)
    clock.now = start + 3600 + 1  # the first requests have left the window
    svc.request_code(PHONE, ip=IP)
    assert svc.verify_code(PHONE, svc.delivery.codes[PHONE], ip=IP) is True


def test_wrong_codes_block_for_a_fixed_period_then_recover(clock, svc):
    svc.request_code(PHONE, ip=IP)
    code = svc.delivery.codes[PHONE]
    for _ in range(settings().OTP_MAX_VERIFY_ATTEMPTS - 1):
        assert svc.verify_code(PHONE, wrong(code), ip=IP) is False
    blocked_at = clock.now
    assert_429(svc.verify_code, PHONE, wrong(code), ip=IP)
    # While blocked: requests and verifies are refused (the code was invalidated)...
    clock.advance(settings().OTP_BLOCK_SECONDS / 2)
    assert_429(svc.request_code, PHONE, ip=IP)
    assert_429(svc.verify_code, PHONE, code, ip=IP)
    # ...and those retries did not extend the block.
    clock.now = blocked_at + settings().OTP_BLOCK_SECONDS + 1
    svc.request_code(PHONE, ip=IP)
    assert svc.verify_code(PHONE, svc.delivery.codes[PHONE], ip=IP) is True


def test_a_few_wrong_codes_do_not_lock_the_phone(clock, svc):
    svc.request_code(PHONE, ip=IP)
    code = svc.delivery.codes[PHONE]
    for _ in range(settings().OTP_MAX_VERIFY_ATTEMPTS - 1):
        assert svc.verify_code(PHONE, wrong(code), ip=IP) is False
    assert svc.verify_code(PHONE, code, ip=IP) is True


# 3. Limits still bound abuse --------------------------------------------------------

def test_request_flood_still_limited_per_phone(clock, svc):
    for _ in range(settings().OTP_MAX_REQUESTS_PER_HOUR):
        svc.request_code(PHONE)
    assert_429(svc.request_code, PHONE)


def test_wrong_code_guessing_still_capped_per_code(clock, svc):
    svc.request_code(PHONE)
    code = svc.delivery.codes[PHONE]
    for _ in range(settings().OTP_MAX_VERIFY_ATTEMPTS - 1):
        svc.verify_code(PHONE, wrong(code))
    err = assert_429(svc.verify_code, PHONE, wrong(code))
    assert err.detail == "Too many wrong attempts. Try again later."
    assert PHONE not in svc._codes  # the guessed-at code is invalidated


# 4. Per-IP protection ---------------------------------------------------------

def test_ip_request_cap_still_applies_across_phones(clock, svc):
    first = "+916912000000"
    svc.request_code(first, ip=IP)
    first_code = svc.delivery.codes[first]
    for i in range(1, settings().OTP_MAX_REQUESTS_PER_HOUR_PER_IP):
        svc.request_code(f"+91691200{i:04d}", ip=IP)
    assert_429(svc.request_code, "+916912999999", ip=IP)
    svc.request_code("+916912999998", ip="10.20.30.41")  # other IPs unaffected
    # The IP cap rejects new requests only; already-delivered codes still verify.
    assert svc.verify_code(first, first_code, ip=IP) is True


def test_ip_wrong_code_cap_still_blocks(clock, svc, monkeypatch):
    monkeypatch.setattr(config_module.settings, "OTP_MAX_VERIFY_ATTEMPTS_PER_IP", 4)
    tripped = False
    for i in range(5):
        phone = f"+91691300{i:04d}"
        svc.request_code(phone, ip=IP)
        try:
            svc.verify_code(phone, wrong(svc.delivery.codes[phone]), ip=IP)
        except HTTPException as e:
            assert e.status_code == 429
            tripped = True
            break
    assert tripped
    assert_429(svc.request_code, "+916913999999", ip=IP)  # IP is blocked
    clock.advance(settings().OTP_BLOCK_SECONDS + 1)
    svc.request_code("+916913999999", ip=IP)  # and recovers after the block


# 5. Expiry and resend ------------------------------------------------------------

def test_expired_code_is_rejected_without_counting_as_a_wrong_attempt(clock, svc):
    svc.request_code(PHONE, ip=IP)
    code = svc.delivery.codes[PHONE]
    clock.advance(settings().OTP_TTL_SECONDS + 1)
    assert svc.verify_code(PHONE, code, ip=IP) is False
    assert svc._ip_verify_failures.get(IP) is None
    # A fresh request works and its code verifies.
    svc.request_code(PHONE, ip=IP)
    assert svc.verify_code(PHONE, svc.delivery.codes[PHONE], ip=IP) is True


def test_code_still_valid_just_before_expiry(clock, svc):
    svc.request_code(PHONE)
    clock.advance(settings().OTP_TTL_SECONDS - 1)
    assert svc.verify_code(PHONE, svc.delivery.codes[PHONE]) is True


def test_resend_replaces_the_previous_code(clock, svc):
    svc.request_code(PHONE, ip=IP)
    old = svc.delivery.codes[PHONE]
    clock.advance(30)
    svc.request_code(PHONE, ip=IP)
    new = svc.delivery.codes[PHONE]
    if old != new:  # 1-in-a-million collision is not a failure of the rule
        assert svc.verify_code(PHONE, old, ip=IP) is False
    assert svc.verify_code(PHONE, new, ip=IP) is True


def test_codes_are_single_use(clock, svc):
    login(svc)
    assert svc.verify_code(PHONE, svc.delivery.codes[PHONE], ip=IP) is False
