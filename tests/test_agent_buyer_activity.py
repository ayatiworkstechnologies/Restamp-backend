"""Agents share the buyer activity flow; owners see who sent each lead.

- An agent (current role BROKER) uses the same /buyer activity endpoints as a
  buyer: wishlist, viewed, call taps, enquiries, visits, owner contact. All of
  it is stored on the agent's own account, and the agent's role is untouched.
- Each new enquiry records the sender's role; owner leads expose it as
  source_role (BUYER / BROKER). Enquiries from before the column existed
  (NULL) read as BUYER: only BUYER-mode accounts could enquire then.
- OWNER-mode accounts are still refused ("BUYER role required"), buyer plans
  stay buyer-only, and buyers and agents never see each other's activity.
Rollback-isolated.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from tests.conftest import auth_header, register_phone
from tests.test_owner_leads import fresh_buyer, owner_header, seed_owner_listing

_n = {"i": 0}
FUTURE = (datetime.now(timezone.utc) + timedelta(days=3)).replace(microsecond=0).isoformat()


def _agent(c, session=None, lid=None):
    """An agent (BROKER). With a listing, the agent's free-trial area is that
    listing's locality, so the agent may act on it (agents are area-limited)."""
    _n["i"] += 1
    tok = register_phone(c, f"+916777{_n['i']:06d}")
    h = auth_header(tok["access_token"])
    if lid is None:
        assert c.post("/users/me/role", json={"role": "BROKER"}, headers=h).status_code == 200
    else:
        _start_trial_for(c, session, h, lid)
    return h, tok["user_id"]


def _start_trial_for(c, session, h, lid):
    area = session.execute(text(
        "SELECT l.name FROM property_listings pl JOIN physical_properties pp ON pp.id=pl.physical_property_id "
        "JOIN postcodes p ON p.id=pp.postcode_id JOIN localities l ON l.id=p.locality_id WHERE pl.id=:l"),
        {"l": lid}).scalar()
    assert c.post("/broker/trial", json={"locality": area}, headers=h).status_code == 200


def _role(c, h):
    return c.get("/users/me/role", headers=h).json()["role"]


def test_agent_uses_the_buyer_activity_endpoints_on_its_own_account(client):
    c, session = client
    lid, _, _ = seed_owner_listing(session)
    h, agent_id = _agent(c, session, lid)
    assert c.post(f"/buyer/saved/{lid}", headers=h).status_code == 201
    assert [i["listing_id"] for i in c.get("/buyer/saved", headers=h).json()["items"]] == [lid]
    assert c.post(f"/buyer/viewed/{lid}", headers=h).status_code == 201
    assert c.get("/buyer/viewed", headers=h).json()["total"] == 1
    assert c.post(f"/buyer/listings/{lid}/call-events", headers=h).status_code == 201
    assert c.get("/buyer/call-events", headers=h).json()["total"] == 1
    e = c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": "Client wants a visit"}, headers=h)
    assert e.status_code == 201
    assert c.get("/buyer/enquiries", headers=h).json()["total"] == 1
    assert c.get(f"/buyer/enquiries/{e.json()['id']}", headers=h).status_code == 200
    v = c.post("/buyer/visits", json={"property_listing_id": lid, "scheduled_at": FUTURE}, headers=h)
    assert v.status_code == 201
    assert c.get("/buyer/visits", headers=h).json()["total"] == 1
    assert c.patch(f"/buyer/visits/{v.json()['id']}", json={"status": "CANCELLED"}, headers=h).status_code == 200
    assert c.delete(f"/buyer/saved/{lid}", headers=h).status_code == 204
    # Stored on the agent's own account; the agent stays an agent.
    assert session.execute(text("SELECT buyer_user_id FROM enquiries WHERE id=:i"), {"i": e.json()["id"]}).scalar() == agent_id
    assert _role(c, h) == "BROKER"


def test_owner_sees_whether_a_buyer_or_an_agent_sent_the_lead(client):
    c, session = client
    lid, _, phone = seed_owner_listing(session)
    hb = auth_header(fresh_buyer(c)["access_token"])
    ha, _ = _agent(c, session, lid)
    buyer_lead = c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": "Buyer here"}, headers=hb).json()
    agent_lead = c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": "Agent here"}, headers=ha).json()
    ho, _ = owner_header(c, phone)
    leads = {l["id"]: l["source_role"] for l in c.get("/owner/leads", headers=ho).json()["items"]}
    assert leads == {buyer_lead["id"]: "BUYER", agent_lead["id"]: "BROKER"}
    assert c.get(f"/owner/leads/{agent_lead['id']}", headers=ho).json()["source_role"] == "BROKER"
    assert c.get(f"/owner/leads/{buyer_lead['id']}", headers=ho).json()["source_role"] == "BUYER"


def test_leads_from_before_the_column_read_as_buyer(client):
    c, session = client
    lid, _, phone = seed_owner_listing(session)
    hb = auth_header(fresh_buyer(c)["access_token"])
    eid = c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": "Old lead"}, headers=hb).json()["id"]
    session.execute(text("UPDATE enquiries SET source_role=NULL WHERE id=:i"), {"i": eid})
    session.flush()
    ho, _ = owner_header(c, phone)
    assert c.get(f"/owner/leads/{eid}", headers=ho).json()["source_role"] == "BUYER"


def test_a_reused_lead_keeps_the_role_it_was_created_with(client):
    c, session = client
    lid, _, phone = seed_owner_listing(session)
    tok = fresh_buyer(c)
    hb = auth_header(tok["access_token"])
    eid = c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": "First"}, headers=hb).json()["id"]
    _start_trial_for(c, session, hb, lid)  # becomes an agent covering this area
    again = c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": "Follow-up"}, headers=hb)
    assert again.status_code == 200 and again.json()["id"] == eid  # appended to the same lead
    ho, _ = owner_header(c, phone)
    assert c.get(f"/owner/leads/{eid}", headers=ho).json()["source_role"] == "BUYER"


def test_owner_mode_and_buyer_plans_stay_refused(client):
    c, session = client
    lid, _, _ = seed_owner_listing(session)
    _n["i"] += 1
    tok = register_phone(c, f"+916777{_n['i']:06d}")
    ho = auth_header(tok["access_token"])
    assert c.post("/users/me/role", json={"role": "OWNER"}, headers=ho).status_code == 200
    for method, path, body in (("GET", "/buyer/saved", None), ("POST", "/buyer/enquiries", {"property_listing_id": lid}),
                               ("GET", "/buyer/visits", None), ("GET", "/buyer/viewed", None),
                               ("GET", "/buyer/call-events", None)):
        r = c.request(method, path, json=body, headers=ho)
        assert r.status_code == 403 and r.json()["detail"] == "BUYER role required", path
    ha, _ = _agent(c, session, lid)
    assert c.get("/buyer/plan", headers=ha).status_code == 403  # buyer plans remain buyer-only


def test_buyers_and_agents_never_see_each_others_activity(client):
    c, session = client
    lid, _, _ = seed_owner_listing(session)
    hb = auth_header(fresh_buyer(c)["access_token"])
    ha, _ = _agent(c, session, lid)
    assert c.post(f"/buyer/saved/{lid}", headers=hb).status_code == 201
    c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": "Buyer only"}, headers=hb)
    assert c.get("/buyer/saved", headers=ha).json()["total"] == 0
    assert c.get("/buyer/enquiries", headers=ha).json()["total"] == 0
    assert c.post(f"/buyer/viewed/{lid}", headers=ha).status_code == 201
    assert c.get("/buyer/viewed", headers=hb).json()["total"] == 0


def test_agent_calls_are_tracked_and_owners_see_how_many_came_from_agents(client):
    c, session = client
    lid, _, phone = seed_owner_listing(session)
    ha, agent_id = _agent(c, session, lid)
    hb = auth_header(fresh_buyer(c)["access_token"])
    assert c.post(f"/buyer/listings/{lid}/call-events", headers=ha).status_code == 201
    assert c.post(f"/buyer/listings/{lid}/call-events", headers=ha).status_code == 200  # double tap: no new row
    assert c.post(f"/buyer/listings/{lid}/call-events", headers=hb).status_code == 201
    # The agent sees its own call in My Activity -> Calls; the buyer sees only its own.
    assert [e["property_listing_id"] for e in c.get("/buyer/call-events", headers=ha).json()["items"]] == [lid]
    assert c.get("/buyer/call-events", headers=hb).json()["total"] == 1
    assert _role(c, ha) == "BROKER"
    ho, _ = owner_header(c, phone)
    card = c.get(f"/owner/listings/{lid}", headers=ho).json()
    assert (card["call_count"], card["agent_call_count"]) == (2, 1)
    row = [i for i in c.get("/owner/listings", headers=ho).json()["items"] if i["listing_id"] == lid][0]
    assert (row["call_count"], row["agent_call_count"]) == (2, 1)
    # Taps recorded before the role was stored (details NULL) count as buyer taps.
    session.execute(text("UPDATE activity_logs SET details=NULL WHERE user_id=:u AND action='CALL_BUTTON_TAP'"),
                    {"u": agent_id})
    session.flush()
    assert c.get(f"/owner/listings/{lid}", headers=ho).json()["agent_call_count"] == 0


def test_a_lead_created_by_revealing_the_number_keeps_the_agent_role(client):
    """Regression: the contact reveal creates the lead itself (P3 leads MVP);
    it must record the revealer's role too, not default to BUYER."""
    c, session = client
    lid, _, phone = seed_owner_listing(session)
    ha, _ = _agent(c, session, lid)
    assert c.post(f"/buyer/listings/{lid}/contact", headers=ha).status_code == 200
    again = c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": "Follow-up"}, headers=ha)
    assert again.status_code == 200  # the reveal already made the lead
    ho, _ = owner_header(c, phone)
    leads = [l for l in c.get("/owner/leads", headers=ho).json()["items"] if l["property_listing_id"] == lid]
    assert [l["source_role"] for l in leads] == ["BROKER"]
    hb = auth_header(fresh_buyer(c)["access_token"])
    assert c.post(f"/buyer/listings/{lid}/contact", headers=hb).status_code == 200
    roles = sorted(l["source_role"] for l in c.get("/owner/leads", headers=ho).json()["items"] if l["property_listing_id"] == lid)
    assert roles == ["BROKER", "BUYER"]
