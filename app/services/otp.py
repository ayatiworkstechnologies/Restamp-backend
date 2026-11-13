"""RESTAMP phone OTP — the fixed code (local/staging; production by opt-in).

The random-code OTP pipeline (Python `secrets` generation, phone-bound
SHA-256 storage, expiry, single use, attempt counters, per-phone/per-IP
rate limits and the SMS delivery seam) was removed with the simplified
login. There is ONE fixed testing code, accepted only where testing is
allowed:

- TEST_OTP = "349086" is the only code. It is verified with a
  constant-time comparison (never a plain `==` on attacker-supplied
  input), so the correctness of the check does not depend on string
  comparison timing.
- otp_display_permitted() (app/config.py) is the environment gate for
  DISPLAY: an explicit development RESTAMP_ENV (local/development/dev/test)
  or staging. Every other value — production, prod, unset, unknown, demo —
  is excluded, so the fixed code never leaks into a production response.
  No environment variable can widen display: obsolete OTP settings are not
  read anywhere, and production startup refuses the leftover
  RESTAMP_OTP_DISPLAY_MODE flag outright. ACCEPTANCE uses
  fixed_otp_permitted(): the same gate plus the explicit production opt-in
  described below.
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

Production opt-in (owner decision): RESTAMP_PRODUCTION_FIXED_OTP=1 with
RESTAMP_ENV=production/prod (config.fixed_otp_in_production) makes
production ACCEPT the same fixed code. The code is still never returned
there (request_code returns None; display stays on otp_display_permitted),
and the auth router refuses admin accounts and marks the session
amr=fixed_otp (see app/routers/auth.py and app/deps.py). With the opt-in
on, anyone can sign in as any ordinary phone account: that is the accepted
cost of having no SMS provider.

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

from ..config import fixed_otp_permitted, otp_display_permitted

TEST_OTP = "349086"
UNAVAILABLE_503 = "OTP sign-in is not available. Try again later."


def request_code(phone: str) -> str | None:
    """The code for `phone`: TEST_OTP in local/staging; None under the
    production opt-in (accepted, never displayed); otherwise fail closed.

    Returns the fixed testing code for the router to display as
    `debug_code` when otp_display_permitted() is true (local/development/
    dev/test or staging). With RESTAMP_PRODUCTION_FIXED_OTP=1 in
    production it returns None: the router answers the generic 200 message
    and the user types the published code. Every other environment raises
    a generic 503. No OTP is ever disclosed in a production response.
    `phone` is validated by the request schema (Indian mobiles only); the
    code is identical for every number, so the service needs no per-phone
    state.
    """
    if otp_display_permitted():
        return TEST_OTP
    if fixed_otp_permitted():
        # Production opt-in: the code is accepted at verification but is
        # never handed back in a response.
        return None
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=UNAVAILABLE_503,
    )


def verify_code(phone: str, code: str) -> bool:
    """True only when `code` is the fixed OTP in local/staging, or in
    production with the explicit RESTAMP_PRODUCTION_FIXED_OTP=1 opt-in.

    Without the opt-in, production-classified environments (production/prod,
    unset/unknown, demo) reject EVERY code — including TEST_OTP — so the testing
    mechanism can never authenticate a real deployment, and no universal
    bypass exists. An empty/missing code is rejected; the comparison is
    constant-time.
    """
    supplied = (code or "").strip()
    if not fixed_otp_permitted() or not supplied:
        return False
    # Compare UTF-8 bytes: compare_digest raises TypeError on non-ASCII str,
    # and the request schema's \d also admits non-ASCII Unicode digits, so a
    # str comparison could turn a wrong code into a 500 instead of a 401.
    return secrets.compare_digest(TEST_OTP.encode("utf-8"), supplied.encode("utf-8"))
