"""Phase 4 test isolation: every test runs inside a rolled-back transaction on the
LOCAL restamp_dev database, so no test rows ever persist. Requires env:
RESTAMP_DATABASE_URL (unix-socket URL), RESTAMP_JWT_SECRET,
RESTAMP_GOOGLE_DEV_MODE=1. RESTAMP_ENV is forced to "local": the app has no
default environment and production is decided by RESTAMP_ENV alone.
"""
import os

import pytest

os.environ["RESTAMP_ENV"] = "local"
os.environ["RESTAMP_JWT_SECRET"] = "phase4-test-secret"
os.environ["RESTAMP_GOOGLE_DEV_MODE"] = "1"
os.environ["RESTAMP_DISABLE_AUTO_VERIFY"] = "1"
# Phase 3: tests drive the email outbox explicitly (email_svc.process_outbox);
# the background loop must not race them.
os.environ["RESTAMP_DISABLE_EMAIL_WORKER"] = "1"
# Phase 4: the push outbox and the reminder scans are driven explicitly by the
# tests (push_svc.process_outbox / reminders.run_scan); the background loops
# must not race them.
os.environ["RESTAMP_DISABLE_PUSH_WORKER"] = "1"
os.environ["RESTAMP_DISABLE_REMINDER_WORKER"] = "1"
# Reva AI endpoints are rate limited against the SHARED rate_limit_windows
# table: the whole suite shares one client IP, so per-minute windows would
# make unrelated tests flaky. Disabled here; the dedicated rate-limit tests
# patch settings directly (services.rate_limit reads limits at call time).
os.environ["RESTAMP_CHAT_RPM_PER_IP"] = "0"
os.environ["RESTAMP_CHAT_RPM_PER_USER"] = "0"
os.environ["RESTAMP_CHAT_RPH_PER_IP"] = "0"
os.environ["RESTAMP_CHAT_RPH_PER_USER"] = "0"
os.environ["RESTAMP_TRANSCRIBE_PER_HOUR_PER_IP"] = "0"
# M8 communication limits: same reason (one shared client IP). Disabled for the
# general suite (production ignores the switch); test_rate_limit_m8.py re-enables
# them per test. A stable
# non-production HMAC key so production-config tests pass the M8 startup check.
os.environ["RESTAMP_COMM_RATE_LIMITS_DISABLED"] = "1"
os.environ["RESTAMP_DISABLE_RATE_LIMIT_CLEANUP"] = "1"
os.environ.setdefault("RESTAMP_RATE_LIMIT_KEY", "m8-test-rate-limit-key-0123456789abcdefghij")
# Development-only approved photo-URL prefix used by test fixtures (ignored in
# production; see app/services/media_urls.py). Every other host is rejected.
os.environ["RESTAMP_DEV_MEDIA_URL_PREFIXES"] = "https://t.test/"

# The test database URL must come from the environment (see README:
# `export RESTAMP_DATABASE_URL=...`; pytest does not read .env). There is
# deliberately no in-repo fallback: credentials never live in tracked files,
# and a missing URL must stop the run instead of silently targeting the app's
# built-in default. Only local URLs (unix socket or loopback host) are
# accepted, so a production URL exported in the shell can never be tested
# against. Messages never include the URL or credentials. app.config reads
# the environment at import, so it is imported only after the forced test
# settings above.
from app.config import _is_local_database_url

if not os.environ.get("RESTAMP_DATABASE_URL", "").strip():
    pytest.exit(
        "RESTAMP_DATABASE_URL is not set. Export the local test database URL "
        "(unix-socket URL, see README) before running pytest.",
        returncode=4,
    )
if not _is_local_database_url(os.environ["RESTAMP_DATABASE_URL"]):
    pytest.exit(
        "RESTAMP_DATABASE_URL is not a local database URL (unix socket or "
        "loopback host). Refusing to run tests against it.",
        returncode=4,
    )

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app import db as db_module
from app.db import engine
from app.main import create_app


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
        # Helpers that only receive the client allocate pincodes on this
        # test's session (fresh_pincode).
        c.test_session = session
        yield c, session
    session.close()
    trans.rollback()
    conn.close()


def captured_otp_code(phone: str) -> str:
    """The code the backend issues for `phone` under the simplified OTP.

    One fixed testing code (app/services.otp.TEST_OTP) exists for every
    number in local/staging, so the value does not depend on the request;
    keeping this helper preserves the call sites that emulate "the user
    reads their SMS". Tests that assert the display contract instead read
    `debug_code` straight from the /auth/otp/request response.
    """
    from app.services.otp import TEST_OTP

    return TEST_OTP


def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def register_phone(client, phone: str) -> dict:
    """Full OTP request+verify cycle; returns the TokenOut body.

    The local request response carries the fixed testing code as
    `debug_code`; the code used for verification comes from
    captured_otp_code() (the same TEST_OTP), so both paths stay covered.
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


def fresh_pincode(c) -> str:
    """A free Chennai pincode for a listing created through the API.

    A pincode belongs to exactly one locality and owner listings must pair
    it with that locality (owner_listings.PINCODE_LOCALITY_MISMATCH), so a
    helper that invents a new locality name needs a pincode nobody owns yet.
    """
    return _allocate_pincode(c.test_session)


def paired_locality(c_or_session, pincode: str, tag: str = "") -> str:
    """Locality name that owner listings must pair with ``pincode``.

    Tests that need a SPECIFIC pincode (range boundaries, PATCH targets)
    cannot invent a locality name: a pincode belongs to exactly one locality
    (owner_listings.PINCODE_LOCALITY_MISMATCH). Rather than hard-coding names
    that happen to exist in one shared database, this reads the pairing from
    the test's own session and, when the pincode is unowned, creates the
    location chain for it inside the test's rolled-back transaction. Never
    modifies pre-existing rows.
    """
    from sqlalchemy import text as _text

    session = getattr(c_or_session, "test_session", c_or_session)
    row = session.execute(
        _text("SELECT l.name FROM postcodes p JOIN localities l ON l.id = p.locality_id "
              "WHERE p.pincode = :p"),
        {"p": pincode},
    ).fetchone()
    if row is not None:
        return row[0]
    tag = f"{tag}{pincode}" if tag else f"Pair{pincode}"
    make_postcode(session, tag, pincode=pincode)
    return f"L1{tag}"


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


# --- Paid-entitlement fixtures (Phase 2) ---------------------------------------
# Restricted listing operations need a current, paid, non-trial Owner/Dealer
# plan (app/services/capabilities.ensure_can_post) and verification is
# admin-only. Tests that need a poster or a VERIFIED listing as SETUP get them
# through the REAL production paths below — there is no bypass of either rule
# anywhere in the test suite.

_fixture_admin_seq = {"n": 0}


def grant_test_posting_plan(c, headers, role: str = "OWNER") -> int:
    """Give the account behind `headers` a real ACTIVE paid poster plan.

    Uses the same service path as an admin assignment
    (subscriptions._activate: provider ADMIN, claims the poster type, ends
    any current plan of the role, history kept). The plan row is a hidden
    (visible=False), unlimited, 365-day fixture plan inside the test's
    rolled-back transaction. Returns the user id.
    """
    user_id = c.get("/auth/me", headers=headers).json()["id"]
    grant_posting_plan_for_user(c.test_session, user_id, role)
    return user_id


def grant_posting_plan_for_user(session, user_id: int, role: str = "OWNER") -> None:
    """Session-level form of grant_test_posting_plan (same real service path)."""
    from app.models import Plan
    from app.services import subscriptions as sub_svc

    code = f"TEST_FIXTURE_{role}_PLAN"
    plan = session.query(Plan).filter(Plan.code == code).first()
    if plan is None:
        plan = Plan(code=code, name=f"Test fixture {role} plan", role=role, price_paise=0,
                    duration_days=365, status="ACTIVE", visible=False, version=1)
        session.add(plan)
        session.flush()
    sub_svc._activate(session, user_id, plan, sub_svc.ADMIN_PROVIDER, "ADMIN", 0, None,
                      "Replaced by test fixture plan")
    session.flush()


def fixture_admin_headers(c) -> dict:
    """Headers of a SUPER_ADMIN created once per test (real admin_accounts row)."""
    cached = getattr(c, "_fixture_admin_headers", None)
    if cached is not None:
        return cached
    _fixture_admin_seq["n"] += 1
    tok = register_phone(c, f"+916889{os.getpid() % 1000:03d}{_fixture_admin_seq['n']:03d}")
    c.test_session.execute(
        text("INSERT INTO admin_accounts (user_id, admin_role) VALUES (:u, 'SUPER_ADMIN')"),
        {"u": tok["user_id"]},
    )
    c.test_session.flush()
    c._fixture_admin_headers = auth_header(tok["access_token"])
    return c._fixture_admin_headers


class _AdminVerifyResult:
    """The admin verify response, with the nested `listing` fields also at the
    top level so lifecycle tests can keep reading verification_status etc."""

    def __init__(self, resp):
        self.status_code, self.text, self._resp = resp.status_code, resp.text, resp

    def json(self):
        body = self._resp.json()
        if isinstance(body, dict) and isinstance(body.get("listing"), dict):
            return {**body["listing"], **body}
        return body


def admin_verify(c, listing_id):
    """Verify a listing the only allowed way: POST /admin/listings/{id}/verify
    (audited, notifies the owner)."""
    return _AdminVerifyResult(
        c.post(f"/admin/listings/{listing_id}/verify", headers=fixture_admin_headers(c))
    )


def seed_verified(c, listing_id) -> None:
    """SETUP ONLY: put a PENDING listing into VERIFIED state through the
    verification service (verifications audit row written), like tests that
    seed VERIFIED rows directly. No HTTP route, no notification. Tests about
    who may verify use admin_verify / the routes instead."""
    from app.models import PropertyListing
    from app.services import owner_listings as owner_svc

    session = c.test_session
    owner_id = session.get(PropertyListing, listing_id).user_id
    owner_svc.transition_verification(session, owner_id, listing_id, "VERIFIED")
    session.flush()

