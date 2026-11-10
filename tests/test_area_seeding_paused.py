"""TEMPORARILY DISABLED — Chennai area seeding paused.

The Chennai locality catalog work stays in the code but is inactive by
default: RESTAMP_AREA_SEEDING_ENABLED is off, and never effective in
production. While paused:
- both operator scripts (locality loader, Perungudi postcode reassignment)
  refuse to run and change nothing;
- the five admin agent-locality endpoints answer 404;
- locality aliases are ignored by name resolution and owner validation;
- the 600119 approval is commented out: the pincode rule is 600001-600118.
Still active: the owner-entered-pincode fix and agent add-locations.
Rollback-isolated.
"""
import importlib.util
import os
import uuid

from sqlalchemy import text

from app import config, schemas
from app.services import localities as loc_svc
from tests.conftest import auth_header, fresh_pincode, register_phone

HERE = os.path.dirname(os.path.abspath(__file__))
_n = {"i": 0}


def _script(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, "..", "scripts", f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _user(c, session=None, admin=False):
    _n["i"] += 1
    tok = register_phone(c, f"+916776{_n['i']:06d}")
    if admin:
        session.execute(text("INSERT INTO admin_accounts (user_id, admin_role) VALUES (:u, 'SUPER_ADMIN')"),
                        {"u": tok["user_id"]})
        session.flush()
    return auth_header(tok["access_token"]), tok["user_id"]


def test_flag_is_off_by_default_and_never_on_in_production(monkeypatch):
    assert config.settings.AREA_SEEDING_ENABLED is False
    assert config.area_seeding_enabled() is False
    monkeypatch.setattr(config.settings, "AREA_SEEDING_ENABLED", True)
    assert config.area_seeding_enabled() is True  # local only
    monkeypatch.setattr(config, "is_production", lambda: True)
    assert config.area_seeding_enabled() is False


def test_admin_locality_endpoints_are_hidden(client, monkeypatch):
    c, session = client
    ha, agent = _user(c, session, admin=True)
    calls = (("GET", "/admin/agent-localities", None),
             ("PATCH", "/admin/agent-localities/1", {"agent_enabled": False}),
             ("GET", f"/admin/agents/{agent}/localities", None),
             ("POST", f"/admin/agents/{agent}/localities", {"localities": ["x"]}),
             ("DELETE", f"/admin/agents/{agent}/localities/1", None))
    for method, path, body in calls:
        for headers in ({}, ha):
            r = c.request(method, path, json=body, headers=headers)
            assert r.status_code == 404, (method, path, r.status_code)
    assert not session.execute(text("SELECT COUNT(*) FROM audit_logs WHERE user_id=:u"), {"u": agent}).scalar()
    monkeypatch.setattr(config.settings, "AREA_SEEDING_ENABLED", True)
    assert c.get("/admin/agent-localities", headers=ha).status_code == 200  # restorable


def test_operator_scripts_refuse_and_change_nothing(tmp_path, capsys):
    csv_file = tmp_path / "loc.csv"
    csv_file.write_text("locality,pincode\nPausedpet,600001\n", encoding="utf-8")
    loader = _script("load_chennai_localities")
    assert loader.main(["--csv", str(csv_file), "--source", "x", "--source-date", "x"]) == 2
    reassign = _script("reassign_placeholder_postcode")
    assert reassign.main(["--pincode", "600096", "--placeholder", "T. Nagar", "--target", "Perungudi"]) == 2
    assert capsys.readouterr().err.count("TEMPORARILY DISABLED") == 2


def test_aliases_are_ignored_while_paused(client, monkeypatch):
    c, session = client
    name, pin, alias = f"Pausepet {uuid.uuid4().hex[:5]}", fresh_pincode(c), f"Pausealias{uuid.uuid4().hex[:5]}"
    loc_svc.load_master(session, [loc_svc.MasterRow(name, pin, (alias,))])
    session.flush()
    assert loc_svc.resolve(session, [alias])[0].status == "unknown"
    assert loc_svc.resolve(session, [name.upper()])[0].status == "ok"  # canonical name still resolves
    monkeypatch.setattr(config.settings, "AREA_SEEDING_ENABLED", True)
    assert loc_svc.resolve(session, [alias])[0].name == name


def test_pincode_rule_is_back_to_600001_600118(client):
    assert schemas.CHENNAI_APPROVED_EXTRA_PINCODES == frozenset()
    assert schemas.CHENNAI_PINCODE_ERROR == "Please enter a valid pincode between 600001 and 600118."
    for ok in ("600001", "600045", "600100", "600118"):
        assert schemas.is_chennai_service_pincode(ok)
    for bad in ("600119", "600120", "600125"):
        assert not schemas.is_chennai_service_pincode(bad)
    _, session = client
    report = loc_svc.load_master(session, [loc_svc.MasterRow(f"Paused {uuid.uuid4().hex[:4]}", "600119")])
    assert report["postcodes_created"] == [] and report["skipped"][0]["pincode"] == "600119"


def test_owner_pincode_fix_and_agent_add_locations_stay_active(client):
    c, session = client
    ho, _ = _user(c)
    assert c.post("/users/me/role", json={"role": "OWNER"}, headers=ho).status_code == 200
    run = uuid.uuid4().hex[:5]
    name, pin, other = f"Activepet {run}", fresh_pincode(c), f"Otherpet {run}"
    loc_svc.load_master(session, [loc_svc.MasterRow(name, pin), loc_svc.MasterRow(other, fresh_pincode(c))])
    session.flush()
    body = {"title": "Flat", "transaction_type": "RENT", "property_type": "APARTMENT",
            "price_rupees": 20000, "city": "Chennai"}
    assert c.post("/owner/listings", json={**body, "locality": other, "pincode": pin}, headers=ho).status_code == 422
    new_pin = fresh_pincode(c)
    r = c.post("/owner/listings", json={**body, "locality": name, "pincode": new_pin}, headers=ho)
    assert r.status_code == 201 and r.json()["pincode"] == new_pin  # never replaced by the catalog's pincode
    ha, _ = _user(c)
    assert c.post("/broker/plans/agent-starter/subscribe", json={"localities": [name]}, headers=ha).status_code == 200
    r = c.post("/broker/plan/localities", json={"localities": [other.lower()]}, headers=ha)
    assert r.status_code == 200 and r.json()["added_localities"] == [other]
