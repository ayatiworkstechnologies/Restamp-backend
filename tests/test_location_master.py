"""Agent Location Master against the database (needs the MySQL test DB).

Covers: seeding 298 records / 112 PIN groups, admin additions to existing
and new PIN codes, duplicate and idempotent submissions, validation, admin
authorization, workbook <-> database synchronisation, failure recovery
(write failure, publish failure after commit, hand-edited workbook, missing
workbook after a restart/redeploy), the cluster lock, agent search, one
slot per PIN group and preservation of existing agent selections.

Every test runs inside the client fixture's outer transaction (rolled back
at teardown); the workbook is written to a per-test temp directory.
"""
from __future__ import annotations

import os

import pytest
from openpyxl import load_workbook
from sqlalchemy import text

from app.services import location_master as lm
from tests.conftest import auth_header, fixture_admin_headers, register_phone

_n = {"i": 0}


@pytest.fixture()
def master(client, tmp_path, monkeypatch):
    """Seeded location master with its workbook in tmp_path."""
    c, session = client
    path = tmp_path / "master" / "agent_location_master.xlsx"
    monkeypatch.setenv("RESTAMP_LOCATION_MASTER_PATH", str(path))
    report = lm.seed(session)
    assert report["status"] == "seeded", report
    return c, session, path


def _user(c):
    _n["i"] += 1
    tok = register_phone(c, f"+916773{os.getpid() % 1000:03d}{_n['i']:03d}")
    return auth_header(tok["access_token"]), tok["user_id"]


def _groups(c, **params):
    r = c.get("/locations/agent-groups", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _by_pin(c):
    return {g["pincode"]: g for g in _groups(c)["items"]}


def _add(c, h, name, pin, zone, key=None):
    headers = dict(h)
    if key:
        headers["Idempotency-Key"] = key
    return c.post("/admin/location-master", json={"locality_name": name, "pincode": pin, "zone": zone}, headers=headers)


def _count(session, sql, **kw):
    return session.execute(text(sql), kw).scalar()


# ------------------------------------------------------------------ seeding ---

def test_seed_preserves_298_records_and_creates_112_groups(master):
    c, session, path = master
    assert _count(session, "SELECT COUNT(*) FROM location_master_records") == 298
    assert _count(session, "SELECT COUNT(*) FROM agent_location_groups") == 112
    data = _groups(c)
    assert data["total"] == 112 and data["version"] == 1
    assert sum(g["locality_count"] for g in data["items"]) == 298
    assert len({g["pincode"] for g in data["items"]}) == 112
    g = _by_pin(c)["600053"]
    assert g["primary_name"] == "Ambattur" and g["zone"] == "North Chennai"
    assert g["display_name"].startswith("Ambattur (Ambattur Bazaar, Ambattur H O, Ambattur West")
    assert g["display_name"].endswith("Vijayalakshmipuram) - 600053")
    assert path.exists() and lm.read_meta(path)["version"] == 1


def test_seed_is_idempotent(master):
    c, session, _ = master
    assert lm.seed(session)["status"] == "already seeded"
    assert _count(session, "SELECT COUNT(*) FROM location_master_records") == 298


def test_zones_are_preserved(master):
    c, session, _ = master
    rows = dict(session.execute(text("SELECT zone, COUNT(*) FROM location_master_records GROUP BY zone")).fetchall())
    assert rows == {"North Chennai": 108, "Central Chennai": 116, "South Chennai": 74}


# ----------------------------------------------------------- agent search ---

@pytest.mark.parametrize("q,pin", [("600053", "600053"), ("ambattur", "600053"), ("Orgadam", "600053"),
                                   ("mylopore", "600004"), ("Santhome", "600004")])
def test_agent_search_by_pin_primary_and_locality(master, q, pin):
    c, _, _ = master
    assert pin in {g["pincode"] for g in _groups(c, q=q)["items"]}


def test_zone_filter(master):
    c, _, _ = master
    items = _groups(c, zone="South Chennai")["items"]
    assert items and {g["zone"] for g in items} == {"South Chennai"}
    assert c.get("/locations/agent-groups", params={"zone": "West Chennai"}).status_code == 422


# --------------------------------------------------------- admin additions ---

def test_scenario_a_existing_pin_updates_that_group(master):
    c, session, path = master
    before = _by_pin(c)["600053"]
    r = _add(c, fixture_admin_headers(c), "Ambattur North", "600053", "North Chennai")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["created_group"] is False and body["record"]["record_id"] == 299
    after = _by_pin(c)
    assert len(after) == 112                                  # no new selectable group
    assert after["600053"]["id"] == before["id"]              # stable id
    assert after["600053"]["localities"][-1] == "Ambattur North"
    wb = load_workbook(path)
    assert wb[lm.SHEET_MASTER].max_row == 300                 # header + 299
    sel = {row[3]: row for row in wb[lm.SHEET_GROUPS].iter_rows(min_row=2, values_only=True)}
    assert "Ambattur North" in sel["600053"][2] and len(sel) == 112
    north = [row for row in wb["North Chennai"].iter_rows(min_row=2, values_only=True)]
    assert ("Ambattur North" in {row[1] for row in north})   # zone sheet not stale


def test_scenario_b_new_pin_creates_a_group(master):
    c, session, _ = master
    r = _add(c, fixture_admin_headers(c), "Test Nagar", "600119", "South Chennai")
    assert r.status_code == 201, r.text
    assert r.json()["created_group"] is True
    groups = _by_pin(c)
    assert len(groups) == 113 and groups["600119"]["display_name"] == "Test Nagar - 600119"
    assert _count(session, "SELECT COUNT(*) FROM postcodes WHERE pincode='600119'") == 1


def test_scenario_c_repeated_submission_is_refused(master):
    c, session, _ = master
    h = fixture_admin_headers(c)
    assert _add(c, h, "Ambattur North", "600053", "North Chennai").status_code == 201
    r = _add(c, h, "  ambattur   north ", "600053", "North Chennai")
    assert r.status_code == 409 and r.json()["detail"]["code"] == lm.LOCATION_DUPLICATE
    # an existing original record cannot be re-added either
    assert _add(c, h, "Ambattur West", "600053", "North Chennai").status_code == 409
    assert _count(session, "SELECT COUNT(*) FROM location_master_records") == 299


def test_idempotency_key_replays_the_original_result(master):
    c, session, _ = master
    h = fixture_admin_headers(c)
    first = _add(c, h, "Ambattur North", "600053", "North Chennai", key="k-1")
    again = _add(c, h, "Ambattur North", "600053", "North Chennai", key="k-1")
    assert first.status_code == 201 and again.status_code == 200 and again.json()["replayed"] is True
    other = _add(c, h, "Other Place", "600053", "North Chennai", key="k-1")
    assert other.status_code == 409
    assert _count(session, "SELECT COUNT(*) FROM location_master_records") == 299


@pytest.mark.parametrize("body,field", [
    ({"locality_name": "X Nagar", "pincode": "60005", "zone": "North Chennai"}, "pincode"),
    ({"locality_name": "X Nagar", "pincode": "60005a", "zone": "North Chennai"}, "pincode"),
    ({"locality_name": "X Nagar", "pincode": "600053", "zone": "West Chennai"}, "zone"),
    ({"locality_name": "   ", "pincode": "600053", "zone": "North Chennai"}, "locality_name"),
])
def test_validation(master, body, field):
    c, session, _ = master
    r = c.post("/admin/location-master", json=body, headers=fixture_admin_headers(c))
    assert r.status_code == 422 and field in r.json()["detail"]["errors"]
    assert _count(session, "SELECT COUNT(*) FROM location_master_records") == 298


def test_zone_conflict_within_a_pin_is_refused(master):
    c, session, _ = master
    r = _add(c, fixture_admin_headers(c), "Ambattur East", "600053", "South Chennai")
    assert r.status_code == 409 and r.json()["detail"]["code"] == lm.LOCATION_ZONE_CONFLICT


def test_admin_authorization(master):
    c, _, _ = master
    h, _uid = _user(c)
    assert _add(c, h, "Ambattur North", "600053", "North Chennai").status_code == 403
    assert c.post("/admin/location-master", json={"locality_name": "A", "pincode": "600053",
                                                  "zone": "North Chennai"}).status_code == 401
    assert c.get("/admin/location-master", headers=h).status_code == 403


def test_no_bulk_import_endpoint(master):
    c, _, _ = master
    paths = c.get("/openapi.json").json()["paths"] if c.get("/openapi.json").status_code == 200 else {}
    assert not [p for p in paths if "location" in p and ("import" in p or "bulk" in p or "upload" in p)]


def test_admin_list_and_search(master):
    c, _, _ = master
    h = fixture_admin_headers(c)
    r = c.get("/admin/location-master", params={"pincode": "600053"}, headers=h).json()
    assert r["total"] == 7
    assert c.get("/admin/location-master", params={"q": "Orgadam"}, headers=h).json()["total"] == 1
    assert c.get("/admin/location-master", params={"zone": "South Chennai"}, headers=h).json()["total"] == 74


# --------------------------------------------------------- failure recovery ---

def test_workbook_write_failure_changes_nothing(master, monkeypatch):
    c, session, path = master
    before = path.read_bytes()

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(lm, "write_temp", boom)
    r = _add(c, fixture_admin_headers(c), "Ambattur North", "600053", "North Chennai")
    assert r.status_code == 503
    assert _count(session, "SELECT COUNT(*) FROM location_master_records") == 298
    assert _count(session, "SELECT committed_version FROM location_master_state WHERE id=1") == 1
    assert path.read_bytes() == before


def test_publish_failure_after_commit_is_hidden_then_recovered(master, monkeypatch):
    c, session, path = master
    real_swap = lm.swap_in

    def fail(*_a, **_k):
        raise OSError("rename failed")

    monkeypatch.setattr(lm, "swap_in", fail)
    r = _add(c, fixture_admin_headers(c), "Ambattur North", "600053", "North Chennai")
    assert r.status_code == 503 and r.json()["detail"]["code"] == lm.LOCATION_PUBLISH_PENDING
    # committed but NOT visible to agents (not in the published workbook)
    assert "Ambattur North" not in _by_pin(c)["600053"]["localities"]
    assert lm.read_meta(path)["version"] == 1
    monkeypatch.setattr(lm, "swap_in", real_swap)
    assert lm.reconcile(session)["status"] == "republished"
    assert "Ambattur North" in _by_pin(c)["600053"]["localities"]
    assert lm.read_meta(path)["version"] == 2


def test_workbook_survives_restart_and_redeploy(master):
    c, session, path = master
    _add(c, fixture_admin_headers(c), "Ambattur North", "600053", "North Chennai")
    path.unlink()                                   # e.g. lost on a redeploy
    assert lm.reconcile(session)["status"] == "restored"
    wb = load_workbook(path)
    assert wb[lm.SHEET_MASTER].max_row == 300
    assert lm.read_meta(path)["version"] == 2


def test_hand_edited_workbook_is_quarantined_not_overwritten(master):
    c, session, path = master
    wb = load_workbook(path)
    wb[lm.SHEET_MASTER].append([999, "Hand Added", "600053", "North Chennai"])
    wb.save(path)
    edited = path.read_bytes()
    assert lm.reconcile(session)["status"] == "blocked"
    assert path.read_bytes() == edited                         # never overwritten
    assert list((path.parent / "quarantine").glob("*.xlsx"))
    r = _add(c, fixture_admin_headers(c), "Ambattur North", "600053", "North Chennai")
    assert r.status_code == 503
    assert _groups(c)["total"] == 112                          # agents keep the published data
    out = lm.resolve_blocked(session)
    assert out["version"] == 1 and lm.reconcile(session)["status"] == "ok"


def test_busy_lock_is_reported_not_waited_forever(master, monkeypatch):
    c, session, _ = master
    from app.db import engine

    monkeypatch.setattr(lm, "LOCK_TIMEOUT_S", 1)
    other = engine.connect()
    try:
        assert other.execute(text("SELECT GET_LOCK(:n, 1)"), {"n": lm.LOCK_NAME}).scalar() == 1
        r = _add(c, fixture_admin_headers(c), "Ambattur North", "600053", "North Chennai")
        assert r.status_code == 503
    finally:
        other.execute(text("SELECT RELEASE_LOCK(:n)"), {"n": lm.LOCK_NAME})
        other.close()
    assert _count(session, "SELECT COUNT(*) FROM location_master_records") == 298


# ------------------------------------------------------- agent selections ---

def test_one_slot_per_pin_group_and_selection_survives_additions(master):
    c, session, _ = master
    h, uid = _user(c)
    pins = _by_pin(c)
    chosen = [pins[p]["id"] for p in ("600053", "600001", "600004", "600020", "600099")]
    r = c.post("/broker/plans/agent-starter/subscribe", json={"group_ids": chosen}, headers=h)
    assert r.status_code == 200, r.text
    plan = c.get("/broker/plan", headers=h).json()
    assert sorted(plan["group_ids"]) == sorted(chosen)
    assert plan["localities_remaining"] == 0                  # 5 PIN groups = 5 slots (600001 has 13 localities)
    # A sixth group is over the Starter limit.
    r = c.post("/broker/plan/localities", json={"group_ids": [pins["600002"]["id"]]}, headers=h)
    assert r.status_code == 422
    # Admin adds a locality to 600053: the agent's selection is unchanged.
    assert _add(c, fixture_admin_headers(c), "Ambattur North", "600053", "North Chennai").status_code == 201
    after = c.get("/broker/plan", headers=h).json()
    assert sorted(after["group_ids"]) == sorted(chosen) and after["localities_remaining"] == 0


def test_unpublished_or_unknown_group_ids_are_not_granted(master):
    c, _, _ = master
    h, _uid = _user(c)
    r = c.post("/broker/plans/agent-starter/subscribe", json={"group_ids": [999999]}, headers=h)
    assert r.status_code == 422


def test_trial_with_a_group_id(master):
    c, _, _ = master
    h, _uid = _user(c)
    gid = _by_pin(c)["600020"]["id"]
    r = c.post("/broker/trial", json={"group_id": gid}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["group_ids"] == [gid]


def test_app_and_website_get_identical_data(master):
    c, _, _ = master
    a = c.get("/locations/agent-groups", headers={"User-Agent": "okhttp/4 (Android)"}).json()
    b = c.get("/locations/agent-groups", headers={"User-Agent": "Mozilla/5.0", "Origin": "http://localhost:3000"}).json()
    assert a == b
