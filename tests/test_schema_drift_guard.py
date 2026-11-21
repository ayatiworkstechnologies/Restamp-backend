"""check_db_schema() — the startup schema-drift guard (regression tests).

2026-11 incident: the deployed database had never received the head
migration (``l2m3n4o5p6q7``, ``enquiries.source_role``), so every ORM read or
write of the Enquiry model raised ``OperationalError 1054 (Unknown column
'enquiries.source_role' in 'field list')`` — surfacing as HTTP 500 on
``GET /buyer/enquiries`` and on ``POST /buyer/listings/{id}/contact`` (which
records the lead) while visits and saved listings kept working.

The guard reads one ``alembic_version`` row at startup and fails closed when
the database is not exactly at this codebase's single Alembic head, with the
remedy (``alembic upgrade head``) in the log — instead of serving partial
functionality whose failures only appear on user screens.

No database is touched: SessionLocal is replaced by a stub session, and the
expected revisions come from the repository's migration files (same source
conftest's head check uses). Messages must never carry credentials.
"""
import asyncio
import logging
import os

import pymysql
import pytest
from sqlalchemy.exc import OperationalError

import app.db as db_module
import app.main as main_module

FAKE_SECRETS = ("avnadmin_fake", "S3cr3t-Pa55w0rd-FAKE", "mysql+pymysql://")


def _script():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return ScriptDirectory.from_config(Config(os.path.join(root, "alembic.ini")))


_HEADS = _script().get_heads()
assert len(_HEADS) == 1, f"expected one Alembic head, got {_HEADS}"
_HEAD = _HEADS[0]
_PARENT = _script().get_revision(_HEAD).down_revision
assert isinstance(_PARENT, str), f"unexpected merge-point head {_HEAD}"


class _StubResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _StubSession:
    """execute() -> StubResult, or raises."""

    def __init__(self, rows=None, exc=None):
        self._rows = rows if rows is not None else []
        self._exc = exc

    def execute(self, *_args, **_kwargs):
        if self._exc is not None:
            raise self._exc
        return _StubResult(self._rows)

    def close(self):
        pass


def _records(caplog):
    return [r for r in caplog.records if r.name == "restamp.db"]


def _assert_no_secrets(text):
    for secret in FAKE_SECRETS:
        assert secret not in text, f"leaked: {secret!r}"


@pytest.fixture()
def install_session(monkeypatch):
    def install(session):
        monkeypatch.setattr(db_module, "SessionLocal", lambda: session)

    return install


@pytest.fixture()
def fake_lifespan_db(monkeypatch):
    """check_db() succeeds (stubbed out); check_db_schema() runs for real
    against a stub session one revision behind the true head."""
    monkeypatch.setattr(db_module, "SessionLocal", lambda: _StubSession([(_PARENT,)]))
    monkeypatch.setattr(main_module, "check_db", lambda: True)
    from app import config as config_module

    monkeypatch.setattr(
        config_module.settings, "DATABASE_URL",
        "mysql+pymysql://u1:p1@localhost/restamp_fake",
    )


def test_database_at_head_passes(install_session):
    install_session(_StubSession([(_HEAD,)]))
    assert db_module.check_db_schema() is True


def test_database_behind_head_fails_with_remedy(install_session, caplog):
    """The incident state: one migration behind -> fail closed, name both
    revisions and the remedy, leak nothing."""
    install_session(_StubSession([(_PARENT,)]))
    with caplog.at_level(logging.ERROR):
        assert db_module.check_db_schema() is False
    line = _records(caplog)[-1].getMessage()
    assert _PARENT in line and _HEAD in line
    assert "alembic upgrade head" in line
    assert "HTTP 500" in line  # the symptom the drift caused
    _assert_no_secrets(caplog.text)


def test_unknown_revision_fails_closed(install_session, caplog):
    """Database ahead of (or diverged from) this codebase: refuse and say
    which side is older. `alembic upgrade head` cannot fix this, so it is
    not offered as the remedy."""
    install_session(_StubSession([("zzzzzzzzzzzz",)]))
    with caplog.at_level(logging.ERROR):
        assert db_module.check_db_schema() is False
    line = _records(caplog)[-1].getMessage()
    assert "zzzzzzzzzzzz" in line
    assert "older than the database" in line
    assert "alembic upgrade head" not in line
    _assert_no_secrets(caplog.text)


def test_missing_alembic_version_table_fails(install_session, caplog):
    install_session(_StubSession(exc=OperationalError(
        "SELECT version_num FROM alembic_version", {},
        pymysql.err.OperationalError(1146, "Table 'restamp.alembic_version' doesn't exist"))))
    with caplog.at_level(logging.ERROR):
        assert db_module.check_db_schema() is False
    line = _records(caplog)[-1].getMessage()
    assert "OperationalError [OperationalError 1146]" in line
    _assert_no_secrets(caplog.text)


def test_connection_failure_is_sanitized(install_session, caplog):
    install_session(_StubSession(exc=OperationalError(
        "SELECT version_num FROM alembic_version",
        {"password": "S3cr3t-Pa55w0rd-FAKE"},
        pymysql.err.OperationalError(1045, "Access denied for user 'avnadmin_fake'@'10.0.0.1' (using password: YES)"),
    )))
    with caplog.at_level(logging.ERROR):
        assert db_module.check_db_schema() is False
    line = _records(caplog)[-1].getMessage()
    assert "1045" in line
    _assert_no_secrets(caplog.text)


def test_broken_project_layout_fails_closed(install_session, monkeypatch, caplog):
    install_session(_StubSession([(_HEAD,)]))
    monkeypatch.setattr(db_module, "_project_schema_heads", lambda: ())
    with caplog.at_level(logging.ERROR):
        assert db_module.check_db_schema() is False
    assert "Alembic head" in _records(caplog)[-1].getMessage()


def test_multiple_head_files_fail_closed(install_session, monkeypatch, caplog):
    install_session(_StubSession([(_HEAD,)]))
    monkeypatch.setattr(db_module, "_project_schema_heads", lambda: (_HEAD, "zzzzzzzzzzzz"))
    with caplog.at_level(logging.ERROR):
        assert db_module.check_db_schema() is False


def test_multiple_version_rows_fail_closed(install_session, caplog):
    install_session(_StubSession([(_HEAD,), (_HEAD,)]))
    with caplog.at_level(logging.ERROR):
        assert db_module.check_db_schema() is False
    assert "alembic_version row" in _records(caplog)[-1].getMessage()


def test_success_logs_nothing(install_session, caplog):
    install_session(_StubSession([(_HEAD,)]))
    with caplog.at_level(logging.DEBUG):
        assert db_module.check_db_schema() is True
    assert _records(caplog) == []


def test_startup_refuses_to_serve_when_schema_is_behind(fake_lifespan_db, caplog):
    """The real lifespan: database reachable but one migration behind -> the
    deploy stays down with a clear reason instead of serving 500s on the
    screens whose models need the newer columns."""
    from fastapi import FastAPI

    async def _drain():
        async with main_module.lifespan(FastAPI()):
            pass

    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError, match="Database schema is out of date at startup"):
            asyncio.run(_drain())
    messages = [r.getMessage() for r in caplog.records]
    assert any(_PARENT in m and "alembic upgrade head" in m for m in messages)
    assert "Database schema is not at the expected migration head." in messages
