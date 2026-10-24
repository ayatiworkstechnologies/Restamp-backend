"""RESTAMP Phase 4 — phone OTP foundation (Phase 1 hardened).

- Provider abstraction: connect a real SMS vendor later by implementing OTPProvider.
- DevOTPProvider generates random codes and only logs them; the code is returned
  to the caller ONLY when RESTAMP_OTP_DEBUG=1 (local dev/tests).
- Only salted SHA-256 hashes are kept in memory; plaintext codes are never stored
  and never logged. Rate limiting + attempt caps live here.
- Throttling is IN-MEMORY and PER PROCESS (thread-safe via a lock, but not
  shared across workers/hosts). Per-phone caps stop single-number abuse;
  per-IP caps stop phone rotation from one client. This does NOT provide
  distributed protection: multi-worker/multi-instance deployments need a
  shared store (e.g. Redis) — see Remaining risks. Do not claim otherwise.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field

import httpx
from fastapi import HTTPException, status

from ..config import settings

log = logging.getLogger("restamp.otp")

# httpx logs every request URL at INFO. The MSG91 request carries the phone
# number and the OTP in its query string, so keep the HTTP client loggers at
# WARNING or above no matter how the host configures logging.
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

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
    """Local-development provider: no SMS is sent; delivery is a no-op log line.

    Available ONLY outside production (enforced by get_otp_provider() and by
    the request-time guard in OTPService.request_code). Never reference this
    class from production configuration.
    """

    def send(self, phone: str, code: str) -> None:
        log.info("OTP dispatched (dev provider, code withheld from logs)")


# Production SMS provider registry: vendor name -> OTPProvider subclass.
# Register a vendor by implementing OTPProvider (raise only on transport or
# vendor failure; never log or return the code) and calling
# register_sms_provider() below. Selected via RESTAMP_SMS_PROVIDER.
PRODUCTION_SMS_PROVIDERS: dict[str, type[OTPProvider]] = {}


class Msg91OTPProvider(OTPProvider):
    """MSG91 OTP API (v5) delivery for India (DLT-approved OTP template).

    Our own code is generated and verified by OTPService; MSG91 is used only
    to deliver it (the `otp` parameter), so verification never depends on the
    vendor. Credentials come from settings (RESTAMP_SMS_MSG91_*): the auth key
    travels in a header, never in the URL. Failures raise a generic
    RuntimeError (OTPService turns it into a safe 503 and rolls the code
    back); the phone, code, credentials and vendor response body are never
    logged.
    """

    API_URL = "https://control.msg91.com/api/v5/otp"

    def __init__(self) -> None:
        required = (
            ("RESTAMP_SMS_MSG91_AUTH_KEY", settings.SMS_MSG91_AUTH_KEY),
            ("RESTAMP_SMS_MSG91_TEMPLATE_ID", settings.SMS_MSG91_TEMPLATE_ID),
        )
        missing = [name for name, value in required if not (value or "").strip()]
        if missing:
            # Names only, never values.
            raise RuntimeError(
                "Insecure production configuration: RESTAMP_SMS_PROVIDER=msg91 "
                "requires " + ", ".join(missing) + " to be set."
            )
        self._auth_key = settings.SMS_MSG91_AUTH_KEY.strip()
        self._template_id = settings.SMS_MSG91_TEMPLATE_ID.strip()
        self._timeout = float(max(1, settings.SMS_TIMEOUT_SECONDS))

    def send(self, phone: str, code: str) -> None:
        # MSG91 wants the international number without the leading "+".
        mobile = phone.lstrip("+")
        if not mobile.isdigit():
            raise RuntimeError("invalid phone for SMS delivery")
        try:
            response = httpx.post(
                self.API_URL,
                params={
                    "template_id": self._template_id,
                    "mobile": mobile,
                    "otp": code,
                },
                headers={
                    "authkey": self._auth_key,
                    "accept": "application/json",
                    "content-type": "application/json",
                },
                json={},
                timeout=httpx.Timeout(self._timeout, connect=min(3.0, self._timeout)),
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            log.warning("MSG91 delivery failed: transport error (%s)", type(exc).__name__)
            raise RuntimeError("sms delivery failed") from None
        try:
            body = response.json()
        except ValueError:
            body = None
        kind = body.get("type") if isinstance(body, dict) else None
        # MSG91 can answer HTTP 200 with {"type": "error"}; only an explicit
        # success counts as delivered.
        if response.status_code == 200 and kind == "success":
            return
        # Log only coarse, non-identifying facts: HTTP status, the vendor's
        # `type`, and its short error `code` (never the message body).
        vendor_code = body.get("code") if isinstance(body, dict) else None
        if not (isinstance(vendor_code, (str, int)) and len(str(vendor_code)) <= 8):
            vendor_code = None
        log.warning(
            "MSG91 delivery failed: http=%s type=%s code=%s",
            response.status_code,
            kind if isinstance(kind, str) and len(kind) <= 16 else None,
            vendor_code,
        )
        raise RuntimeError("sms delivery failed")


def register_sms_provider(name: str, cls: type[OTPProvider]) -> None:
    """Register a production SMS vendor implementation (call at import time).

    The class must be zero-arg-constructible (read its own RESTAMP_SMS_*
    settings); get_otp_provider() instantiates it with cls().
    """
    PRODUCTION_SMS_PROVIDERS[name.strip().lower()] = cls


register_sms_provider("msg91", Msg91OTPProvider)


def _configured_provider_name() -> str:
    return (settings.SMS_PROVIDER or "").strip().lower()


def is_sms_configured() -> bool:
    """True when a registered (non-dev) production provider is selected."""
    return _configured_provider_name() in PRODUCTION_SMS_PROVIDERS


def get_otp_provider() -> OTPProvider:
    """Resolve the OTP provider for the current environment.

    Local/dev/test always get DevOTPProvider. Production gets the configured
    registered provider, else RuntimeError naming only the setting (never
    values). Called at startup (fail fast) and available to request paths.
    """
    from ..config import is_production

    if not is_production():
        return DevOTPProvider()
    name = _configured_provider_name()
    cls = PRODUCTION_SMS_PROVIDERS.get(name)
    if cls is None:
        raise RuntimeError(
            "RESTAMP_SMS_PROVIDER must name a configured production SMS "
            "provider (no dev fallback in production)."
        )
    return cls()


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

    def request_code(self, phone: str, ip: str | None = None) -> str | None:
        """Issue a code; returns it only in OTP_DEBUG mode, else None.

        Enforces the existing per-phone cap plus a per-IP cap (when ip is
        provided) so rotating phones from one client still trips protection.
        In production the dev provider is rejected (503) and a send failure
        rolls back this request's OTP state before raising a generic 503, so
        a failed delivery never leaves a live-but-undelivered code behind.
        API contract unchanged on success: same return, same 429 shapes.
        """
        with self._lock:
            self._ensure_not_blocked(phone)
            self._ensure_ip_not_blocked(ip)
            self._ensure_provider_allowed()
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
            try:
                self.provider.send(phone, code)
            except HTTPException:
                self._rollback_request(phone, ip, now, entry)
                raise
            except Exception:
                # Transport/vendor failure: invalidate this attempt so no
                # live-but-undelivered code exists, then fail generically.
                # Never include the code, phone, or provider detail.
                self._rollback_request(phone, ip, now, entry)
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Failed to send code. Try again later.",
                )
            return code if settings.OTP_DEBUG else None

    def _ensure_provider_allowed(self) -> None:
        """Reject dev delivery in production (defense in depth behind startup)."""
        from ..config import is_production

        if is_production() and isinstance(self.provider, DevOTPProvider):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="OTP delivery is not configured. Try again later.",
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
            # LOCAL DEMO ONLY: accept a fixed demo code in place of the
            # issued code, provided demo mode is enabled and a live OTP
            # request exists. Consumes the entry like a normal success and
            # never counts as a wrong attempt. No effect when disabled.
            if settings.OTP_DEMO_MODE and code in DEMO_OTPS:
                self._codes.pop(phone, None)
                return True
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
