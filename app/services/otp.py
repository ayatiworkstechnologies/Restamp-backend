"""RESTAMP phone OTP — the fixed testing code (local/staging only).

The random-code OTP pipeline (Python `secrets` generation, phone-bound
SHA-256 storage, expiry, single use, attempt counters, per-phone/per-IP
rate limits and the SMS delivery seam) was removed with the simplified
login. There is ONE fixed testing code, accepted only where testing is
allowed:

- TEST_OTP = "349086" is the only code. It is verified with a
  constant-time comparison (never a plain `==` on attacker-supplied
  input), so the correctness of the check does not depend on string
  comparison timing.
- otp_display_permitted() (app/config.py) is the single environment gate:
  an explicit development RESTAMP_ENV (local/development/dev/test) or
  staging. Every other value — production, prod, unset, unknown, demo —
  is excluded, so the fixed code can never leak into or authenticate on a
  real deployment. There is no environment variable that can widen the
  gate: obsolete OTP settings are not read anywhere, and production
  startup refuses the leftover RESTAMP_OTP_DISPLAY_MODE flag outright.
- Production/prod behaviour: request_code fails closed with a generic 503
  BEFORE anything else (no SMS provider exists, so nothing can be sent
  and no OTP is ever returned in a response); verify_code rejects every
  code (401 "Invalid code" from the router). Phone-OTP login is therefore
  unavailable in production exactly as before this simplification —
  Google login and every other authenticated flow are unchanged.
- The service holds NO state: no storage, no expiry, no counters, no
  locks, no reset hook. Repeat logins with the correct code each issue a
  fresh session; a wrong code is always rejected. This is acceptable
  precisely because the code is fixed, public to testers, and never
  accepted outside local/staging — none of the removed protections
  (hashing, expiry, attempt caps, rate limits) would protect a value that
  is published by design.

Deliberately NOT here (and not to be reintroduced without a product
decision): demo logins, phone allowlists, SMS-provider code, or any path
that authenticates without the fixed-OTP check. Registration,
account isolation, roles and sessions are unchanged: the auth router
still resolves/creates the account for the requested phone, issues the
JWT through security.create_access_token, and enforces ensure_active_user
and the normal role rules.
"""
from __future__ import annotations

import secrets

from fastapi import HTTPException, status

from ..config import otp_display_permitted

TEST_OTP = "349086"
UNAVAILABLE_503 = "OTP sign-in is not available. Try again later."


def request_code(phone: str) -> str | None:
    """The code for `phone`: TEST_OTP in local/staging; otherwise fail closed.

    Returns the fixed testing code for the router to display as
    `debug_code` when otp_display_permitted() is true (local/development/
    dev/test or staging). Every other environment raises a generic 503:
    no SMS provider exists, no code can be sent, and no OTP is ever
    disclosed in a production response. `phone` is validated by the
    request schema (Indian mobiles only); the code is identical for every
    number, so the service needs no per-phone state.
    """
    if not otp_display_permitted():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=UNAVAILABLE_503,
        )
    return TEST_OTP


def verify_code(phone: str, code: str) -> bool:
    """True only when `code` is the fixed testing OTP in local/staging.

    Production-classified environments (production/prod, unset/unknown,
    demo) reject EVERY code — including TEST_OTP — so the testing
    mechanism can never authenticate a real deployment, and no universal
    bypass exists. An empty/missing code is rejected; the comparison is
    constant-time.
    """
    supplied = (code or "").strip()
    if not otp_display_permitted() or not supplied:
        return False
    return secrets.compare_digest(TEST_OTP, supplied)
