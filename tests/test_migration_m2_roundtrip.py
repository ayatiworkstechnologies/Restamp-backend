"""M2: round trip of the Phase 5 (f1b49e75c9a9) and refresh-token
(f4445dd86627) migrations on a DISPOSABLE database, with real data.

head -> seed PENDING/REJECTED (and other) visits + a refresh token
     -> downgrade to 163133965cad -> verify every row kept and converted,
        refresh_tokens table/indexes/FK gone, enum restored
     -> upgrade head -> verify schema identical to before and model-compatible.

Runs only when RESTAMP_MIGRATION_TEST_DATABASE_URL names a LOCAL database
called restamp_disposable_* (never restamp_dev, restamp_test or the suite's
RESTAMP_DATABASE_URL), because downgrading drops tables. Each Alembic step
runs in its OWN subprocess with RESTAMP_DATABASE_URL set explicitly to the
disposable URL (alembic/env.py would otherwise fall back to .env), and the
test's engine uses NullPool and holds no connection during a step, so no
pooled connection can keep a metadata lock (the cause of the earlier
in-suite migration hang). Seeded rows are removed at the end.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from app.config import _is_local_database_url

URL = os.environ.get("RESTAMP_MIGRATION_TEST_DATABASE_URL", "").strip()
REPO = Path(__file__).resolve().parents[1]
HEAD = "f4445dd86627"
BEFORE_PHASE5 = "163133965cad"
OLD_ENUM = "enum('REQUESTED','PROPOSED','CONFIRMED','DECLINED','CANCELLED','COMPLETED')"
NEW_ENUM = "enum('PENDING','CONFIRMED','DECLINED','CANCELLED','COMPLETED','REQUESTED','PROPOSED','REJECTED')"
RT_INDEXES = {"ix_refresh_tokens_jti", "ix_refresh_tokens_token_hash", "ix_refresh_tokens_user_id"}

pytestmark = pytest.mark.skipif(not URL, reason="RESTAMP_MIGRATION_TEST_DATABASE_URL not set (needs a disposable DB)")


def _db_name(url: str) -> str:
    return (make_url(url).database or "").strip()


def _guard() -> None:
    name = _db_name(URL)
    suite = _db_name(os.environ.get("RESTAMP_DATABASE_URL", "") or "sqlite://")
    if not _is_local_database_url(URL):
        pytest.fail("RESTAMP_MIGRATION_TEST_DATABASE_URL must be a local database")
    if not name.startswith("restamp_disposable_") or name in {"restamp_dev", "restamp_test", suite}:
        pytest.fail("refusing: the migration round trip needs a database named restamp_disposable_*")


def _alembic(*args: str) -> str:
    env = {**os.environ, "RESTAMP_DATABASE_URL": URL, "PYTHONDONTWRITEBYTECODE": "1"}
    out = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=REPO, env=env,
                         capture_output=True, text=True, timeout=600)
    assert out.returncode == 0, f"alembic {' '.join(args)} failed:\n{out.stderr[-3000:]}"
    return out.stdout + out.stderr


def _engine():
    return sa.create_engine(URL, poolclass=NullPool)


def _version(e) -> str | None:
    with e.connect() as c:
        if not sa.inspect(c).has_table("alembic_version"):
            return None
        return c.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()


def _schema(e) -> dict[str, str]:
    """table -> SHOW CREATE TABLE, without the volatile AUTO_INCREMENT counter."""
    with e.connect() as c:
        names = [r[0] for r in c.execute(sa.text("SHOW TABLES"))]
        return {n: re.sub(r" AUTO_INCREMENT=\d+", "", c.execute(sa.text(f"SHOW CREATE TABLE `{n}`")).fetchone()[1])
                for n in names if n != "alembic_version"}


def _column_type(c, table, column):
    return c.execute(sa.text(
        "SELECT COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:t AND COLUMN_NAME=:c"), {"t": table, "c": column}).fetchone()


def _visits(c, ids):
    rows = c.execute(sa.text(
        "SELECT id, property_listing_id, buyer_user_id, owner_user_id, scheduled_at, status, initiator, note, "
        "created_at, updated_at, rescheduled_at FROM property_visits WHERE id IN :ids ORDER BY id"
    ).bindparams(sa.bindparam("ids", expanding=True)), {"ids": ids}).fetchall()
    return {r[0]: tuple(r) for r in rows}


def _free_pincode(c) -> str:
    """First unused pincode in the accepted Chennai range 600001-600118.

    postcodes.pincode is UNIQUE, so a fixed value would collide on a rerun
    after an interrupted cleanup left the previous seed behind. Deterministic
    (same rule as tests/conftest._allocate_pincode); fails clearly if the
    disposable database has exhausted the range."""
    taken = {r[0] for r in c.execute(sa.text(
        "SELECT pincode FROM postcodes WHERE pincode BETWEEN '600001' AND '600118'"))}
    for n in range(600001, 600119):
        if str(n) not in taken:
            return str(n)
    pytest.fail("no free Chennai pincode left in the disposable database; recreate it")


def _seed(c) -> dict:
    """Minimal real FK chain + one visit per interesting (status, initiator)."""
    tag = datetime.now(timezone.utc).strftime("%H%M%S%f")
    ins = lambda sql, p: c.execute(sa.text(sql), p).lastrowid  # noqa: E731
    owner = ins("INSERT INTO users (display_name) VALUES (:n)", {"n": f"m2 owner {tag}"})
    buyer = ins("INSERT INTO users (display_name) VALUES (:n)", {"n": f"m2 buyer {tag}"})
    city = ins("INSERT INTO cities (name) VALUES (:n)", {"n": f"M2City{tag}"})
    dist = ins("INSERT INTO districts (city_id, name) VALUES (:c, :n)", {"c": city, "n": f"D{tag}"})
    tal = ins("INSERT INTO taluks (district_id, name) VALUES (:d, :n)", {"d": dist, "n": f"T{tag}"})
    vil = ins("INSERT INTO villages (taluk_id, name) VALUES (:t, :n)", {"t": tal, "n": f"V{tag}"})
    loc = ins("INSERT INTO localities (village_id, name) VALUES (:v, :n)", {"v": vil, "n": f"L{tag}"})
    pc = ins("INSERT INTO postcodes (locality_id, pincode) VALUES (:l, :p)", {"l": loc, "p": _free_pincode(c)})
    pp = ins("INSERT INTO physical_properties (postcode_id, user_id) VALUES (:p, :u)", {"p": pc, "u": owner})
    lid = ins("INSERT INTO property_listings (physical_property_id, user_id, title, price_paise, price_period, "
              "verification_status, listing_status, transaction_type, property_type) VALUES "
              "(:pp, :u, 'M2 listing', 100, 'TOTAL', 'VERIFIED', 'AVAILABLE', 'BUY', 'APARTMENT')",
              {"pp": pp, "u": owner})
    cases = [("PENDING", "BUYER"), ("PENDING", "OWNER"), ("REJECTED", "BUYER"), ("REJECTED", "OWNER"),
             ("CONFIRMED", "BUYER"), ("DECLINED", "BUYER"), ("CANCELLED", "OWNER"), ("COMPLETED", "BUYER"),
             ("REQUESTED", "BUYER"), ("PROPOSED", "OWNER")]
    visits = {}
    for i, (st, ini) in enumerate(cases):
        vid = ins("INSERT INTO property_visits (property_listing_id, buyer_user_id, owner_user_id, scheduled_at, "
                  "status, initiator, note, created_at, updated_at, rescheduled_at) VALUES "
                  "(:l, :b, :o, :s, :st, :i, :n, '2026-11-01 10:00:00', '2026-11-02 11:00:00', :r)",
                  {"l": lid, "b": buyer, "o": owner, "s": f"2030-11-21 15:{30 + i}:00", "st": st, "i": ini,
                   "n": f"note {st}/{ini}", "r": "2026-11-03 09:00:00" if st == "CONFIRMED" else None})
        visits[vid] = (st, ini)
    c.execute(sa.text("INSERT INTO refresh_tokens (user_id, token_hash, jti, created_at) "
                      "VALUES (:u, :h, :j, '2026-11-21 10:00:00')"),
              {"u": buyer, "h": f"hash-{tag}", "j": f"jti-{tag}"})
    return {"visits": visits, "users": [owner, buyer], "listing": lid, "pp": pp, "pc": pc,
            "chain": [("localities", loc), ("villages", vil), ("taluks", tal), ("districts", dist), ("cities", city)]}


def _cleanup(e, s) -> None:
    with e.begin() as c:
        c.execute(sa.text("DELETE FROM property_visits WHERE id IN :ids").bindparams(
            sa.bindparam("ids", expanding=True)), {"ids": list(s["visits"])})
        c.execute(sa.text("DELETE FROM property_listings WHERE id=:i"), {"i": s["listing"]})
        c.execute(sa.text("DELETE FROM physical_properties WHERE id=:i"), {"i": s["pp"]})
        c.execute(sa.text("DELETE FROM postcodes WHERE id=:i"), {"i": s["pc"]})
        for table, rid in s["chain"]:
            c.execute(sa.text(f"DELETE FROM {table} WHERE id=:i"), {"i": rid})
        c.execute(sa.text("DELETE FROM users WHERE id IN :ids").bindparams(
            sa.bindparam("ids", expanding=True)), {"ids": s["users"]})


EXPECTED_AFTER_DOWNGRADE = {
    ("PENDING", "BUYER"): "REQUESTED", ("PENDING", "OWNER"): "PROPOSED",
    ("REJECTED", "BUYER"): "DECLINED", ("REJECTED", "OWNER"): "DECLINED",
}


def test_phase5_and_refresh_token_migrations_round_trip_with_data():
    _guard()
    e = _engine()
    _alembic("upgrade", "head")
    assert _version(e) == HEAD
    schema_before = _schema(e)
    with e.begin() as c:
        seeded = _seed(c)
    ids = list(seeded["visits"])
    test_failed = False
    try:
        with e.connect() as c:
            before = _visits(c, ids)
            assert len(before) == len(ids)

        # NOTE: the downgrade rewrites ALL property_visits rows in the
        # disposable database (every PENDING/REJECTED row, not only the rows
        # seeded here) and drops refresh_tokens with all of its rows. The
        # re-upgrade does not restore those values, and cleanup removes only
        # this test's seed. That is why the database must be disposable.
        _alembic("downgrade", BEFORE_PHASE5)  # through f4445dd86627 and f1b49e75c9a9
        assert _version(e) == BEFORE_PHASE5
        with e.connect() as c:
            insp = sa.inspect(c)
            assert not insp.has_table("refresh_tokens")
            left = c.execute(sa.text("SELECT INDEX_NAME FROM information_schema.STATISTICS WHERE "
                                     "TABLE_SCHEMA=DATABASE() AND INDEX_NAME IN :n").bindparams(
                sa.bindparam("n", expanding=True)), {"n": sorted(RT_INDEXES)}).fetchall()
            assert left == []
            fk = c.execute(sa.text("SELECT COUNT(*) FROM information_schema.REFERENTIAL_CONSTRAINTS WHERE "
                                   "CONSTRAINT_SCHEMA=DATABASE() AND CONSTRAINT_NAME='fk_refresh_tokens_user'")).scalar()
            assert fk == 0
            assert _column_type(c, "property_visits", "status") == (OLD_ENUM, "NO", "REQUESTED")
            after = _visits(c, ids)
        assert set(after) == set(before), "a visit row was lost by the downgrade"
        for vid, row in before.items():
            original = seeded["visits"][vid]
            expected = EXPECTED_AFTER_DOWNGRADE.get(original, original[0])
            assert after[vid][5] == expected, (vid, original, after[vid][5])
            # Every other column is untouched.
            assert after[vid][:5] + after[vid][6:] == row[:5] + row[6:], vid

        _alembic("upgrade", "head")
        assert _version(e) == HEAD
        schema_after = _schema(e)
        assert schema_after == schema_before, "schema differs after the round trip"
        with e.connect() as c:
            assert _column_type(c, "property_visits", "status") == (NEW_ENUM, "NO", "REQUESTED")
            idx = {r[0] for r in c.execute(sa.text(
                "SELECT DISTINCT INDEX_NAME FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() "
                "AND TABLE_NAME='refresh_tokens'"))}
            assert RT_INDEXES <= idx
            again = _visits(c, ids)
            # Converted values stay converted (the upgrade only widens the enum).
            assert {v: r[5] for v, r in again.items()} == {v: r[5] for v, r in after.items()}
            # Model compatibility: every ORM table/column exists.
            from app.models import Base
            from app.models.conversations import Base as ConvBase
            insp = sa.inspect(c)
            missing = [f"{t.name}.{col.name}" for md in (Base.metadata, ConvBase.metadata) for t in md.tables.values()
                       for col in t.columns
                       if not insp.has_table(t.name) or col.name not in {x["name"] for x in insp.get_columns(t.name)}]
            assert missing == []
    except BaseException:
        test_failed = True
        raise
    finally:
        # Restore head and remove the seed even after a failure, but never let
        # a failure HERE replace the test's own failure: each step is
        # attempted independently, and its error is either raised (when the
        # test itself passed) or reported as a warning next to the original.
        teardown_errors = []
        try:
            if _version(e) != HEAD:
                _alembic("upgrade", "head")
        except Exception as exc:  # noqa: BLE001 - reported below
            teardown_errors.append(f"final re-upgrade failed: {exc!r}"[:2000])
        try:
            _cleanup(e, seeded)
        except Exception as exc:  # noqa: BLE001 - reported below
            teardown_errors.append(f"seed cleanup failed: {exc!r}"[:2000])
        e.dispose()
        if teardown_errors:
            if test_failed:
                warnings.warn("teardown after the failed round trip also failed: " + " | ".join(teardown_errors),
                              RuntimeWarning, stacklevel=1)
            else:
                pytest.fail("round trip passed but teardown failed: " + " | ".join(teardown_errors))
