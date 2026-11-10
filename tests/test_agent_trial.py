"""Agent free trial: one area, unlimited owner numbers, enforced by the backend.

- POST /broker/trial starts the trial with ONE area (no payment, no expiry),
  once per account; it survives re-login (server-saved) and cannot be
  restarted to hop to another area. A second area needs a paid plan.
- Agents (BROKER) can act only on listings inside their covered areas:
  reveal, enquiry, visit, call tap, save, viewed and the agent detail /
  similar endpoints answer 402 AGENT_AREA_NOT_IN_PLAN outside them, and
  /broker/listings never leaks another area whatever the filters.
- Agents have no owner-number limit; buyers keep 3 free reveals.
- Upgrading to a paid plan replaces the trial and its area limit.
Rollback-isolated.
"""
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import bindparam, text

from tests.conftest import auth_header, make_postcode, register_phone
from tests.test_owner_leads import owner_header

_n = {"i": 0}
FUTURE = (datetime.now(timezone.utc) + timedelta(days=3)).replace(microsecond=0).isoformat()
AREA_CODE = "AGENT_AREA_NOT_IN_PLAN"


def _phone():
    _n["i"] += 1
    return f"+916778{_n['i']:06d}"


def _area(session):
    """A fresh locality with one postcode; returns (name, postcode_id)."""
    pid = make_postcode(session, tag=f"TR{uuid.uuid4().hex[:6]}")
    name = session.execute(text(
        "SELECT l.name FROM postcodes p JOIN localities l ON l.id=p.locality_id WHERE p.id=:p"), {"p": pid}).scalar()
    return name, pid


def _owner_with_listings(session, postcode_id, count, title):
    """An owner with a phone identity and `count` live listings in one area."""
    phone = _phone()
    session.execute(text("INSERT INTO users (display_name) VALUES ('Trial Owner')"))
    owner = session.execute(text("SELECT LAST_INSERT_ID()")).scalar()
    session.execute(text("INSERT INTO auth_identities (user_id, provider, provider_identifier) VALUES (:u, 'phone', :p)"),
                    {"u": owner, "p": phone})
    ids = []
    for i in range(count):
        session.execute(text("INSERT INTO physical_properties (postcode_id, user_id) VALUES (:p, :u)"),
                        {"p": postcode_id, "u": owner})
        pp = session.execute(text("SELECT LAST_INSERT_ID()")).scalar()
        session.execute(text(
            "INSERT INTO property_listings (physical_property_id, user_id, title, price_paise, price_period, "
            "verification_status, listing_status, transaction_type, property_type) "
            "VALUES (:pp, :u, :t, 500000, 'TOTAL', 'VERIFIED', 'AVAILABLE', 'BUY', 'APARTMENT')"),
            {"pp": pp, "u": owner, "t": f"{title} {i}"})
        ids.append(session.execute(text("SELECT LAST_INSERT_ID()")).scalar())
    session.flush()
    return ids, phone


def _user(c, role="BUYER"):
    phone = _phone()
    tok = register_phone(c, phone)
    h = auth_header(tok["access_token"])
    if role != "BUYER":
        assert c.post("/users/me/role", json={"role": role}, headers=h).status_code == 200
    return h, tok["user_id"], phone


def _setup(c, session):
    a_name, a_pc = _area(session)
    b_name, b_pc = _area(session)
    a_ids, a_phone = _owner_with_listings(session, a_pc, 5, f"Alpha {uuid.uuid4().hex[:4]}")
    b_ids, _ = _owner_with_listings(session, b_pc, 2, f"Beta {uuid.uuid4().hex[:4]}")
    h, uid, phone = _user(c)
    r = c.post("/broker/trial", json={"locality": a_name}, headers=h)
    assert r.status_code == 200, r.text
    return {"h": h, "uid": uid, "phone": phone, "a": a_name, "b": b_name, "a_ids": a_ids, "b_ids": b_ids,
            "a_owner_phone": a_phone, "start": r.json()}


def _area_refused(r):
    return r.status_code == 402 and r.json()["detail"]["code"] == AREA_CODE


def test_trial_starts_with_one_area_and_survives_relogin(client):
    c, session = client
    s = _setup(c, session)
    plan = s["start"]
    assert plan["has_plan"] is True and plan["is_trial"] is True and plan["plan_id"] == "agent-trial"
    assert plan["locality_limit"] == 1 and plan["localities"] == [s["a"]] and plan["valid_until"] is None
    assert c.get("/users/me/role", headers=s["h"]).json()["role"] == "BROKER"
    pay = session.execute(text("SELECT provider, amount_paise FROM payments WHERE user_id=:u"), {"u": s["uid"]}).fetchall()
    assert [tuple(p) for p in pay] == [("FREE_TRIAL", 0)]  # records the activation; no money
    # A new login for the same account keeps the area (server-saved).
    h2 = auth_header(register_phone(c, s["phone"])["access_token"])
    again = c.get("/broker/plan", headers=h2).json()
    assert again["is_trial"] is True and again["localities"] == [s["a"]]


def test_trial_cannot_be_restarted_or_widened(client):
    c, session = client
    s = _setup(c, session)
    r = c.post("/broker/trial", json={"locality": s["b"]}, headers=s["h"])
    assert r.status_code == 409  # no hopping to another area
    r = c.post("/broker/plan/localities", json={"localities": [s["b"]]}, headers=s["h"])
    assert r.status_code == 422 and r.json()["detail"] == "Agent Free Trial covers up to 1 localities."
    assert c.get("/broker/plan", headers=s["h"]).json()["localities"] == [s["a"]]
    # Upgrading replaces the trial; the trial can never be started again.
    up = c.post("/broker/plans/agent-starter/subscribe", json={"localities": [s["a"], s["b"]]}, headers=s["h"])
    assert up.status_code == 200 and up.json()["is_trial"] is False
    assert sorted(up.json()["localities"]) == sorted([s["a"], s["b"]])
    session.execute(text("UPDATE subscriptions SET end_date=:e WHERE user_id=:u AND status='ACTIVE'"),
                    {"e": datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1), "u": s["uid"]})
    session.flush()
    r = c.post("/broker/trial", json={"locality": s["a"]}, headers=s["h"])
    assert r.status_code == 409 and r.json()["detail"] == "Free trial already used"


def test_trial_area_must_be_a_served_locality(client):
    c, _ = client
    h, _, _ = _user(c)
    r = c.post("/broker/trial", json={"locality": f"Nowhere {uuid.uuid4().hex}"}, headers=h)
    assert r.status_code == 422
    assert c.post("/broker/trial", json={"locality": ""}, headers=h).status_code == 422
    assert c.post("/broker/trial", json={"locality": "x"}).status_code == 401


def test_browsing_never_leaks_another_area(client):
    c, session = client
    s = _setup(c, session)
    titles = lambda r: {i["listing_id"] for i in r.json()["items"]}  # noqa: E731
    base = c.get("/broker/listings", params={"page_size": 100}, headers=s["h"])
    assert titles(base) == set(s["a_ids"])
    for params in ({"locality": s["b"]}, {"q": "Beta"}, {"q": s["b"]}, {"city": "Testville"}):
        r = c.get("/broker/listings", params={**params, "page_size": 100}, headers=s["h"])
        assert titles(r) <= set(s["a_ids"]) and not titles(r) & set(s["b_ids"]), params
    assert c.get(f"/broker/listings/{s['a_ids'][0]}", headers=s["h"]).status_code == 200
    r = c.get(f"/broker/listings/{s['b_ids'][0]}", headers=s["h"])
    assert _area_refused(r) and r.json()["detail"]["is_trial"] is True
    assert "one area" in r.json()["detail"]["message"]
    similar = c.get(f"/broker/listings/{s['a_ids'][0]}/similar", headers=s["h"]).json()
    assert {x["listing_id"] for x in similar} <= set(s["a_ids"])
    assert _area_refused(c.get(f"/broker/listings/{s['b_ids'][0]}/similar", headers=s["h"]))


def test_agent_actions_outside_the_area_are_refused_and_write_nothing(client):
    c, session = client
    s = _setup(c, session)
    lid, h = s["b_ids"][0], s["h"]
    counts = lambda: tuple(session.execute(text(f"SELECT COUNT(*) FROM {t} WHERE {col}=:u"), {"u": s["uid"]}).scalar()  # noqa: E731
                           for t, col in (("enquiries", "buyer_user_id"), ("property_visits", "buyer_user_id"),
                                          ("saved_properties", "buyer_user_id"), ("contact_reveal_audits", "user_id"),
                                          ("activity_logs", "user_id")))
    before = counts()
    calls = (("POST", f"/buyer/listings/{lid}/contact", None),
             ("POST", "/buyer/enquiries", {"property_listing_id": lid, "message": "hi"}),
             ("POST", "/buyer/visits", {"property_listing_id": lid, "scheduled_at": FUTURE}),
             ("POST", f"/buyer/listings/{lid}/call-events", None),
             ("POST", f"/buyer/saved/{lid}", None),
             ("POST", f"/buyer/viewed/{lid}", None))
    for method, path, body in calls:
        r = c.request(method, path, json=body, headers=h)
        assert _area_refused(r), (path, r.status_code, r.text)
    assert counts() == before


def test_agents_have_unlimited_reveals_buyers_keep_three(client):
    c, session = client
    s = _setup(c, session)
    for lid in s["a_ids"]:  # 5 owner numbers, no plan prompt
        r = c.post(f"/buyer/listings/{lid}/contact", headers=s["h"])
        assert r.status_code == 200 and r.json()["phone"] == s["a_owner_phone"]
    hb, _, _ = _user(c)
    for lid in s["a_ids"][:3]:
        assert c.post(f"/buyer/listings/{lid}/contact", headers=hb).status_code == 200
    fourth = c.post(f"/buyer/listings/{s['a_ids'][3]}/contact", headers=hb)
    assert fourth.status_code == 402 and fourth.json()["detail"]["code"] == "BUYER_PLAN_REQUIRED"
    # Buyers are never area-restricted.
    assert c.post(f"/buyer/listings/{s['b_ids'][0]}/contact", headers=hb).status_code in (200, 402)
    assert c.post("/buyer/enquiries", json={"property_listing_id": s["b_ids"][0]}, headers=hb).status_code == 201


def test_agent_activity_inside_the_area_reaches_the_owner_as_agent(client):
    c, session = client
    s = _setup(c, session)
    lid, h = s["a_ids"][0], s["h"]
    assert c.post(f"/buyer/saved/{lid}", headers=h).status_code == 201
    assert c.post(f"/buyer/viewed/{lid}", headers=h).status_code == 201
    assert c.post(f"/buyer/listings/{lid}/call-events", headers=h).status_code == 201
    e = c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": "Client interested"}, headers=h)
    assert e.status_code == 201
    assert c.post("/buyer/visits", json={"property_listing_id": lid, "scheduled_at": FUTURE}, headers=h).status_code == 201
    ho, _ = owner_header(c, s["a_owner_phone"])
    lead = c.get(f"/owner/leads/{e.json()['id']}", headers=ho).json()
    assert lead["source_role"] == "BROKER"
    assert c.get("/owner/visits", params={"property_listing_id": lid}, headers=ho).json()["total"] == 1
    card = c.get(f"/owner/listings/{lid}", headers=ho).json()
    assert (card["call_count"], card["agent_call_count"]) == (1, 1)


def test_an_agent_without_any_plan_has_no_area(client):
    c, session = client
    s = _setup(c, session)
    h, _, _ = _user(c, role="BROKER")
    r = c.post(f"/buyer/listings/{s['a_ids'][0]}/contact", headers=h)
    assert _area_refused(r) and r.json()["detail"]["has_plan"] is False


# --- Area = canonical locality (regression: trial area showed zero properties) --

def _add_postcode_to_locality(session, postcode_id):
    """A second pincode for the SAME locality, as owner_listings adds one when
    an owner lists with a new pincode for a known locality (dev: Anna Nagar
    600093 granted, later 600002 added)."""
    from tests.conftest import _allocate_pincode
    loc = session.execute(text("SELECT locality_id FROM postcodes WHERE id=:p"), {"p": postcode_id}).scalar()
    session.execute(text("INSERT INTO postcodes (locality_id, pincode) VALUES (:l, :p)"),
                    {"l": loc, "p": _allocate_pincode(session)})
    session.flush()
    return session.execute(text("SELECT LAST_INSERT_ID()")).scalar()


def _ids(r):
    return {i["listing_id"] for i in r.json()["items"]}


def test_area_covers_postcodes_added_to_the_locality_after_the_trial_started(client):
    c, session = client
    s = _setup(c, session)
    a_pc = session.execute(text("SELECT pp.postcode_id FROM property_listings pl JOIN physical_properties pp "
                                "ON pp.id=pl.physical_property_id WHERE pl.id=:l"), {"l": s["a_ids"][0]}).scalar()
    later_ids, _ = _owner_with_listings(session, _add_postcode_to_locality(session, a_pc), 2, "Later pin")
    # The buyer catalog lists all of area A for the locality name ...
    buyer = c.get("/buyer/listings", params={"locality": s["a"], "page_size": 100})
    assert _ids(buyer) == set(s["a_ids"]) | set(later_ids)
    # ... and the agent sees exactly the same listings, B still excluded.
    r = c.get("/broker/listings", params={"page_size": 100}, headers=s["h"])
    assert _ids(r) == set(s["a_ids"]) | set(later_ids) and r.json()["total"] == 7
    assert c.get("/broker/listings", params={"count_only": True}, headers=s["h"]).json()["total"] == 7
    assert _ids(c.get("/broker/listings", params={"locality": s["a"], "page_size": 100}, headers=s["h"])) == _ids(buyer)
    lid = later_ids[0]
    assert c.get(f"/broker/listings/{lid}", headers=s["h"]).status_code == 200
    similar = c.get(f"/broker/listings/{lid}/similar", headers=s["h"]).json()
    assert {x["listing_id"] for x in similar} <= set(s["a_ids"]) | set(later_ids)
    assert c.post(f"/buyer/listings/{lid}/contact", headers=s["h"]).status_code == 200
    assert c.post(f"/buyer/saved/{lid}", headers=s["h"]).status_code == 201
    assert _area_refused(c.get(f"/broker/listings/{s['b_ids'][0]}", headers=s["h"]))
    # The plan still counts ONE locality; the grant rows are unchanged.
    plan = c.get("/broker/plan", headers=s["h"]).json()
    assert plan["localities"] == [s["a"]] and plan["locality_limit"] == 1


def test_bhk_budget_and_search_filters_stay_inside_the_area(client):
    c, session = client
    s = _setup(c, session)
    a2, a3 = s["a_ids"][0], s["a_ids"][1]
    session.execute(text("UPDATE physical_properties pp JOIN property_listings pl ON pl.physical_property_id=pp.id "
                         "SET pp.bedrooms=2 WHERE pl.id=:l"), {"l": a2})
    session.execute(text("UPDATE physical_properties pp JOIN property_listings pl ON pl.physical_property_id=pp.id "
                         "SET pp.bedrooms=3 WHERE pl.id IN :ids").bindparams(
                             bindparam("ids", expanding=True)),
                    {"ids": [a3, s["b_ids"][0]]})
    session.execute(text("UPDATE property_listings SET price_paise=9000000 WHERE id IN :ids").bindparams(
        bindparam("ids", expanding=True)), {"ids": [a3, s["b_ids"][1]]})
    session.flush()
    get = lambda **p: _ids(c.get("/broker/listings", params={**p, "page_size": 100}, headers=s["h"]))  # noqa: E731
    assert get(bedrooms=2) == {a2}
    assert get(bedrooms=3) == {a3}  # B's 3 BHK never shows
    assert get(min_price_paise=8000000) == {a3}  # B's pricey listing never shows
    assert get(max_price_paise=600000) == set(s["a_ids"]) - {a3}
    assert get(q=s["a"]) == set(s["a_ids"])
    assert get(bedrooms=3, q=s["b"]) == set()


def test_alias_of_the_trial_area_maps_to_the_canonical_locality(client, monkeypatch):
    from app.config import settings
    from app.services import localities as loc_svc
    from tests.conftest import fresh_pincode

    c, session = client
    name, alias = f"Canonpet {uuid.uuid4().hex[:5]}", f"Canonalias{uuid.uuid4().hex[:5]}"
    loc_svc.load_master(session, [loc_svc.MasterRow(name, fresh_pincode(c), (alias,))])
    session.flush()
    pc = session.execute(text("SELECT p.id FROM postcodes p JOIN localities l ON l.id=p.locality_id WHERE l.name=:n"),
                         {"n": name}).scalar()
    ids, _ = _owner_with_listings(session, pc, 2, "Canon")
    monkeypatch.setattr(settings, "AREA_SEEDING_ENABLED", True)  # aliases are paused by default
    h, _, _ = _user(c)
    r = c.post("/broker/trial", json={"locality": alias}, headers=h)
    assert r.status_code == 200 and r.json()["localities"] == [name]
    assert _ids(c.get("/broker/listings", params={"page_size": 100}, headers=h)) == set(ids)


def test_agent_switched_to_buyer_mode_must_return_to_agent_mode_to_browse(client):
    """Regression (dev: the trial agent's role was BUYER, so the agent
    endpoints answered 403 and the app showed zero properties). The role is
    required; coming back to BROKER needs no new grant and keeps the area."""
    c, session = client
    s = _setup(c, session)
    assert c.post("/users/me/role", json={"role": "BUYER"}, headers=s["h"]).status_code == 200
    r = c.get("/broker/listings", headers=s["h"])
    assert r.status_code == 403 and r.json()["detail"] == "BROKER role required"
    assert c.get("/broker/plan", headers=s["h"]).json()["localities"] == [s["a"]]  # plan still readable
    assert c.post("/users/me/role", json={"role": "BROKER"}, headers=s["h"]).status_code == 200
    assert _ids(c.get("/broker/listings", params={"page_size": 100}, headers=s["h"])) == set(s["a_ids"])
