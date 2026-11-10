"""RESTAMP Phase 4 — environment-based configuration.

No secrets are hardcoded. Production/cloud configuration is intentionally absent.
Database passwords come from RESTAMP_DATABASE_URL; the JWT secret from RESTAMP_JWT_SECRET.
"""
from __future__ import annotations

import ipaddress
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

    # Phone OTP behaviour.
    OTP_LENGTH: int = 6
    OTP_TTL_SECONDS: int = _get_int("RESTAMP_OTP_TTL_SECONDS", 300)
    # Code requests per phone per ROLLING hour. Rejects only the excess
    # request (no block); a successful login clears the phone's window, so
    # re-logins after token expiry never accumulate into a lockout.
    OTP_MAX_REQUESTS_PER_HOUR: int = _get_int("RESTAMP_OTP_MAX_REQUESTS_PER_HOUR", 10)
    # Wrong guesses allowed against ONE code before it is invalidated.
    OTP_MAX_VERIFY_ATTEMPTS: int = _get_int("RESTAMP_OTP_MAX_VERIFY_ATTEMPTS", 5)
    # Fixed block after too many wrong codes (per code or per IP). Attempts
    # made while blocked are refused without extending it.
    OTP_BLOCK_SECONDS: int = _get_int("RESTAMP_OTP_BLOCK_SECONDS", 600)
    # NOTE — SMS delivery: the OTP service has NO provider and NO vendor
    # configuration by design (see app/services/otp.py). Until real SMS
    # delivery is connected in code, OTP requests fail closed (generic 503)
    # and phone-OTP login is unavailable. There is nothing to configure via
    # environment variables.
    # LOCAL-DEVELOPMENT DISPLAY (explicitly restricted). When this is "1"
    # in a development environment (RESTAMP_ENV=local/development/dev/test),
    # the OTP request response INCLUDES the backend-selected code
    # ("debug_code") so developers can log in without SMS delivery; the
    # delivery seam is bypassed while the mode is active and no SMS is sent.
    # Production can never show a code: validate_security_config() refuses
    # production startup with this flag on, the OTP service re-checks
    # is_production() on every request, and the auth router drops any code
    # in production. There is no production override.
    OTP_DISPLAY_MODE: bool = _get("RESTAMP_OTP_DISPLAY_MODE", "0") == "1"
    # RESTRICTED INTERNAL TESTER DISPLAY (production-capable, off by default).
    # The code is added to the OTP response ONLY when ALL of these hold,
    # decided server-side per request (otp_test_display_permitted):
    #   - RESTAMP_OTP_TEST_DISPLAY=1;
    #   - the phone is one of RESTAMP_OTP_TEST_PHONES (dedicated staff test
    #     numbers, server config only);
    #   - the client IP is inside RESTAMP_OTP_TEST_ALLOWED_CIDRS (office /
    #     VPN egress ranges, server config only).
    # Nothing in a request body, query or header can turn it on. Any other
    # phone or network gets the normal response (no code). Production startup
    # refuses an incomplete or over-broad configuration. Separate from
    # RESTAMP_OTP_DISPLAY_MODE, which stays development-only.
    OTP_TEST_DISPLAY: bool = _get("RESTAMP_OTP_TEST_DISPLAY", "0") == "1"
    OTP_TEST_PHONES: str = _get("RESTAMP_OTP_TEST_PHONES", "")
    OTP_TEST_ALLOWED_CIDRS: str = _get("RESTAMP_OTP_TEST_ALLOWED_CIDRS", "")
    # Per-IP abuse protection (in-memory, per process — see otp.py).
    # These bound total OTP request/verify-failure volume from a single
    # client IP across all phones, so rotating phones does not bypass
    # the per-phone caps. Tunable via env; defaults allow normal
    # NAT sharing while stopping bulk abuse.
    OTP_MAX_REQUESTS_PER_HOUR_PER_IP: int = _get_int(
        "RESTAMP_OTP_MAX_REQUESTS_PER_HOUR_PER_IP", 60
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


settings = Settings()


# --- Phase 1: production hardening helpers (no secrets in messages/logs) ----
_log = logging.getLogger("restamp.config")

_PROD_ENVS = {"production", "prod", "staging"}
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


def otp_display_permitted() -> bool:
    """Whether the OTP display mode may be active in this process: only in
    an explicit development environment, never in production (no override).
    Re-evaluated per request by the OTP service and the auth router."""
    return is_development()


_TEST_PHONE = re.compile(r"^\+91[6-9]\d{9}$")
_MAX_TEST_PHONES = 20
# Narrowest-allowed breadth for an internal tester network: an office / VPN
# egress range, never "the internet" or a whole provider.
_MIN_IPV4_PREFIX = 24
_MIN_IPV6_PREFIX = 48


def otp_test_phones() -> set[str]:
    """RESTAMP_OTP_TEST_PHONES as a set (only valid +91 mobiles)."""
    return {p.strip() for p in (settings.OTP_TEST_PHONES or "").split(",") if _TEST_PHONE.match(p.strip())}


def otp_test_networks() -> list:
    """RESTAMP_OTP_TEST_ALLOWED_CIDRS as ip_network objects (invalid entries skipped)."""
    nets = []
    for raw in (settings.OTP_TEST_ALLOWED_CIDRS or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            nets.append(ipaddress.ip_network(raw, strict=False))
        except ValueError:
            continue
    return nets


def _network_is_acceptable(net) -> bool:
    """Narrow enough, and never loopback/unspecified (a misconfigured proxy
    makes every client look like 127.0.0.1, which must never qualify)."""
    if net.is_loopback or net.is_unspecified or net.network_address.is_unspecified:
        return False
    minimum = _MIN_IPV4_PREFIX if net.version == 4 else _MIN_IPV6_PREFIX
    return net.prefixlen >= minimum


def otp_test_display_permitted(phone: str | None, ip: str | None) -> bool:
    """Whether this OTP request may receive its code as an internal tester.

    Server-side only: the switch, the phone list and the network list all come
    from server configuration; the request contributes only the validated
    phone and the connection's client IP (never a header the client chooses
    when the proxy is set up as documented). In development the ordinary
    RESTAMP_OTP_DISPLAY_MODE applies instead; this is for any environment.
    """
    if not settings.OTP_TEST_DISPLAY or not phone or not ip:
        return False
    if phone not in otp_test_phones():
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in otp_test_networks() if _network_is_acceptable(net))


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


def _validate_otp_test_display() -> None:
    """Production startup checks for RESTAMP_OTP_TEST_DISPLAY=1 (settings
    names only, never values)."""
    raw_phones = [p.strip() for p in (settings.OTP_TEST_PHONES or "").split(",") if p.strip()]
    if not raw_phones or any(not _TEST_PHONE.match(p) for p in raw_phones):
        raise RuntimeError(
            "Insecure production configuration: RESTAMP_OTP_TEST_DISPLAY=1 requires "
            "RESTAMP_OTP_TEST_PHONES to list only valid +91 staff test numbers."
        )
    if len(set(raw_phones)) > _MAX_TEST_PHONES:
        raise RuntimeError(
            "Insecure production configuration: RESTAMP_OTP_TEST_PHONES may list at "
            f"most {_MAX_TEST_PHONES} numbers."
        )
    raw_nets = [n.strip() for n in (settings.OTP_TEST_ALLOWED_CIDRS or "").split(",") if n.strip()]
    nets = otp_test_networks()
    if not raw_nets or len(nets) != len(raw_nets) or not all(_network_is_acceptable(n) for n in nets):
        raise RuntimeError(
            "Insecure production configuration: RESTAMP_OTP_TEST_DISPLAY=1 requires "
            "RESTAMP_OTP_TEST_ALLOWED_CIDRS to list only valid internal network ranges "
            f"(IPv4 /{_MIN_IPV4_PREFIX} or narrower, IPv6 /{_MIN_IPV6_PREFIX} or narrower, "
            "never loopback or 0.0.0.0/0)."
        )
    _log.warning(
        "RESTAMP_OTP_TEST_DISPLAY is enabled: OTP codes are returned for %d staff "
        "test number(s) from %d allowed network(s) only.",
        len(set(raw_phones)), len(nets),
    )


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
    - RESTAMP_OTP_DISPLAY_MODE=1 (OTP codes are never returned in
      production; there is no override);
    - RESTAMP_GOOGLE_DEV_MODE=1, or RESTAMP_GOOGLE_ENABLED=1 without a real
      RESTAMP_GOOGLE_CLIENT_ID;
    - RESTAMP_CORS_ORIGINS empty, containing "*", or containing anything
      other than exact public https origins (no localhost/LAN, no paths).
    SMS delivery is not validated here: with no provider connected, OTP
    requests fail closed (503) at request time.
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
    if settings.OTP_DISPLAY_MODE:
        raise RuntimeError(
            "Insecure production configuration: RESTAMP_OTP_DISPLAY_MODE must "
            "be '0' in production (OTP codes must never be returned in API "
            "responses)."
        )
    if settings.OTP_TEST_DISPLAY:
        _validate_otp_test_display()
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
