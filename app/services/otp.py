"""RESTAMP Phase 4 — phone OTP generation + verification (provider-independent).

- The service generates a random code with Python `secrets`, stores ONLY a
  phone-bound SHA-256 hash with the full lifecycle (expiry, single use,
  per-phone and per-IP attempt caps and rate limits), and verifies strictly:
  only the code stored for that phone can consume it. Phone input is
  restricted to Indian mobiles (+91 + 10 digits, 6-9 start) by the schemas.
- DELIVERY IS NOT CONNECTED YET: this codebase contains no SMS vendor or
  provider. `OTPService.delivery` is a single call seam `delivery(phone,
  code)` left unconfigured on purpose — with no delivery connected, OTP
  requests fail closed with a generic 503 BEFORE any code is generated, so
  no "sent" response can ever lie. Real SMS delivery must be implemented and
  attached behind this seam (a `Callable`) before public phone login can
  work.
- TEMPORARY INTERNAL-TESTING DISPLAY (explicitly restricted): when
  RESTAMP_OTP_DISPLAY_MODE="1" in a NON-production-classified process
  (local development / internal test deployments only), request_code
  RETURNS the generated code so the API response can display it — no SMS
  is sent and the delivery seam is bypassed while the mode is active. In
  every other configuration — ALWAYS in production, where the startup gate
  (validate_security_config) also refuses the flag — the code is never
  returned, never logged, and never included in API responses: production
  fails closed both ways (delivery seam unconfigured → 503; display mode
  suppressed by the is_production() double-check at request time).
- Throttling is IN-MEMORY and PER PROCESS (thread-safe via a lock, but not
  shared across workers/hosts). Per-phone caps stop single-number abuse;
  per-IP caps stop phone rotation from one client. This does NOT provide
  distributed protection: multi-worker/multi-instance deployments need a
  shared store (e.g. Redis) — see Remaining risks. Do not claim otherwise.
"""
from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from fastapi import HTTPException, status

from ..config import is_production, settings

NOT_CONNECTED_503 = "OTP delivery is not configured. Try again later."


@dataclass
class _Entry:
    code_hash: str
    expires_at: float
    attempts: int = 0


@dataclass
class OTPService:
    # Delivery seam. None (the default) means NOT CONNECTED: no SMS vendor,
    # no mock, no console/logging delivery exists in production code, and
    # request_code refuses to generate anything. Tests install a recording
    # stand-in (tests/conftest.py) representing the phone's SMS; product
    # code will attach real SMS delivery here when the business side is
    # ready — with no other change to generation, storage or verification.
    delivery: Callable[[str, str], None] | None = None
    _codes: dict[str, _Entry] = field(default_factory=dict)
    _requests: dict[str, list[float]] = field(default_factory=dict)
    _blocked_until: dict[str, float] = field(default_factory=dict)
    # Per-IP abuse protection (same in-memory, per-process scope as above).
    # _ip_requests bounds OTP request volume per client IP across all phones;
    # _ip_verify_failures bounds wrong-code volume per IP; _ip_blocked_until
    # holds IP-level blocks. All guarded by _lock with the phone state.
    _ip_requests: dict[str, list[float]] = field(default_factory=dict)
    _ip_verify_failures: dict[str, list[float]] = field(default_factory=dict)
    _ip_blocked_until: dict[str, float] = field(default_factory=dict)
    # Guards all shared mutable state: uvicorn runs sync handlers in a thread
    # pool, so concurrent same-phone requests must not interleave check-then-act
    # sequences (rate windows, attempt counters, code consumption).
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def reset(self) -> None:
        """Test hook: clear all in-memory OTP/rate-limit state (incl. per-IP)."""
        with self._lock:
            self._codes.clear()
            self._requests.clear()
            self._blocked_until.clear()
            self._ip_requests.clear()
            self._ip_verify_failures.clear()
            self._ip_blocked_until.clear()

    @staticmethod
    def _hash(phone: str, code: str) -> str:
        return hashlib.sha256(f"{phone}:{code}".encode()).hexdigest()

    def _ensure_not_blocked(self, phone: str) -> None:
        if time.time() < self._blocked_until.get(phone, 0):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many attempts. Try again later.",
            )

    def _ensure_ip_not_blocked(self, ip: str | None) -> None:
        if ip and time.time() < self._ip_blocked_until.get(ip, 0):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many attempts. Try again later.",
            )

    def _display_active(self) -> bool:
        """Whether the temporary internal-testing display mode is active.

        Deliberately restricted on TWO independent conditions, re-evaluated
        at every call: the explicit env flag must be "1", AND the process
        must not be production-classified (is_production() refuses unknown
        or malformed DB URLs / envs into production — fail closed). In
        production this is therefore unreachable even if the flag is left
        set; the startup gate additionally refuses production boot with it.
        """
        if not settings.OTP_DISPLAY_MODE:
            return False
        return not is_production()

    def request_code(self, phone: str, ip: str | None = None) -> str | None:
        """Generate a code with `secrets` and store its hash.

        Returns None in the normal mode: the plaintext code is never
        returned, logged, or disclosed in API responses — it exists
        transiently during the delivery call only. Tests that need the code
        read it from the test-only delivery stand-in installed as
        `self.delivery` (never from an API response).

        In the explicitly restricted local/internal TESTING display mode
        (_display_active), returns the generated code for the API response
        to display. No SMS is sent there and the delivery seam is bypassed;
        all lifecycle rules (rate caps, expiry, single use, attempt caps)
        apply unchanged.

        With NO delivery connected and display inactive (the production
        state) the request fails closed with a generic 503 BEFORE rate
        accounting or code generation, so no state is created and no retry
        budget is burned. Once delivery raises, this attempt is rolled back
        (no live-but-undelivered code, no burned budget) and the request
        ends with a generic 503.
        """
        with self._lock:
            self._ensure_not_blocked(phone)
            self._ensure_ip_not_blocked(ip)
            display = self._display_active()
            if not display:
                self._ensure_delivery_connected()
            now = time.time()
            window = [t for t in self._requests.get(phone, []) if now - t < 3600]
            if len(window) >= settings.OTP_MAX_REQUESTS_PER_HOUR:
                self._blocked_until[phone] = now + settings.OTP_BLOCK_SECONDS
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many OTP requests. Try again later.",
                )
            if ip:
                ip_window = [t for t in self._ip_requests.get(ip, []) if now - t < 3600]
                if len(ip_window) >= settings.OTP_MAX_REQUESTS_PER_HOUR_PER_IP:
                    self._ip_blocked_until[ip] = now + settings.OTP_BLOCK_SECONDS
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail="Too many OTP requests. Try again later.",
                    )
                ip_window.append(now)
                self._ip_requests[ip] = ip_window
            window.append(now)
            self._requests[phone] = window
            code = f"{secrets.randbelow(10**settings.OTP_LENGTH):0{settings.OTP_LENGTH}d}"
            entry = _Entry(
                code_hash=self._hash(phone, code), expires_at=now + settings.OTP_TTL_SECONDS
            )
            self._codes[phone] = entry
            if display:
                # Restricted internal-testing display: the backend-selected
                # code is handed back to the router for response display.
                # It is never logged here, and the delivery seam is not
                # touched (no SMS, no leak channel beyond the response).
                return code
            try:
                self.delivery(phone, code)
            except HTTPException:
                self._rollback_request(phone, ip, now, entry)
                raise
            except Exception:
                # Delivery failure: invalidate this attempt so no
                # live-but-undelivered code exists, then fail generically.
                # Never include the code, phone, or delivery detail.
                self._rollback_request(phone, ip, now, entry)
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Failed to send code. Try again later.",
                )
            return None

    def _ensure_delivery_connected(self) -> None:
        """Fail closed (generic 503) while no delivery is connected.

        Checked BEFORE rate-limit accounting and code generation, so an
        unconnected deployment fails closed instantly and burns no retry
        budget (and no OTP state is created).
        """
        if self.delivery is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=NOT_CONNECTED_503,
            )

    def _rollback_request(
        self, phone: str, ip: str | None, now: float, entry: _Entry
    ) -> None:
        """Undo one request_code attempt: drop its code entry and its slot in
        the phone/IP rate-limit windows, so a failed delivery neither leaves
        an unusable live code nor burns the caller's retry budget."""
        if self._codes.get(phone) is entry:
            self._codes.pop(phone, None)
        window = self._requests.get(phone, [])
        try:
            window.remove(now)
        except ValueError:
            pass
        if not window:
            self._requests.pop(phone, None)
        if ip:
            ip_window = self._ip_requests.get(ip, [])
            try:
                ip_window.remove(now)
            except ValueError:
                pass
            if not ip_window:
                self._ip_requests.pop(ip, None)

    def verify_code(self, phone: str, code: str, ip: str | None = None) -> bool:
        """True on success (consumes the code); False on wrong/expired code.

        Wrong attempts count against both the phone and (when provided) the
        client IP, so phone rotation does not bypass attempt caps.
        """
        with self._lock:
            self._ensure_not_blocked(phone)
            self._ensure_ip_not_blocked(ip)
            entry = self._codes.get(phone)
            if entry is None or time.time() > entry.expires_at:
                self._codes.pop(phone, None)
                return False
            if not secrets.compare_digest(entry.code_hash, self._hash(phone, code)):
                entry.attempts += 1
                now = time.time()
                ip_tripped = False
                if ip:
                    failures = [t for t in self._ip_verify_failures.get(ip, []) if now - t < 3600]
                    failures.append(now)
                    self._ip_verify_failures[ip] = failures
                    if len(failures) >= settings.OTP_MAX_VERIFY_ATTEMPTS_PER_IP:
                        self._ip_blocked_until[ip] = now + settings.OTP_BLOCK_SECONDS
                        ip_tripped = True
                if entry.attempts >= settings.OTP_MAX_VERIFY_ATTEMPTS or ip_tripped:
                    self._blocked_until[phone] = time.time() + settings.OTP_BLOCK_SECONDS
                    self._codes.pop(phone, None)
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail="Too many wrong attempts. Try again later.",
                    )
                return False
            self._codes.pop(phone, None)
            return True


otp_service = OTPService()
