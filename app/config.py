"""RESTAMP Phase 4 — environment-based configuration.

No secrets are hardcoded. Production/cloud configuration is intentionally absent.
Database passwords come from RESTAMP_DATABASE_URL; the JWT secret from RESTAMP_JWT_SECRET.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import sys
from urllib.parse import urlsplit

from sqlalchemy.engine import make_url as _sqlalchemy_make_url

# Local-development convenience: the backend's own .env (gitignored, next to
# app/) is read BEFORE Settings below evaluates the environment, so
# `uvicorn app.main:app` picks up RESTAMP_DATABASE_URL etc. without a manual
# `export`. Without this, an unexported .env silently fell back to the
# built-in placeholder URL (user:pass@localhost) and MySQL refused it (1045).
#
# Precedence (never weakens deployment configuration):
# - A variable already set in the process environment ALWAYS wins; .env only
#   fills names that are unset. Render/PM2/systemd values are never
#   overridden, and Render deploys have no .env file at all (gitignored).
# - Skipped under pytest: the suite must get its database URL only from an
#   explicit export (tests/conftest.py refuses anything else), so a developer
#   .env can never silently point tests at restamp_dev.
# - RESTAMP_LOAD_DOTENV=0 disables it entirely.
# Values are never printed or logged. Same parsing as alembic/env.py.
_DOTENV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")


def _load_local_dotenv(path: str = _DOTENV_PATH) -> list[str]:
    """Fill UNSET environment variables from `path`; returns the names set."""
    if os.environ.get("RESTAMP_LOAD_DOTENV", "1") == "0" or "pytest" in sys.modules:
        return []
    if not os.path.isfile(path):
        return []
    loaded: list[str] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip().removeprefix("export ").strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value
                loaded.append(key)
    return loaded


DOTENV_LOADED_KEYS = _load_local_dotenv()


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _resolve_proxy_trust() -> tuple[bool, bool]:
    """(trusted, conflict) for forwarded client IPs (M8).

    RESTAMP_TRUST_PROXY is the canonical setting; RESTAMP_CHAT_TRUST_PROXY
    (the original Reva-only name) is a backward-compatible alias, so an
    existing deployment keeps working. Only the exact value "1" trusts the
    proxy. When BOTH are set and disagree, the result fails safe: the
    forwarded header is NOT trusted and `conflict` is reported, which
    production startup refuses (validate_security_config).
    """
    canonical = os.environ.get("RESTAMP_TRUST_PROXY")
    legacy = os.environ.get("RESTAMP_CHAT_TRUST_PROXY")
    values = [v.strip() == "1" for v in (canonical, legacy) if v is not None]
    if not values:
        return False, False
    if len(values) == 2 and values[0] != values[1]:
        return False, True
    return values[0], False


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


class Settings:
    # Explicit environment name, REQUIRED (no default): the app refuses to
    # start without it (validate_security_config). Development values:
    # local / development / dev / test. Production values: production /
    # prod / staging. Production is decided ONLY by this setting, never
    # inferred from the database host; an unset or unknown value is treated
    # as production by is_production() (fail closed) and refused at startup.
    ENV: str = _get("RESTAMP_ENV", "")
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

    # Phone OTP: the simplified login uses ONE fixed testing code
    # (app/services/otp.py TEST_OTP = "349086"), accepted and returned by
    # /auth/otp/request as `debug_code` where otp_display_permitted() is
    # true (local/development/dev/test or staging).
    # Production/prod fails closed at request time (503 on the request
    # endpoint, every verify rejected) unless the explicit opt-in below is
    # set; that opt-in is the ONLY OTP environment variable. The removed
    # RESTAMP_OTP_TTL_SECONDS/_MAX_*_/*_BLOCK_* rate-limit settings and
    # RESTAMP_OTP_DISPLAY_MODE are not read
    # anywhere, and validate_security_config refuses a production boot
    # with the obsolete display flag still set (a leftover testing
    # configuration must be cleaned up, not silently ignored).
    #
    # Explicit PRODUCTION opt-in for the same fixed code (owner decision,
    # no SMS provider exists). Read as the raw string: only the exact value
    # "1" enables it (fixed_otp_in_production) — "true", "yes", " 1" and
    # every other value leave production failing closed. When enabled,
    # production accepts TEST_OTP at verification but never returns it
    # (otp_display_permitted() is unchanged), refuses it for admin accounts,
    # and marks the resulting session amr=fixed_otp, which can never reach
    # admin endpoints or link a Google identity.
    PRODUCTION_FIXED_OTP: str = _get("RESTAMP_PRODUCTION_FIXED_OTP", "0")

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
    # Allowed browser origins (comma-separated exact origins, e.g.
    # "https://restamp.in,https://www.restamp.in"). REQUIRED in production,
    # where it is the ONLY source of allowed origins (each must be an exact
    # https origin; no "*", no localhost/LAN, no path). In development, empty
    # = the built-in local defaults in main.py, and localhost/LAN origins are
    # also allowed.
    CORS_ORIGINS: str = _get("RESTAMP_CORS_ORIGINS", "")

    # Local media storage for listing photo uploads (see app/services/storage.py).
    # Directory only; served at MEDIA_URL_PREFIX. Never inside the git repo.
    # An S3/object-storage backend replaces storage.py without config changes
    # beyond these two values.
    MEDIA_ROOT: str = _get("RESTAMP_MEDIA_ROOT", "/tmp/restamp-media")
    MEDIA_URL_PREFIX: str = "/media"

    # Where NEW property-photo uploads go: "cloudinary" (default, unchanged
    # behaviour) or "local" (server disk under MEDIA_ROOT, served at
    # MEDIA_URL_PREFIX). Existing rows are never migrated: each row's own
    # public_id ("local:" prefix or a Cloudinary id) decides how it is
    # deleted, so Cloudinary credentials stay required for legacy photos.
    # Any other value is refused at startup (validate_security_config).
    PHOTO_STORAGE: str = _get("RESTAMP_PHOTO_STORAGE", "cloudinary").strip().lower()
    # Absolute public base URL that local photo URLs are built from, e.g.
    # "https://api.restamp.in" -> https://api.restamp.in/media/<id>/<file>.
    # REQUIRED (https) for local mode in production: request.base_url is
    # never used there, because behind Nginx it is the internal upstream
    # (http://127.0.0.1:8000) and would be stored permanently in the DB.
    MEDIA_PUBLIC_BASE_URL: str = _get("RESTAMP_MEDIA_PUBLIC_BASE_URL", "").strip()
    # Per-photo limits for EVERY upload (local and Cloudinary). Kept as raw
    # strings: parse_photo_limit() parses them, startup refuses a malformed
    # or out-of-range value (validate_photo_storage_config), and unset keeps
    # the historical defaults (10 MB; 10000 px per side).
    PHOTO_MAX_BYTES_RAW: str = _get("RESTAMP_PHOTO_MAX_BYTES", "")
    PHOTO_MAX_DIMENSION_RAW: str = _get("RESTAMP_PHOTO_MAX_DIMENSION", "")

    # Cloudinary (property photo uploads). Credentials come ONLY from the
    # environment — never hardcoded, never logged. When CLOUDINARY_CLOUD_NAME
    # is empty, photo uploads are treated as unconfigured (503).
    CLOUDINARY_CLOUD_NAME: str = _get("CLOUDINARY_CLOUD_NAME", "")
    CLOUDINARY_API_KEY: str = _get("CLOUDINARY_API_KEY", "")
    CLOUDINARY_API_SECRET: str = _get("CLOUDINARY_API_SECRET", "")
    CLOUDINARY_FOLDER: str = _get("CLOUDINARY_FOLDER", "restamp")
    # Cloudinary (property photo uploads). Credentials come ONLY from the
    # environment — never hardcoded, never logged. When CLOUDINARY_CLOUD_NAME
    # is empty, photo uploads are treated as unconfigured (503).
    CLOUDINARY_CLOUD_NAME: str = _get("CLOUDINARY_CLOUD_NAME", "")
    CLOUDINARY_API_KEY: str = _get("CLOUDINARY_API_KEY", "")
    CLOUDINARY_API_SECRET: str = _get("CLOUDINARY_API_SECRET", "")
    CLOUDINARY_FOLDER: str = _get("CLOUDINARY_FOLDER", "restamp")
    # CORS configuration for development environments (local/development/dev/test)
    # These are additional allowed origins that supplement the built-in local defaults
    # when RESTAMP_ENV is a development value. Ignored in production unless
    # RESTAMP_CORS_ALLOW_LOCALHOST_IN_PRODUCTION is explicitly enabled.
    # Examples: "http://localhost:3000,http://localhost:8081"
    DEV_CORS_ORIGINS: str = _get("RESTAMP_CORS_DEV_ORIGINS", "")

    # PRODUCTION ONLY: explicit opt-in that lets a local frontend call a
    # production-classified backend. Only the exact value "1" enables it, and
    # it then allows the fixed origins in PRODUCTION_LOCAL_DEV_ORIGINS (below)
    # plus the exact localhost / 127.0.0.1 origins listed in
    # RESTAMP_CORS_DEV_ORIGINS (each validated by is_valid_localhost_origin;
    # anything else in that list is ignored). Never LAN or external hosts.
    # Default "0" (disabled): production allows only RESTAMP_CORS_ORIGINS.
    CORS_ALLOW_LOCALHOST_IN_PRODUCTION: bool = _get("RESTAMP_CORS_ALLOW_LOCALHOST_IN_PRODUCTION", "0") == "1"

    # DEVELOPMENT ONLY: comma-separated exact base-URL prefixes (each ending
    # in "/") that client-supplied listing photo URLs may also start with,
    # e.g. a local media server. Ignored entirely in production (see
    # app/services/media_urls.py), so it can never widen production
    # validation; a production process that has it set logs a warning.
    DEV_MEDIA_URL_PREFIXES: str = _get("RESTAMP_DEV_MEDIA_URL_PREFIXES", "")
    # Gates the Chennai locality catalog work (locality loader + Perungudi
    # postcode reassignment scripts, admin agent-locality endpoints, locality
    # aliases). Default OFF; ignored in production (area_seeding_enabled()),
    # so it can never switch on in a deployed environment by accident.
    AREA_SEEDING_ENABLED: bool = _get("RESTAMP_AREA_SEEDING_ENABLED", "0") == "1"

    # Public interactive API docs (/docs, /redoc, /openapi.json) in
    # production. Development always has them. Production only with an
    # explicit "1" (an owner decision: the docs list every route publicly but
    # grant no access — every endpoint keeps its own authentication).
    DOCS_ENABLED: bool = _get("RESTAMP_DOCS_ENABLED", "0") == "1"

    # Email notifications (Phase 3). Provider credentials are read ONLY from
    # the server environment — never hardcoded, never logged, never sent to a
    # client. EMAIL_PROVIDER selects the adapter (app/services/email_providers.py):
    #   disabled (default) — nothing is ever sent or queued;
    #   resend             — Resend HTTPS API (RESTAMP_RESEND_API_KEY);
    #   smtp               — any SMTP relay, e.g. Amazon SES / SendGrid / Postmark
    #                        (RESTAMP_SMTP_HOST/PORT/USERNAME/PASSWORD);
    #   log                — DEVELOPMENT ONLY: records metadata, sends nothing
    #                        (treated as disabled in production).
    # A provider with missing credentials or no EMAIL_FROM is treated as
    # disabled, so delivery can never be half-configured.
    EMAIL_PROVIDER: str = _get("RESTAMP_EMAIL_PROVIDER", "disabled")
    EMAIL_FROM: str = _get("RESTAMP_EMAIL_FROM", "")
    EMAIL_FROM_NAME: str = _get("RESTAMP_EMAIL_FROM_NAME", "RESTAMP")
    # Shown in every email footer; empty = no support line.
    EMAIL_SUPPORT: str = _get("RESTAMP_EMAIL_SUPPORT", "")
    # Public https base URL of the web app for "open in RESTAMP" links
    # (fixed paths only, see email_templates.py). Empty = emails carry no link.
    APP_BASE_URL: str = _get("RESTAMP_APP_BASE_URL", "")
    RESEND_API_KEY: str = _get("RESTAMP_RESEND_API_KEY", "")
    SMTP_HOST: str = _get("RESTAMP_SMTP_HOST", "")
    SMTP_PORT: int = _get_int("RESTAMP_SMTP_PORT", 587)
    SMTP_USERNAME: str = _get("RESTAMP_SMTP_USERNAME", "")
    SMTP_PASSWORD: str = _get("RESTAMP_SMTP_PASSWORD", "")
    EMAIL_TIMEOUT_SECONDS: int = _get_int("RESTAMP_EMAIL_TIMEOUT_SECONDS", 15)

    # Push notifications (Phase 4). Provider credentials are read ONLY from
    # the server environment — never hardcoded, never logged, never sent to a
    # client. PUSH_PROVIDER selects the adapter (app/services/push_providers.py):
    #   disabled (default) — nothing is queued or sent and no push endpoint is
    #                        contacted at all (the mobile app stays dormant too);
    #   expo               — Expo push HTTPS API (RESTAMP_EXPO_ACCESS_TOKEN
    #                        optional, set later from the company's Expo
    #                        account; never the personal project);
    #   log                — DEVELOPMENT ONLY: records metadata, sends nothing
    #                        (treated as disabled in production).
    # Push delivery is therefore OFF everywhere until an operator explicitly
    # selects a provider — mirroring the email providers' fail-closed rule.
    PUSH_PROVIDER: str = _get("RESTAMP_PUSH_PROVIDER", "disabled")
    EXPO_ACCESS_TOKEN: str = _get("RESTAMP_EXPO_ACCESS_TOKEN", "")
    PUSH_TIMEOUT_SECONDS: int = _get_int("RESTAMP_PUSH_TIMEOUT_SECONDS", 15)

    # Reva AI Chatbot configuration (optional external LLM; falls back to verified demo/FAQ mode).
    OPENAI_API_KEY: str = _get("OPENAI_API_KEY", "")
    OPENAI_MODEL: str = _get("OPENAI_MODEL", "gpt-4o-mini")
    OPENAI_TRANSCRIBE_MODEL: str = _get("OPENAI_TRANSCRIBE_MODEL", "gpt-transcribe")

    # Agaram 2.5B (Murai-Labs Tamil-English LLM: github.com/Murai-Labs/Agaram2.5b)
    AGARAM_ENDPOINT: str = _get("AGARAM_ENDPOINT", "")
    AGARAM_MODEL_NAME: str = _get("AGARAM_MODEL_NAME", "murai-labs/agaram-2.5b-instruct")
    AGARAM_API_KEY: str = _get("AGARAM_API_KEY", _get("HF_TOKEN", _get("HUGGINGFACE_API_KEY", "")))
    AGARAM_ENABLED: bool = _get("AGARAM_ENABLED", "1") == "1"

    # Forwarded client IP trust for ALL rate limiting (Reva + M8 communication
    # limits). RESTAMP_TRUST_PROXY=1 (canonical; RESTAMP_CHAT_TRUST_PROXY is a
    # backward-compatible alias) ONLY where a reverse proxy we control forwards
    # the real client IP in X-Forwarded-For (Render). When on, the client IP is
    # the RIGHTMOST forwarded entry (the one the trusted proxy appended); the
    # header is otherwise ignored, so a caller can never spoof a different
    # bucket by sending the header itself. Conflicting values fail safe (not
    # trusted) and production refuses to start. The attribute is deliberately
    # NOT named TRUST_PROXY: that bare attribute is a removed-OTP-setting lock
    # in test_otp_core_contract.py. Independent of every OTP gate.
    FORWARDED_IP_TRUSTED, TRUST_PROXY_CONFLICT = _resolve_proxy_trust()
    # M8: secret for HMAC-SHA256 rate-limit keys (IPs are never stored in
    # plaintext in communication buckets). Dedicated: must differ from the JWT
    # secret. Required (>=32 chars, not a placeholder) in production; local
    # development uses a fixed non-production key (see services/rate_limit).
    RATE_LIMIT_KEY: str = _get("RESTAMP_RATE_LIMIT_KEY", "")
    # Local test runs only: "1" disables the M8 communication limits (the test
    # client shares one IP across the whole suite). IGNORED in production.
    COMM_RATE_LIMITS_DISABLED: bool = _get("RESTAMP_COMM_RATE_LIMITS_DISABLED", "0") == "1"
    # Fixed-window limits (per bucket) for the AI endpoints. Shared storage
    # (rate_limit_windows) means one instance's hits count everywhere, so
    # these numbers are totals, not per-instance. 0 disables a limit (never
    # the recommended posture; allowed for local load tests only).
    CHAT_RPM_PER_IP: int = _get_int("RESTAMP_CHAT_RPM_PER_IP", 20)
    CHAT_RPM_PER_USER: int = _get_int("RESTAMP_CHAT_RPM_PER_USER", 10)
    CHAT_RPH_PER_IP: int = _get_int("RESTAMP_CHAT_RPH_PER_IP", 120)
    CHAT_RPH_PER_USER: int = _get_int("RESTAMP_CHAT_RPH_PER_USER", 60)
    TRANSCRIBE_PER_HOUR_PER_IP: int = _get_int("RESTAMP_TRANSCRIBE_PER_HOUR_PER_IP", 20)
    # Hard body-size caps (bytes) for the AI endpoints, checked before the
    # body is parsed: chat JSON stays tiny; audio uploads match the 8 MB
    # application cap with multipart overhead headroom.
    CHAT_MAX_BODY_BYTES: int = _get_int("RESTAMP_CHAT_MAX_BODY_BYTES", 16_384)
    TRANSCRIBE_MAX_BODY_BYTES: int = _get_int("RESTAMP_TRANSCRIBE_MAX_BODY_BYTES", 9_000_000)

    # Payments (Razorpay). Credentials are read ONLY from the server
    # environment — never hardcoded, never logged, never sent to a client.
    # PAYMENT_PROVIDER="none" (default) fails closed: checkout, verification
    # and webhook handling all return 503 and no subscription can be
    # activated through a payment. "razorpay" enables the real provider
    # flow; a razorpay selection with missing secrets is treated as
    # unconfigured (never half-configured), mirroring the email providers.
    PAYMENT_PROVIDER: str = _get("RESTAMP_PAYMENT_PROVIDER", "none")
    RAZORPAY_KEY_ID: str = _get("RAZORPAY_KEY_ID", "")
    RAZORPAY_KEY_SECRET: str = _get("RAZORPAY_KEY_SECRET", "")
    RAZORPAY_WEBHOOK_SECRET: str = _get("RAZORPAY_WEBHOOK_SECRET", "")
    RAZORPAY_TIMEOUT_SECONDS: int = _get_int("RESTAMP_RAZORPAY_TIMEOUT_SECONDS", 15)


settings = Settings()


# --- Phase 1: production hardening helpers (no secrets in messages/logs) ----
_log = logging.getLogger("restamp.config")

_PROD_ENVS = {"production", "prod", "staging"}
# The one production-classified environment where the fixed testing OTP is
# still usable (testers log in with the published code; every other
# production hardening rule applies unchanged).
_STAGING_ENV = "staging"
# The public website. A staging backend must never serve it: the fixed
# testing OTP is always active in staging, so the site pointing at a
# staging backend would let anyone log in as any phone number
# (validate_security_config refuses that combination).
_PUBLIC_SITE_ORIGINS = {"https://restamp.in", "https://www.restamp.in"}
_DEV_ENVS = {"local", "development", "dev", "test"}
# The built-in placeholder DATABASE_URL (never valid outside a laptop).
_DEFAULT_DATABASE_URL = "mysql+pymysql://user:pass@localhost/restamp"
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


def _env() -> str:
    return (settings.ENV or "").strip().lower()


def is_development() -> bool:
    """True only for an explicit development RESTAMP_ENV (local/development/dev/test)."""
    return _env() in _DEV_ENVS


def is_production() -> bool:
    """True unless RESTAMP_ENV is explicitly a development value.

    Production is decided by RESTAMP_ENV alone (production/prod/staging),
    never inferred from the database host. Fail-closed: an unset or unknown
    value is also treated as production here, so request-time hardening
    (hidden docs, no OTP display, no synthetic Google identities) can never
    be skipped by a missing setting; startup refuses those values outright
    (validate_security_config).
    """
    return not is_development()


def payment_bypass_enabled() -> bool:
    """TEMPORARY business decision (approved): users may activate a plan by
    pressing Continue on the payment page without paying (subscription
    source/payment provider BYPASS, never counted as paid). On by default;
    RESTAMP_PAYMENT_BYPASS=0 turns it off at once (read at call time). Remove
    this switch when a real payment gateway is connected."""
    return os.environ.get("RESTAMP_PAYMENT_BYPASS", "1").strip() != "0"


def area_seeding_enabled() -> bool:
    """TEMPORARILY DISABLED — Chennai area seeding paused.

    True only when RESTAMP_AREA_SEEDING_ENABLED=1 in a NON-production
    process. Read at call time, so it never latches."""
    return bool(settings.AREA_SEEDING_ENABLED) and not is_production()


def is_staging() -> bool:
    """True only for RESTAMP_ENV=staging (production-classified otherwise)."""
    return _env() == _STAGING_ENV


def otp_display_permitted() -> bool:
    """Whether the fixed OTP may be DISPLAYED (`debug_code`): only in an
    explicit development environment or RESTAMP_ENV=staging, never for
    production/prod (no override, no environment variable can widen it).
    Re-evaluated per request by the OTP service and the auth router.
    Acceptance at verification uses fixed_otp_permitted(), which adds only
    the explicit production opt-in."""
    return is_development() or is_staging()


# The production environment names that may opt in to the fixed OTP.
# Staging is deliberately excluded: it already has the testing OTP through
# otp_display_permitted(), with its own public-site CORS guard.
_FIXED_OTP_PROD_ENVS = {"production", "prod"}


def fixed_otp_in_production() -> bool:
    """True only for RESTAMP_ENV=production/prod with RESTAMP_PRODUCTION_FIXED_OTP
    set to exactly "1" (no truthy-string parsing, no whitespace tolerance).

    Re-evaluated per request, never latched. Callers use it to switch on the
    production-only restrictions that go with the opt-in: no code in any
    response, admin accounts refused, and the amr=fixed_otp session marker.
    """
    return _env() in _FIXED_OTP_PROD_ENVS and settings.PRODUCTION_FIXED_OTP == "1"


def fixed_otp_permitted() -> bool:
    """Whether the fixed OTP may be ACCEPTED at verification: the existing
    local/staging gate, or the explicit production opt-in. Display stays
    governed by otp_display_permitted() alone, so production never returns
    the code."""
    return otp_display_permitted() or fixed_otp_in_production()


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


# The ONLY local origins a production-classified backend may allow, and only
# with RESTAMP_CORS_ALLOW_LOCALHOST_IN_PRODUCTION=1: the website dev server
# (Next.js :3000) and Expo web (:8081). Fixed in code on purpose: no
# environment value can widen this to other ports, 127.0.0.1, LAN hosts or
# external origins.
PRODUCTION_LOCAL_DEV_ORIGINS: tuple[str, ...] = ("http://localhost:3000", "http://localhost:8081")


def production_local_dev_origins() -> list[str]:
    """The local origins production allows when opted in, else none: the
    fixed pair plus each RESTAMP_CORS_DEV_ORIGINS entry that is an exact
    http(s) localhost / 127.0.0.1 origin. Other entries are dropped."""
    if not settings.CORS_ALLOW_LOCALHOST_IN_PRODUCTION:
        return []
    extra = [o for o in parse_cors_origins(settings.DEV_CORS_ORIGINS) if is_valid_localhost_origin(o)]
    return list(dict.fromkeys(list(PRODUCTION_LOCAL_DEV_ORIGINS) + extra))


def parse_cors_origins(raw: str | None) -> list[str]:
    """RESTAMP_CORS_ORIGINS as a list of exact origins (comma-separated, trimmed)."""
    return [o.strip() for o in (raw or "").split(",") if o.strip()]


_PRIVATE_HOST = re.compile(
    r"^(localhost|.*\.localhost|127\.\d+\.\d+\.\d+|0\.0\.0\.0|::1|10\.\d+\.\d+\.\d+|"
    r"192\.168\.\d+\.\d+|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+|169\.254\.\d+\.\d+)$"
)


def is_valid_production_origin(origin: str) -> bool:
    """An exact public https origin: scheme + host (+ optional port), nothing
    else. Rejects "*", wildcards, http, paths/query/fragment, trailing
    slashes, upper-case hosts, credentials in the URL, and localhost /
    loopback / private-LAN hosts (those are development-only)."""
    if not origin or "*" in origin or origin != origin.strip():
        return False
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    if parts.scheme != "https" or not parts.hostname:
        return False
    if parts.path or parts.query or parts.fragment or parts.username or parts.password:
        return False
    try:
        port = parts.port
    except ValueError:
        return False
    # Exactly as a browser sends it (lowercase host, no default-port noise):
    # CORS compares origins as exact strings, so anything else never matches.
    rebuilt = f"https://{parts.hostname}" + (f":{port}" if port else "")
    if origin != rebuilt:
        return False
    return not _PRIVATE_HOST.match(parts.hostname.strip("[]"))


def is_valid_production_origin(origin: str) -> bool:
    """An exact public https origin: scheme + host (+ optional port), nothing
    else. Rejects "*", wildcards, http, paths/query/fragment, trailing
    slashes, upper-case hosts, credentials in the URL, and localhost /
    loopback / private-LAN hosts (those are development-only)."""
    if not origin or "*" in origin or origin != origin.strip():
        return False
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    if parts.scheme != "https" or not parts.hostname:
        return False
    if parts.path or parts.query or parts.fragment or parts.username or parts.password:
        return False
    try:
        port = parts.port
    except ValueError:
        return False
    # Exactly as a browser sends it (lowercase host, no default-port noise):
    # CORS compares origins as exact strings, so anything else never matches.
    rebuilt = f"https://{parts.hostname}" + (f":{port}" if port else "")
    if origin != rebuilt:
        return False
    return not _PRIVATE_HOST.match(parts.hostname.strip("[]"))


def is_valid_localhost_origin(origin: str) -> bool:
    """Validate that an origin is an exact http/https localhost or 127.0.0.1 origin with optional port.
    Used for RESTAMP_CORS_DEV_ORIGINS when CORS_ALLOW_LOCALHOST_IN_PRODUCTION is enabled.
    Only allows exact http/https origins for localhost or 127.0.0.1 with optional port.
    No wildcards, no paths, no credentials, no subdomains other than localhost.
    """
    if not origin or "*" in origin or origin != origin.strip():
        return False
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    if not parts.hostname:
        return False
    if parts.path or parts.query or parts.fragment or parts.username or parts.password:
        return False
    try:
        port = parts.port
    except ValueError:
        return False
    # Only allow localhost or 127.0.0.1 (with optional port)
    host = parts.hostname.strip("[]").lower()
    if host not in ("localhost", "127.0.0.1"):
        return False
    # Exactly as a browser sends it (lowercase host, no default-port noise):
    # CORS compares origins as exact strings, so anything else never matches.
    rebuilt = f"{parts.scheme}://{parts.hostname}" + (f":{port}" if port else "")
    if origin != rebuilt:
        return False
    return True


PHOTO_STORAGE_MODES = ("cloudinary", "local")

# (default, minimum, maximum) for the per-photo limits. The byte default is
# the historical 10 MB contract; the upper bounds stop a typo from turning
# the upload endpoint into a memory sink (uploads are buffered in memory).
PHOTO_MAX_BYTES_LIMITS = (10 * 1024 * 1024, 64 * 1024, 50 * 1024 * 1024)
# 10000 px per side covers current phone cameras (e.g. 8160x6120) while
# refusing decompression-bomb dimensions that would choke client decoders.
PHOTO_MAX_DIMENSION_LIMITS = (10000, 256, 30000)


def parse_photo_limit(raw: str | None, limits: tuple[int, int, int]) -> int:
    """Parse one photo-limit setting: unset/blank -> default; otherwise a
    plain decimal integer within [min, max], else ValueError."""
    default, low, high = limits
    value = (raw or "").strip()
    if not value:
        return default
    if not value.isdigit() or not value.isascii():
        raise ValueError("not a positive integer")
    number = int(value)
    if number < low or number > high:
        raise ValueError("out of range")
    return number


def is_valid_media_public_base_url(url: str | None, *, production: bool) -> bool:
    """True when ``url`` is a usable base for public photo URLs.

    Production: an https URL with a real public host (no credentials, no
    query/fragment, no IP literal, no localhost/.local/.internal) and an
    optional path, without a trailing "/". Development additionally allows
    http and loopback/LAN hosts so local devices can load photos.
    """
    raw = (url or "").strip()
    if not raw or raw.endswith("/") or any(ord(ch) <= 0x20 for ch in raw) or "\\" in raw:
        return False
    try:
        parts = urlsplit(raw)
        parts.port  # noqa: B018 — raises ValueError for a malformed port
    except ValueError:
        return False
    if parts.query or parts.fragment or "?" in raw or "#" in raw:
        return False
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return False
    host = (parts.hostname or "").lower()
    if not host:
        return False
    if not production:
        return parts.scheme in ("http", "https")
    if parts.scheme != "https":
        return False
    try:
        ipaddress.ip_address(host)
        return False  # IP literal: never a stable public media origin
    except ValueError:
        pass
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".localdomain")):
        return False
    return "." in host


# Repository root (…/Restamp-backend): photos must never be written inside
# the deployed source tree, where a redeploy or `git clean` could remove them.
_REPO_ROOT = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
# Locations that are wiped on reboot/restart or are per-process private
# (systemd PrivateTmp) — never persistent photo storage.
_EPHEMERAL_ROOTS = ("/tmp", "/var/tmp", "/dev/shm", "/run", "/var/run",
                    "/private/tmp", "/private/var/tmp", "/proc", "/sys", "/dev")


def effective_media_root() -> str:
    """The root storage.get_media_root() will use (env read per call)."""
    return os.environ.get("RESTAMP_MEDIA_ROOT") or settings.MEDIA_ROOT


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def unsafe_media_root_reason(raw: str | None) -> str | None:
    """Why ``raw`` cannot be production photo storage, or None when usable.

    Requires an absolute path without '..' components that does not resolve
    (symlinks included) to the filesystem root, a temporary/volatile
    location, the system temp dir, or anywhere inside the repository.
    """
    value = (raw or "").strip()
    if not value or not os.path.isabs(value):
        return "must be an absolute path"
    if ".." in value.replace("\\", "/").split("/"):
        return "must not contain '..'"
    resolved = os.path.realpath(value)
    if resolved == "/":
        return "must not be the filesystem root"
    try:
        import tempfile

        temp_dirs = {os.path.realpath(tempfile.gettempdir())}
    except Exception:  # pragma: no cover - gettempdir never fails in practice
        temp_dirs = set()
    for root in (*_EPHEMERAL_ROOTS, *temp_dirs):
        if _within(resolved, os.path.realpath(root)) or _within(value, root):
            return "must not be a temporary or volatile directory"
    if _within(resolved, _REPO_ROOT) or _within(_REPO_ROOT, resolved):
        return "must be outside the application repository"
    return None


def _running_on_render() -> bool:
    # Render sets RENDER=true in every service environment.
    return os.environ.get("RENDER", "").strip().lower() == "true"


def _render_disk_acknowledged() -> bool:
    return os.environ.get("RESTAMP_RENDER_PERSISTENT_DISK", "").strip() == "1"


def validate_photo_storage_config(*, production: bool) -> None:
    """Refuse an unusable photo-storage configuration at startup.

    Every environment: RESTAMP_PHOTO_STORAGE must be "cloudinary" or
    "local", the photo limits must parse, and "local" is refused on Render
    without an acknowledged persistent disk. Production + disk storage:
    RESTAMP_MEDIA_ROOT must be a persistent absolute directory outside the
    repository (unsafe_media_root_reason). Production + local:
    RESTAMP_MEDIA_PUBLIC_BASE_URL must be a valid https base URL.
    Development + local with a base URL set: it must at least be a valid
    http(s) base. Messages name settings, never values.
    """
    for setting, raw, limits in (
        ("RESTAMP_PHOTO_MAX_BYTES", settings.PHOTO_MAX_BYTES_RAW, PHOTO_MAX_BYTES_LIMITS),
        ("RESTAMP_PHOTO_MAX_DIMENSION", settings.PHOTO_MAX_DIMENSION_RAW,
         PHOTO_MAX_DIMENSION_LIMITS),
    ):
        try:
            parse_photo_limit(raw, limits)
        except ValueError:
            raise RuntimeError(
                f"Invalid configuration: {setting} must be an integer between "
                f"{limits[1]} and {limits[2]}."
            ) from None
    mode = (settings.PHOTO_STORAGE or "").strip().lower()
    if mode not in PHOTO_STORAGE_MODES:
        raise RuntimeError(
            "Invalid configuration: RESTAMP_PHOTO_STORAGE must be "
            "'cloudinary' or 'local'."
        )
    # Production disk storage is used in "local" mode, and also by the
    # historical "cloudinary"-without-a-cloud fallback once a public base URL
    # exists (without one, uploads fail closed before any write). Either way
    # the root must be persistent, so validate it whenever disk is in play.
    disk_in_play = mode == "local" or (
        not settings.CLOUDINARY_CLOUD_NAME and bool(settings.MEDIA_PUBLIC_BASE_URL)
    )
    if production and disk_in_play:
        reason = unsafe_media_root_reason(effective_media_root())
        if reason:
            raise RuntimeError(
                "Invalid production configuration: RESTAMP_MEDIA_ROOT "
                f"{reason}. Use a dedicated persistent directory such as "
                "/var/www/restamp/uploads."
            )
    if mode == "local" and _running_on_render() and not _render_disk_acknowledged():
        # Render's normal filesystem is wiped on every deploy/restart: photos
        # would vanish while their DB rows remain. Only an explicitly mounted
        # Render persistent disk may hold them (acknowledged by flag).
        raise RuntimeError(
            "Invalid configuration: RESTAMP_PHOTO_STORAGE=local on Render "
            "requires a mounted persistent disk under RESTAMP_MEDIA_ROOT and "
            "RESTAMP_RENDER_PERSISTENT_DISK=1; otherwise keep 'cloudinary'."
        )
    if mode != "local":
        return
    base = settings.MEDIA_PUBLIC_BASE_URL
    if production and not is_valid_media_public_base_url(base, production=True):
        raise RuntimeError(
            "Invalid production configuration: RESTAMP_PHOTO_STORAGE=local "
            "requires RESTAMP_MEDIA_PUBLIC_BASE_URL to be an absolute https "
            "URL with a public host and no trailing slash."
        )
    if not production and base and not is_valid_media_public_base_url(base, production=False):
        raise RuntimeError(
            "Invalid configuration: RESTAMP_MEDIA_PUBLIC_BASE_URL must be an "
            "absolute http(s) URL with no trailing slash."
        )


def validate_security_config() -> None:
    """Fail fast with non-sensitive errors when the configuration is unsafe.

    Always (every environment):
    - RESTAMP_ENV must be set explicitly to a known value: development
      (local/development/dev/test) or production (production/prod/staging).
      Unset or unknown values are refused (no silent default), and the
      removed RESTAMP_ENV=demo is refused by name.
    - A development RESTAMP_ENV must use a local database (unix socket or
      loopback host): a deployment that forgot RESTAMP_ENV=production must
      not boot with development behaviour against a real database.

    Production (RESTAMP_ENV=production/prod/staging) also refuses:
    - RESTAMP_DATABASE_URL unset (the built-in placeholder);
    - a missing, weak (<32 chars) or placeholder RESTAMP_JWT_SECRET;
    - the obsolete RESTAMP_OTP_DISPLAY_MODE=1 (the flag was removed with
      the simplified OTP login; a production environment still carrying it
      is a leftover testing configuration);
    - RESTAMP_GOOGLE_DEV_MODE=1, or RESTAMP_GOOGLE_ENABLED=1 without a real
      RESTAMP_GOOGLE_CLIENT_ID;
    - RESTAMP_CORS_ORIGINS empty, containing "*", or containing anything
      other than exact public https origins (no localhost/LAN, no paths).
    Staging additionally refuses RESTAMP_CORS_ORIGINS listing the public
    site (https://restamp.in, https://www.restamp.in): the fixed testing
    OTP is always active there, so a staging backend must never serve the
    public site; staging also logs a startup warning that the testing OTP
    is active. Production with RESTAMP_PRODUCTION_FIXED_OTP=1 passes every
    check above unchanged and then logs a startup warning.
    SMS delivery is not a startup failure (that would take the whole API
    down): without the opt-in, production logs a warning that OTP login is
    unavailable, and OTP requests fail closed with a generic 503 at
    request time.
    Messages name only the setting, never its value.
    """
    env = _env()
    if env == "demo":
        # The dedicated demo mode (predefined codes shown for login) was
        # removed: RESTAMP_ENV=demo must fail fast instead of silently
        # behaving like local/production anywhere it is left configured.
        raise RuntimeError(
            "Insecure configuration: RESTAMP_ENV=demo is no longer a "
            "supported environment (the demo OTP login was removed); use "
            "local/development locally or production/prod/staging for "
            "deployments."
        )
    if env not in _DEV_ENVS and env not in _PROD_ENVS:
        raise RuntimeError(
            "Unsafe configuration: RESTAMP_ENV must be set explicitly — "
            "production (or prod/staging) for deployments, local (or "
            "development/dev/test) for development."
        )
    validate_photo_storage_config(production=env in _PROD_ENVS)
    if env in _DEV_ENVS:
        if not _is_local_database_url(settings.DATABASE_URL):
            raise RuntimeError(
                "Unsafe configuration: RESTAMP_DATABASE_URL points at a "
                "non-local database while RESTAMP_ENV is a development value; "
                "deployments must set RESTAMP_ENV=production."
            )
        return

    # --- production --------------------------------------------------------
    if (settings.DATABASE_URL or "").strip() in ("", _DEFAULT_DATABASE_URL):
        raise RuntimeError(
            "Insecure production configuration: RESTAMP_DATABASE_URL must be "
            "set explicitly in production."
        )
    if not is_jwt_secret_strong(settings.JWT_SECRET):
        raise RuntimeError(
            "Insecure production configuration: RESTAMP_JWT_SECRET must be "
            "set to a strong random value at least 32 characters long."
        )
    # M8: rate-limit keys are HMAC-SHA256 of the client IP with a DEDICATED
    # secret. Without a strong one, keys could not be derived (no plaintext
    # fallback exists), so refuse to boot. Setting names only, never values.
    if not is_jwt_secret_strong(settings.RATE_LIMIT_KEY):
        raise RuntimeError(
            "Insecure production configuration: RESTAMP_RATE_LIMIT_KEY must be "
            "set to a strong random value at least 32 characters long."
        )
    if settings.RATE_LIMIT_KEY == settings.JWT_SECRET:
        raise RuntimeError(
            "Insecure production configuration: RESTAMP_RATE_LIMIT_KEY must be "
            "a dedicated secret, different from RESTAMP_JWT_SECRET."
        )
    if settings.TRUST_PROXY_CONFLICT:
        raise RuntimeError(
            "Unsafe production configuration: RESTAMP_TRUST_PROXY and "
            "RESTAMP_CHAT_TRUST_PROXY disagree; set only RESTAMP_TRUST_PROXY."
        )
    if settings.COMM_RATE_LIMITS_DISABLED:
        # Fail safe rather than refuse: production IGNORES this local-test
        # switch (services.rate_limit.comm_limit enforces the limits whenever
        # is_production()), so it can never disable them here. Say so.
        _log.warning(
            "RESTAMP_COMM_RATE_LIMITS_DISABLED is set but ignored in production: "
            "communication rate limits are always enforced."
        )
    if os.environ.get("RESTAMP_OTP_DISPLAY_MODE", "").strip() == "1":
        # Obsolete flag from the removed display-mode OTP: it is inert
        # (nothing reads it), but a production environment still carrying
        # it signals a leftover testing configuration. Refuse to boot so
        # the testing mechanism can never be assumed active on a real
        # deployment. Production never displays the code; acceptance is
        # controlled only by RESTAMP_PRODUCTION_FIXED_OTP.
        raise RuntimeError(
            "Insecure production configuration: RESTAMP_OTP_DISPLAY_MODE was "
            "removed with the simplified OTP login. Delete it from the "
            "environment; production never displays OTP codes (fixed-OTP "
            "acceptance is controlled only by RESTAMP_PRODUCTION_FIXED_OTP)."
        )
    if settings.GOOGLE_DEV_MODE:
        raise RuntimeError(
            "Insecure production configuration: RESTAMP_GOOGLE_DEV_MODE must be '0' "
            "in production (synthetic dev tokens must never be accepted)."
        )
    if settings.GOOGLE_ENABLED and not settings.GOOGLE_CLIENT_ID.strip():
        raise RuntimeError(
            "Insecure production configuration: RESTAMP_GOOGLE_ENABLED requires "
            "a real RESTAMP_GOOGLE_CLIENT_ID in production."
        )
    cors = parse_cors_origins(settings.CORS_ORIGINS)
    if "*" in cors:
        raise RuntimeError(
            "Insecure production configuration: RESTAMP_CORS_ORIGINS must not "
            "contain '*' when credentials are enabled."
        )
    if not cors:
        raise RuntimeError(
            "Insecure production configuration: RESTAMP_CORS_ORIGINS must list "
            "the allowed website origins in production."
        )
    if not all(is_valid_production_origin(o) for o in cors):
        raise RuntimeError(
            "Insecure production configuration: RESTAMP_CORS_ORIGINS must contain "
            "only exact public https origins (scheme and host, no path, no "
            "wildcard, no localhost or private-network hosts)."
        )
    # Local frontend origins (explicit opt-in): the fixed pair plus the
    # exact localhost / 127.0.0.1 entries of RESTAMP_CORS_DEV_ORIGINS. Never
    # silently: say which origins are open (all are localhost-only by
    # construction) and how many configured entries were dropped as invalid.
    dev_listed = parse_cors_origins(settings.DEV_CORS_ORIGINS)
    if settings.CORS_ALLOW_LOCALHOST_IN_PRODUCTION:
        _log.warning(
            "RESTAMP_CORS_ALLOW_LOCALHOST_IN_PRODUCTION=1: browser origins %s "
            "may call this production backend (credentialed CORS). CORS is not "
            "authentication: every endpoint still requires its normal "
            "authentication and authorization.",
            ", ".join(production_local_dev_origins()),
        )
        dropped = sum(1 for o in dev_listed if not is_valid_localhost_origin(o))
        if dropped:
            _log.warning(
                "RESTAMP_CORS_DEV_ORIGINS: %d entr%s ignored in production (only "
                "exact http(s)://localhost or 127.0.0.1 origins are allowed).",
                dropped, "y" if dropped == 1 else "ies",
            )
    elif dev_listed:
        _log.warning(
            "RESTAMP_CORS_DEV_ORIGINS is set but ignored in production; set "
            "RESTAMP_CORS_ALLOW_LOCALHOST_IN_PRODUCTION=1 to allow its "
            "localhost origins."
        )
    if is_staging():
        # The fixed testing OTP is ALWAYS active in staging (there is no
        # flag): anyone who can reach this backend can log in as any phone
        # number with the published testing code. A staging process must
        # therefore never serve the public site — refuse that combination
        # and say so loudly at startup.
        if _PUBLIC_SITE_ORIGINS & set(cors):
            raise RuntimeError(
                "Insecure staging configuration: RESTAMP_CORS_ORIGINS must "
                "not list the public site (https://restamp.in or "
                "https://www.restamp.in) while the fixed testing OTP is "
                "active. Point the public site at a production backend."
            )
        _log.warning(
            "RESTAMP_ENV=staging: the fixed testing OTP is active — every "
            "phone number can log in with the published testing code. Never "
            "point real users at this deployment."
        )
    if fixed_otp_in_production():
        # Not a refusal (an explicit owner opt-in), but never silent. All the
        # production checks above still ran and passed before this point.
        _log.warning(
            "RESTAMP_PRODUCTION_FIXED_OTP=1: the fixed OTP is accepted in "
            "production. Anyone can sign in as any ordinary (non-admin) phone "
            "account. Admin accounts are refused and fixed-OTP sessions cannot "
            "use admin endpoints or link Google."
        )
    # Photo-upload storage: NOT a hard failure (that would be a behavior
    # change requiring an explicit product decision). When production has no
    # Cloudinary cloud configured, uploads fall back to the local disk.
    # Warn loudly at startup so the trade-off is visible, naming settings
    # only (never values).
    if not settings.CLOUDINARY_CLOUD_NAME:
        if settings.PHOTO_STORAGE == "local":
            # New uploads go to disk; Cloudinary is only needed to destroy
            # legacy Cloudinary photos when an owner removes them.
            _log.warning(
                "CLOUDINARY_CLOUD_NAME is empty: removing legacy Cloudinary "
                "listing photos will leave their Cloudinary assets in place."
            )
        else:
            _log.warning(
                "Production media storage not fully configured: CLOUDINARY_CLOUD_NAME "
                "is empty; listing photo uploads will fall back to local-disk storage "
                "under RESTAMP_MEDIA_ROOT. Set RESTAMP_PHOTO_STORAGE=local with "
                "RESTAMP_MEDIA_PUBLIC_BASE_URL for server storage, or configure "
                "CLOUDINARY_CLOUD_NAME/CLOUDINARY_API_KEY/CLOUDINARY_API_SECRET."
            )
    if is_staging():
        # The fixed testing OTP is ALWAYS active in staging (there is no
        # flag): anyone who can reach this backend can log in as any phone
        # number with the published testing code. A staging process must
        # therefore never serve the public site — refuse that combination
        # and say so loudly at startup.
        if _PUBLIC_SITE_ORIGINS & set(cors):
            raise RuntimeError(
                "Insecure staging configuration: RESTAMP_CORS_ORIGINS must "
                "not list the public site (https://restamp.in or "
                "https://www.restamp.in) while the fixed testing OTP is "
                "active. Point the public site at a production backend."
            )
        _log.warning(
            "RESTAMP_ENV=staging: the fixed testing OTP is active — every "
            "phone number can log in with the published testing code. Never "
            "point real users at this deployment."
        )
        return
    if fixed_otp_in_production():
        # Not a refusal (an explicit owner opt-in), but never silent. All the
        # production checks above still ran and passed before this point.
        _log.warning(
            "RESTAMP_PRODUCTION_FIXED_OTP=1: the fixed OTP is accepted in "
            "production. Anyone can sign in as any ordinary (non-admin) phone "
            "account. Admin accounts are refused and fixed-OTP sessions cannot "
            "use admin endpoints or link Google."
        )
    # Photo-upload storage: NOT a hard failure (that would be a behavior
    # change requiring an explicit product decision). When production has no
    # Cloudinary cloud configured, uploads fall back to the local disk.
    # Warn loudly at startup so the trade-off is visible, naming settings
    # only (never values).
    if not settings.CLOUDINARY_CLOUD_NAME:
        if settings.PHOTO_STORAGE == "local":
            # New uploads go to disk; Cloudinary is only needed to destroy
            # legacy Cloudinary photos when an owner removes them.
            _log.warning(
                "CLOUDINARY_CLOUD_NAME is empty: removing legacy Cloudinary "
                "listing photos will leave their Cloudinary assets in place."
            )
        else:
            _log.warning(
                "Production media storage not fully configured: CLOUDINARY_CLOUD_NAME "
                "is empty; listing photo uploads will fall back to local-disk storage "
                "under RESTAMP_MEDIA_ROOT. Set RESTAMP_PHOTO_STORAGE=local with "
                "RESTAMP_MEDIA_PUBLIC_BASE_URL for server storage, or configure "
                "CLOUDINARY_CLOUD_NAME/CLOUDINARY_API_KEY/CLOUDINARY_API_SECRET."
            )
    if not all(is_valid_production_origin(o) for o in cors):
        raise RuntimeError(
            "Insecure production configuration: RESTAMP_CORS_ORIGINS must contain "
            "only exact public https origins (scheme and host, no path, no "
            "wildcard, no localhost or private-network hosts)."
        )
    if is_staging():
        # The fixed testing OTP is ALWAYS active in staging (there is no
        # flag): anyone who can reach this backend can log in as any phone
        # number with the published testing code. A staging process must
        # therefore never serve the public site — refuse that combination
        # and say so loudly at startup.
        if _PUBLIC_SITE_ORIGINS & set(cors):
            raise RuntimeError(
                "Insecure staging configuration: RESTAMP_CORS_ORIGINS must "
                "not list the public site (https://restamp.in or "
                "https://www.restamp.in) while the fixed testing OTP is "
                "active. Point the public site at a production backend."
            )
        _log.warning(
            "RESTAMP_ENV=staging: the fixed testing OTP is active — every "
            "phone number can log in with the published testing code. Never "
            "point real users at this deployment."
        )
        return
    if fixed_otp_in_production():
        # Not a refusal (an explicit owner opt-in), but never silent. All the
        # production checks above still ran and passed before this point.
        _log.warning(
            "RESTAMP_PRODUCTION_FIXED_OTP=1: the fixed OTP is accepted in "
            "production. Anyone can sign in as any ordinary (non-admin) phone "
            "account. Admin accounts are refused and fixed-OTP sessions cannot "
            "use admin endpoints or link Google."
        )
    # Photo-upload storage: NOT a hard failure (that would be a behavior
    # change requiring an explicit product decision). When production has no
    # Cloudinary cloud configured, uploads fall back to the local disk.
    # Warn loudly at startup so the trade-off is visible, naming settings
    # only (never values).
    if not settings.CLOUDINARY_CLOUD_NAME:
        if settings.PHOTO_STORAGE == "local":
            # New uploads go to disk; Cloudinary is only needed to destroy
            # legacy Cloudinary photos when an owner removes them.
            _log.warning(
                "CLOUDINARY_CLOUD_NAME is empty: removing legacy Cloudinary "
                "listing photos will leave their Cloudinary assets in place."
            )
        else:
            _log.warning(
                "Production media storage not fully configured: CLOUDINARY_CLOUD_NAME "
                "is empty; listing photo uploads will fall back to local-disk storage "
                "under RESTAMP_MEDIA_ROOT. Set RESTAMP_PHOTO_STORAGE=local with "
                "RESTAMP_MEDIA_PUBLIC_BASE_URL for server storage, or configure "
                "CLOUDINARY_CLOUD_NAME/CLOUDINARY_API_KEY/CLOUDINARY_API_SECRET."
            )
    if (settings.DEV_MEDIA_URL_PREFIXES or "").strip():
        # Not fatal: the media-URL validator ignores it in production, so
        # production validation is unchanged. Warn so the stray setting is
        # visible (setting name only, never its value).
        _log.warning(
            "RESTAMP_DEV_MEDIA_URL_PREFIXES is set but ignored in production; "
            "listing photo URLs must be approved Cloudinary URLs."
        )



# Backwards-compatible alias (Phase 3 code read DBConfig.SQLALCHEMY_DATABASE_URI).
class DBConfig:
    SQLALCHEMY_DATABASE_URI = settings.DATABASE_URL
    SQLALCHEMY_TRACK_MODIFICATIONS = False
