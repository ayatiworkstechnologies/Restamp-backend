"""Chennai locality master data, admin-managed agent catalog, agent plan adds.

- Master loading (services/localities.load_master) creates canonical
  localities + postcodes + aliases, reuses existing rows by spelling key,
  never duplicates or rebinds, and is idempotent.
- Resolution: canonical names, spelling/punctuation variants and aliases;
  no substring matching; unknown names are refused clearly.
- Agent plans: unused allowance is added without payment, the plan limit
  holds (also under concurrency), disabled localities can't be newly
  selected, and existing coverage + expiry survive admin changes.
- Admin endpoints: users.view to read, settings.configure_platform to
  change; buyers/owners/agents are refused; every change is audited.

FIXTURE DATA: the pincodes below are allocated free test values, NOT the
real India Post pincodes of these places. Rollback-isolated, except the
concurrency test, which needs real commits: it runs only with
RESTAMP_TEST_REQUIRE_DISPOSABLE=1 (disposable test DB) and deletes what it
created.
"""
import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.services import agent_plans as plan_svc
from app.services import localities as loc_svc
from tests.conftest import auth_header, register_phone

_n = {"i": 0}


# TEMPORARILY DISABLED — Chennai area seeding paused. The implementation is
# kept and tested here with the flag switched on for these tests only.
@pytest.fixture(autouse=True)
def _area_seeding_on_for_these_tests(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "AREA_SEEDING_ENABLED", True)


def _user(c, role="BUYER"):
    _n["i"] += 1
    tok = register_phone(c, f"+916772{_n['i']:06d}")
    h = auth_header(tok["access_token"])
    if role != "BUYER":
        assert c.post("/users/me/role", json={"role": role}, headers=h).status_code == 200
    return h, tok["user_id"]


def _admin(c, session, admin_role="SUPER_ADMIN"):
    h, uid = _user(c)
    session.execute(text("INSERT INTO admin_accounts (user_id, admin_role) VALUES (:u, :r)"), {"u": uid, "r": admin_role})
    session.flush()
    return h, uid


def _free_pins(session, n):
    used = {r[0] for r in session.execute(text("SELECT pincode FROM postcodes"))}
    free = [str(p) for p in range(600001, 600119) if str(p) not in used]
    assert len(free) >= n, "restamp_test has too few free Chennai pincodes"
    return free[:n]


def _key_rows(session, name):
    """Locality rows whose spelling key equals `name`'s."""
    return session.execute(
        text("SELECT id, name FROM localities WHERE REGEXP_REPLACE(LOWER(name), '[^a-z0-9]', '') = :k ORDER BY id"),
        {"k": loc_svc.name_key(name)},
    ).fetchall()


def _count(session, sql, **params):
    return session.execute(text(sql), params).scalar()


def _audits(session, action):
    rows = session.execute(
        text("SELECT user_id, before_json, after_json FROM audit_logs WHERE action=:a ORDER BY id"), {"a": action}
    ).fetchall()
    load = lambda v: json.loads(v) if isinstance(v, str) else v  # noqa: E731
    return [(uid, load(b), load(a)) for uid, b, a in rows]


def _unique_places(session, n):
    """n fresh, uniquely named canonical localities (one postcode each)."""
    run = uuid.uuid4().hex[:6]
    pins = _free_pins(session, n)
    names = [f"Zone {run} {chr(65 + i)}" for i in range(n)]
    loc_svc.load_master(session, [loc_svc.MasterRow(nm, pin) for nm, pin in zip(names, pins)])
    session.flush()
    return names


# --- master data ------------------------------------------------------------------

def test_kodambakkam_and_perambur_resolve_to_canonical_records(client):
    _, session = client
    p1, p2 = _free_pins(session, 2)
    before = {n: len(_key_rows(session, n)) for n in ("Kodambakkam", "Perambur")}
    report = loc_svc.load_master(session, [
        loc_svc.MasterRow("Kodambakkam", p1, ("Kodambakam",)),
        loc_svc.MasterRow("Perambur", p2),
    ])
    assert sorted(report["postcodes_created"]) == sorted([p1, p2]) and not report["conflicts"]
    for name in ("Kodambakkam", "Perambur"):
        assert len(_key_rows(session, name)) == max(before[name], 1)  # one place, never duplicated
    res = {r.requested: r for r in loc_svc.resolve(session, ["kodambakkam", "PERAMBUR", "Kodambakam"])}
    assert res["kodambakkam"].status == "ok" and res["kodambakkam"].name == "Kodambakkam"
    assert res["PERAMBUR"].status == "ok" and res["PERAMBUR"].name == "Perambur"
    # The misspelling is an alias of the same canonical record, not a new row.
    assert res["Kodambakam"].status == "ok" and res["Kodambakam"].locality_ids == res["kodambakkam"].locality_ids
    pins = session.execute(
        text("SELECT p.pincode FROM postcodes p JOIN localities l ON l.id=p.locality_id WHERE l.id IN :ids"),
        {"ids": tuple(res["kodambakkam"].locality_ids)},
    ).fetchall()
    assert p1 in {r[0] for r in pins}


def test_existing_adyar_anna_nagar_t_nagar_are_reused_not_duplicated(client):
    c, session = client
    ho, _ = _user(c, "OWNER")
    p_t, p_t2, p_a, p_an = _free_pins(session, 4)
    # Pre-existing rows exactly as owner listings create them ("T Nagar" spelling).
    for title, locality, pin in (("T Flat", "T Nagar", p_t), ("A Flat", "Adyar", p_a), ("AN Flat", "Anna Nagar", p_an)):
        r = c.post("/owner/listings", json={"title": title, "transaction_type": "RENT", "property_type": "APARTMENT",
                                            "price_rupees": 20000, "locality": locality, "city": "Chennai",
                                            "pincode": pin}, headers=ho)
        assert r.status_code == 201, r.text
    rows_before = {n: _key_rows(session, n) for n in ("T. Nagar", "Adyar", "Anna Nagar")}
    report = loc_svc.load_master(session, [
        loc_svc.MasterRow("T. Nagar", p_t, ("Pondy Bazaar",)),
        loc_svc.MasterRow("T. Nagar", p_t2),
        loc_svc.MasterRow("Adyar", p_a),
        loc_svc.MasterRow("Anna Nagar", p_an),
    ])
    assert report["localities_created"] == [] and not report["conflicts"]
    # (Owner create reuses an existing same-named locality's postcode, so
    # which pincodes pre-existed depends on the DB; the outcome must not.)
    assert sorted(report["postcodes_existing"] + report["postcodes_created"]) == sorted([p_t, p_t2, p_a, p_an])
    for pin, name in ((p_t, "T. Nagar"), (p_t2, "T. Nagar"), (p_a, "Adyar"), (p_an, "Anna Nagar")):
        bound = session.execute(
            text("SELECT l.name FROM postcodes p JOIN localities l ON l.id=p.locality_id WHERE p.pincode=:p"), {"p": pin}
        ).scalar()
        assert loc_svc.name_key(bound) == loc_svc.name_key(name), (pin, bound)
    for name, rows in rows_before.items():
        assert _key_rows(session, name) == rows  # same rows, names not rewritten
    owner_t_nagar = session.execute(
        text("SELECT l.id FROM postcodes p JOIN localities l ON l.id=p.locality_id WHERE p.pincode=:p"), {"p": p_t}
    ).scalar()
    assert session.execute(
        text("SELECT locality_id FROM postcodes WHERE pincode=:p"), {"p": p_t2}
    ).scalar() == owner_t_nagar  # the new pincode joins the existing record
    pondy = loc_svc.resolve(session, ["pondy bazaar"])[0]
    assert pondy.status == "ok" and owner_t_nagar in pondy.locality_ids


def test_master_load_is_idempotent(client):
    _, session = client
    p1, p2 = _free_pins(session, 2)
    run = uuid.uuid4().hex[:5]
    rows = [loc_svc.MasterRow(f"Seedpet {run}", p1, (f"Seed Pet {run} Extn",)), loc_svc.MasterRow(f"Seedpet {run}", p2)]
    tables = ("localities", "postcodes", "locality_aliases", "villages", "taluks")
    first = loc_svc.load_master(session, rows)
    assert len(first["postcodes_created"]) == 2 and len(first["localities_created"]) == 1
    counts = {t: _count(session, f"SELECT COUNT(*) FROM {t}") for t in tables}
    second = loc_svc.load_master(session, rows)
    assert second["localities_created"] == [] and second["postcodes_created"] == [] and second["aliases_created"] == []
    assert sorted(second["postcodes_existing"]) == sorted([p1, p2])
    assert {t: _count(session, f"SELECT COUNT(*) FROM {t}") for t in tables} == counts


def test_master_load_never_rebinds_or_invents(client):
    _, session = client
    (pin,) = _free_pins(session, 1)
    run = uuid.uuid4().hex[:5]
    loc_svc.load_master(session, [loc_svc.MasterRow(f"Firstpet {run}", pin)])
    report = loc_svc.load_master(session, [
        loc_svc.MasterRow(f"Otherpet {run}", pin),          # pincode already belongs elsewhere
        loc_svc.MasterRow(f"Farpet {run}", "600125"),       # outside the approved service area
        loc_svc.MasterRow("...", "600001"),                 # no usable name
    ])
    assert report["conflicts"] == [{"pincode": pin, "requested": f"Otherpet {run}", "existing_locality": f"Firstpet {run}"}]
    assert [s["reason"] for s in report["skipped"]] == [
        "pincode outside the approved Chennai service area", "invalid locality name"]
    assert loc_svc.resolve(session, [f"Otherpet {run}", f"Farpet {run}"])[0].status == "unknown"
    owner = session.execute(
        text("SELECT l.name FROM postcodes p JOIN localities l ON l.id=p.locality_id WHERE p.pincode=:p"), {"p": pin}
    ).scalar()
    assert owner == f"Firstpet {run}"


def test_india_post_directory_format_is_parsed():
    csv_text = (
        "circlename,regionname,divisionname,officename,pincode,officetype,delivery,district,districtname,statename\n"
        "Tamilnadu Circle,Chennai,Chennai City South,T.Nagar S.O,600017,SO,Delivery,,CHENNAI,TAMIL NADU\n"
        "Tamilnadu Circle,Chennai,Chennai City South,Pondy Bazaar B.O,600017,BO,Delivery,,CHENNAI,TAMIL NADU\n"
        "Tamilnadu Circle,Chennai,Chennai City South,Adyar S.O,600020,SO,Delivery,,CHENNAI,TAMIL NADU\n"
        "Tamilnadu Circle,Coimbatore,Coimbatore,Gandhipuram S.O,641012,SO,Delivery,,COIMBATORE,TAMIL NADU\n"
    )
    rows = loc_svc.parse_master_csv(csv_text)
    assert rows == [
        loc_svc.MasterRow("T.Nagar", "600017", ("Pondy Bazaar",)),
        loc_svc.MasterRow("Adyar", "600020", ()),
    ]
    curated = loc_svc.parse_master_csv("locality,pincode,aliases\nPerambur,600011,Perambur Barracks|Perambur East\n")
    assert curated == [loc_svc.MasterRow("Perambur", "600011", ("Perambur Barracks", "Perambur East"))]
    with pytest.raises(ValueError):
        loc_svc.parse_master_csv("name,code\nx,1\n")


def test_spelling_variants_resolve_and_substrings_do_not(client):
    _, session = client
    (pin,) = _free_pins(session, 1)
    run = uuid.uuid4().hex[:4].upper()
    canonical = f"Q{run}. Nagar"
    loc_svc.load_master(session, [loc_svc.MasterRow(canonical, pin)])
    variants = [f"Q{run}. Nagar", f"q{run} nagar", f"Q{run}-NAGAR", f"q{run}nagar", f"  Q{run} .  Nagar "]
    for v in variants:
        (r,) = loc_svc.resolve(session, [v])
        assert r.status == "ok" and r.name == canonical, v
    assert len(loc_svc.resolve(session, variants)) == 1  # one place, not five
    assert loc_svc.resolve(session, [f"Q{run}"])[0].status == "unknown"  # no substring match


# --- agent plan: add localities ---------------------------------------------------

def _starter(c, session, names, chosen=3):
    h, uid = _user(c)
    r = c.post("/broker/plans/agent-starter/subscribe", json={"localities": names[:chosen]}, headers=h)
    assert r.status_code == 200, r.text
    return h, uid


def test_unknown_localities_get_a_clear_error_and_mixed_requests_report_them(client):
    c, session = client
    names = _unique_places(session, 4)
    h, uid = _starter(c, session, names)
    unknown = c.post("/broker/plan/localities", json={"localities": ["Atlantis Nagar", "Nagar"]}, headers=h)
    assert unknown.status_code == 422 and unknown.json()["detail"] == plan_svc.NOT_SERVED
    mixed = c.post("/broker/plan/localities", json={"localities": [names[3].upper(), "Atlantis Nagar"]}, headers=h)
    assert mixed.status_code == 200
    assert mixed.json()["added_localities"] == [names[3]]
    assert mixed.json()["unmatched_localities"] == ["Atlantis Nagar"]
    assert mixed.json()["unavailable_localities"] == []


def test_add_within_allowance_without_payment_and_sixth_refused(client):
    c, session = client
    names = _unique_places(session, 6)
    h, uid = _starter(c, session, names)
    payments = _count(session, "SELECT COUNT(*) FROM payments WHERE user_id=:u", u=uid)
    end = _count(session, "SELECT end_date FROM subscriptions WHERE user_id=:u AND status='ACTIVE'", u=uid)
    r = c.post("/broker/plan/localities", json={"localities": [names[3], names[4].lower()]}, headers=h)
    assert r.status_code == 200 and sorted(r.json()["added_localities"]) == sorted(names[3:5])
    assert r.json()["localities_remaining"] == 0
    sixth = c.post("/broker/plan/localities", json={"localities": [names[5]]}, headers=h)
    assert sixth.status_code == 422 and sixth.json()["detail"] == "Agent Starter Plan covers up to 5 localities."
    assert _count(session, "SELECT COUNT(*) FROM payments WHERE user_id=:u", u=uid) == payments
    assert _count(session, "SELECT end_date FROM subscriptions WHERE user_id=:u AND status='ACTIVE'", u=uid) == end


def test_missing_or_expired_plan_cannot_add(client):
    c, session = client
    names = _unique_places(session, 4)
    h, _ = _user(c)
    assert c.post("/broker/plan/localities", json={"localities": [names[0]]}, headers=h).status_code == 409
    h2, uid2 = _starter(c, session, names)
    session.execute(text("UPDATE subscriptions SET end_date=:e WHERE user_id=:u"),
                    {"e": datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1), "u": uid2})
    session.flush()
    r = c.post("/broker/plan/localities", json={"localities": [names[3]]}, headers=h2)
    assert r.status_code == 409 and r.json()["detail"] == "No active agent plan"


def test_disabled_locality_cannot_be_newly_selected_but_existing_coverage_stays(client):
    c, session = client
    names = _unique_places(session, 5)
    ha, admin_id = _admin(c, session)
    h, uid = _starter(c, session, names)  # covers A, B, C
    end = _count(session, "SELECT end_date FROM subscriptions WHERE user_id=:u AND status='ACTIVE'", u=uid)
    ids = {n: loc_svc.resolve(session, [n])[0].locality_ids[0] for n in names}
    for n in (names[0], names[3]):  # one already covered, one not yet
        r = c.patch(f"/admin/agent-localities/{ids[n]}", json={"agent_enabled": False, "reason": "policy"}, headers=ha)
        assert r.status_code == 200 and r.json()["agent_enabled"] is False

    # Existing assignment and plan expiry are preserved.
    plan = c.get("/broker/plan", headers=h).json()
    assert names[0] in plan["localities"] and len(plan["localities"]) == 3
    assert _count(session, "SELECT end_date FROM subscriptions WHERE user_id=:u AND status='ACTIVE'", u=uid) == end
    assert c.get("/broker/locations", headers=h).json()["total"] == 3

    # ...but the disabled one can't be newly selected: by add, purchase or admin.
    r = c.post("/broker/plan/localities", json={"localities": [names[3]]}, headers=h)
    assert r.status_code == 422 and r.json()["detail"] == plan_svc.NOT_AVAILABLE
    r = c.post("/broker/plan/localities", json={"localities": [names[3], names[4]]}, headers=h)
    assert r.status_code == 200 and r.json()["added_localities"] == [names[4]]
    assert r.json()["unavailable_localities"] == [names[3]]
    hb, _ = _user(c)
    r = c.post("/broker/plans/agent-starter/subscribe", json={"localities": [names[3]]}, headers=hb)
    assert r.status_code == 422 and r.json()["detail"] == plan_svc.NOT_AVAILABLE
    r = c.post(f"/admin/agents/{uid}/localities", json={"localities": [names[3]]}, headers=ha)
    assert r.status_code == 422

    # Re-enabling makes it selectable again.
    assert c.patch(f"/admin/agent-localities/{ids[names[3]]}", json={"agent_enabled": True}, headers=ha).status_code == 200
    r = c.post("/broker/plans/agent-starter/subscribe", json={"localities": [names[3]]}, headers=hb)
    assert r.status_code == 200


# --- admin API --------------------------------------------------------------------

ADMIN_CALLS = (
    ("GET", "/admin/agent-localities", None),
    ("PATCH", "/admin/agent-localities/{loc}", {"agent_enabled": False}),
    ("GET", "/admin/agents/{agent}/localities", None),
    ("POST", "/admin/agents/{agent}/localities", {"localities": ["x"]}),
    ("DELETE", "/admin/agents/{agent}/localities/{loc}", None),
)


def test_admin_locality_management_rejects_non_admins(client):
    c, session = client
    names = _unique_places(session, 3)
    h_agent, agent_id = _starter(c, session, names)
    loc = loc_svc.resolve(session, [names[0]])[0].locality_ids[0]
    h_buyer, _ = _user(c)
    h_owner, _ = _user(c, "OWNER")
    h_broker, _ = _user(c, "BROKER")
    for method, path, body in ADMIN_CALLS:
        url = path.format(loc=loc, agent=agent_id)
        assert c.request(method, url, json=body).status_code == 401, url
        for h in (h_agent, h_buyer, h_owner, h_broker):
            r = c.request(method, url, json=body, headers=h)
            assert r.status_code == 403 and r.json()["detail"] == "Forbidden", (url, r.status_code)
    # A reviewer may read but not change.
    h_rev, _ = _admin(c, session, "LISTING_REVIEWER")
    assert c.get("/admin/agent-localities", headers=h_rev).status_code == 200
    assert c.get(f"/admin/agents/{agent_id}/localities", headers=h_rev).status_code == 200
    for method, path, body in [call for call in ADMIN_CALLS if call[0] != "GET"]:
        r = c.request(method, path.format(loc=loc, agent=agent_id), json=body, headers=h_rev)
        assert r.status_code == 403 and r.json()["detail"] == "settings.configure_platform permission required"
    # The refused calls changed nothing.
    assert _count(session, "SELECT agent_enabled FROM localities WHERE id=:i", i=loc) == 1
    assert c.get("/broker/plan", headers=h_agent).json()["localities"] == sorted(names[:3])


def test_admin_changes_are_audited_and_preserve_plan(client):
    c, session = client
    names = _unique_places(session, 7)
    ha, admin_id = _admin(c, session)
    h, uid = _starter(c, session, names)
    loc = {n: loc_svc.resolve(session, [n])[0].locality_ids[0] for n in names}
    end = _count(session, "SELECT end_date FROM subscriptions WHERE user_id=:u AND status='ACTIVE'", u=uid)
    payments = _count(session, "SELECT COUNT(*) FROM payments WHERE user_id=:u", u=uid)

    # Catalog listing shows enabled state, pincodes and coverage counts.
    cat = c.get("/admin/agent-localities", params={"q": names[0]}, headers=ha).json()
    assert cat["total"] == 1 and cat["items"][0]["agent_enabled"] is True
    assert cat["items"][0]["assigned_agent_count"] == 1 and len(cat["items"][0]["pincodes"]) == 1

    # Enable/disable: audited once per real change.
    n_before = len(_audits(session, "AGENT_LOCALITY_DISABLED"))
    for _ in range(2):
        assert c.patch(f"/admin/agent-localities/{loc[names[5]]}", json={"agent_enabled": False, "reason": "r"},
                       headers=ha).status_code == 200
    rows = _audits(session, "AGENT_LOCALITY_DISABLED")
    assert len(rows) == n_before + 1
    assert rows[-1] == (admin_id, {"locality_id": loc[names[5]], "agent_enabled": True},
                        {"locality_id": loc[names[5]], "agent_enabled": False, "reason": "r"})
    assert c.get("/admin/agent-localities", params={"enabled": "false", "q": names[5]}, headers=ha).json()["total"] == 1

    # Assign under the plan limit: audited, no payment, same expiry.
    r = c.post(f"/admin/agents/{uid}/localities", json={"localities": [names[3]]}, headers=ha)
    assert r.status_code == 200 and r.json()["added_localities"] == [names[3]]
    assert r.json()["plan"]["localities_remaining"] == 1 and len(r.json()["coverage"]) == 4
    uid_a, before, after = _audits(session, "AGENT_LOCALITY_ASSIGNED")[-1]
    assert uid_a == admin_id and after == {"agent_user_id": uid, "added": [names[3]], "plan_code": "RESTAMP_AGENT_STARTER"}
    assert c.post(f"/admin/agents/{uid}/localities", json={"localities": names[4:5] + names[:1]},
                  headers=ha).json()["plan"]["localities_remaining"] == 0
    # Plan full (5/5): admins can't exceed it either; nothing is written.
    over = c.post(f"/admin/agents/{uid}/localities", json={"localities": [names[6]]}, headers=ha)
    assert over.status_code == 422 and over.json()["detail"] == "Agent Starter Plan covers up to 5 localities."
    # Already-covered names are skipped, never duplicated.
    again = c.post(f"/admin/agents/{uid}/localities", json={"localities": [names[2].upper()]}, headers=ha)
    assert again.status_code == 200 and again.json()["added_localities"] == []
    assert len(_audits(session, "AGENT_LOCALITY_ASSIGNED")) == 2  # only the two real additions
    # Revoke one: grant gone, history kept as REVOKED, plan untouched, slot freed.
    assert c.delete(f"/admin/agents/{uid}/localities/{loc[names[1]]}", headers=ha).status_code == 204
    assert names[1] not in c.get("/broker/plan", headers=h).json()["localities"]
    assert _count(session, "SELECT COUNT(*) FROM broker_postcode_access_history WHERE broker_user_id=:u "
                           "AND status='REVOKED' AND revoked_by=:a", u=uid, a=admin_id) == 1
    uid_r, before, after = _audits(session, "AGENT_LOCALITY_REVOKED")[-1]
    assert uid_r == admin_id and before["locality_id"] == loc[names[1]] and after["revoked"] is True
    assert c.delete(f"/admin/agents/{uid}/localities/{loc[names[1]]}", headers=ha).status_code == 404
    assert _count(session, "SELECT end_date FROM subscriptions WHERE user_id=:u AND status='ACTIVE'", u=uid) == end
    assert _count(session, "SELECT COUNT(*) FROM payments WHERE user_id=:u", u=uid) == payments
    assert c.get("/broker/plan", headers=h).json()["localities_remaining"] == 1

    view = c.get(f"/admin/agents/{uid}/localities", headers=ha).json()
    assert view["plan"]["locality_limit"] == 5 and len(view["coverage"]) == 4
    assert all(row["agent_enabled"] for row in view["coverage"])


def test_admin_assignment_requires_a_plan_and_a_real_agent(client):
    c, session = client
    names = _unique_places(session, 1)
    ha, _ = _admin(c, session)
    _, no_plan = _user(c)
    r = c.post(f"/admin/agents/{no_plan}/localities", json={"localities": names}, headers=ha)
    assert r.status_code == 409 and r.json()["detail"] == "No active agent plan"
    assert c.post("/admin/agents/999999999/localities", json={"localities": names}, headers=ha).status_code == 404
    assert c.get("/admin/agents/999999999/localities", headers=ha).status_code == 404
    assert c.patch("/admin/agent-localities/999999999", json={"agent_enabled": False}, headers=ha).status_code == 404
    assert _audits(session, "AGENT_LOCALITY_ASSIGNED") == []  # refused calls write no audit


# --- concurrency (real commits; disposable DB only) -------------------------------

@pytest.mark.skipif(os.environ.get("RESTAMP_TEST_REQUIRE_DISPOSABLE") != "1",
                    reason="commits real rows: runs only against the disposable test DB")
def test_concurrent_adds_cannot_exceed_the_plan_limit():
    from app.db import SessionLocal

    setup = SessionLocal()
    created = {"pins": [], "user": None}
    try:
        used = {r[0] for r in setup.execute(text("SELECT pincode FROM postcodes"))}
        pins = [str(p) for p in range(600001, 600119) if str(p) not in used][:6]
        run = uuid.uuid4().hex[:6]
        names = [f"Racepet {run} {chr(65 + i)}" for i in range(6)]
        loc_svc.load_master(setup, [loc_svc.MasterRow(n, p) for n, p in zip(names, pins)])
        created["pins"] = pins
        setup.execute(text("INSERT INTO users (display_name) VALUES ('Race Agent')"))
        uid = setup.execute(text("SELECT LAST_INSERT_ID()")).scalar()
        created["user"] = uid
        plan_svc.subscribe_simulated(setup, uid, "agent-starter", names[:4])  # 4 of 5 used
        setup.commit()

        first_locked = threading.Event()
        results = {}

        def first():
            db = SessionLocal()
            try:
                plan_svc.add_localities(db, uid, [names[4]])
                first_locked.set()           # holds the plan lock, not yet committed
                threading.Event().wait(0.8)
                db.commit()
                results["first"] = "added"
            except HTTPException as exc:
                db.rollback()
                first_locked.set()
                results["first"] = exc.status_code
            finally:
                db.close()

        def second():
            first_locked.wait(5)
            db = SessionLocal()
            try:
                plan_svc.add_localities(db, uid, [names[5]])  # blocks on the lock until `first` commits
                db.commit()
                results["second"] = "added"
            except HTTPException as exc:
                db.rollback()
                results["second"] = exc.status_code
            finally:
                db.close()

        threads = [threading.Thread(target=first), threading.Thread(target=second)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(15)
        assert results == {"first": "added", "second": 422}
        check = SessionLocal()
        try:
            assert len(plan_svc.status_for(check, uid)["localities"]) == 5
        finally:
            check.close()
    finally:
        setup.rollback()
        uid = created["user"]
        if uid is not None:
            for sql in ("DELETE FROM broker_postcode_access_current WHERE broker_user_id=:u",
                        "DELETE FROM broker_postcode_access_history WHERE broker_user_id=:u",
                        "DELETE FROM subscriptions WHERE user_id=:u",
                        "DELETE FROM payments WHERE user_id=:u",
                        "DELETE FROM users WHERE id=:u"):
                setup.execute(text(sql), {"u": uid})
        if created["pins"]:
            loc_ids = [r[0] for r in setup.execute(
                text("SELECT locality_id FROM postcodes WHERE pincode IN :p"), {"p": tuple(created["pins"])})]
            setup.execute(text("DELETE FROM postcodes WHERE pincode IN :p"), {"p": tuple(created["pins"])})
            if loc_ids:
                vil = [r[0] for r in setup.execute(text("SELECT village_id FROM localities WHERE id IN :l"),
                                                     {"l": tuple(loc_ids)})]
                tal = [r[0] for r in setup.execute(text("SELECT taluk_id FROM villages WHERE id IN :v"),
                                                     {"v": tuple(vil)})]
                setup.execute(text("DELETE FROM localities WHERE id IN :l"), {"l": tuple(loc_ids)})
                setup.execute(text("DELETE FROM villages WHERE id IN :v"), {"v": tuple(vil)})
                setup.execute(text("DELETE FROM taluks WHERE id IN :t"), {"t": tuple(tal)})
        setup.commit()
        setup.close()


# --- loader script ----------------------------------------------------------------

def test_loader_script_dry_run_writes_nothing_and_refuses_bad_input(tmp_path, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "load_chennai_localities", os.path.join(os.path.dirname(__file__), "..", "scripts", "load_chennai_localities.py")
    )
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        used = {r[0] for r in db.execute(text("SELECT pincode FROM postcodes"))}
    finally:
        db.close()
    pin = next(str(p) for p in range(600001, 600119) if str(p) not in used)
    name = f"Dryrunpet {uuid.uuid4().hex[:6]}"
    good = tmp_path / "loc.csv"
    good.write_text(f"locality,pincode,aliases\n{name},{pin},\n", encoding="utf-8")
    args = ["--csv", str(good), "--source", "test fixture", "--source-date", "2026-11-04"]
    assert script.main(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "DRY RUN (rolled back)" and out["counts"]["postcodes_created"] == 1
    db = SessionLocal()
    try:
        assert db.execute(text("SELECT COUNT(*) FROM postcodes WHERE pincode=:p"), {"p": pin}).scalar() == 0
    finally:
        db.close()
    bad = tmp_path / "bad.csv"
    bad.write_text("name,code\nx,1\n", encoding="utf-8")
    assert script.main(["--csv", str(bad), "--source", "x", "--source-date", "x"]) == 2
    assert script.main(["--csv", str(tmp_path / "missing.csv"), "--source", "x", "--source-date", "x"]) == 2


# --- India Post district column variants (Chennai, Tamil Nadu only) ----------------

_IP_ROWS = (
    ("T.Nagar S.O", "600017", "PO", "CHENNAI", "TAMIL NADU"),
    ("Pondy Bazaar B.O", "600017", "BO", "Chennai", "Tamil Nadu"),    # case-insensitive values
    ("Adyar S.O", "600020", "PO", "CHENNAI", "TAMIL NADU"),
    ("Gandhipuram S.O", "641012", "PO", "COIMBATORE", "TAMIL NADU"),  # other district
    ("Fake Chennai S.O", "600050", "PO", "CHENNAI", "KERALA"),        # other state
)
_IP_EXPECTED = [
    loc_svc.MasterRow("T.Nagar", "600017", ("Pondy Bazaar",)),
    loc_svc.MasterRow("Adyar", "600020", ()),
]


def _ip_csv(district_column):
    head = f"circlename,officename,pincode,officetype,delivery,{district_column},statename,latitude,longitude\n"
    return head + "".join(f"Tamilnadu Circle,{o},{p},{t},Delivery,{d},{s},13.0,80.2\n" for o, p, t, d, s in _IP_ROWS)


@pytest.mark.parametrize("district_column", ["districtname", "district", " District ", "DISTRICTNAME"])
def test_india_post_csv_accepts_either_district_column(district_column):
    assert loc_svc.parse_master_csv(_ip_csv(district_column)) == _IP_EXPECTED


def test_india_post_csv_keeps_only_chennai_tamil_nadu_rows():
    rows = loc_svc.parse_master_csv(_ip_csv("district"))
    pins = {r.pincode for r in rows}
    assert "641012" not in pins  # Coimbatore district
    assert "600050" not in pins  # a CHENNAI district outside Tamil Nadu
    names, missing = loc_svc.rows_for_names(_ip_csv("district"), ["T. Nagar", "Adyar", "Gandhipuram"])
    assert names == [loc_svc.MasterRow("T. Nagar", "600017", ()), loc_svc.MasterRow("Adyar", "600020", ())]
    assert missing == ["Gandhipuram"]


def test_india_post_csv_without_a_district_column_is_refused():
    no_district = "officename,pincode,officetype,statename\nAdyar S.O,600020,PO,TAMIL NADU\n"
    with pytest.raises(ValueError):
        loc_svc.parse_master_csv(no_district)
    with pytest.raises(ValueError):
        loc_svc.rows_for_names(no_district, ["Adyar"])


@pytest.mark.parametrize("district_key", ["districtname", "district"])
def test_india_post_api_json_still_parses(district_key):
    payload = json.dumps({"records": [
        {"officename": o, "pincode": p, "officetype": t, district_key: d, "statename": s} for o, p, t, d, s in _IP_ROWS
    ]})
    assert loc_svc.parse_master_csv(payload) == _IP_EXPECTED


def test_curated_csv_format_is_unchanged():
    curated = "locality,pincode,aliases\nT. Nagar,600017,Pondy Bazaar|Thyagaraya Nagar\nAdyar,600020,\n"
    assert loc_svc.parse_master_csv(curated) == [
        loc_svc.MasterRow("T. Nagar", "600017", ("Pondy Bazaar", "Thyagaraya Nagar")),
        loc_svc.MasterRow("Adyar", "600020", ()),
    ]


# --- Chennai service-area pincode policy (one rule for owners and the loader) ------

@pytest.mark.skip(reason="TEMPORARILY DISABLED — Chennai area seeding paused: the 600119 approval is commented out in app/schemas.py")
def test_service_policy_is_the_range_plus_only_approved_pincodes(client):
    from app import schemas

    assert schemas.CHENNAI_APPROVED_EXTRA_PINCODES == frozenset({"600119"})
    for ok in ("600001", "600118", "600040", "600045", "600100", "600119"):
        assert schemas.is_chennai_service_pincode(ok) and schemas.normalize_chennai_pincode(ok) == ok
    # No blind maximum and no gap-filling: neighbours of 600119 stay out.
    for bad in ("600000", "600120", "600121", "600125", "600500", "560038", "60001", "6000118", "abcdef", ""):
        assert not schemas.is_chennai_service_pincode(bad), bad
    with pytest.raises(ValueError, match="600001 to 600118, or 600119"):
        schemas.normalize_chennai_pincode("600125")
    _, session = client
    report = loc_svc.load_master(session, [loc_svc.MasterRow(f"Mugalivakkam {uuid.uuid4().hex[:4]}", "600125")])
    assert report["postcodes_created"] == [] and report["skipped"][0]["pincode"] == "600125"


@pytest.mark.skip(reason="TEMPORARILY DISABLED — Chennai area seeding paused: the 600119 approval is commented out in app/schemas.py")
def test_approved_pincodes_work_for_loader_owners_and_agents(client):
    c, session = client
    if session.execute(text("SELECT COUNT(*) FROM postcodes WHERE pincode='600119'")).scalar():
        pytest.skip("600119 already exists in this test DB")
    name = f"Sholinganallur {uuid.uuid4().hex[:4]}"
    report = loc_svc.load_master(session, [loc_svc.MasterRow(name, "600119")])
    assert report["postcodes_created"] == ["600119"] and report["skipped"] == []
    # Owner listings accept the same pincode with the same locality...
    tok = register_phone(c, f"+916774{uuid.uuid4().int % 10**6:06d}")
    ho = auth_header(tok["access_token"])
    assert c.post("/users/me/role", json={"role": "OWNER"}, headers=ho).status_code == 200
    body = {"title": "OMR flat", "transaction_type": "RENT", "property_type": "APARTMENT",
            "price_rupees": 25000, "locality": name, "city": "Chennai"}
    r = c.post("/owner/listings", json={**body, "pincode": "600119"}, headers=ho)
    assert r.status_code == 201 and r.json()["pincode"] == "600119", r.text
    assert c.post("/owner/listings", json={**body, "pincode": "600125"}, headers=ho).status_code == 422
    # ...and an agent can select the approved locality within the plan.
    names = _unique_places(session, 2)
    h, _ = _starter(c, session, names, chosen=2)
    added = c.post("/broker/plan/localities", json={"localities": [name.lower()]}, headers=h)
    assert added.status_code == 200 and added.json()["added_localities"] == [name]
