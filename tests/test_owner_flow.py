"""Owner flow end to end: profile, listing lifecycle, ownership of
client-supplied ids, removal, and visit isolation.

Complements test_owner_lead_details.py (leads/names/counts/historical
leads) and test_visit_timezone.py (visit times). Rollback-isolated.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from tests.conftest import auth_header
from tests.test_owner_leads import fresh_buyer
from tests.test_property_lifecycle import _create, _owner

FUTURE = (datetime.now(timezone.utc) + timedelta(days=5)).replace(microsecond=0).isoformat()


# --- profile ------------------------------------------------------------------------

def test_owner_profile_read_and_update(client):
    c, _ = client
    token = _owner(c)
    h = auth_header(token)
    me = c.get("/auth/me", headers=h).json()
    assert me["role"] == "OWNER" and me["display_name"].startswith("+91")  # OTP default

    r = c.patch("/users/me", json={"display_name": "  Lakshmi Narayanan  "}, headers=h)
    assert r.status_code == 200
    assert r.json()["display_name"] == "Lakshmi Narayanan"
    assert r.json()["role"] == "OWNER"  # editing the name never changes the role
    assert c.get("/auth/me", headers=h).json()["display_name"] == "Lakshmi Narayanan"


def test_owner_profile_update_validation_and_auth(client):
    c, _ = client
    h = auth_header(_owner(c))
    assert c.patch("/users/me", json={"display_name": "   "}, headers=h).status_code == 422
    assert c.patch("/users/me", json={"display_name": "x" * 256}, headers=h).status_code == 422
    assert c.patch("/users/me", json={}, headers=h).status_code == 422
    assert c.patch("/users/me", json={"display_name": "Anon"}).status_code == 401


# --- listings -----------------------------------------------------------------------

def test_listing_create_read_edit_remove(client):
    c, session = client
    token = _owner(c)
    h = auth_header(token)
    created = _create(c, token, title="Flow Flat")
    lid = created["listing_id"]
    assert created["verification_status"] == "PENDING" and created["removed"] is False
    assert (created["enquiry_count"], created["visit_count"]) == (0, 0)

    assert c.get(f"/owner/listings/{lid}", headers=h).json()["title"] == "Flow Flat"
    r = c.patch(f"/owner/listings/{lid}", json={"title": "Flow Flat Renovated"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Flow Flat Renovated"
    assert r.json()["verification_status"] == "PENDING"  # editing never verifies

    assert c.delete(f"/owner/listings/{lid}", headers=h).status_code == 204
    row = [i for i in c.get("/owner/listings", headers=h).json()["items"] if i["listing_id"] == lid][0]
    assert row["removed"] is True
    assert session.execute(  # soft delete: the record is kept
        text("SELECT deleted_at IS NOT NULL FROM property_listings WHERE id=:i"), {"i": lid}
    ).fetchone()[0] == 1
    assert c.get(f"/buyer/listings/{lid}").status_code == 404


def test_client_supplied_owner_ids_are_ignored(client):
    c, session = client
    token_a = _owner(c)
    token_b = _owner(c)
    b_id = c.get("/auth/me", headers=auth_header(token_b)).json()["id"]
    a_id = c.get("/auth/me", headers=auth_header(token_a)).json()["id"]
    r = c.post(
        "/owner/listings",
        json={
            "title": "Spoofed owner", "transaction_type": "BUY", "property_type": "APARTMENT",
            "price_rupees": 100000, "locality": "Spoof Nagar", "city": "Chennai", "pincode": "600080",
            "user_id": b_id, "owner_id": b_id, "owner_user_id": b_id,
        },
        headers=auth_header(token_a),
    )
    assert r.status_code == 201, r.text
    owner = session.execute(
        text("SELECT user_id FROM property_listings WHERE id=:i"), {"i": r.json()["listing_id"]}
    ).fetchone()[0]
    assert owner == a_id
    assert r.json()["listing_id"] not in [
        i["listing_id"] for i in c.get("/owner/listings", headers=auth_header(token_b)).json()["items"]
    ]


def test_owner_cannot_touch_another_owners_listing(client):
    c, _ = client
    token_a, token_b = _owner(c), _owner(c)
    lid = _create(c, token_a)["listing_id"]
    hb = auth_header(token_b)
    assert c.get(f"/owner/listings/{lid}", headers=hb).status_code == 404
    assert c.patch(f"/owner/listings/{lid}", json={"title": "Hijack"}, headers=hb).status_code == 404
    assert c.delete(f"/owner/listings/{lid}", headers=hb).status_code == 404
    assert c.post(f"/owner/listings/{lid}/archive", headers=hb).status_code == 404
    assert c.get(f"/owner/listings/{lid}", headers=auth_header(token_a)).json()["title"] != "Hijack"


# --- visits -------------------------------------------------------------------------

def _visit_for(c, session, token_owner):
    """A REQUESTED visit on a VERIFIED listing of token_owner's."""
    lid = _create(c, token_owner)["listing_id"]
    session.execute(text("UPDATE property_listings SET verification_status='VERIFIED' WHERE id=:i"), {"i": lid})
    buyer = fresh_buyer(c)
    r = c.post("/buyer/visits", json={"property_listing_id": lid, "scheduled_at": FUTURE},
               headers=auth_header(buyer["access_token"]))
    assert r.status_code == 201, r.text
    return r.json()["id"], lid


def test_visits_are_isolated_between_owners(client):
    c, session = client
    token_a, token_b = _owner(c), _owner(c)
    vid, lid = _visit_for(c, session, token_a)
    ha, hb = auth_header(token_a), auth_header(token_b)

    assert [v["id"] for v in c.get("/owner/visits", headers=ha).json()["items"]] == [vid]
    assert c.get("/owner/visits", headers=hb).json()["total"] == 0
    assert c.get(f"/owner/visits/{vid}", headers=hb).status_code == 404
    assert c.patch(f"/owner/visits/{vid}", json={"status": "CONFIRMED"}, headers=hb).status_code == 404
    # A foreign property filter fails closed (404, never an empty page).
    assert c.get(f"/owner/visits?property_listing_id={lid}", headers=hb).status_code == 404
    assert c.get(f"/owner/visits/{vid}", headers=ha).json()["status"] == "REQUESTED"
    card = c.get(f"/owner/listings/{lid}", headers=ha).json()
    assert card["visit_count"] == 1


def test_owner_visit_endpoints_reject_anonymous_and_buyers(client):
    c, session = client
    vid, _ = _visit_for(c, session, _owner(c))
    buyer = fresh_buyer(c)
    hb = auth_header(buyer["access_token"])
    for method, path in (("get", "/owner/visits"), ("get", f"/owner/visits/{vid}")):
        assert getattr(c, method)(path).status_code == 401
        assert getattr(c, method)(path, headers=hb).status_code == 403
    assert c.patch(f"/owner/visits/{vid}", json={"status": "CONFIRMED"}).status_code == 401
    assert c.patch(f"/owner/visits/{vid}", json={"status": "CONFIRMED"}, headers=hb).status_code == 403
