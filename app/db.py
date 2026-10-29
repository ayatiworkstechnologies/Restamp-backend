"""RESTAMP Phase 4 — SQLAlchemy engine/session integration.

Aiven MySQL requires SSL. Aiven connection strings use ``?ssl-mode=REQUIRED``
(hyphen form; ``ssl_mode`` underscore also occurs), but PyMySQL 1.2.3 does not
accept a ``ssl-mode``/``ssl_mode`` keyword — SQLAlchemy passes URL query params
straight through to the driver, so the raw URL fails on first connect with::

    TypeError: Connection.__init__() got an unexpected keyword argument 'ssl-mode'

This module strips Aiven-style SSL query params from the URL and translates
them into PyMySQL-supported ``connect_args={"ssl": {"ca": ...}}`` with
certificate verification enabled (CERT_REQUIRED + hostname check via
``ssl.create_default_context(cafile=...)``). SSL is never disabled for remote
hosts and verification is never turned off.

Configuration (never hardcoded):
- ``RESTAMP_DATABASE_URL`` — full SQLAlchemy URL (Render dashboard, manually
  set; see render.yaml ``sync: false``). May contain ``?ssl-mode=REQUIRED``.
- ``RESTAMP_DB_SSL_CA`` — filesystem path to the Aiven ``ca.pem`` (Render
  Secret File mount path). Required for non-local TCP hosts.
"""
import logging
import os
import re

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from .config import settings

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

log = logging.getLogger("restamp.db")

# Query keys (normalised: lower-case, "-" -> "_") that are consumed here and
# must never reach the driver. Everything else (e.g. charset, unix_socket)
# stays in the URL so SQLAlchemy can pass it through normally.
_SSL_MODE_KEYS = {"ssl_mode", "sslmode"}
_SSL_FLAG_KEYS = {"ssl"}
_SSL_CA_KEYS = {"ssl_ca"}
_SSL_EXTRA_KEYS = {
    "ssl_cert",
    "ssl_key",
    "ssl_key_password",
    "ssl_password",
    "ssl_capath",
    "ssl_cipher",
    "ssl_verify_cert",
    "ssl_verify_identity",
}

_FALSE_VALUES = {"0", "false", "no", "off", "disabled"}


def _norm_key(key: str) -> str:
    return key.lower().replace("-", "_")


def _is_local_url(url, has_unix_socket: bool) -> bool:
    if has_unix_socket:
        return True
    host = (url.host or "").lower()
    return host in _LOCAL_HOSTS


def _resolve_ssl_ca(explicit: str | None) -> str:
    """Resolve the Aiven CA path: explicit arg > env > settings (no hardcode)."""
    if explicit:
        return explicit
    from_env = os.environ.get("RESTAMP_DB_SSL_CA", "")
    if from_env:
        return from_env
    return settings.DB_SSL_CA or ""


def normalize_database_url(
    raw_url: str, ssl_ca: str | None = None
) -> tuple[str, dict]:
    """Split a raw DATABASE_URL into (clean_url, connect_args).

    - Removes ``ssl-mode``/``ssl_mode`` (unsupported by PyMySQL) from the URL.
    - Translates the request into ``{"ssl": {"ca": ..., "check_hostname": True}}``
      for remote hosts so verification stays enabled.
    - Local unix-socket/localhost URLs keep working with no SSL and no CA.
    - Never disables SSL or verification for remote hosts (fail-closed).
    """
    url = make_url(raw_url)
    # Normalise bare "mysql://" to the pinned PyMySQL driver.
    if url.drivername == "mysql":
        url = url.set(drivername="mysql+pymysql")

    original_query = dict(url.query)
    has_unix_socket = any(_norm_key(k) == "unix_socket" for k in original_query)

    ssl_mode: str | None = None
    ssl_flag: str | None = None
    ssl_ca_from_url: str | None = None
    verify_cert_val: str | None = None
    verify_identity_val: str | None = None
    remaining: dict = {}

    for key, value in original_query.items():
        norm = _norm_key(key)
        if norm in _SSL_MODE_KEYS:
            ssl_mode = str(value).upper() if value is not None else None
        elif norm in _SSL_FLAG_KEYS:
            ssl_flag = str(value).lower() if value is not None else "true"
        elif norm in _SSL_CA_KEYS:
            ssl_ca_from_url = str(value)
        elif norm in _SSL_EXTRA_KEYS:
            if norm == "ssl_verify_cert":
                verify_cert_val = str(value).lower() if value is not None else None
            elif norm == "ssl_verify_identity":
                verify_identity_val = str(value).lower() if value is not None else None
            # Other discrete ssl_* options (cert/key/cipher/...) are dropped
            # here: the dict-form {"ssl": {"ca": ...}} below is the single
            # verified path for PyMySQL 1.2.3. Bare ssl_ca alone would map to
            # verify_mode=False (CERT_NONE), i.e. silently unverified.
        elif norm.startswith("ssl"):
            # Any other ssl-* variant (e.g. future "ssl-foo") is unsupported
            # by PyMySQL and must not reach the driver.
            continue
        else:
            remaining[key] = value

    clean_url_obj = url.set(query=remaining)
    # Normalise driver again after set() (defensive; set() preserves it).
    if clean_url_obj.drivername == "mysql":
        clean_url_obj = clean_url_obj.set(drivername="mysql+pymysql")
    # render_as_string(hide_password=False): str() would mask the password as
    # "***", which would break authentication. Never log the clean URL.
    clean_url = clean_url_obj.render_as_string(hide_password=False)

    if _is_local_url(url, has_unix_socket):
        # Local dev over unix socket / localhost: no SSL, no CA needed.
        return clean_url, {}

    # ---- Remote host: SSL is mandatory, verification stays enabled. ----
    host_for_error = url.host or "unknown-host"
    if ssl_mode == "DISABLED":
        raise RuntimeError(
            f"Refusing remote database connection to '{host_for_error}' with "
            "ssl-mode=DISABLED: SSL must remain enabled for Aiven (Render -> Aiven)."
        )
    if ssl_flag is not None and ssl_flag in _FALSE_VALUES:
        raise RuntimeError(
            f"Refusing remote database connection to '{host_for_error}' with "
            "ssl disabled (?ssl=false): SSL must remain enabled for Aiven."
        )
    if verify_cert_val in _FALSE_VALUES or verify_identity_val in _FALSE_VALUES:
        raise RuntimeError(
            f"Refusing remote database connection to '{host_for_error}' with "
            "certificate verification disabled: verification must remain enabled."
        )

    ca_path = _resolve_ssl_ca(ssl_ca_from_url if ssl_ca_from_url else ssl_ca)
    if not ca_path:
        raise RuntimeError(
            f"Remote database host '{host_for_error}' requires SSL with certificate "
            "verification, but no CA certificate is configured. Download ca.pem from "
            "Aiven Console -> your service -> Connection information -> "
            "Download CA certificate, mount it on Render as a Secret File, and set "
            "RESTAMP_DB_SSL_CA to the Secret File path (or add ?ssl_ca=<path> to "
            "RESTAMP_DATABASE_URL). SSL will not be disabled as a workaround."
        )
    if not os.path.isfile(ca_path):
        raise RuntimeError(
            f"Configured Aiven CA certificate not found at '{ca_path}' for host "
            f"'{host_for_error}'. Re-download ca.pem from Aiven Console -> your "
            "service -> Connection information and fix RESTAMP_DB_SSL_CA "
            "(Render Dashboard -> service -> Environment) to point at the "
            "Secret File mount path."
        )

    # Dict form enables CERT_REQUIRED + hostname check by default in PyMySQL
    # 1.2.3 (verified empirically); check_hostname is set explicitly to make
    # the guarantee obvious and testable. Never use ssl_disabled /
    # ssl_verify_cert=False here.
    connect_args = {"ssl": {"ca": ca_path, "check_hostname": True}}
    return clean_url, connect_args


def build_engine_kwargs(
    raw_url: str | None = None, ssl_ca: str | None = None
) -> dict:
    """Return ``{"url": ..., "connect_args": ...}`` for ``create_engine``."""
    resolved_url = raw_url if raw_url is not None else settings.DATABASE_URL
    clean_url, connect_args = normalize_database_url(resolved_url, ssl_ca=ssl_ca)
    return {"url": clean_url, "connect_args": connect_args}


def create_app_engine(raw_url: str | None = None, ssl_ca: str | None = None):
    """Create a pooled engine with correct Aiven SSL handling."""
    kwargs = build_engine_kwargs(raw_url=raw_url, ssl_ca=ssl_ca)
    return create_engine(
        kwargs["url"],
        connect_args=kwargs["connect_args"],
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )


# Application-layer connection pooling (NullPool is Alembic-only).
engine = create_app_engine(settings.DATABASE_URL)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db():
    """FastAPI dependency yielding a request-scoped session."""
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Redaction for connection-failure diagnostics. Driver messages can echo the
# user name (MySQL 1045 "Access denied for user '<user>'@'<host>'") and, in
# odd wrappers, a DSN or key=value secrets; none of that may reach the logs.
_URL_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://\S+")
# MySQL's own "(using password: YES|NO)" is diagnosis, not a secret: kept.
_KV_SECRET_RE = re.compile(
    r"(?i)(?<!using )\b(password|passwd|pwd|secret|token|ssl[_-]?key[_-]?password)\s*[=:]\s*\S+"
)
_MYSQL_USER_RE = re.compile(r"(?i)(for user )'[^']*'")
_MAX_DIAGNOSTIC_LEN = 300


def _sanitize_db_error(message: str) -> str:
    """Strip credentials/URLs from a DB error message; keep the diagnosis.

    Host names, MySQL error text and SSL reasons (e.g.
    CERTIFICATE_VERIFY_FAILED) are kept because they explain *why* the
    connection failed; the configured URL, user name and password, any URL
    and any key=value secret are replaced with "***".
    """
    redacted = str(message)
    password = username = None
    try:
        url = make_url(settings.DATABASE_URL)
        password, username = url.password, url.username
    except Exception:  # unparseable URL: still redact the raw string below
        pass
    # The password is redacted whatever its length (safety over legibility);
    # the URL/user name only when long enough not to garble unrelated text.
    if password:
        redacted = redacted.replace(str(password), "***")
    for literal in (settings.DATABASE_URL, username):
        if literal and len(str(literal)) >= 3:
            redacted = redacted.replace(str(literal), "***")
    redacted = _URL_RE.sub("***", redacted)
    redacted = _KV_SECRET_RE.sub(lambda m: f"{m.group(1)}=***", redacted)
    redacted = _MYSQL_USER_RE.sub(r"\1'***'", redacted)
    redacted = " ".join(redacted.split())  # one log line
    if len(redacted) > _MAX_DIAGNOSTIC_LEN:
        redacted = redacted[:_MAX_DIAGNOSTIC_LEN] + "..."
    return redacted


def _describe_db_error(exc: Exception) -> str:
    """Format a failure as: <Class> [<DriverClass> <errno>]: <sanitized message>.

    For SQLAlchemy DBAPI errors the driver exception (``exc.orig``) is used:
    its args are (errno, message) for PyMySQL and it carries no SQL
    statement or bound parameters.
    """
    kind = type(exc).__name__
    orig = getattr(exc, "orig", None)
    if orig is not None:
        args = getattr(orig, "args", ())
        errno = args[0] if args and isinstance(args[0], int) else None
        message = args[1] if errno is not None and len(args) > 1 else str(orig)
        kind = f"{kind} [{type(orig).__name__}{f' {errno}' if errno is not None else ''}]"
    else:
        message = str(exc)
    return f"{kind}: {_sanitize_db_error(message)}"


def check_db() -> bool:
    """Readiness probe target: True when a trivial query succeeds.

    Fail-closed: any failure returns False. The failure is logged (ERROR,
    logger "restamp.db") as exception class + driver error + sanitized
    message so the real cause (DNS, auth, SSL/CA, timeout) is visible —
    deliberately without exc_info, because a traceback would repeat the raw,
    unsanitized driver message.
    """
    try:
        db: Session = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
        finally:
            db.close()
        return True
    except Exception as exc:
        try:
            log.error("Database check failed: %s", _describe_db_error(exc))
        except Exception:  # diagnostics must never change the outcome
            log.error("Database check failed: %s", type(exc).__name__)
        return False
