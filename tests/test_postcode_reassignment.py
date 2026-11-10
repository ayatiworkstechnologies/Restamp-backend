"""Placeholder postcode reassignment (the Perungudi 600096 fix).

services/localities.reassign_placeholder_postcode moves ONE postcode row from
a seed placeholder locality to its real locality, keeping the row id, only
after every check passes; otherwise ReassignRefused and nothing changes. The
returned rollback statement restores the old association.

Each test builds its own placeholder city/chain with unique names; pincodes
are free test values except the end-to-end case, which uses the reviewed
catalog's real 600096/600017 and is skipped if those already exist.
Rollback-isolated.
"""
import os
import uuid

import pytest
from sqlalchemy import text

from app.services import localities as loc_svc

HERE = os.path.dirname(os.path.abspath(__file__))
REVIEWED = os.path.join(HERE, "..", "scripts", "chennai_picker_localities_reviewed.csv")


# TEMPORARILY DISABLED — Chennai area seeding paused. The implementation is
# kept and tested here with the flag switched on for these tests only.
@pytest.fixture(autouse=True)
def _area_seeding_on_for_these_tests(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "AREA_SEEDING_ENABLED", True)


def _free_pins(session, n):
    used = {r[0] for r in session.execute(text("SELECT pincode FROM postcodes"))}
    free = [str(p) for p in range(600001, 600119) if str(p) not in used]
    assert len(free) >= n
    return free[:n]


def _ins(session, sql, **p):
    session.execute(text(sql), p)
    return session.execute(text("SELECT LAST_INSERT_ID()")).scalar()


def _placeholder(session, name, pins):
    """A seed-shaped placeholder locality holding `pins` under a unique city."""
    city = f"Seed Chennai P{uuid.uuid4().hex[:6]}"
    cid = _ins(session, "INSERT INTO cities (name) VALUES (:n)", n=city)
    did = _ins(session, "INSERT INTO districts (city_id, name) VALUES (:c, 'Seed Central')", c=cid)
    tid = _ins(session, "INSERT INTO taluks (district_id, name) VALUES (:d, 'Seed Taluk')", d=did)
    vid = _ins(session, "INSERT INTO villages (taluk_id, name) VALUES (:t, 'Seed Village')", t=tid)
    lid = _ins(session, "INSERT INTO localities (village_id, name) VALUES (:v, :n)", v=vid, n=name)
    for pin in pins:
        _ins(session, "INSERT INTO postcodes (locality_id, pincode) VALUES (:l, :p)", l=lid, p=pin)
    session.flush()
    return city, lid


def _owner_of(session, pin):
    return session.execute(text(
        "SELECT p.id, l.id, l.name FROM postcodes p JOIN localities l ON l.id=p.locality_id WHERE p.pincode=:p"),
        {"p": pin}).fetchone()


def _setup(session):
    run = uuid.uuid4().hex[:5]
    placeholder, target = f"Tnagar {run}", f"Perungudi {run}"
    wrong, real = _free_pins(session, 2)
    city, lid = _placeholder(session, placeholder, [wrong, real])
    return placeholder, target, wrong, real, city, lid


def _reassign(session, wrong, placeholder, target, city):
    return loc_svc.reassign_placeholder_postcode(
        session, wrong, placeholder_locality=placeholder, target_locality=target, placeholder_city=city)


def test_reassigns_the_same_row_to_the_real_locality_and_can_be_rolled_back(client):
    _, session = client
    placeholder, target, wrong, real, city, lid = _setup(session)
    pid_before, _, _ = _owner_of(session, wrong)
    result = _reassign(session, wrong, placeholder, target, city)
    pid, new_lid, new_name = _owner_of(session, wrong)
    assert pid == pid_before == result["postcode_id"]  # row identity preserved
    assert new_name == target and result["old_locality_id"] == lid and result["target_created"] is True
    assert loc_svc._city_of(session, new_lid) == "Chennai"  # real hierarchy, not the placeholder city
    assert _owner_of(session, real)[1] == lid  # the placeholder keeps its real pincode
    (r,) = loc_svc.resolve(session, [target])
    assert r.status == "ok" and pid in r.postcode_ids
    # Documented rollback restores the old association exactly.
    session.execute(text(result["rollback_sql"]))
    assert _owner_of(session, wrong)[1] == lid


@pytest.mark.parametrize("reference", ["property", "grant", "grant_history", "audit"])
def test_any_reference_refuses_and_changes_nothing(client, reference):
    _, session = client
    placeholder, target, wrong, _, city, lid = _setup(session)
    pid = _owner_of(session, wrong)[0]
    user = _ins(session, "INSERT INTO users (display_name) VALUES ('Ref User')")
    if reference == "property":
        _ins(session, "INSERT INTO physical_properties (postcode_id, user_id) VALUES (:p, :u)", p=pid, u=user)
    elif reference == "grant":
        _ins(session, "INSERT INTO broker_postcode_access_current (broker_user_id, postcode_id, granted_by) "
                      "VALUES (:u, :p, :u)", u=user, p=pid)
    elif reference == "grant_history":
        _ins(session, "INSERT INTO broker_postcode_access_history (broker_user_id, postcode_id, granted_by, status) "
                      "VALUES (:u, :p, :u, 'REVOKED')", u=user, p=pid)
    else:
        _ins(session, "INSERT INTO audit_logs (user_id, action, entity, before_json, after_json) "
                      "VALUES (:u, 'AGENT_LOCALITY_REVOKED', 'broker_coverage', JSON_OBJECT('postcode_ids', "
                      "JSON_ARRAY(:p)), NULL)", u=user, p=pid)
    with pytest.raises(loc_svc.ReassignRefused, match="referenced"):
        _reassign(session, wrong, placeholder, target, city)
    assert _owner_of(session, wrong)[1] == lid
    assert loc_svc.resolve(session, [target])[0].status == "unknown"


def test_refuses_when_the_placeholder_would_be_left_without_a_pincode(client):
    _, session = client
    run = uuid.uuid4().hex[:5]
    (wrong,) = _free_pins(session, 1)
    city, lid = _placeholder(session, f"Tnagar {run}", [wrong])
    with pytest.raises(loc_svc.ReassignRefused, match="load the reviewed catalog first"):
        _reassign(session, wrong, f"Tnagar {run}", f"Perungudi {run}", city)
    assert _owner_of(session, wrong)[1] == lid


def test_refuses_wrong_owner_wrong_city_missing_pincode_and_ambiguous_target(client):
    _, session = client
    placeholder, target, wrong, real, city, lid = _setup(session)
    with pytest.raises(loc_svc.ReassignRefused, match="belongs to"):
        _reassign(session, wrong, "Someone Else", target, city)
    with pytest.raises(loc_svc.ReassignRefused, match="placeholder city"):
        _reassign(session, wrong, placeholder, target, "Not The Placeholder City")
    with pytest.raises(loc_svc.ReassignRefused, match="does not exist"):
        _reassign(session, "600000", placeholder, target, city)
    # Target already exists only outside the real city -> ambiguous.
    _placeholder(session, target, _free_pins(session, 1))
    with pytest.raises(loc_svc.ReassignRefused, match="ambiguous"):
        _reassign(session, wrong, placeholder, target, city)
    assert _owner_of(session, wrong)[1] == lid


def test_reuses_a_single_existing_target_in_the_real_city(client):
    _, session = client
    placeholder, target, wrong, _, city, _ = _setup(session)
    (pin,) = _free_pins(session, 1)
    loc_svc.load_master(session, [loc_svc.MasterRow(target, pin)])
    existing = _owner_of(session, pin)[1]
    result = _reassign(session, wrong, placeholder, target, city)
    assert result["target_created"] is False and result["new_locality_id"] == existing


def test_end_to_end_with_the_reviewed_catalog(client):
    _, session = client
    taken = session.execute(text(
        "SELECT p.pincode, l.name FROM postcodes p JOIN localities l ON l.id=p.locality_id "
        "WHERE p.pincode IN ('600096','600017')")).fetchall()
    existing = [r.requested for r in loc_svc.resolve(session, ["T. Nagar", "Perungudi"], selectable_only=False)
                if r.status != "unknown"]
    if taken or existing:
        pytest.skip(f"already in this test DB: pincodes {sorted(map(tuple, taken))}, localities {existing}")
    city, lid = _placeholder(session, "T. Nagar", ["600096"])
    with open(REVIEWED, encoding="utf-8") as fh:
        rows = loc_svc.parse_master_csv(fh.read())
    first = loc_svc.load_master(session, rows)
    assert {"pincode": "600096", "requested": "Perungudi", "existing_locality": "T. Nagar"} in first["conflicts"]
    assert _owner_of(session, "600017")[1] == lid  # T. Nagar now holds its real pincode
    result = loc_svc.reassign_placeholder_postcode(
        session, "600096", placeholder_locality="T. Nagar", target_locality="Perungudi", placeholder_city=city)
    assert result["old_locality_id"] == lid and result["new_locality"] == "Perungudi"
    second = loc_svc.load_master(session, rows)
    assert not any(c.get("pincode") == "600096" for c in second["conflicts"])
    assert "600096" in second["postcodes_existing"]
