"""RESTAMP — restricted test-only login for deployments without SMS delivery.

WHY THIS EXISTS: with no SMS provider connected, code-connected production
deployments keep /auth/otp/request fail-closed (generic 503) — by design —
so phone login is unusable until real SMS delivery is integrated. During
that window this module offers a narrow, explicitly authorized ALTERNATIVE
that works without SMS and without ever generating, storing, returning, or
logging an OTP code:

- `POST /auth/test-login` issues the same TokenOut as `/auth/otp/verify`,
  but ONLY when ALL guards hold: RESTAMP_TEST_LOGIN_ENABLED=1, the
  request carries the RESTAMP_TEST_LOGIN_KEY value (compared in constant
  time via `secrets.compare_digest`), AND the configured authorization
  mode (RESTAMP_TEST_LOGIN_MODE) holds:
  * "allowlist" (default): the phone must be on the explicit
    RESTAMP_TEST_LOGIN_ALLOWED_PHONES allowlist;
  * "existing": ANY valid Indian mobile may log in ONLY when a Restamp
    account for that phone identity ALREADY exists — unknown phones get a
    clear 404 ("No account found for this phone number.") with ZERO rows
    written, registration is impossible, and the allowlist is not
    consulted. Mode checks run strictly AFTER the key verification, so
    the account-existence distinction is never an unauthenticated oracle.
  * "suffix": presenter convenience ONLY — the sole authorization is the
    LAST SIX DIGITS of the entered phone (predictable by design, NOT a
    secret and never proof of ownership). It is active ONLY inside the
    RESTAMP_TEST_LOGIN_SUFFIX_UNTIL window (a future, at-most-24h-ahead
    ISO-8601 deadline, required and bounded at production boot); once the
    deadline passes the endpoint self-disarms to the exact inactive 404,
    re-checked on EVERY request. The X-Test-Login-Key header is NOT
    consulted in this mode (the long-key flow remains fully available in
    the other modes); existing accounts only, registration is impossible,
    and a wrong code gets the same generic 401 whether the phone has an
    account or not (no account-existence oracle). The shared per-IP /
    per-phone rate limits apply exactly as to keyed modes.
- Disabled by default, twice over: the flag defaults to "0", and the
  feature is also inactive when RESTAMP_TEST_LOGIN_KEY is empty. An
  inactive deployment answers 404 {"detail": "Not Found"} — the SAME
  body FastAPI gives to unknown routes — so the endpoint's existence is
  never disclosed.
- The allowlist FAILS CLOSED: an empty RESTAMP_TEST_LOGIN_ALLOWED_PHONES
  rejects EVERY phone (403 "Forbidden") even with a valid key, and no
  account is ever created for a phone outside the list — the list is the
  ONLY permission to use the endpoint. It is checked AFTER the key
  comparison (a bad key gets the same generic 401 for any phone, so
  allowlist membership is never an oracle) and BEFORE any database
  access.
- Account CREATION needs its own separate flag: with
  RESTAMP_TEST_LOGIN_ALLOW_REGISTRATION unset (the default "0"), EXISTING
  allowlisted accounts log in normally, but an allowlisted UNKNOWN phone
  is rejected with the SAME generic 401 body as a wrong key (no
  account-existence oracle, nothing written) — with the flag "1", an
  unknown allowlisted phone registers exactly like /auth/otp/verify does.
- Deliberately SEPARATE from the local OTP display mode: independent env
  keys, service, and state; this module never reads or writes
  RESTAMP_OTP_DISPLAY_MODE and never touches OTPService state. Enabling
  one never influences the other.
- Production hardening STRENGTHENS the existing gate: when
  RESTAMP_TEST_LOGIN_ENABLED=1 in a production-classified process,
  startup (validate_security_config, app/config.py) refuses to boot
  unless the key is strong (>= 32 chars, not a known placeholder), and
  refuses allowlist entries that could never match a login (an empty
  allowlist boots but is warned about — and rejects every attempt at
  request time). The default-disabled state requires nothing and boots
  as before.
- Throttling mirrors OTPService: in-memory, per process, thread-safe via
  a lock — per-IP attempt cap (failed AND successful attempts count, and
  failures cannot be evaded by rotating phones) plus a per-phone cap,
  each blocking for OTP_AUTH-style OTP_BLOCK_SECONDS after tripping.

Authorization is possession of the key PLUS explicit allowlisting of the
phone; ACCOUNT CREATION is additionally gated by
RESTAMP_TEST_LOGIN_ALLOW_REGISTRATION (default "0"): existing allowlisted
Indian mobiles always log in, while a first-time registration mirrors the
OTP verify flow (with `verified_by="test_login"` recorded on
auth_identities so test-issued identities stay auditable/distinct from
real SMS onboarding) ONLY when that flag is "1" as well. Treat
RESTAMP_TEST_LOGIN_KEY as a live credential and the allowlist as the
sponsor roster: enable only for bounded testing windows, then set
RESTAMP_TEST_LOGIN_ENABLED=0 (and/or rotate the key and empty the
allowlist) when testing is done. Never log the key or the phone.
"""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import HTTPException, status

from ..config import parse_test_login_suffix_until, settings

_UNAUTHORIZED_401 = "Unauthorized"
_FORBIDDEN_403 = "Forbidden"
_TOO_MANY_429 = "Too many attempts. Try again later."
# Existing-accounts-only ("existing") and suffix-demo ("suffix") modes:
# the explicit 404 shown for a valid phone with NO Restamp account.
# Reachable ONLY after full authorization — a valid key in "existing"
# mode, or a suffix-code match in "suffix" mode (which is the phone
# itself, inside the bounded expiry window and under the shared rate
# limits) — so it is never an UNauthenticated account-existence oracle.
_NO_ACCOUNT_404 = "No account found for this phone number."


@dataclass
class TestLoginService:
    # In-memory, per-process throttle state, guarded by _lock (same model as
    # OTPService; NOT shared across workers/hosts — multi-instance
    # deployments need a shared store, see module docstring).
    _ip_requests: dict[str, list[float]] = field(default_factory=dict)
    _phone_requests: dict[str, list[float]] = field(default_factory=dict)
    _blocked_until: dict[str, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def reset(self) -> None:
        """Test hook: clear all in-memory throttle state (incl. blocks)."""
        with self._lock:
            self._ip_requests.clear()
            self._phone_requests.clear()
            self._blocked_until.clear()

    def is_active(self) -> bool:
        """True only when flag on AND strong-key-shaped non-empty key AND
        a valid RESTAMP_TEST_LOGIN_MODE — and, for the "suffix" demo mode,
        ONLY inside its explicit, self-disarming expiry window.

        Fail-closed four ways: an enabled-but-misconfigured deployment
        (no key, an unknown mode value, or a suffix mode without a future
        RESTAMP_TEST_LOGIN_SUFFIX_UNTIL deadline) stays inactive; callers
        must treat inactive as "endpoint does not exist". The window is
        re-checked here on EVERY request, so a past deadline deactivates
        the endpoint (exact 404) without any redeploy.
        """
        if not settings.TEST_LOGIN_ENABLED:
            return False
        if not (settings.TEST_LOGIN_KEY or "").strip():
            return False
        mode = self.mode()
        if mode == "invalid":
            return False
        if mode == "suffix":
            deadline = self.suffix_deadline()
            return (
                deadline is not None
                and deadline > datetime.now(timezone.utc)
            )
        return True

    def suffix_deadline(self) -> "datetime | None":
        """Parsed RESTAMP_TEST_LOGIN_SUFFIX_UNTIL deadline, or None.

        Lenient via config.parse_test_login_suffix_until: 'Z', naive →
        UTC, explicit offsets honored. None (empty/unparseable) is
        fail-closed: suffix mode is inactive without the window. The
        >24h-ahead bound is enforced ONLY at production boot (a window
        that predates the process start is still honored at request time;
        the boot gate prevents configuring an unbounded window).
        """
        return parse_test_login_suffix_until(settings.TEST_LOGIN_SUFFIX_UNTIL)

    # Authorization modes (config: RESTAMP_TEST_LOGIN_MODE) — "allowlist"
    # keeps the original restricted behavior (explicit allowlist is the
    # ONLY permission; registration separately gated); "existing" permits
    # ONLY phones that already have a Restamp account (no allowlist, no
    # registration, zero writes for unknown phones); "suffix" is the
    # presenter convenience above (last-six-digit code, expiry-bounded).
    # All share the same never-rewritten-identity rule; suffix additionally
    # shares the key-independent per-IP/per-phone rate limits.
    _MODES = ("allowlist", "existing", "suffix")
    # Class attribute so the router (which binds the service INSTANCE) can
    # reference the explicit no-account message without a second import.
    NO_ACCOUNT_404 = _NO_ACCOUNT_404

    def mode(self) -> str:
        """Configured authorization mode: 'allowlist', 'existing', 'suffix',
        or 'invalid' (any unrecognized env value — treated as disabled)."""
        m = str(settings.TEST_LOGIN_MODE or "allowlist").strip().lower()
        return m if m in self._MODES else "invalid"

    def require_existing_user(self, user) -> None:
        """Existing-accounts-only mode: raise the explicit 404 when the
        phone has NO Restamp account.

        NEVER creates or modifies rows; the check runs strictly AFTER the
        key verification, so the message distinguishes accounts only for
        key holders and is never an unauthenticated oracle.
        """
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=self.NO_ACCOUNT_404,
            )

    def require_active(self) -> None:
        """Refuse with 404 when the mechanism is inactive.

        Uses the exact body FastAPI/Starlette generates for unknown routes
        ("Not Found") so a disabled endpoint is indistinguishable from a
        missing one.
        """
        if not self.is_active():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Not Found"
            )

    def record_attempt(self, ip: str | None, phone: str) -> None:
        """Count one login attempt and enforce the per-IP / per-phone caps.

        Called BEFORE the key comparison so wrong-key attempts (brute
        force) consume budget and trip the same caps. Blocking keys are
        the client IP ("not logged") and the phone (prefixed "phone::" in
        the shared block table so an IP string can never collide).
        """
        with self._lock:
            now = time.time()
            if ip and now < self._blocked_until.get(ip, 0):
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=_TOO_MANY_429,
                )
            phone_key = f"phone::{phone}"
            if now < self._blocked_until.get(phone_key, 0):
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=_TOO_MANY_429,
                )
            window = [
                t for t in self._ip_requests.get(ip, []) if now - t < 3600
            ]
            if ip:
                if len(window) >= settings.TEST_LOGIN_MAX_PER_HOUR_PER_IP:
                    self._blocked_until[ip] = now + settings.OTP_BLOCK_SECONDS
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail=_TOO_MANY_429,
                    )
                window.append(now)
                self._ip_requests[ip] = window
            phone_window = [
                t
                for t in self._phone_requests.get(phone, [])
                if now - t < 3600
            ]
            if (
                len(phone_window)
                >= settings.TEST_LOGIN_MAX_PER_HOUR_PER_PHONE
            ):
                self._blocked_until[phone_key] = (
                    now + settings.OTP_BLOCK_SECONDS
                )
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=_TOO_MANY_429,
                )
            phone_window.append(now)
            self._phone_requests[phone] = phone_window

    def require_key(self, candidate: str | None) -> None:
        """Authorize the attempt or fail with a generic 401.

        Constant-time comparison (bytes form: arbitrary-length UTF-8
        candidates can never raise). Missing/empty/wrong candidates are
        indistinguishable, and nothing ever logs the candidate, the
        configured key, or whether ANY phone exists — no oracle.
        """
        expected = (settings.TEST_LOGIN_KEY or "").strip()
        if not expected or not candidate:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=_UNAUTHORIZED_401,
            )
        if not secrets.compare_digest(
            candidate.encode("utf-8"), expected.encode("utf-8")
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=_UNAUTHORIZED_401,
            )

    # Length of the "suffix" demo code: the last six digits of the phone.
    # A string slice of the ALREADY schema-validated Indian mobile — the
    # leading zeros are part of the suffix and MUST be preserved (e.g.
    # "+917530006460" → "006460").
    _SUFFIX_LEN = 6

    def require_suffix_code(self, phone: str, code: str) -> None:
        """Authorize a "suffix"-mode attempt or fail with a generic 401.

        The demo code MUST equal the last six digits of the phone (string
        slice — leading zeros preserved), compared in constant time via
        secrets.compare_digest. This value is PREDICTABLE — anyone who can
        type the phone can derive it — so it is presenter convenience
        ONLY, never a secret and never proof of ownership; that is exactly
        why the mode is bounded by the hard expiry window checked in
        is_active() (self-disarming exact 404) and production-boot gates,
        shares the per-IP / per-phone rate limits, is existing-accounts-
        only, and structurally CANNOT create accounts.

        A code that is not exactly six ASCII digits (short, long, with a
        dropped zero, non-digit content, or surrounding whitespace) gets
        the SAME generic 401 — all in one bucket with any other wrong
        value, never a shape-revealing 422. The check runs BEFORE the
        account lookup, so a wrong code on ANY phone — one with an account
        or not — is byte-identical and account existence is never an
        oracle through code failures.
        """
        candidate = code if isinstance(code, str) else ""
        if len(candidate) != self._SUFFIX_LEN or not candidate.isascii() or (
            not candidate.isdigit()
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=_UNAUTHORIZED_401,
            )
        expected = phone[-self._SUFFIX_LEN :]
        if not secrets.compare_digest(
            candidate.encode("utf-8"), expected.encode("utf-8")
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=_UNAUTHORIZED_401,
            )

    def require_phone_allowed(self, phone: str) -> None:
        """Reject with a generic 403 unless the phone is explicitly allowlisted.

        RESTAMP_TEST_LOGIN_ALLOWED_PHONES is the ONLY permission for a phone
        to use this login; REGISTERING an unknown number additionally
        requires TEST_LOGIN_ALLOW_REGISTRATION=1 (see the next guard).
        FAIL CLOSED: an empty allowlist rejects every phone, and any phone
        not present is rejected EVEN WITH a valid key. There are no
        implicit default test numbers.

        Ordering contract (see the router): this call runs AFTER the key
        comparison, so only an authorized key holder can ever distinguish
        "allowlisted" (proceeds) from "not allowlisted" (403) — the
        allowlist membership is never an unauthenticated oracle. It also
        runs BEFORE any database access, so a rejected phone can never
        create a user, an identity, or any OTP state, and no response
        detail ever names the phone.
        """
        if phone not in (settings.TEST_LOGIN_ALLOWED_PHONES or ()):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=_FORBIDDEN_403,
            )

    def require_registration_allowed(self, user: object | None) -> None:
        """Permit the login to continue ONLY for an existing identity, or
        for an unknown phone when RESTAMP_TEST_LOGIN_ALLOW_REGISTRATION=1.

        Account creation through this endpoint is OFF by default: with the
        flag unset, an allowlisted-but-UNKNOWN phone can never create an
        account — the attempt fails with the SAME generic 401 body as a
        wrong key (deliberately indistinguishable, so account existence is
        never an oracle, even for key holders), and not a single row is
        ever written. An existing identity passes regardless of the flag:
        the gate can only permit NEW accounts, never touch old ones.

        Ordering contract (see the router): this runs AFTER the key and
        allowlist guards, so it can never bypass them, and BEFORE the
        INSERT — a flag-off attempt on an unknown phone reaches only the
        read-only lookup already done to reach this point.
        """
        if user is not None:
            return
        if not settings.TEST_LOGIN_ALLOW_REGISTRATION:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=_UNAUTHORIZED_401,
            )


test_login_service = TestLoginService()
