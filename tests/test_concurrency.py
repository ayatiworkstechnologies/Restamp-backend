"""Foundation concurrency regression: shared OTP state must behave deterministically
when many threads hit the same phone simultaneously (uvicorn runs sync handlers
in a thread pool). Uses isolated OTPService instances; no DB involved."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException

from app.services.otp import OTPService
from tests.conftest import TestSMSInbox


def test_concurrent_correct_verify_consumed_once():
    svc = OTPService()
    inbox = TestSMSInbox()
    svc.delivery = inbox
    phone = "+916900000001"
    assert svc.request_code(phone) is None  # codes are never returned
    code = inbox.codes[phone]  # read the SMS via the test inbox
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda _: svc.verify_code(phone, code), range(20)))
    assert results.count(True) == 1  # exactly one winner consumes the code
    assert results.count(False) == 19


def test_concurrent_wrong_attempts_block_deterministically():
    svc = OTPService()
    inbox = TestSMSInbox()
    svc.delivery = inbox
    phone = "+916900000002"
    assert svc.request_code(phone) is None  # codes are never returned
    assert phone in svc._codes  # a live code exists for the wrong attempts to hit

    def wrong(_):
        try:
            return svc.verify_code(phone, "000000")
        except HTTPException as e:
            return e.status_code

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(wrong, range(10)))
    assert 429 in results  # attempt cap tripped despite concurrency
    with pytest.raises(HTTPException) as e:
        svc.verify_code(phone, "000000")
    assert e.value.status_code == 429  # phone stays blocked
