"""Unit tests for the disposable-database guard and migration-head check.

Pure unit tests: no database connection, no fixtures, no provisioning
needed. The guard helpers only parse strings; the head check runs against
stub connections.
"""
import pytest

from tests.conftest import check_migration_head, check_test_database_allowed

_DEV_URL = "mysql+pymysql://u:s3cret-pw@localhost/restamp_dev?unix_socket=/tmp/mysql.sock"
_TEST_URL = "mysql+pymysql://u:s3cret-pw@localhost/restamp_test?unix_socket=/tmp/mysql.sock"


class _StubConn:
    """Minimal execute/fetchone stub for check_migration_head."""

    def __init__(self, row=None, exc=None):
        self._row = row
        self._exc = exc

    def execute(self, stmt):
        if self._exc is not None:
            raise self._exc
        return self

    def fetchone(self):
        return self._row


def test_guard_off_preserves_existing_behavior():
    # Opt-in: with the flag off, everything passes through unchanged,
    # including garbage (today's local workflow byte-for-byte).
    check_test_database_allowed(_DEV_URL, False)
    check_test_database_allowed("not-a-url", False)
    check_test_database_allowed("", False)
    check_test_database_allowed(None, False)


def test_guard_on_blocks_restamp_dev():
    with pytest.raises(RuntimeError, match="restamp_dev"):
        check_test_database_allowed(_DEV_URL, True)


def test_guard_on_allows_restamp_test():
    check_test_database_allowed(_TEST_URL, True)


def test_guard_on_rejects_malformed_and_missing_names():
    for bad in ["", "   ", None, "not-a-url",
                "mysql+pymysql://u:p@localhost",
                "mysql+pymysql://u:p@localhost/"]:
        with pytest.raises(RuntimeError):
            check_test_database_allowed(bad, True)


def test_guard_on_blocks_configured_production_names(monkeypatch):
    monkeypatch.setenv("RESTAMP_TEST_BLOCKED_DBS", "restamp_prod, Acme-Prod ")
    prod = "mysql+pymysql://u:p@db.example.com/restamp_prod"
    with pytest.raises(RuntimeError, match="restamp_prod"):
        check_test_database_allowed(prod, True)
    with pytest.raises(RuntimeError, match="Acme-Prod"):
        check_test_database_allowed(
            "mysql+pymysql://u:p@db.example.com/Acme-Prod", True)
    # Unlisted names still pass.
    check_test_database_allowed(_TEST_URL, True)


def test_guard_errors_never_disclose_credentials():
    with pytest.raises(RuntimeError) as info:
        check_test_database_allowed(_DEV_URL, True)
    assert "s3cret-pw" not in str(info.value)
    assert "unix_socket" not in str(info.value)


def test_head_check_accepts_matching_revision():
    check_migration_head(_StubConn(row=("e6f7a8b9c0d1",)), ("e6f7a8b9c0d1",))


def test_head_check_rejects_mismatch():
    with pytest.raises(RuntimeError, match="d5e6f7a8b9c0"):
        check_migration_head(_StubConn(row=("d5e6f7a8b9c0",)), ("e6f7a8b9c0d1",))


def test_head_check_rejects_multiple_heads():
    with pytest.raises(RuntimeError, match="exactly one"):
        check_migration_head(_StubConn(row=("e6f7a8b9c0d1",)), ("a", "b"))


def test_head_check_rejects_missing_table_and_empty_row():
    from sqlalchemy.exc import OperationalError

    with pytest.raises(RuntimeError, match="alembic upgrade head"):
        check_migration_head(
            _StubConn(exc=OperationalError("x", None, Exception("no such table"))),
            ("e6f7a8b9c0d1",),
        )
    with pytest.raises(RuntimeError, match="no alembic revision"):
        check_migration_head(_StubConn(row=None), ("e6f7a8b9c0d1",))


def test_head_check_errors_never_disclose_credentials():
    from sqlalchemy.exc import OperationalError

    boom = OperationalError(
        "SELECT", None, Exception("Access denied for user 'u'@'x' (using password: YES)"))
    with pytest.raises(RuntimeError) as info:
        check_migration_head(_StubConn(exc=boom), ("e6f7a8b9c0d1",))
    assert "password" not in str(info.value).lower()
    assert "s3cret" not in str(info.value)
