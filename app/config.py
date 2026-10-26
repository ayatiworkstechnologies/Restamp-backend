"""RESTAMP Phase 4 — environment-based configuration.

No secrets are hardcoded. Production/cloud configuration is intentionally absent.
Database passwords come from RESTAMP_DATABASE_URL; the JWT secret from RESTAMP_JWT_SECRET.
"""
from __future__ import annotations

import logging
import os

from sqlalchemy.engine import make_url as _sqlalchemy_make_url


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


class Settings:
    # Explicit environment name. Local dev/tests use "local" (default);
    # production/staging deployments MUST set RESTAMP_ENV=production
    # (or staging). Production hardening is also triggered automatically
    # when DATABASE_URL points at a non-local host (see is_production()).
    ENV: str = _get("RESTAMP_ENV", "local")
    # Local development MySQL. Example:
    #   mysql+pymysql://restamp_dev:<pwd>@localhost/restamp_dev?unix_socket=/tmp/mysql.sock
    DATABASE_URL: str = _get(
        "RESTAMP_DATABASE_URL",
        "mysql+pymysql://user:pass@localhost/restamp",
    )
    # Aiven MySQL CA certificate path (production only, never hardcoded).
    # Download ca.pem from Aiven Console -> service -> Connection information,
    # mount it on Render as a Secret File, and set this to the mount path
    # (e.g. the Secret File path). Empty = local dev (no SSL via unix socket).
    DB_SSL_CA: str = _get("RESTAMP_DB_SSL_CA", "")
    # JWT signing secret — MUST be overridden via env outside local dev.
    JWT_SECRET: str = _get("RESTAMP_JWT_SECRET", "restamp-local-dev-secret-change-me")
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = _get_int("RESTAMP_JWT_EXPIRE_MINUTES", 30)

    # Phone OTP behaviour.
    OTP_LENGTH: int = 6
    OTP_TTL_SECONDS: int = _get_int("RESTAMP_OTP_TTL_SECONDS", 300)
    OTP_MAX_REQUESTS_PER_HOUR: int = _get_int("RESTAMP_OTP_MAX_REQUESTS_PER_HOUR", 5)
    OTP_MAX_VERIFY_ATTEMPTS: int = _get_int("RESTAMP_OTP_MAX_VERIFY_ATTEMPTS", 5)
    OTP_BLOCK_SECONDS: int = _get_int("RESTAMP_OTP_BLOCK_SECONDS", 900)
    # SMS delivery mode for production. "provider" (default) = strict behavior:
    # startup requires RESTAMP_SMS_PROVIDER to name a configured registered
    # production provider with its credentials. "disabled" = explicit DEFERRED
    # mode: production starts without SMS — phone-OTP login is unavailable,
    # OTP requests fail closed (generic 503) BEFORE any code is generated, and
    # verification has no bypass. MSG91 ACTIVATION IS DEFERRED until the
    # business SMS prerequisites are complete (Ayatiworks DLT registration,
    # approved sender header, approved OTP template, credentials) — see
    # render.yaml. Unknown values fail production startup. Local dev/tests
    # always use the dev provider regardless of this value.
    SMS_MODE: str = _get("RESTAMP_SMS_MODE", "provider")
    # Production SMS delivery selection. Names a key in the provider registry
    # (see app/services/otp.py PRODUCTION_SMS_PROVIDERS). Empty = unconfigured.
    # Registered production vendors: "msg91". Production requires a real
    # registered provider (enforced at startup) unless SMS_MODE="disabled".
    # Local dev/tests always use the dev provider regardless of this value.
    SMS_PROVIDER: str = _get("RESTAMP_SMS_PROVIDER", "")
    # MSG91 credentials (used only when RESTAMP_SMS_PROVIDER=msg91). Secrets:
    # set them in the host dashboard, never in the repo. Startup fails fast,
    # naming only the missing setting names, when either one is empty.
    #   AUTH_KEY    - MSG91 account auth key (sent as a request header).
    #   TEMPLATE_ID - DLT-approved OTP template id from the MSG91 OTP section.
    SMS_MSG91_AUTH_KEY: str = _get("RESTAMP_SMS_MSG91_AUTH_KEY", "")
    SMS_MSG91_TEMPLATE_ID: str = _get("RESTAMP_SMS_MSG91_TEMPLATE_ID", "")
    # Whole-request timeout (seconds) for one SMS vendor call. Kept short:
    # OTPService holds its lock while sending.
    SMS_TIMEOUT_SECONDS: int = _get_int("RESTAMP_SMS_TIMEOUT_SECONDS", 8)
    # Per-IP abuse protection (in-memory, per process — see otp.py).
    # These bound total OTP request/verify-failure volume from a single
    # client IP across all phones, so rotating phones does not bypass
    # the per-phone caps. Tunable via env; defaults allow normal
    # NAT sharing while stopping bulk abuse.
    OTP_MAX_REQUESTS_PER_HOUR_PER_IP: int = _get_int(
        "RESTAMP_OTP_MAX_REQUESTS_PER_HOUR_PER_IP", 30
    )
    OTP_MAX_VERIFY_ATTEMPTS_PER_IP: int = _get_int(
        "RESTAMP_OTP_MAX_VERIFY_ATTEMPTS_PER_IP", 30
    )
    # When "1", the OTP per-IP throttling in app/routers/auth.py derives the
    # client IP from the rightmost X-Forwarded-For entry added by the platform
    # proxy (e.g. Render), instead of treating every client as the proxy IP.
    # Default "0" keeps today's socket-peer behavior (recommended wherever
    # requests do NOT arrive through a trusted reverse proxy; clients could
    # otherwise fake the header and evade per-IP caps).
    TRUST_PROXY: bool = _get("RESTAMP_TRUST_PROXY", "0") == "1"
    # When "1", OTP responses include the code (LOCAL DEV + TESTS ONLY).
    OTP_DEBUG: bool = _get("RESTAMP_OTP_DEBUG", "0") == "1"
    OTP_DEMO_MODE: bool = _get("RESTAMP_OTP_DEMO_MODE", "0") == "1"
    # LOCAL DEMO ONLY: when "1", a fixed set of demo OTPs is accepted by
    # verify_code (a prior OTP request must still exist). Absent/disabled:
    # no effect whatsoever (production behavior unchanged).

    # Google verification. Real verification needs GOOGLE_CLIENT_ID and a real
    # verifier; dev mode accepts only synthetic "dev:<subject>" tokens.
    # GOOGLE_ENABLED explicitly opts production in to Google login ("1").
    # Production login additionally requires a real GOOGLE_CLIENT_ID and
    # GOOGLE_DEV_MODE=0 (enforced at startup); when not enabled, Google
    # routes fail closed with 501 and never accept synthetic identities.
    GOOGLE_CLIENT_ID: str = _get("RESTAMP_GOOGLE_CLIENT_ID", "")
    GOOGLE_DEV_MODE: bool = _get("RESTAMP_GOOGLE_DEV_MODE", "0") == "1"
    GOOGLE_ENABLED: bool = _get("RESTAMP_GOOGLE_ENABLED", "0") == "1"

    APP_NAME: str = "restamp-backend"
    # Local CORS origins (comma-separated). Empty = built-in local defaults in main.py.
    CORS_ORIGINS: str = _get("RESTAMP_CORS_ORIGINS", "")

    # Local media storage for listing photo uploads (see app/services/storage.py).
    # Directory only; served at MEDIA_URL_PREFIX. Never inside the git repo.
    # An S3/object-storage backend replaces storage.py without config changes
    # beyond these two values.
    MEDIA_ROOT: str = _get("RESTAMP_MEDIA_ROOT", "/tmp/restamp-media")
    MEDIA_URL_PREFIX: str = "/media"

    # Cloudinary (property photo uploads). Credentials come ONLY from the
    # environment — never hardcoded, never logged. When CLOUDINARY_CLOUD_NAME
    # is empty, photo uploads are treated as unconfigured (503).
    CLOUDINARY_CLOUD_NAME: str = _get("CLOUDINARY_CLOUD_NAME", "")
    CLOUDINARY_API_KEY: str = _get("CLOUDINARY_API_KEY", "")
    CLOUDINARY_API_SECRET: str = _get("CLOUDINARY_API_SECRET", "")
    CLOUDINARY_FOLDER: str = _get("CLOUDINARY_FOLDER", "restamp")


settings = Settings()


# --- Phase 1: production hardening helpers (no secrets in messages/logs) ----
_log = logging.getLogger("restamp.config")

_PROD_ENVS = {"production", "prod", "staging"}
_JWT_KNOWN_WEAK = {
    "",
    "restamp-local-dev-secret-change-me",
    "change-me-to-a-long-random-secret",
    "change-me",
    "changeme",
    "password",
    "secret",
    "test",
    "phase4-test-secret",
}
_JWT_MIN_LENGTH = 32


def _is_local_database_url(url: str) -> bool:
    """True for local dev/test URLs (unix socket or loopback host).

    The host is extracted with a real URL parser (SQLAlchemy make_url), so a
    name that merely CONTAINS a loopback substring (e.g. `@localhost.attacker.tld`)
    can never masquerade as local and skip production hardening. Fail-closed:
    empty or unparseable URLs are reported NOT local, which is_production()
    treats as production.
    """
    if not (url or "").strip():
        return False
    if "unix_socket" in (url or "").lower():
        return True
    try:
        host = (_sqlalchemy_make_url(str(url)).host or "").strip().lower()
    except Exception:
        return False
    return host in ("localhost", "127.0.0.1", "::1")


def is_production() -> bool:
    """True when running against production-like configuration.

    Explicit RESTAMP_ENV=production/prod/staging wins; otherwise a
    non-local DATABASE_URL (no unix socket, non-loopback host) is
    treated as production. Fail-closed: unknown/malformed URLs are
    treated as production so hardening cannot be skipped by misconfig.
    """
    env = (settings.ENV or "").strip().lower()
    if env in _PROD_ENVS:
        return True
    try:
        return not _is_local_database_url(settings.DATABASE_URL)
    except Exception:
        return True


def is_jwt_secret_strong(secret: str | None) -> bool:
    """Check length + blocklist without ever logging the value."""
    if not secret or not isinstance(secret, str):
        return False
    s = secret.strip()
    if len(s) < _JWT_MIN_LENGTH:
        return False
    if s in _JWT_KNOWN_WEAK:
        return False
    low = s.lower()
    for marker in ("change-me", "changeme", "password", "secret-change-me", "dev-secret"):
        if marker in low:
            return False
    return True


def validate_security_config() -> None:
    """Fail fast with non-sensitive errors when production config is unsafe.

    Safe in local/dev/test (no-op). Raises RuntimeError in production when:
    - JWT secret is missing, weak (<32 chars), or a known placeholder;
    - RESTAMP_OTP_DEBUG / GOOGLE_DEV_MODE are enabled;
    - RESTAMP_OTP_DEMO_MODE is enabled anywhere except RESTAMP_ENV=demo (
      the dedicated demo/staging deployment; production never accepts the
      fixed demo codes);
    - RESTAMP_CORS_ORIGINS contains '*' (never valid with credentials);
    - no production SMS provider is configured (RESTAMP_SMS_PROVIDER must
      name a registered production provider);
    - Google login is enabled without a real RESTAMP_GOOGLE_CLIENT_ID.
    Messages name only the setting, never its value.
    """
    if not is_production():
        return
    if not is_jwt_secret_strong(settings.JWT_SECRET):
        raise RuntimeError(
            "Insecure production configuration: RESTAMP_JWT_SECRET must be "
            "set to a strong random value at least 32 characters long."
        )
    if settings.OTP_DEBUG:
        raise RuntimeError(
            "Insecure production configuration: RESTAMP_OTP_DEBUG must be '0' "
            "in production (OTP codes must never be returned in API responses)."
        )
    # RESTAMP_OTP_DEMO_MODE=1 is allowed ONLY with RESTAMP_ENV=demo: a
    # dedicated, explicitly named demo/staging deployment. In every other
    # configuration — production/prod/staging, or any service whose
    # DATABASE_URL makes is_production() treat it as production — fixed demo
    # codes are refused at startup exactly as before. The demo OTP flow
    # itself (selection/return/verify of one predefined code per request)
    # lives in app/services/otp.py on demo_login_active().
    if (settings.ENV or "").strip().lower() == "demo":
        if not settings.OTP_DEMO_MODE:
            _log.warning(
                "RESTAMP_ENV=demo without RESTAMP_OTP_DEMO_MODE=1: the demo "
                "OTP login is INACTIVE. Enable RESTAMP_OTP_DEMO_MODE=1 to use "
                "the predefined demo codes."
            )
        else:
            _log.warning(
                "Demo login ACTIVE (RESTAMP_ENV=demo + RESTAMP_OTP_DEMO_MODE=1): "
                "OTP requests return a selected predefined demo code and no "
                "SMS is sent. This must never be combined with a real "
                "production service configuration."
            )
        _log.warning(
            "Demo configuration hardening: on a deployed demo service "
            "(production-classified via its DATABASE_URL) the JWT/CORS/Google "
            "checks below still run exactly as in production."
        )
    elif settings.OTP_DEMO_MODE:
        raise RuntimeError(
            "Insecure production configuration: RESTAMP_OTP_DEMO_MODE must be '0' "
            "in production (fixed demo codes require RESTAMP_ENV=demo)."
        )
    if settings.GOOGLE_DEV_MODE:
        raise RuntimeError(
            "Insecure production configuration: RESTAMP_GOOGLE_DEV_MODE must be '0' "
            "in production (synthetic dev tokens must never be accepted)."
        )
    cors = [o.strip() for o in (settings.CORS_ORIGINS or "").split(",") if o.strip()]
    if "*" in cors:
        raise RuntimeError(
            "Insecure production configuration: RESTAMP_CORS_ORIGINS must not "
            "contain '*' when credentials are enabled."
        )
    # Photo-upload storage: NOT a hard failure (that would be a behavior
    # change requiring an explicit product decision). When production has no
    # Cloudinary cloud configured, uploads fall back to the local disk, which
    # is ephemeral on Render and bakes request-base-URLs into property_media.
    # Warn loudly at startup so the trade-off is visible, naming settings
    # only (never values).
    if not settings.CLOUDINARY_CLOUD_NAME:
        _log.warning(
            "Production media storage not fully configured: CLOUDINARY_CLOUD_NAME "
            "is empty; listing photo uploads will fall back to local-disk storage "
            "(ephemeral across restarts). Configure CLOUDINARY_CLOUD_NAME/"
            "CLOUDINARY_API_KEY/CLOUDINARY_API_SECRET for persistent uploads."
        )
    # Lazy imports: services import this module at load time.
    from .services import otp as otp_module

    mode_error = otp_module.sms_mode_error()
    if mode_error:
        raise RuntimeError(f"Insecure production configuration: {mode_error}")
    if otp_module.is_sms_disabled():
        # Explicit DEFERRED mode (RESTAMP_SMS_MODE=disabled): production starts
        # WITHOUT SMS/OTP delivery by design. Nothing above is weakened: OTP
        # requests fail closed at request time (generic 503, before any code
        # exists), verification has no bypass, and all dev flags stay forbidden
        # (the checks above run in every mode). Loud warning — not an error —
        # so the trade-off stays visible in the deploy log.
        _log.warning(
            "Production SMS delivery is DISABLED (RESTAMP_SMS_MODE=disabled): "
            "phone-OTP login is unavailable and OTP requests fail closed (503); "
            "no OTP is generated, sent, or disclosed. To enable SMS, set "
            "RESTAMP_SMS_MODE=provider with a configured production provider."
        )
    elif not otp_module.is_sms_configured():
        raise RuntimeError(
            "Insecure production configuration: RESTAMP_SMS_PROVIDER must name "
            "a configured production SMS provider (no dev fallback in production)."
        )
    if settings.GOOGLE_ENABLED and not settings.GOOGLE_CLIENT_ID.strip():
        raise RuntimeError(
            "Insecure production configuration: RESTAMP_GOOGLE_ENABLED requires "
            "a real RESTAMP_GOOGLE_CLIENT_ID in production."
        )


# Backwards-compatible alias (Phase 3 code read DBConfig.SQLALCHEMY_DATABASE_URI).
class DBConfig:
    SQLALCHEMY_DATABASE_URI = settings.DATABASE_URL
    SQLALCHEMY_TRACK_MODIFICATIONS = False
