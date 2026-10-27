"""RESTAMP Phase 4 — environment-based configuration.

No secrets are hardcoded. Production/cloud configuration is intentionally absent.
Database passwords come from RESTAMP_DATABASE_URL; the JWT secret from RESTAMP_JWT_SECRET.
"""
from __future__ import annotations

import logging
import os
import re

from sqlalchemy.engine import make_url as _sqlalchemy_make_url


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _split_test_login_phones(raw: str) -> tuple[str, ...]:
    """Parse the comma-separated test-phone allowlist for storage on Settings.

    Entries are trimmed, interior empties are dropped, and the result is a
    tuple (no accidental mutability at runtime). Membership checks in
    app/services/test_login.py treat an EMPTY tuple as fail-closed.
    """
    return tuple(
        piece
        for piece in (p.strip() for p in (raw or "").split(","))
        if piece
    )


def invalid_test_login_phones(
    phones: "tuple[str, ...] | list[str]",
) -> "tuple[str, ...]":
    """Allowlist entries that can NEVER match a login: not a parsed-format
    Indian mobile (+91 + 10 digits starting 6-9 — the same single-source
    pattern the OTP endpoints enforce). Such entries would silently never
    match, which is a configuration trap, so callers use this to fail fast
    (production boot) or warn (local). Never returns/exposes the values.
    """
    from .schemas import INDIAN_MOBILE_PATTERN

    compiled = re.compile(INDIAN_MOBILE_PATTERN)  # type: ignore[arg-type]
    return tuple(p for p in phones if not compiled.fullmatch(p))


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
    # NOTE — SMS delivery: the OTP service has NO provider and NO vendor
    # configuration by design (see app/services/otp.py). Until real SMS
    # delivery is connected in code, OTP requests fail closed (generic 503)
    # and phone-OTP login is unavailable. There is nothing to configure via
    # environment variables.
    # TEMPORARY INTERNAL-TESTING DISPLAY (explicitly restricted). When this
    # is "1" in a NON-production-classified process, the OTP request
    # response INCLUDES the backend-selected code ("debug_code") so
    # internal testers can log in without SMS delivery; the delivery seam
    # is bypassed while the mode is active and no SMS is sent. In
    # production this MUST be "0": validate_security_config() refuses
    # production startup with it enabled, and the OTP service re-checks
    # is_production() at request time — so production API responses and
    # logs can never contain a code. There is no SMS vendor behind it.
    OTP_DISPLAY_MODE: bool = _get("RESTAMP_OTP_DISPLAY_MODE", "0") == "1"
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

    # Restricted test-only login (see app/services/test_login.py): lets the
    # deployed backend support phone login WITHOUT an SMS provider during the
    # delivery-deferral window. Deliberately separate from the local OTP
    # display mode (independent flags, service, and state): enabling one
    # never affects the other. No OTP is generated, stored, returned, or
    # logged anywhere in this flow.
    # Disabled by default twice over: RESTAMP_TEST_LOGIN_ENABLED defaults "0",
    # and the endpoint is ALSO inactive while RESTAMP_TEST_LOGIN_KEY is empty
    # (inactive = 404 with the same body as an unknown route, so the
    # endpoint's existence is never disclosed). Candidates carry the key in
    # the X-Test-Login-Key header — kept out of the request body on purpose.
    # Production startup REFUSES the enabled state with a weak/missing key
    # (validate_security_config) — the production gate is strengthened,
    # never bypassed. Treat the key as a credential: enable only for bounded
    # test windows, then disable (flag back to "0") and/or rotate.
    TEST_LOGIN_ENABLED: bool = _get("RESTAMP_TEST_LOGIN_ENABLED", "0") == "1"
    TEST_LOGIN_KEY: str = _get("RESTAMP_TEST_LOGIN_KEY", "")
    # Explicit test-phone allowlist, comma-separated (each entry must be an
    # E.164 Indian mobile "+91###...", the same pattern the OTP endpoints
    # enforce). FAIL CLOSED: empty (or a never-matchable entry is dropped
    # only via invalid_test_login_phones at boot) → EVERY /auth/test-login
    # attempt is rejected (403) even with a valid key — no implicit default
    # test numbers exist. This list is the ONLY permission to USE the
    # endpoint: no phone outside it can ever log in, and list membership
    # is checked AFTER the constant-time key comparison (never an
    # unauthenticated oracle).
    TEST_LOGIN_ALLOWED_PHONES: tuple[str, ...] = _split_test_login_phones(
        _get("RESTAMP_TEST_LOGIN_ALLOWED_PHONES", "")
    )
    # Account CREATION is gated SEPARATELY from the key + allowlist
    # authorization (see app/services/test_login.py): existing allowlisted
    # identities always log in, but an allowlisted UNKNOWN phone creates a
    # user only when this flag is explicitly "1". With the default "0"
    # (login-only) the endpoint can never create an account: an unknown
    # phone gets the SAME generic 401 body as a wrong key (no
    # account-existence oracle) and not a single row is written.
    TEST_LOGIN_ALLOW_REGISTRATION: bool = _get(
        "RESTAMP_TEST_LOGIN_ALLOW_REGISTRATION", "0"
    ) == "1"
    TEST_LOGIN_MAX_PER_HOUR_PER_IP: int = _get_int(
        "RESTAMP_TEST_LOGIN_MAX_PER_HOUR_PER_IP", 10
    )
    TEST_LOGIN_MAX_PER_HOUR_PER_PHONE: int = _get_int(
        "RESTAMP_TEST_LOGIN_MAX_PER_HOUR_PER_PHONE", 20
    )

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

    Raises RuntimeError naming RESTAMP_ENV=demo FIRST, unconditionally
    (regardless of environment classification): the demo OTP login was
    removed from the codebase, so that legacy value must never boot as
    anything else.

    Safe in local/dev/test beyond that (no-op). Raises RuntimeError in
    production when:
    - JWT secret is missing, weak (<32 chars), or a known placeholder;
    - RESTAMP_OTP_DISPLAY_MODE is enabled (it must be '0' in production:
      OTP codes must never be returned in API responses; the temporary
      display mode exists for local/internal testing only);
    - RESTAMP_TEST_LOGIN_ENABLED is set without a strong RESTAMP_TEST_LOGIN_KEY
      (the restricted test-only login must never boot half-configured; it is
      a separate mechanism from the display mode with its own key);
    - RESTAMP_TEST_LOGIN_ALLOWED_PHONES contains entries that could never
      match a login (not valid Indian mobile numbers) while test login is
      enabled (fail fast; an EMPTY allowlist is bootable but rejects every
      attempt at request time);
    - RESTAMP_GOOGLE_DEV_MODE is enabled;
    - RESTAMP_CORS_ORIGINS contains '*' (never valid with credentials);
    - Google login is enabled without a real RESTAMP_GOOGLE_CLIENT_ID.
    (SMS/OTP delivery is not configurable and not validated here: real SMS
    delivery must be connected in code per app/services/otp.py; until then
    OTP requests fail closed at request time.)
    Messages name only the setting, never its value.
    """
    if (settings.ENV or "").strip().lower() == "demo":
        # The dedicated demo mode (predefined codes shown for login) was
        # removed: RESTAMP_ENV=demo must fail fast instead of silently
        # behaving like local/production anywhere it is left configured.
        raise RuntimeError(
            "Insecure configuration: RESTAMP_ENV=demo is no longer a "
            "supported environment (the demo OTP login was removed); use "
            "local/development locally or production/prod/staging for "
            "deployments."
        )
    if not is_production():
        if settings.TEST_LOGIN_ENABLED and not (settings.TEST_LOGIN_KEY or "").strip():
            _log.warning(
                "RESTAMP_TEST_LOGIN_ENABLED is '1' but RESTAMP_TEST_LOGIN_KEY is "
                "empty: /auth/test-login stays INACTIVE (404) until a strong "
                "key is configured."
            )
        return
    if not is_jwt_secret_strong(settings.JWT_SECRET):
        raise RuntimeError(
            "Insecure production configuration: RESTAMP_JWT_SECRET must be "
            "set to a strong random value at least 32 characters long."
        )
    if settings.OTP_DISPLAY_MODE:
        # The temporary internal-testing display mode must NEVER be enabled
        # on a production-classified deployment: OTP codes must never be
        # returned in API responses (or logs). The service also enforces
        # this at request time (see app/services/otp.py _display_active).
        raise RuntimeError(
            "Insecure production configuration: RESTAMP_OTP_DISPLAY_MODE must "
            "be '0' in production (OTP codes must never be returned in API "
            "responses)."
        )
    if settings.TEST_LOGIN_ENABLED and not is_jwt_secret_strong(
        settings.TEST_LOGIN_KEY
    ):
        # The restricted test-only login may run in production ONLY with a
        # strong key (same policy bar as the JWT secret: length + known
        # placeholder blocklist). Fail fast so the endpoint can never boot
        # half-configured. Message names the setting, never the value.
        raise RuntimeError(
            "Insecure production configuration: RESTAMP_TEST_LOGIN_KEY must "
            "be set to a strong random value at least 32 characters long "
            "when RESTAMP_TEST_LOGIN_ENABLED is '1'."
        )
    if settings.TEST_LOGIN_ENABLED:
        # The allowlist is the ONLY permission for a phone to use the
        # restricted test login (and registration additionally requires
        # TEST_LOGIN_ALLOW_REGISTRATION=1). Entries that could
        # never match a login silently disable the number instead — refuse
        # production boot so misconfiguration cannot slip through (setting
        # name only, never entry values). An EMPTY list is intentionally
        # bootable but fails closed at request time (every attempt → 403);
        # warn so the state is visible in logs.
        bad_entries = invalid_test_login_phones(
            settings.TEST_LOGIN_ALLOWED_PHONES
        )
        if bad_entries:
            raise RuntimeError(
                "Insecure production configuration: "
                "RESTAMP_TEST_LOGIN_ALLOWED_PHONES contains entries that are "
                "not valid Indian mobile numbers (+91 + 10 digits starting "
                "6-9); fix or remove them."
            )
        if not settings.TEST_LOGIN_ALLOWED_PHONES:
            _log.warning(
                "RESTAMP_TEST_LOGIN_ENABLED is '1' but "
                "RESTAMP_TEST_LOGIN_ALLOWED_PHONES is EMPTY: every "
                "/auth/test-login attempt will be rejected (403). Add "
                "explicit test phone numbers to enable logins."
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
    if settings.GOOGLE_ENABLED and not settings.GOOGLE_CLIENT_ID.strip():
        raise RuntimeError(
            "Insecure production configuration: RESTAMP_GOOGLE_ENABLED requires "
            "a real RESTAMP_GOOGLE_CLIENT_ID in production."
        )


# Backwards-compatible alias (Phase 3 code read DBConfig.SQLALCHEMY_DATABASE_URI).
class DBConfig:
    SQLALCHEMY_DATABASE_URI = settings.DATABASE_URL
    SQLALCHEMY_TRACK_MODIFICATIONS = False
