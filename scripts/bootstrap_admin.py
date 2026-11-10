#!/usr/bin/env python3
"""RESTAMP one-time admin bootstrap — LOCAL restamp_dev ONLY (never production).

Every supported way to grant admin (POST /admin/staff, POST /admin/grants)
requires an existing admin, so the very first admin cannot be created through
the API. This script creates exactly that first admin, and nothing else:

- Local `restamp_dev` only: refuses any URL whose host is not
  localhost/127.0.0.1/::1 (or a unix socket), any database other than
  `restamp_dev`, and any process that app.config classifies as production.
  The URL is checked BEFORE app.db is imported, because importing it builds
  the engine; the session therefore always targets the database checked here.
- One-time: refuses if ANY admin account already exists (the check holds a
  FOR UPDATE lock on admin_accounts, so two concurrent runs cannot both pass).
  Every later admin must be granted through POST /admin/staff (audited).
- Grants SUPER_ADMIN through the app's own users_roles.grant_admin() and
  writes an ADMIN_BOOTSTRAP audit entry (admin.record_audit) in the SAME
  transaction: both are committed together or neither is.
- The target must be an existing, active user, given explicitly with
  --user-id. The run stops for an interactive confirmation (a TTY is
  required, so the phrase cannot be piped in).
- Prints only the user id and account status: never a phone number,
  display name, token, credential or database URL.

Usage (from the backend root, venv active, RESTAMP_DATABASE_URL pointing at
local restamp_dev in the environment — never on the command line):
    python3 scripts/bootstrap_admin.py --user-id <id>
Exit codes: 0 granted, 2 refused (nothing changed), 1 unexpected error
(transaction rolled back).
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Callable

# Allow `python3 scripts/bootstrap_admin.py` from the backend root.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

ALLOWED_DATABASE = "restamp_dev"
BOOTSTRAP_ROLE = "SUPER_ADMIN"
AUDIT_ACTION = "ADMIN_BOOTSTRAP"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class Refused(Exception):
    """A safety check failed. The message is safe to print (no secrets/PII)."""


def confirmation_phrase(user_id: int) -> str:
    return f"GRANT {BOOTSTRAP_ROLE} TO {user_id}"


def check_database_url(url: str | None) -> None:
    """Refuse unless `url` is a local MySQL URL for the `restamp_dev` database.

    Messages name the problem only, never the URL or any part of a credential.
    """
    from sqlalchemy.engine import make_url

    if not url or not str(url).strip():
        raise Refused("RESTAMP_DATABASE_URL is not set.")
    try:
        parsed = make_url(str(url).strip())
    except Exception:
        raise Refused("RESTAMP_DATABASE_URL could not be parsed.") from None
    if not parsed.drivername.startswith("mysql"):
        raise Refused("Only the local MySQL restamp_dev database is supported.")
    has_socket = bool(parsed.query.get("unix_socket"))
    host = (parsed.host or "").strip().lower()
    if not has_socket and host not in _LOCAL_HOSTS:
        raise Refused("The database host is not local (localhost / 127.0.0.1 / unix socket required).")
    if (parsed.database or "").strip() != ALLOWED_DATABASE:
        raise Refused(f"Only the '{ALLOWED_DATABASE}' database is allowed.")


def bootstrap_admin(db, user_id: int) -> None:
    """Grant SUPER_ADMIN to `user_id` and write the audit entry (flush only).

    The caller owns the transaction: commit on success, roll back on any
    exception. Raises Refused when an admin already exists or the user is
    missing / not active.
    """
    from app.models import AdminAccount, User
    from app.services import users_roles
    from app.services.admin import record_audit

    if db.query(AdminAccount).with_for_update().first() is not None:
        raise Refused("An admin account already exists. Grant further admins through POST /admin/staff.")
    user = db.get(User, user_id)
    if user is None:
        raise Refused(f"User {user_id} does not exist.")
    if user.status != "active":
        raise Refused(f"User {user_id} is not active (status: {user.status}).")
    users_roles.grant_admin(db, user_id, BOOTSTRAP_ROLE)
    record_audit(
        db, user_id, AUDIT_ACTION, "admin_account", None,
        {"user_id": user_id, "admin_role": BOOTSTRAP_ROLE, "via": "scripts/bootstrap_admin.py"},
    )
    db.flush()


def _parse_args(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="One-time local restamp_dev admin bootstrap (grants SUPER_ADMIN).",
    )
    parser.add_argument("--user-id", type=int, required=True, help="the users.id from GET /auth/me")
    args = parser.parse_args(argv)
    if args.user_id < 1:
        raise Refused("--user-id must be a positive integer.")
    return args.user_id


def main(
    argv: list[str] | None = None,
    *,
    database_url: str | None = None,
    session_factory: Callable | None = None,
    input_fn: Callable[[str], str] = input,
    stdin_is_tty: Callable[[], bool] = lambda: sys.stdin.isatty(),
    out: Callable[[str], None] = print,
) -> int:
    """Run the bootstrap. The keyword arguments exist for tests only."""
    argv = sys.argv[1:] if argv is None else argv
    try:
        user_id = _parse_args(argv)
    except Refused as e:
        out(f"REFUSED: {e}")
        return 2

    try:
        if database_url is None:
            from app.config import is_production, settings

            if is_production():
                raise Refused("This process is classified as production.")
            database_url = settings.DATABASE_URL
        check_database_url(database_url)
    except Refused as e:
        out(f"REFUSED: {e} Nothing was changed.")
        return 2

    if session_factory is None:
        # Imported only now: app.db builds its engine from the URL checked above.
        from app.db import SessionLocal

        session_factory = SessionLocal

    db = session_factory()
    try:
        from app.models import AdminAccount, User

        if db.query(AdminAccount).first() is not None:
            raise Refused("An admin account already exists. Grant further admins through POST /admin/staff.")
        user = db.get(User, user_id)
        if user is None:
            raise Refused(f"User {user_id} does not exist.")
        if user.status != "active":
            raise Refused(f"User {user_id} is not active (status: {user.status}).")

        out(f"Target: user {user_id} (status: {user.status}) in local {ALLOWED_DATABASE}.")
        out(f"This grants {BOOTSTRAP_ROLE} (full admin access) and writes an {AUDIT_ACTION} audit entry.")
        if not stdin_is_tty():
            raise Refused("An interactive terminal is required for the confirmation.")
        phrase = confirmation_phrase(user_id)
        answer = input_fn(f"Type '{phrase}' to continue: ")
        if (answer or "").strip() != phrase:
            raise Refused("Confirmation did not match.")

        bootstrap_admin(db, user_id)  # re-checks under a lock before granting
        db.commit()
        out(f"Granted {BOOTSTRAP_ROLE} to user {user_id}. Verify with GET /auth/me (is_admin) and GET /admin/me.")
        return 0
    except Refused as e:
        db.rollback()
        out(f"REFUSED: {e} Nothing was changed.")
        return 2
    except Exception as e:  # never echo details that could include connection info
        db.rollback()
        out(f"ERROR: {type(e).__name__}; the transaction was rolled back and nothing was changed.")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
