"""RESTAMP Phase 4 — environment-based configuration.

No secrets are hardcoded. Production/cloud configuration is intentionally absent.
Database passwords come from RESTAMP_DATABASE_URL; the JWT secret from RESTAMP_JWT_SECRET.
"""
from __future__ import annotations

import logging
import os
import re
from urllib.parse import urlsplit

from sqlalchemy.engine import make_url as _sqlalchemy_make_url


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


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

    # Cloudinary (property photo uploads). Credentials come ONLY from the
    # environment — never hardcoded, never logged. When CLOUDINARY_CLOUD_NAME
    # is empty, photo uploads are treated as unconfigured (503).
    CLOUDINARY_CLOUD_NAME: str = _get("CLOUDINARY_CLOUD_NAME", "")
    CLOUDINARY_API_KEY: str = _get("CLOUDINARY_API_KEY", "")
    CLOUDINARY_API_SECRET: str = _get("CLOUDINARY_API_SECRET", "")
    CLOUDINARY_FOLDER: str = _get("CLOUDINARY_FOLDER", "restamp")
    # DEVELOPMENT ONLY: comma-separated exact base-URL prefixes (each ending
    # in "/") that client-supplied listing photo URLs may also start with,
    # e.g. a local media server. Ignored entirely in production (see
    # app/services/media_urls.py), so it can never widen production
    # validation; a production process that has it set logs a warning.
    DEV_MEDIA_URL_PREFIXES: str = _get("RESTAMP_DEV_MEDIA_URL_PREFIXES", "")

    # TEMPORARILY DISABLED — Chennai area seeding paused.
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

    # Reva AI Chatbot configuration (optional external LLM; falls back to verified demo/FAQ mode).
    OPENAI_API_KEY: str = _get("OPENAI_API_KEY", "")
    OPENAI_MODEL: str = _get("OPENAI_MODEL", "gpt-4o-mini")
    OPENAI_TRANSCRIBE_MODEL: str = _get("OPENAI_TRANSCRIBE_MODEL", "gpt-transcribe")

    # Agaram 2.5B (Murai-Labs Tamil-English LLM: github.com/Murai-Labs/Agaram2.5b)
    AGARAM_ENDPOINT: str = _get("AGARAM_ENDPOINT", "")
    AGARAM_MODEL_NAME: str = _get("AGARAM_MODEL_NAME", "murai-labs/agaram-2.5b-instruct")
    AGARAM_API_KEY: str = _get("AGARAM_API_KEY", _get("HF_TOKEN", _get("HUGGINGFACE_API_KEY", "")))
    AGARAM_ENABLED: bool = _get("AGARAM_ENABLED", "1") == "1"


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
        _log.warning(
            "Production media storage not fully configured: CLOUDINARY_CLOUD_NAME "
            "is empty; listing photo uploads will fall back to local-disk storage "
            "(ephemeral across restarts). Configure CLOUDINARY_CLOUD_NAME/"
            "CLOUDINARY_API_KEY/CLOUDINARY_API_SECRET for persistent uploads."
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
