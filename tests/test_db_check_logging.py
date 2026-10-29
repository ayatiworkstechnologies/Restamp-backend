"""check_db() failure diagnostics: visible cause, no credentials, fail-closed.

Render previously showed only "Database unreachable at startup." because
check_db() swallowed every exception. It now logs ONE error line on the
"restamp.db" logger with the exception class, the driver error class/errno
and a sanitized message — never the database URL, user name, password,
URL query values (e.g. ssl_ca / ssl_key_password) or key=value secrets, and
never a traceback (which would repeat the raw driver message).

No database is touched: SessionLocal is replaced by a stub session, and
settings.DATABASE_URL by a fake URL holding recognizable fake secrets.
"""
import asyncio
import logging

import pymysql
import pytest
from sqlalchemy.exc import OperationalError

import app.db as db_module
import app.main as main_module
from app import config as config_module

FAKE_USER = "avnadmin_fake"
FAKE_PASSWORD = "S3cr3t-Pa55w0rd-FAKE"
FAKE_HOST = "restamp-db-fake.aivencloud.com"
FAKE_CA = "/etc/secrets/aiven-ca.pem"
FAKE_URL = (
    f"mysql+pymysql://{FAKE_USER}:{FAKE_PASSWORD}@{FAKE_HOST}:12345/defaultdb"
    f"?ssl-mode=REQUIRED&ssl_ca={FAKE_CA}&ssl_key_password=KEYPASS-FAKE"
)
SECRETS = (FAKE_USER, FAKE_PASSWORD, "KEYPASS-FAKE", FAKE_URL, "mysql+pymysql://", "ssl_key_password=KEY")


class _FailingSession:
    def __init__(self, exc):
        self._exc = exc

    def execute(self, *_args, **_kwargs):
        raise self._exc

    def close(self):
        pass


class _OkSession:
    def execute(self, *_args, **_kwargs):
        return None

    def close(self):
        pass


def _db_failure(errno, message):
    """A real SQLAlchemy OperationalError wrapping a real PyMySQL error."""
    return OperationalError("SELECT 1", {"secret_param": FAKE_PASSWORD}, pymysql.err.OperationalError(errno, message))


@pytest.fixture()
def fake_db(monkeypatch):
    monkeypatch.setattr(config_module.settings, "DATABASE_URL", FAKE_URL)

    def install(session_or_exc):
        session = session_or_exc if isinstance(session_or_exc, _OkSession) else _FailingSession(session_or_exc)
        monkeypatch.setattr(db_module, "SessionLocal", lambda: session)

    return install


def _db_records(caplog):
    return [r for r in caplog.records if r.name == "restamp.db"]


def _assert_no_secrets(text):
    for secret in SECRETS:
        assert secret not in text, f"leaked: {secret!r} in {text!r}"


def test_access_denied_logs_cause_without_user_or_password(fake_db, caplog):
    fake_db(_db_failure(1045, f"Access denied for user '{FAKE_USER}'@'10.20.30.40' (using password: YES)"))
    with caplog.at_level(logging.DEBUG):
        assert db_module.check_db() is False  # still fail-closed
    records = _db_records(caplog)
    assert len(records) == 1 and records[0].levelno == logging.ERROR
    line = records[0].getMessage()
    assert line.startswith("Database check failed: OperationalError [OperationalError 1045]: ")
    assert "Access denied for user '***'@'10.20.30.40' (using password: YES)" in line
    _assert_no_secrets(caplog.text)
    assert records[0].exc_info is None  # no traceback (it would carry the raw message)
    assert "SELECT 1" not in line and "secret_param" not in line  # no statement / bound params


def test_ssl_ca_failure_keeps_host_and_reason(fake_db, caplog):
    fake_db(_db_failure(
        2003,
        f"Can't connect to MySQL server on '{FAKE_HOST}' "
        "([SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate)",
    ))
    with caplog.at_level(logging.ERROR):
        assert db_module.check_db() is False
    line = _db_records(caplog)[0].getMessage()
    assert "OperationalError [OperationalError 2003]" in line
    assert FAKE_HOST in line and "CERTIFICATE_VERIFY_FAILED" in line  # the diagnosis survives
    _assert_no_secrets(caplog.text)


def test_driver_message_echoing_the_dsn_is_redacted(fake_db, caplog):
    fake_db(_db_failure(2005, f"bad DSN {FAKE_URL} password={FAKE_PASSWORD} pwd: other-secret token=tok-123"))
    with caplog.at_level(logging.ERROR):
        assert db_module.check_db() is False
    line = _db_records(caplog)[0].getMessage()
    _assert_no_secrets(caplog.text)
    for leaked in ("other-secret", "tok-123", FAKE_CA, "ssl-mode"):
        assert leaked not in line, leaked
    assert "password=***" in line and "pwd=***" in line and "token=***" in line


def test_non_driver_exception_is_classified_and_sanitized(fake_db, caplog):
    fake_db(RuntimeError(f"pool exploded for {FAKE_URL}\n  password: {FAKE_PASSWORD}"))
    with caplog.at_level(logging.ERROR):
        assert db_module.check_db() is False
    line = _db_records(caplog)[0].getMessage()
    assert line.startswith("Database check failed: RuntimeError: pool exploded for ***")
    assert "\n" not in line  # one log line
    _assert_no_secrets(caplog.text)


def test_short_password_is_still_redacted(monkeypatch, caplog):
    monkeypatch.setattr(config_module.settings, "DATABASE_URL", f"mysql+pymysql://u1:x9@{FAKE_HOST}/db")
    monkeypatch.setattr(db_module, "SessionLocal", lambda: _FailingSession(RuntimeError("auth x9 rejected")))
    with caplog.at_level(logging.ERROR):
        assert db_module.check_db() is False
    assert "x9" not in _db_records(caplog)[0].getMessage()


def test_long_messages_are_capped(fake_db, caplog):
    fake_db(RuntimeError("timeout " * 200))
    with caplog.at_level(logging.ERROR):
        assert db_module.check_db() is False
    assert len(_db_records(caplog)[0].getMessage()) < 400


def test_diagnostics_failure_never_changes_the_outcome(fake_db, monkeypatch, caplog):
    fake_db(RuntimeError(f"boom {FAKE_PASSWORD}"))

    def broken(_exc):
        raise ValueError("formatter bug")

    monkeypatch.setattr(db_module, "_describe_db_error", broken)
    with caplog.at_level(logging.ERROR):
        assert db_module.check_db() is False
    assert _db_records(caplog)[0].getMessage() == "Database check failed: RuntimeError"
    _assert_no_secrets(caplog.text)


def test_success_returns_true_and_logs_nothing(fake_db, caplog):
    fake_db(_OkSession())
    with caplog.at_level(logging.DEBUG):
        assert db_module.check_db() is True
    assert _db_records(caplog) == []


def test_startup_still_fails_closed_with_the_sanitized_cause(fake_db, monkeypatch, caplog):
    """Real check_db through the real lifespan: startup refuses to serve and
    the log shows WHY (sanitized) before the existing generic message."""
    from fastapi import FastAPI

    fake_db(_db_failure(2003, f"Can't connect to MySQL server on '{FAKE_HOST}' (timed out)"))
    # A local-looking URL with the same fake credentials: a remote URL would
    # (correctly) trip the production security gate before the DB check.
    monkeypatch.setattr(
        config_module.settings, "DATABASE_URL",
        f"mysql+pymysql://{FAKE_USER}:{FAKE_PASSWORD}@localhost/restamp_fake?ssl_key_password=KEYPASS-FAKE",
    )
    monkeypatch.setattr(main_module, "check_db", db_module.check_db)

    async def _drain():
        async with main_module.lifespan(FastAPI()):
            pass

    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError, match="Database unreachable at startup"):
            asyncio.run(_drain())
    messages = [r.getMessage() for r in caplog.records]
    assert any("OperationalError [OperationalError 2003]" in m and "timed out" in m for m in messages)
    assert "Database unreachable at startup." in messages
    _assert_no_secrets(caplog.text)
