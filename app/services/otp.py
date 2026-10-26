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

from ..config import demo_phone_allowed, settings

log = logging.getLogger("restamp.otp")

# httpx logs every request URL at INFO. The MSG91 request carries the phone
# number and the OTP in its query string, so keep the HTTP client loggers at
# WARNING or above no matter how the host configures logging.
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# LOCAL DEMO ONLY: the fixed six-digit codes used by the demo OTP login
# (RESTAMP_ENV=demo + RESTAMP_OTP_DEMO_MODE=1). Each OTP request SELECTS one
# of these codes, stores it against the requested phone (normal lifecycle /
# expiry / limits) and returns it for display; verification then follows the
# ordinary strict path, so ONLY the selected code works. In every other
# configuration these are ordinary unpredictable codes: never returned,
# displayed, or logged, and production startup refuses the demo flags.
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


class DisabledOTPProvider(OTPProvider):
    """Provider for the explicit SMS-deferred mode (RESTAMP_SMS_MODE=disabled).

    Production-safe by construction: send() ALWAYS refuses (fail closed), so
    in this mode no OTP can be generated for delivery —
    OTPService._ensure_provider_allowed rejects this provider BEFORE rate
    accounting and code generation, making the request fail with the same
    generic 503 as any unconfigured production deployment. This class is NOT
    in PRODUCTION_SMS_PROVIDERS, can never be selected via
    RESTAMP_SMS_PROVIDER, and offers no demo/console/log delivery and no
    verification bypass: OTP verification keeps every existing rule.

    MSG91 ACTIVATION IS DEFERRED (see render.yaml): until the business SMS
    prerequisites are ready (Ayatiworks DLT registration, approved sender
    header, approved OTP template, credentials), production starts in this
    mode and phone-OTP login is intentionally unavailable.
    """

    def send(self, phone: str, code: str) -> None:
        raise RuntimeError("otp delivery is disabled (RESTAMP_SMS_MODE=disabled)")


# Production SMS provider registry: vendor name -> OTPProvider subclass.
# Register a vendor by implementing OTPProvider (raise only on transport or
# vendor failure; never log or return the code) and calling
# register_sms_provider() below. Selected via RESTAMP_SMS_PROVIDER.
# NOTE — msg91 stays REGISTERED while its ACTIVATION is deferred: render.yaml
# currently sets RESTAMP_SMS_MODE=disabled so production starts without SMS.
# Re-activation needs no code change: set RESTAMP_SMS_MODE=provider (+ keep
# RESTAMP_SMS_PROVIDER=msg91 and its dashboard secrets).
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


# RESTAMP_SMS_MODE values (see app/config.py). "provider" = the strict default
# behavior (production requires a configured production provider).
# "disabled" = the explicit TEMPORARY deferred mode chosen while the MSG91
# business prerequisites (Ayatiworks DLT registration, approved sender header,
# approved OTP template, credentials) are pending: production starts WITHOUT
# SMS, OTP requests fail closed with a generic 503 BEFORE any code is
# generated, and verification keeps every existing rule with no bypass.
SMS_MODES = frozenset({"provider", "disabled"})

_SMS_MODE_BAD = (
    "RESTAMP_SMS_MODE must be one of the documented values: "
    '"provider" (default, strict) or "disabled" (deferred).'
)


def _configured_sms_mode() -> str:
    return (settings.SMS_MODE or "").strip().lower()


def sms_mode_error() -> str | None:
    """Production-only validation of RESTAMP_SMS_MODE: None when OK, else an
    error for the startup gate (setting name and documented mode names only,
    never secret values) so an unrecognized mode can never silently weaken or
    disable the production SMS checks."""
    from ..config import is_production

    if not is_production():
        return None
    if _configured_sms_mode() in SMS_MODES:
        return None
    return _SMS_MODE_BAD


def is_sms_disabled() -> bool:
    """True only in the explicit deferred mode (RESTAMP_SMS_MODE=disabled).

    Consulted by production startup to decide whether the configured-provider
    requirement applies; in every other mode the strict requirement stands.
    """
    return _configured_sms_mode() == "disabled"


def demo_login_active() -> bool:
    """True ONLY for the dedicated demo environment: RESTAMP_ENV=demo AND
    RESTAMP_OTP_DEMO_MODE=1.

    Two-part explicit switch behind the temporary demo OTP login: an OTP
    request then SELECTS one predefined code (DEMO_OTPS), stores it with the
    normal lifecycle/expiry/limits, and returns it for display; verification
    follows the ordinary strict path, so exactly that selected code works for
    that phone — the other predefined codes stand rejected. The public
    production service cannot activate this: the startup gate refuses
    RESTAMP_OTP_DEMO_MODE=1 anywhere except RESTAMP_ENV=demo, and no other
    demo-code path exists in this file.
    """
    return (
        (settings.ENV or "").strip().lower() == "demo"
        and bool(settings.OTP_DEMO_MODE)
    )


def get_otp_provider() -> OTPProvider:
    """Resolve the OTP provider for the current environment.

    The dedicated demo environment gets DisabledOTPProvider (demo login
    selects and returns a predefined code; nothing is ever sent there — an
    accidental non-demo request still fails closed). Local/dev/test always
    get DevOTPProvider (the disabled mode is a production concern — it never
    changes local development). Production gets the deferred
    DisabledOTPProvider in the explicit disabled mode, else the configured
    registered provider, else RuntimeError naming only the setting (never
    values). Called at startup (fail fast) and available to request paths.
    """
    from ..config import is_production

    if demo_login_active():
        # The demo login never sends anything: the selected predefined code
        # is returned directly in the response. Install the refusing
        # provider so an accidental non-demo request would still fail closed.
        return DisabledOTPProvider()
    if not is_production():
        return DevOTPProvider()
    if _configured_sms_mode() not in SMS_MODES:
        # Defense in depth behind the startup gate (config.sms_mode_error).
        raise RuntimeError(_SMS_MODE_BAD)
    if is_sms_disabled():
        return DisabledOTPProvider()
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

        Dedicated demo environment (RESTAMP_ENV=demo + RESTAMP_OTP_DEMO_MODE=1):
        selects one PREDEFINED code (DEMO_OTPS), stores it with the normal
        expiry/limits and returns it for display; no SMS provider is involved
        and verification later accepts exactly that stored code. Public
        production never reaches this path (see demo_login_active()), and on
        a production-classified demo only explicitly allowlisted private
        test phones (RESTAMP_OTP_DEMO_PHONES) do — every other phone fails
        closed at the provider guard BEFORE any rate accounting or code
        generation.
        """
        with self._lock:
            self._ensure_not_blocked(phone)
            self._ensure_ip_not_blocked(ip)
            demo = demo_login_active() and demo_phone_allowed(phone)
            if not demo:
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
            if demo:
                # Demo login: select one predefined code, store it with the
                # NORMAL lifecycle (hash, expiry, attempt/consumption rules
                # unchanged), and return it for display. No provider/send —
                # no SMS can happen here, and only this selected code will
                # verify for this phone.
                code = secrets.choice(tuple(DEMO_OTPS))
                self._codes[phone] = _Entry(
                    code_hash=self._hash(phone, code),
                    expires_at=now + settings.OTP_TTL_SECONDS,
                )
                return code
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
        """Reject delivery that must never run in production (defense in
        depth behind startup): the dev provider, and the provider installed in
        the explicit SMS-disabled/deferred mode. Checked BEFORE rate-limit
        accounting and code generation, so a disabled deployment fails closed
        instantly and burns no retry budget (and no OTP state is created)."""
        from ..config import is_production

        if is_production() and isinstance(
            self.provider, (DevOTPProvider, DisabledOTPProvider)
        ):
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

        There is NO demo shortcut here: in the dedicated demo environment the
        request stored the selected predefined code, so this ordinary strict
        path verifies exactly that code and rejects every other predefined
        code for that phone (with the normal attempt counting on failures).
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
