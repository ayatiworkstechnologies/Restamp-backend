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
  time via `secrets.compare_digest`), AND the phone is on the explicit
  RESTAMP_TEST_LOGIN_ALLOWED_PHONES allowlist.
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

from fastapi import HTTPException, status

from ..config import settings

_UNAUTHORIZED_401 = "Unauthorized"
_FORBIDDEN_403 = "Forbidden"
_TOO_MANY_429 = "Too many attempts. Try again later."


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
        """True only when BOTH the flag is on AND a non-empty key is set.

        Fail-closed: an enabled-but-misconfigured deployment (no key) stays
        inactive; callers must treat inactive as "endpoint does not exist".
        """
        if not settings.TEST_LOGIN_ENABLED:
            return False
        return bool((settings.TEST_LOGIN_KEY or "").strip())

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
