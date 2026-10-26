"""Phase 4 test isolation: every test runs inside a rolled-back transaction on the
LOCAL restamp_dev database, so no test rows ever persist. Requires env:
RESTAMP_DATABASE_URL (unix-socket URL), RESTAMP_JWT_SECRET,
RESTAMP_GOOGLE_DEV_MODE=1.
"""
import os

os.environ.setdefault(
    "RESTAMP_DATABASE_URL",
    "mysql+pymysql://restamp_dev:Restamp%40123@localhost/restamp_dev?unix_socket=/tmp/mysql.sock",
)
os.environ["RESTAMP_JWT_SECRET"] = "phase4-test-secret"
os.environ["RESTAMP_GOOGLE_DEV_MODE"] = "1"
os.environ["RESTAMP_DISABLE_AUTO_VERIFY"] = "1"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app import db as db_module
from app.db import engine
from app.main import create_app
from app.services.otp import otp_service


# --- Disposable-database safety guard (test-only) ---------------------------
#Enabled only via RESTAMP_TEST_REQUIRE_DISPOSABLE=1. When enabled, tests
# refuse to run against the shared dev database or any configured
# production database, so historical rows can never pollute results.
# Unset (default) preserves the existing local workflow byte-for-byte.
_REQUIRE_DISPOSABLE = os.environ.get("RESTAMP_TEST_REQUIRE_DISPOSABLE", "0") == "1"

# Always blocked when the guard is enabled. Production names are NOT
# hardcoded (none are configured in-repo); supply them via
# RESTAMP_TEST_BLOCKED_DBS as a comma-separated list when known.
_ALWAYS_BLOCKED_DBS = {"restamp_dev"}


def _blocked_db_names() -> set:
    extra = os.environ.get("RESTAMP_TEST_BLOCKED_DBS", "")
    return _ALWAYS_BLOCKED_DBS | {
        n.strip().lower() for n in extra.split(",") if n.strip()
    }


def _parse_db_name(url: str | None) -> str | None:
    """Extract the database name with a proper URL parser (fail closed).

    Returns None when the URL is missing, malformed, or carries no database
    name. Never raises; callers treat None as blocked when the guard is on.
    """
    if not url or not str(url).strip():
        return None
    try:
        name = make_url(str(url).strip()).database
    except Exception:
        return None
    name = (name or "").strip()
    return name or None


def check_test_database_allowed(url: str | None, require_disposable: bool) -> None:
    """Raise RuntimeError unless tests may run against `url`.

    Pass-through when the guard is off. When on: missing/malformed URLs,
    undeterminable names, and blocked names (dev + configured production)
    all fail closed. Messages name only the database, never credentials,
    hosts, or the full URL.
    """
    if not require_disposable:
        return
    name = _parse_db_name(url)
    if name is None:
        raise RuntimeError(
            "RESTAMP_TEST_REQUIRE_DISPOSABLE=1 is set, but RESTAMP_DATABASE_URL "
            "is missing, malformed, or has no database name. Set it to the "
            "dedicated disposable test database."
        )
    if name.lower() in _blocked_db_names():
        raise RuntimeError(
            f"Refusing to run tests against '{name}' with "
            "RESTAMP_TEST_REQUIRE_DISPOSABLE=1. Set RESTAMP_DATABASE_URL to "
            "the dedicated disposable test database."
        )


def _expected_heads() -> tuple:
    """Resolve expected Alembic heads from the project config (files only)."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    here = os.path.dirname(os.path.abspath(__file__))
    cfg = Config(os.path.join(here, "..", "alembic.ini"))
    return tuple(ScriptDirectory.from_config(cfg).get_heads())


def check_migration_head(connection, expected_heads: tuple) -> None:
    """Require the connected DB to sit exactly at the single expected head.

    Read-only (one SELECT on alembic_version); never migrates, creates, or
    writes. Multiple heads, missing table/row, or revision mismatch all
    fail. Messages carry revision ids only — never credentials.
    """
    if len(expected_heads) != 1:
        raise RuntimeError(
            "Expected exactly one Alembic head, found "
            f"{sorted(expected_heads)}; resolve multiple heads before testing."
        )
    try:
        row = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).fetchone()
    except Exception as exc:
        raise RuntimeError(
            "Cannot read alembic_version from the test database "
            f"({type(exc).__name__}); provision it with "
            "`alembic upgrade head`."
        )
    if row is None or not row[0]:
        raise RuntimeError(
            "Test database has no alembic revision recorded; run "
            "`alembic upgrade head` on the test database."
        )
    if row[0] != expected_heads[0]:
        raise RuntimeError(
            f"Test database is at revision '{row[0]}' but the code expects "
            f"'{expected_heads[0]}'; run `alembic upgrade head` on the test "
            "database."
        )


@pytest.fixture(scope="session", autouse=True)
def _guard_test_database():
    """Session gate: fail fast unless the target DB is allowed and migrated.

    Runs once per pytest session before database-dependent fixtures.
    Read-only; connection failures surface as credential-free errors.
    """
    url = os.environ.get("RESTAMP_DATABASE_URL", "")
    check_test_database_allowed(
        url, os.environ.get("RESTAMP_TEST_REQUIRE_DISPOSABLE", "0") == "1")
    expected = _expected_heads()
    try:
        with engine.connect() as conn:
            check_migration_head(conn, expected)
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(
            "Cannot connect to the test database "
            f"({type(exc).__name__}); check RESTAMP_DATABASE_URL points at a "
            "provisioned test database."
        )
    yield


@pytest.fixture()
def client():
    otp_service.reset()
    conn = engine.connect()
    trans = conn.begin()
    # Join the test Session into the outer transaction via SAVEPOINTs
    # (documented SQLAlchemy 2.x "joining a Session into an external
    # transaction" pattern, sqlalchemy.orm.Session join_transaction_mode).
    # With "create_savepoint", every Session transaction — including the
    # ones implicitly begun around route-level db.commit() calls — runs as
    # a savepoint inside the outer transaction: commits release only the
    # savepoint, so test rows stay visible to all requests within the test
    # (multi-request flows keep working) while the teardown rollback below
    # reliably discards everything, even when application code commits.
    # Production sessions (app/db.get_db, request-scoped SessionLocal) are
    # untouched by this test-only configuration.
    TestSession = db_module.sessionmaker(
        bind=conn, join_transaction_mode="create_savepoint")
    session: Session = TestSession()

    def _override():
        try:
            yield session
        finally:
            pass

    app = create_app()
    app.dependency_overrides[db_module.get_db] = _override
    with TestClient(app) as c:
        # Production code ships with NO OTP delivery connected (requests fail
        # closed). Install the TEST-ONLY recording stand-in for the phone's
        # SMS inbox as the delivery seam, so tests can read the delivered
        # code the way a real phone would — at the delivery boundary, never
        # from an API response.
        otp_service.delivery = TestSMSInbox()
        yield c, session
    session.close()
    trans.rollback()
    conn.close()
    otp_service.delivery = None
    otp_service.reset()


class TestSMSInbox:
    """TEST-ONLY delivery stand-in for the phone's SMS inbox (never app code).

    Production has no delivery connected; tests install this callable as
    `otp_service.delivery` to emulate a delivered SMS. Codes leave the server
    exclusively through the delivery boundary — API responses never contain
    them. Tests read the delivered code via captured_otp_code(), emulating
    the user reading their SMS.
    """

    def __init__(self) -> None:
        self.codes: dict[str, str] = {}

    def __call__(self, phone: str, code: str) -> None:
        self.codes[phone] = code


def captured_otp_code(phone: str) -> str:
    """The OTP most recently delivered to `phone` by the test SMS inbox."""
    delivery = otp_service.delivery
    code = getattr(delivery, "codes", {}).get(phone)
    assert code, (
        f"no captured OTP code for {phone}: the client fixture must have "
        "installed the TestSMSInbox delivery before the OTP request"
    )
    return code


def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def register_phone(client, phone: str) -> dict:
    """Full OTP request+verify cycle; returns the TokenOut body.

    The request response discloses no code ({message} only); the code comes
    from the test SMS inbox installed by the client fixture.
    """
    r = client.post("/auth/otp/request", json={"phone": phone})
    assert r.status_code == 200, r.text
    code = captured_otp_code(phone)
    r = client.post("/auth/otp/verify", json={"phone": phone, "code": code})
    assert r.status_code == 200, r.text
    return r.json()


_postcode_seq = {"n": 0}


def _allocate_pincode(session) -> str:
    """Deterministically allocate a free in-range Chennai test pincode.

    Cycles 600001–600118 (the application's accepted rule) and returns the
    first value with no postcode row, so repeated calls — within one test,
    across tests, and across suite reruns against the same dev database —
    never collide with each other or with pre-existing rows. Probing (not
    randomness) keeps runs reproducible; the whole range is covered before
    giving up. The probe runs on the caller's session, so same-test flushed
    rows and previously committed rows are both skipped.
    """
    from sqlalchemy import text as _text

    for _ in range(118):
        _postcode_seq["n"] = _postcode_seq["n"] % 118 + 1
        cand = f"{600000 + _postcode_seq['n']}"
        exists = session.execute(
            _text("SELECT id FROM postcodes WHERE pincode=:p"), {"p": cand}
        ).fetchone()
        if not exists:
            return cand
    raise RuntimeError("no free Chennai test pincode in 600001–600118")


def make_postcode(session, tag: str = "", pincode: str | None = None) -> int:
    """Insert the location chain down to a postcode; returns the postcode id.

    The pincode is allocated by _allocate_pincode (unique, in-range) unless
    the caller passes an explicit `pincode` for tests that need a specific
    value — the caller then owns collision responsibility. `tag` still
    namespaces the city/district/taluk/village/locality names.
    """
    from sqlalchemy import text as _text

    pin = pincode if pincode is not None else _allocate_pincode(session)
    session.execute(_text("INSERT INTO cities (name) VALUES (:n)"), {"n": f"Testville{tag}"})
    city = session.execute(_text("SELECT LAST_INSERT_ID()")).fetchone()[0]
    session.execute(_text("INSERT INTO districts (city_id, name) VALUES (:c, :n)"), {"c": city, "n": f"D1{tag}"})
    dist = session.execute(_text("SELECT LAST_INSERT_ID()")).fetchone()[0]
    session.execute(_text("INSERT INTO taluks (district_id, name) VALUES (:d, :n)"), {"d": dist, "n": f"T1{tag}"})
    taluk = session.execute(_text("SELECT LAST_INSERT_ID()")).fetchone()[0]
    session.execute(_text("INSERT INTO villages (taluk_id, name) VALUES (:t, :n)"), {"t": taluk, "n": f"V1{tag}"})
    vil = session.execute(_text("SELECT LAST_INSERT_ID()")).fetchone()[0]
    session.execute(_text("INSERT INTO localities (village_id, name) VALUES (:v, :n)"), {"v": vil, "n": f"L1{tag}"})
    loc = session.execute(_text("SELECT LAST_INSERT_ID()")).fetchone()[0]
    session.execute(
        _text("INSERT INTO postcodes (locality_id, pincode) VALUES (:l, :p)"), {"l": loc, "p": pin}
    )
    session.flush()
    return session.execute(_text("SELECT LAST_INSERT_ID()")).fetchone()[0]
