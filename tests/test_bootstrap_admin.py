"""Safety tests for scripts/bootstrap_admin.py (one-time local admin bootstrap).

Database-guard tests are pure URL checks. Everything that touches the DB runs
inside an outer transaction that is always rolled back (same pattern as the
conftest `client` fixture), so restamp_test is never changed. The script is
driven through main() with injected input / TTY / session so no real prompt,
terminal or restamp_dev connection is involved.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest
from sqlalchemy import delete, func, select

from app import config as config_module
from app import db as db_module
from app.db import engine
from app.models import AdminAccount, AuditLog, User
from app.services import users_roles

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "bootstrap_admin.py"
_spec = importlib.util.spec_from_file_location("bootstrap_admin_script", _SCRIPT)
bootstrap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bootstrap)

LOCAL_DEV = "mysql+pymysql://dev_user:s3cret-pass@localhost/restamp_dev?unix_socket=/tmp/mysql.sock"


# --- database guard (no DB access) ------------------------------------------------

@pytest.mark.parametrize("url", [
    LOCAL_DEV,
    "mysql+pymysql://dev_user:s3cret-pass@localhost/restamp_dev",
    "mysql+pymysql://dev_user:s3cret-pass@127.0.0.1:3306/restamp_dev",
    "mysql+pymysql://dev_user:s3cret-pass@[::1]/restamp_dev",
    "mysql+pymysql://dev_user:s3cret-pass@ignored/restamp_dev?unix_socket=/tmp/mysql.sock",
])
def test_accepts_only_local_restamp_dev(url):
    bootstrap.check_database_url(url)


@pytest.mark.parametrize("url", [
    None,
    "",
    "   ",
    "not a url",
    "mysql+pymysql://u:s3cret-pass@restamp-db.aivencloud.com:12345/restamp_dev?ssl-mode=REQUIRED",
    "mysql+pymysql://u:s3cret-pass@localhost.evil.example/restamp_dev",
    "mysql+pymysql://u:s3cret-pass@10.0.0.5/restamp_dev",
    "mysql+pymysql://u:s3cret-pass@localhost/restamp_test",
    "mysql+pymysql://u:s3cret-pass@localhost/restamp_prod",
    "mysql+pymysql://u:s3cret-pass@localhost/restamp_dev_backup",
    "mysql+pymysql://u:s3cret-pass@localhost/",
    "postgresql://u:s3cret-pass@localhost/restamp_dev",
    "sqlite:///restamp_dev",
])
def test_refuses_any_other_database(url):
    with pytest.raises(bootstrap.Refused) as e:
        bootstrap.check_database_url(url)
    msg = str(e.value)
    assert "s3cret-pass" not in msg and "aivencloud" not in msg and "://" not in msg


# --- argument handling --------------------------------------------------------------

def _never_called(*_a, **_k):
    raise AssertionError("must not be reached")


def test_user_id_is_required():
    with pytest.raises(SystemExit) as e:
        bootstrap.main([], database_url=LOCAL_DEV, session_factory=_never_called)
    assert e.value.code == 2


@pytest.mark.parametrize("value", ["0", "-3"])
def test_user_id_must_be_positive(value):
    out = []
    assert bootstrap.main(["--user-id", value], database_url=LOCAL_DEV, session_factory=_never_called, out=out.append) == 2
    assert out and out[0].startswith("REFUSED")


def test_refuses_remote_database_before_opening_a_session():
    out = []
    code = bootstrap.main(
        ["--user-id", "1"],
        database_url="mysql+pymysql://u:s3cret-pass@restamp-db.aivencloud.com/restamp_dev",
        session_factory=_never_called, out=out.append,
    )
    assert code == 2
    assert all("s3cret-pass" not in line and "aivencloud" not in line for line in out)


def test_refuses_production_classified_process(monkeypatch):
    monkeypatch.setattr(config_module, "is_production", lambda: True)
    out = []
    assert bootstrap.main(["--user-id", "1"], session_factory=_never_called, out=out.append) == 2
    assert "production" in out[0]


def test_default_url_comes_from_settings_and_is_checked(monkeypatch):
    # With no injected URL the script checks settings.DATABASE_URL — here the
    # test DB (restamp_test), which must be refused.
    monkeypatch.setattr(config_module, "is_production", lambda: False)
    out = []
    assert bootstrap.main(["--user-id", "1"], session_factory=_never_called, out=out.append) == 2
    assert "restamp_dev" in out[0]


# --- DB-backed behaviour (always rolled back) ---------------------------------------

@pytest.fixture()
def db_conn():
    conn = engine.connect()
    trans = conn.begin()
    try:
        yield conn
    finally:
        trans.rollback()
        conn.close()


def _session(conn):
    return db_module.sessionmaker(bind=conn, join_transaction_mode="create_savepoint")()


@pytest.fixture()
def no_admins(db_conn):
    """restamp_test already holds admins from other tests; clear them inside the
    rolled-back transaction so the one-time path can be exercised."""
    db_conn.execute(delete(AdminAccount))
    return db_conn


def _new_user(conn, status="active") -> int:
    s = _session(conn)
    user = users_roles.create_user(s, display_name="Bootstrap Target")
    user.status = status
    s.commit()  # releases a savepoint only; the outer transaction is rolled back
    uid = user.id
    s.close()
    return uid


def _run(conn, user_id, *, answer=None, tty=True):
    out, prompts = [], []

    def _input(prompt):
        prompts.append(prompt)
        return bootstrap.confirmation_phrase(user_id) if answer is None else answer

    code = bootstrap.main(
        ["--user-id", str(user_id)],
        database_url=LOCAL_DEV,
        session_factory=lambda: _session(conn),
        input_fn=_input,
        stdin_is_tty=lambda: tty,
        out=out.append,
    )
    return code, out, prompts


def _admin_role(conn, user_id):
    return conn.execute(select(AdminAccount.admin_role).where(AdminAccount.user_id == user_id)).scalar()


def _bootstrap_audits(conn, user_id):
    return conn.execute(
        select(func.count()).select_from(AuditLog).where(
            AuditLog.action == "ADMIN_BOOTSTRAP", AuditLog.user_id == user_id
        )
    ).scalar()


def test_grants_super_admin_and_audits_in_one_transaction(no_admins):
    uid = _new_user(no_admins)
    code, out, prompts = _run(no_admins, uid)
    assert code == 0, out
    assert _admin_role(no_admins, uid) == "SUPER_ADMIN"
    assert _bootstrap_audits(no_admins, uid) == 1
    assert len(prompts) == 1 and f"GRANT SUPER_ADMIN TO {uid}" in prompts[0]


def test_refuses_when_any_admin_already_exists(no_admins):
    existing = _new_user(no_admins)
    s = _session(no_admins)
    users_roles.grant_admin(s, existing, "LISTING_REVIEWER")
    s.commit()
    s.close()
    target = _new_user(no_admins)
    code, out, prompts = _run(no_admins, target)
    assert code == 2 and "already exists" in out[-1]
    assert prompts == []  # refused before asking
    assert _admin_role(no_admins, target) is None
    assert _bootstrap_audits(no_admins, target) == 0


def test_refuses_with_existing_admins_in_the_test_db(db_conn):
    # Without clearing: restamp_test's existing admins block the bootstrap.
    if not db_conn.execute(select(func.count()).select_from(AdminAccount)).scalar():
        pytest.skip("no admin rows present to exercise this path")
    uid = _new_user(db_conn)
    code, _, _ = _run(db_conn, uid)
    assert code == 2 and _admin_role(db_conn, uid) is None


def test_refuses_missing_user(no_admins):
    missing = (no_admins.execute(select(func.max(User.id))).scalar() or 0) + 1000
    code, out, prompts = _run(no_admins, missing)
    assert code == 2 and "does not exist" in out[-1] and prompts == []


@pytest.mark.parametrize("status", ["suspended", "closed"])
def test_refuses_inactive_user(no_admins, status):
    uid = _new_user(no_admins, status=status)
    code, out, prompts = _run(no_admins, uid)
    assert code == 2 and "not active" in out[-1] and prompts == []
    assert _admin_role(no_admins, uid) is None


@pytest.mark.parametrize("answer", ["", "yes", "y", "GRANT SUPER_ADMIN", "GRANT SUPER_ADMIN TO 999999", "grant super_admin to {uid}"])
def test_wrong_confirmation_changes_nothing(no_admins, answer):
    uid = _new_user(no_admins)
    code, out, _ = _run(no_admins, uid, answer=answer.format(uid=uid))
    assert code == 2 and "Confirmation did not match" in out[-1]
    assert _admin_role(no_admins, uid) is None
    assert _bootstrap_audits(no_admins, uid) == 0


def test_requires_an_interactive_terminal(no_admins):
    uid = _new_user(no_admins)
    code, out, prompts = _run(no_admins, uid, tty=False)
    assert code == 2 and "interactive terminal" in out[-1]
    assert prompts == []  # the phrase cannot be piped in
    assert _admin_role(no_admins, uid) is None


def test_audit_failure_rolls_back_the_grant(no_admins, monkeypatch):
    from app.services import admin as admin_svc

    def boom(*_a, **_k):
        raise RuntimeError("simulated audit failure")

    monkeypatch.setattr(admin_svc, "record_audit", boom)
    uid = _new_user(no_admins)
    code, out, _ = _run(no_admins, uid)
    assert code == 1 and out[-1].startswith("ERROR: RuntimeError")
    assert "simulated" not in out[-1]  # error details are not echoed
    assert _admin_role(no_admins, uid) is None  # grant and audit are atomic


def test_bootstrap_function_rechecks_for_existing_admin(no_admins):
    # The lock-protected re-check inside bootstrap_admin() itself.
    first, second = _new_user(no_admins), _new_user(no_admins)
    s = _session(no_admins)
    bootstrap.bootstrap_admin(s, first)
    with pytest.raises(bootstrap.Refused):
        bootstrap.bootstrap_admin(s, second)
    s.rollback()
    s.close()


def test_output_never_contains_pii_or_connection_details(no_admins):
    s = _session(no_admins)
    user = users_roles.create_user(s, display_name="+919876543210")  # phone-shaped default name
    s.commit()
    uid = user.id
    s.close()
    code, out, prompts = _run(no_admins, uid)
    assert code == 0
    text = "\n".join(out + prompts)
    for forbidden in ("+919876543210", "9876543210", "s3cret-pass", "dev_user", "mysql", "unix_socket", "/tmp/"):
        assert forbidden not in text, forbidden
