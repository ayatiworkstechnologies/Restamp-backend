"""Tests for LOCAL demo OTP mode (RESTAMP_OTP_DEMO_MODE).

Demo codes are accepted only when demo mode is enabled AND a live OTP
request exists. Nothing is returned, displayed, or logged. Disabled mode:
zero effect (normal behavior, including rejection of demo codes).
"""
import pytest

from app import config as config_module
from app.services.otp import DEMO_OTPS, OTPService

ALL_DEMO = sorted(DEMO_OTPS)
assert len(ALL_DEMO) == 10


@pytest.fixture()
def demo_on(monkeypatch):
    monkeypatch.setattr(config_module.settings, "OTP_DEMO_MODE", True)


@pytest.fixture()
def demo_off(monkeypatch):
    monkeypatch.setattr(config_module.settings, "OTP_DEMO_MODE", False)


def test_each_demo_code_succeeds_in_demo_mode(demo_on):
    for code in ALL_DEMO:
        svc = OTPService()
        svc.request_code("+10000000201")
        assert svc.verify_code("+10000000201", code) is True


def test_invalid_code_fails_in_demo_mode(demo_on):
    svc = OTPService()
    svc.request_code("+10000000202")
    assert svc.verify_code("+10000000202", "111111") is False


def test_demo_code_without_request_fails(demo_on):
    assert OTPService().verify_code("+10000000203", ALL_DEMO[0]) is False


def test_demo_codes_rejected_when_disabled(demo_off):
    for code in ALL_DEMO:
        svc = OTPService()
        svc.request_code("+10000000204")
        assert svc.verify_code("+10000000204", code) is False


def test_normal_otp_still_works_in_demo_mode(demo_on):
    svc = OTPService()
    real = svc.request_code("+10000000205")
    assert svc.verify_code("+10000000205", real) is True


def test_demo_code_consumes_entry(demo_on):
    svc = OTPService()
    svc.request_code("+10000000206")
    assert svc.verify_code("+10000000206", ALL_DEMO[0]) is True
    # Entry consumed: a second attempt (even the real path) finds nothing.
    assert svc.verify_code("+10000000206", ALL_DEMO[1]) is False


def test_demo_verify_endpoint_accepts_demo_code(client, demo_on):
    c, _ = client
    phone = "+10000000207"
    assert c.post("/auth/otp/request", json={"phone": phone}).status_code == 200
    r = c.post("/auth/otp/verify", json={"phone": phone, "code": ALL_DEMO[3]})
    assert r.status_code == 200, r.text
    assert "access_token" in r.json()


def test_demo_verify_endpoint_rejects_when_disabled(client, demo_off):
    c, _ = client
    phone = "+10000000208"
    assert c.post("/auth/otp/request", json={"phone": phone}).status_code == 200
    r = c.post("/auth/otp/verify", json={"phone": phone, "code": ALL_DEMO[3]})
    assert r.status_code == 401, r.text
