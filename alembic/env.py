from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))
from app.models import Base
target_metadata = Base.metadata

from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.mysql import TINYINT, INTEGER, SMALLINT, DECIMAL, BIGINT, MEDIUMINT, ENUM

@compiles(TINYINT, "sqlite")
def _compile_tinyint(type_, compiler, **kw):
    return "INTEGER"

@compiles(INTEGER, "sqlite")
def _compile_integer(type_, compiler, **kw):
    return "INTEGER"

@compiles(SMALLINT, "sqlite")
def _compile_smallint(type_, compiler, **kw):
    return "INTEGER"

@compiles(BIGINT, "sqlite")
def _compile_bigint(type_, compiler, **kw):
    return "INTEGER"

@compiles(MEDIUMINT, "sqlite")
def _compile_mediumint(type_, compiler, **kw):
    return "INTEGER"

@compiles(DECIMAL, "sqlite")
def _compile_decimal(type_, compiler, **kw):
    return "NUMERIC"

@compiles(ENUM, "sqlite")
def _compile_enum(type_, compiler, **kw):
    return "VARCHAR(50)"



# Phase 3: prefer environment-based credentials for the dedicated LOCAL
# development database. Falls back to alembic.ini (placeholder) when unset.
# Never hardcode real passwords here; export RESTAMP_DATABASE_URL per shell.
# Aiven URLs may contain ?ssl-mode=REQUIRED which PyMySQL rejects, so the URL
# is normalised via app.db (same helper as the app runtime) and the translated
# SSL connect_args are applied to the migration engine. SSL stays required.
_db_url = os.environ.get("RESTAMP_DATABASE_URL")
if _db_url:
    try:
        from app.db import normalize_database_url as _normalize_db_url

        _clean_url, _connect_args = _normalize_db_url(_db_url)
        # Escape % for configparser interpolation (e.g. %40 in passwords).
        config.set_main_option("sqlalchemy.url", _clean_url.replace("%", "%%"))
        # Stash connect_args for run_migrations_online (configparser is str-only).
        _migration_connect_args = dict(_connect_args)
    except Exception:
        # Fail-closed for remote hosts without a CA would break `alembic`
        # bootstrapping; fall back to the raw URL so the underlying error
        # surfaces from the driver with full context instead of at config time.
        # Local URLs never take this branch.
        config.set_main_option("sqlalchemy.url", _db_url.replace("%", "%%"))
        _migration_connect_args = {}
else:
    _migration_connect_args = {}

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    from sqlalchemy import create_engine

    if _db_url:
        # Env-based URL (already normalised above): use the cleaned URL plus
        # the translated SSL connect_args so ?ssl-mode=REQUIRED never reaches
        # PyMySQL during migrations either.
        url = config.get_main_option("sqlalchemy.url")
        if url:
            url = url.replace("%%", "%")
        connect_args = globals().get("_migration_connect_args", {})
        connectable = create_engine(url, poolclass=pool.NullPool, connect_args=connect_args)
    else:
        connectable = engine_from_config(
            config.get_section(config.config_ini_section, {}),
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
