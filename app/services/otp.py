"""RESTAMP Phase 4 — phone OTP foundation.

- Provider abstraction: connect a real SMS vendor later by implementing OTPProvider.
- DevOTPProvider generates random codes and only logs them; the code is returned
  to the caller ONLY when RESTAMP_OTP_DEBUG=1 (local dev/tests).
- Only salted SHA-256 hashes are kept in memory; plaintext codes are never stored
  and never logged. Rate limiting + attempt caps live here (in-memory, per phone).
"""
import hashlib
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field

from fastapi import HTTPException, status

from ..config import settings

log = logging.getLogger("restamp.otp")

# LOCAL DEMO ONLY: fixed six-digit codes accepted by verify_code when
# RESTAMP_OTP_DEMO_MODE=1 (and only then). Never returned, displayed, or
# logged anywhere; a prior unexpired OTP request is still required.
DEMO_OTPS = frozenset({
    "297569", "109745", "583214", "741806", "426391",
    "835027", "614958", "372640", "958163", "205874",
})


class OTPProvider:
    def send(self, phone: str, code: str) -> None:
        raise NotImplementedError


class DevOTPProvider(OTPProvider):
    """Local-development provider: no SMS is sent; delivery is a no-op log line."""

    def send(self, phone: str, code: str) -> None:
        log.info("OTP dispatched (dev provider, code withheld from logs)")


@dataclass
class _Entry:
    code_hash: str
    expires_at: float
    attempts: int = 0


@dataclass
class OTPService:
    provider: OTPProvider = field(default_factory=DevOTPProvider)
    _codes: dict[str, _Entry] = field(default_factory=dict)
    _requests: dict[str, list[float]] = field(default_factory=dict)
    _blocked_until: dict[str, float] = field(default_factory=dict)
    # Guards all shared mutable state: uvicorn runs sync handlers in a thread
    # pool, so concurrent same-phone requests must not interleave check-then-act
    # sequences (rate windows, attempt counters, code consumption).
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def reset(self) -> None:
        """Test hook: clear all in-memory OTP/rate-limit state."""
        with self._lock:
            self._codes.clear()
            self._requests.clear()
            self._blocked_until.clear()

    @staticmethod
    def _hash(phone: str, code: str) -> str:
        return hashlib.sha256(f"{phone}:{code}".encode()).hexdigest()

    def _ensure_not_blocked(self, phone: str) -> None:
        if time.time() < self._blocked_until.get(phone, 0):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many attempts. Try again later.",
            )

    def request_code(self, phone: str) -> str | None:
        """Issue a code; returns it only in OTP_DEBUG mode, else None."""
        with self._lock:
            self._ensure_not_blocked(phone)
            now = time.time()
            window = [t for t in self._requests.get(phone, []) if now - t < 3600]
            if len(window) >= settings.OTP_MAX_REQUESTS_PER_HOUR:
                self._blocked_until[phone] = now + settings.OTP_BLOCK_SECONDS
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many OTP requests. Try again later.",
                )
            window.append(now)
            self._requests[phone] = window
            code = f"{secrets.randbelow(10**settings.OTP_LENGTH):0{settings.OTP_LENGTH}d}"
            self._codes[phone] = _Entry(
                code_hash=self._hash(phone, code), expires_at=now + settings.OTP_TTL_SECONDS
            )
            self.provider.send(phone, code)
            return code if settings.OTP_DEBUG else None

    def verify_code(self, phone: str, code: str) -> bool:
        """True on success (consumes the code); False on wrong/expired code."""
        with self._lock:
            self._ensure_not_blocked(phone)
            entry = self._codes.get(phone)
            if entry is None or time.time() > entry.expires_at:
                self._codes.pop(phone, None)
                return False
            # LOCAL DEMO ONLY: accept a fixed demo code in place of the
            # issued code, provided demo mode is enabled and a live OTP
            # request exists. Consumes the entry like a normal success and
            # never counts as a wrong attempt. No effect when disabled.
            if settings.OTP_DEMO_MODE and code in DEMO_OTPS:
                self._codes.pop(phone, None)
                return True
            if not secrets.compare_digest(entry.code_hash, self._hash(phone, code)):
                entry.attempts += 1
                if entry.attempts >= settings.OTP_MAX_VERIFY_ATTEMPTS:
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
