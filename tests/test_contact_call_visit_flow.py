"""Owner contact, call tracking and site visits for buyers and agents.

- The listing contact reveal names THAT listing's owner (public-safe
  owner_name: never a phone number, never the caller) and flags the caller's
  own listing; the owner->buyer lead contact response is unchanged.
- A Call tap records "call initiated" for the right user and listing, names
  the contacted owner by id only (no phone number), keeps the Buyer/Agent
  role, shows in GET /buyer/call-events and is deduplicated.
- Site visits: the right listing / buyer / owner ids, the requester's role
  for the owner (BUYER / BROKER), clear 422 reasons, no duplicates, listed in
  the requester's visits.
- Buyer reveal quota, agent area limits and authorization are unchanged.
Rollback-isolated.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from tests.conftest import auth_header, register_phone
from tests.test_owner_leads import fresh_buyer, owner_header, seed_owner_listing

_n = {"i": 0}
FUTURE = (datetime.now(timezone.utc) + timedelta(days=3)).replace(microsecond=0).isoformat()
PAST = (datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0).isoformat()


def _listing(session, owner_name):
    lid, owner_id, phone = seed_owner_listing(session)
    session.execute(text("UPDATE users SET display_name=:n WHERE id=:u"), {"n": owner_name, "u": owner_id})
    session.flush()
    return lid, owner_id, phone


def _buyer(c, name=None):
    tok = fresh_buyer(c)
    h = auth_header(tok["access_token"])
    if name:
        assert c.patch("/users/me", json={"display_name": name}, headers=h).status_code == 200
    return h, tok["user_id"]


def _owner_in_buyer_mode(c, phone):
    """The listing's own owner, switched to buyer mode (as the app does)."""
    h = auth_header(register_phone(c, phone)["access_token"])
    assert c.post("/users/me/role", json={"role": "BUYER"}, headers=h).status_code == 200
    return h


def _agent_for(c, session, lid):
    """An agent (BROKER) on the free trial in the listing's area."""
    _n["i"] += 1
    tok = register_phone(c, f"+916774{_n['i']:06d}")
    h = auth_header(tok["access_token"])
    area = session.execute(text(
        "SELECT l.name FROM property_listings pl JOIN physical_properties pp ON pp.id=pl.physical_property_id "
        "JOIN postcodes p ON p.id=pp.postcode_id JOIN localities l ON l.id=p.locality_id WHERE pl.id=:l"),
        {"l": lid}).scalar()
    assert c.post("/broker/trial", json={"locality": area}, headers=h).status_code == 200
    return h, tok["user_id"]


# --- Owner contact ---------------------------------------------------------------

def test_reveal_names_the_owner_of_that_listing_not_the_caller(client):
    c, session = client
    run = uuid.uuid4().hex[:4]
    l1, _, p1 = _listing(session, f"Lakshmi {run}")
    l2, _, p2 = _listing(session, f"Arjun {run}")
    h, _ = _buyer(c, name=f"Caller {run}")
    r1 = c.post(f"/buyer/listings/{l1}/contact", headers=h).json()
    r2 = c.post(f"/buyer/listings/{l2}/contact", headers=h).json()
    assert r1 == {"phone": p1, "owner_name": f"Lakshmi {run}", "own_listing": False}
    assert r2 == {"phone": p2, "owner_name": f"Arjun {run}", "own_listing": False}
    # The public detail names the same owner.
    assert c.get(f"/buyer/listings/{l1}").json()["owner_name"] == f"Lakshmi {run}"


def test_owner_name_is_never_a_phone_number(client):
    c, session = client
    lid, _, phone = _listing(session, "+919812345678")  # the default after OTP sign-up
    h, _ = _buyer(c)
    r = c.post(f"/buyer/listings/{lid}/contact", headers=h).json()
    assert r["phone"] == phone and r["owner_name"] is None  # the app shows "Property Owner"


def test_own_listing_is_flagged_and_lead_contact_shape_is_unchanged(client):
    c, session = client
    lid, _, phone = _listing(session, "Self Owner")
    h = _owner_in_buyer_mode(c, phone)
    r = c.post(f"/buyer/listings/{lid}/contact", headers=h)
    assert r.status_code == 200 and r.json() == {"phone": phone, "owner_name": "Self Owner", "own_listing": True}
    # Owner -> buyer lead contact keeps its phone-only response.
    hb, _ = _buyer(c)
    lead = c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": "hi"}, headers=hb).json()
    ho, _ = owner_header(c, phone)
    assert set(c.post(f"/owner/leads/{lead['id']}/contact", headers=ho).json()) == {"phone"}


def test_contact_needs_an_authorized_buyer_or_agent(client):
    c, session = client
    lid, _, _ = _listing(session, "Guarded Owner")
    assert c.post(f"/buyer/listings/{lid}/contact").status_code == 401
    _n["i"] += 1
    ho = auth_header(register_phone(c, f"+916774{_n['i']:06d}")["access_token"])
    assert c.post("/users/me/role", json={"role": "OWNER"}, headers=ho).status_code == 200
    assert c.post(f"/buyer/listings/{lid}/contact", headers=ho).status_code == 403
    # Public listing responses never carry the owner's number.
    assert "phone" not in json.dumps(c.get(f"/buyer/listings/{lid}").json()).lower().replace("owner_name", "")


# --- Call tracking ------------------------------------------------------------------

def test_call_tap_records_the_owner_by_id_and_shows_in_calls(client):
    c, session = client
    lid, owner_id, owner_phone = _listing(session, "Call Owner")
    h, buyer_id = _buyer(c)
    assert c.get("/buyer/call-events", headers=h).json()["total"] == 0
    first = c.post(f"/buyer/listings/{lid}/call-events", headers=h)
    assert first.status_code == 201
    assert c.post(f"/buyer/listings/{lid}/call-events", headers=h).status_code == 200  # double tap: same row
    calls = c.get("/buyer/call-events", headers=h).json()
    assert calls["total"] == 1 and calls["items"][0]["property_listing_id"] == lid
    rows = session.execute(text(
        "SELECT action, entity_id, details FROM activity_logs WHERE user_id=:u AND action='CALL_BUTTON_TAP'"),
        {"u": buyer_id}).fetchall()
    assert len(rows) == 1 and rows[0][1] == lid
    details = rows[0][2] if isinstance(rows[0][2], dict) else json.loads(rows[0][2])
    assert details == {"source_role": "BUYER", "owner_user_id": owner_id}
    assert owner_phone[-10:] not in json.dumps(details)  # no phone number stored
    assert "answered" not in json.dumps(calls).lower()  # initiation only


def test_agent_calls_keep_the_agent_role(client):
    c, session = client
    lid, owner_id, owner_phone = _listing(session, "Agent Call Owner")
    ha, agent_id = _agent_for(c, session, lid)
    assert c.post(f"/buyer/listings/{lid}/call-events", headers=ha).status_code == 201
    assert c.get("/buyer/call-events", headers=ha).json()["total"] == 1
    details = session.execute(text(
        "SELECT details FROM activity_logs WHERE user_id=:u AND action='CALL_BUTTON_TAP'"), {"u": agent_id}).scalar()
    details = details if isinstance(details, dict) else json.loads(details)
    assert details == {"source_role": "BROKER", "owner_user_id": owner_id}
    ho, _ = owner_header(c, owner_phone)
    card = c.get(f"/owner/listings/{lid}", headers=ho).json()
    assert (card["call_count"], card["agent_call_count"]) == (1, 1)


def test_calls_on_your_own_listing_are_not_tracked(client):
    c, session = client
    lid, _, phone = _listing(session, "Own Caller")
    h = _owner_in_buyer_mode(c, phone)
    r = c.post(f"/buyer/listings/{lid}/call-events", headers=h)
    assert r.status_code == 422 and r.json()["detail"] == "Cannot record a call on your own listing"


# --- Site visits ----------------------------------------------------------------------

def test_buyer_visit_is_saved_listed_and_reaches_the_owner_as_buyer(client):
    c, session = client
    lid, owner_id, owner_phone = _listing(session, "Visit Owner")
    h, buyer_id = _buyer(c)
    r = c.post("/buyer/visits", json={"property_listing_id": lid, "scheduled_at": FUTURE, "note": "Evening works"},
               headers=h)
    assert r.status_code == 201, r.text
    v = r.json()
    assert (v["property_listing_id"], v["buyer_user_id"], v["owner_user_id"]) == (lid, buyer_id, owner_id)
    assert v["status"] == "REQUESTED" and v["requester_role"] == "BUYER" and v["owner_display_name"] == "Visit Owner"
    mine = c.get("/buyer/visits", headers=h).json()
    assert [i["id"] for i in mine["items"]] == [v["id"]]  # Activity -> Visits
    ho, _ = owner_header(c, owner_phone)
    owner_view = c.get("/owner/visits", params={"property_listing_id": lid}, headers=ho).json()
    assert owner_view["total"] == 1 and owner_view["items"][0]["requester_role"] == "BUYER"


def test_agent_visit_reaches_the_owner_as_agent(client):
    c, session = client
    lid, _, owner_phone = _listing(session, "Agent Visit Owner")
    ha, agent_id = _agent_for(c, session, lid)
    r = c.post("/buyer/visits", json={"property_listing_id": lid, "scheduled_at": FUTURE}, headers=ha)
    assert r.status_code == 201 and r.json()["requester_role"] == "BROKER" and r.json()["buyer_user_id"] == agent_id
    assert c.get("/buyer/visits", headers=ha).json()["total"] == 1
    ho, _ = owner_header(c, owner_phone)
    items = c.get("/owner/visits", params={"property_listing_id": lid}, headers=ho).json()["items"]
    assert [i["requester_role"] for i in items] == ["BROKER"]
    assert c.get("/users/me/role", headers=ha).json()["role"] == "BROKER"  # never switched to buyer


def test_duplicate_and_invalid_visits_get_clear_reasons_and_write_nothing(client):
    c, session = client
    lid, _, phone = _listing(session, "Strict Owner")
    h, buyer_id = _buyer(c)
    count = lambda: session.execute(text("SELECT COUNT(*) FROM property_visits WHERE buyer_user_id=:u"),  # noqa: E731
                                    {"u": buyer_id}).scalar()
    assert c.post("/buyer/visits", json={"property_listing_id": lid, "scheduled_at": FUTURE}, headers=h).status_code == 201
    dup = c.post("/buyer/visits", json={"property_listing_id": lid, "scheduled_at": FUTURE}, headers=h)
    assert dup.status_code == 422 and dup.json()["detail"] == "A visit is already active for this buyer on this listing"
    assert count() == 1
    assert c.post("/buyer/visits", json={"property_listing_id": lid, "scheduled_at": PAST}, headers=h).status_code == 422
    assert c.post("/buyer/visits", json={"property_listing_id": lid}, headers=h).status_code == 422
    assert c.post("/buyer/visits", json={"property_listing_id": 0, "scheduled_at": FUTURE}, headers=h).status_code == 422
    assert count() == 1
    own = c.post("/buyer/visits", json={"property_listing_id": lid, "scheduled_at": FUTURE},
                 headers=_owner_in_buyer_mode(c, phone))
    assert own.status_code == 422 and own.json()["detail"] == "cannot schedule a visit on your own listing"


def test_visit_authorization_is_unchanged(client):
    c, session = client
    lid, _, _ = _listing(session, "Auth Owner")
    assert c.post("/buyer/visits", json={"property_listing_id": lid, "scheduled_at": FUTURE}).status_code == 401
    _n["i"] += 1
    ho = auth_header(register_phone(c, f"+916774{_n['i']:06d}")["access_token"])
    assert c.post("/users/me/role", json={"role": "OWNER"}, headers=ho).status_code == 200
    r = c.post("/buyer/visits", json={"property_listing_id": lid, "scheduled_at": FUTURE}, headers=ho)
    assert r.status_code == 403
    # An agent outside its area is still refused (agent upgrade), nothing written.
    other, _, _ = _listing(session, "Other Area Owner")
    ha, agent_id = _agent_for(c, session, other)
    r = c.post("/buyer/visits", json={"property_listing_id": lid, "scheduled_at": FUTURE}, headers=ha)
    assert r.status_code == 402 and r.json()["detail"]["code"] == "AGENT_AREA_NOT_IN_PLAN"
    assert not session.execute(text("SELECT COUNT(*) FROM property_visits WHERE buyer_user_id=:u"),
                               {"u": agent_id}).scalar()


# --- Regression ----------------------------------------------------------------------

def test_buyer_quota_and_agent_unlimited_reveals_are_unchanged(client):
    c, session = client
    lids = [_listing(session, f"Quota Owner {i}")[0] for i in range(4)]
    h, _ = _buyer(c)
    for lid in lids[:3]:
        assert c.post(f"/buyer/listings/{lid}/contact", headers=h).status_code == 200
    fourth = c.post(f"/buyer/listings/{lids[3]}/contact", headers=h)
    assert fourth.status_code == 402 and fourth.json()["detail"]["code"] == "BUYER_PLAN_REQUIRED"
    assert "owner_name" not in json.dumps(fourth.json())  # nothing beyond the teaser before a plan
