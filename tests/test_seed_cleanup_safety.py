"""scripts/seed_dev_data.py cleanup safety.

`--clean` used to delete postcodes by bare pincode and localities by bare
name across ALL cities ("Anna Nagar", "Adyar", ...), so after real catalog
data was loaded it could fail or delete real rows. Now only rows inside the
exact seed chain with seed names/pincodes are deleted, by id, and any outside
reference refuses the whole clean before a single delete. Seeding also
refuses to attach seed listings to a pincode owned by a real locality.

Each test builds its own uniquely named seed chain; fixture pincodes are free
test values. Rollback-isolated (the clean functions never commit).
"""
import importlib.util
import os
import uuid

import pytest
from sqlalchemy import text

from tests.conftest import make_postcode

_spec = importlib.util.spec_from_file_location(
    "seed_dev_data", os.path.join(os.path.dirname(__file__), "..", "scripts", "seed_dev_data.py"))
seed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed)


def _free_pins(session, n):
    used = {r[0] for r in session.execute(text("SELECT pincode FROM postcodes"))}
    free = [str(p) for p in range(600001, 600119) if str(p) not in used]
    assert len(free) >= n
    return free[:n]


def _ins(conn, sql, params):
    conn.execute(text(sql), params)
    return conn.execute(text("SELECT LAST_INSERT_ID()")).scalar()


def _seed_chain(session, names=("Adyar", "Seed OMR")):
    """A seed-shaped chain under a unique city; returns (conn, city, pins, locality ids)."""
    conn = session.connection()
    city = f"Seed Chennai T{uuid.uuid4().hex[:6]}"
    cid = _ins(conn, "INSERT INTO cities (name) VALUES (:n)", {"n": city})
    did = _ins(conn, "INSERT INTO districts (city_id, name) VALUES (:c, 'Seed Central')", {"c": cid})
    tid = _ins(conn, "INSERT INTO taluks (district_id, name) VALUES (:d, 'Seed Taluk')", {"d": did})
    vid = _ins(conn, "INSERT INTO villages (taluk_id, name) VALUES (:t, 'Seed Village')", {"t": tid})
    pins = _free_pins(session, len(names))
    locs = []
    for name, pin in zip(names, pins):
        lid = _ins(conn, "INSERT INTO localities (village_id, name) VALUES (:v, :n)", {"v": vid, "n": name})
        _ins(conn, "INSERT INTO postcodes (locality_id, pincode) VALUES (:l, :p)", {"l": lid, "p": pin})
        locs.append(lid)
    return conn, city, tuple(pins), locs


def _plan(conn, city, pins, owner=None):
    return seed.seed_location_plan(conn, city=city, pincodes=pins, seed_owner_id=owner)


def _count(conn, sql, **p):
    return conn.execute(text(sql), p).scalar()


def test_clean_deletes_only_the_seed_chain_never_same_named_real_rows(client):
    _, session = client
    real_pc = make_postcode(session, tag=f"R{uuid.uuid4().hex[:5]}")
    session.execute(text("UPDATE localities SET name='Adyar' WHERE id=(SELECT locality_id FROM postcodes WHERE id=:p)"),
                    {"p": real_pc})
    conn, city, pins, locs = _seed_chain(session)
    plan = _plan(conn, city, pins)
    assert plan["blockers"] == [] and sorted(plan["locality_ids"]) == sorted(locs)
    counts = seed.clean_locations(conn, plan)
    assert counts == {"postcodes": 2, "localities": 2, "villages": 1, "taluks": 1, "districts": 1, "cities": 1}
    assert _count(conn, "SELECT COUNT(*) FROM cities WHERE name=:n", n=city) == 0
    # The real "Adyar" with its own pincode is untouched.
    assert _count(conn, "SELECT COUNT(*) FROM postcodes WHERE id=:p", p=real_pc) == 1
    assert _count(conn, "SELECT l.name FROM postcodes p JOIN localities l ON l.id=p.locality_id WHERE p.id=:p",
                  p=real_pc) == "Adyar"


@pytest.mark.parametrize("make_reference, expected", [
    ("real_pincode", "non-seed pincode"),
    ("property", "properties not owned by the seed owner"),
    ("grant", "current agent grants"),
    ("grant_history", "agent grant history"),
    ("alias", "locality aliases"),
    ("foreign_locality", "non-seed locality"),
])
def test_any_outside_reference_refuses_the_whole_clean(client, make_reference, expected):
    _, session = client
    conn, city, pins, locs = _seed_chain(session)
    pid = _count(conn, "SELECT id FROM postcodes WHERE pincode=:p", p=pins[0])
    user = _ins(conn, "INSERT INTO users (display_name) VALUES ('Outside User')", {})
    if make_reference == "real_pincode":
        (extra,) = _free_pins(session, 1)
        _ins(conn, "INSERT INTO postcodes (locality_id, pincode) VALUES (:l, :p)", {"l": locs[0], "p": extra})
    elif make_reference == "property":
        _ins(conn, "INSERT INTO physical_properties (postcode_id, user_id) VALUES (:p, :u)", {"p": pid, "u": user})
    elif make_reference == "grant":
        _ins(conn, "INSERT INTO broker_postcode_access_current (broker_user_id, postcode_id, granted_by) "
                   "VALUES (:u, :p, :u)", {"u": user, "p": pid})
    elif make_reference == "grant_history":
        _ins(conn, "INSERT INTO broker_postcode_access_history (broker_user_id, postcode_id, granted_by, status) "
                   "VALUES (:u, :p, :u, 'EXPIRED')", {"u": user, "p": pid})
    elif make_reference == "alias":
        _ins(conn, "INSERT INTO locality_aliases (locality_id, alias, alias_key, created_at) "
                   "VALUES (:l, 'Seedish', :k, NOW())", {"l": locs[0], "k": f"seedish{uuid.uuid4().hex[:6]}"})
    else:
        vid = _count(conn, "SELECT village_id FROM localities WHERE id=:l", l=locs[0])
        _ins(conn, "INSERT INTO localities (village_id, name) VALUES (:v, 'Real Place')", {"v": vid})
    before = _count(conn, "SELECT COUNT(*) FROM postcodes") + _count(conn, "SELECT COUNT(*) FROM localities")
    plan = _plan(conn, city, pins)
    assert any(expected in b for b in plan["blockers"]), plan["blockers"]
    with pytest.raises(seed.SeedCleanRefused):
        seed.clean_locations(conn, plan)
    after = _count(conn, "SELECT COUNT(*) FROM postcodes") + _count(conn, "SELECT COUNT(*) FROM localities")
    assert after == before  # nothing deleted


def test_seed_owners_own_property_does_not_block(client):
    _, session = client
    conn, city, pins, _ = _seed_chain(session)
    owner = _ins(conn, "INSERT INTO users (display_name) VALUES ('Seed Owner T')", {})
    pid = _count(conn, "SELECT id FROM postcodes WHERE pincode=:p", p=pins[0])
    _ins(conn, "INSERT INTO physical_properties (postcode_id, user_id) VALUES (:p, :u)", {"p": pid, "u": owner})
    assert _plan(conn, city, pins, owner=owner)["blockers"] == []
    assert _plan(conn, city, pins)["blockers"]  # without the marker it is not provably seed-owned


def test_seeding_never_attaches_to_a_real_pincode(client):
    _, session = client
    real_pc = make_postcode(session, tag=f"S{uuid.uuid4().hex[:5]}")
    pin = _count(session.connection(), "SELECT pincode FROM postcodes WHERE id=:p", p=real_pc)
    conn, _, _, locs = _seed_chain(session)
    ins = lambda sql, params: _ins(conn, sql, params)  # noqa: E731
    with pytest.raises(SystemExit, match="non-seed locality"):
        seed._get_or_create_postcode(conn, ins, locs[0], pin)
