"""Owner module: lead buyer names, per-listing activity counts, isolation.

Regression coverage for the Owner leads audit:
- leads on a removed (soft-deleted) listing stay visible to their owner
  with the real stored title/price and listing_removed=true; removed
  listings are flagged (removed=true) in the owner's own listing reads;
- GET /owner/leads (unfiltered) returns leads on ANY of the owner's listings,
  not just the newest one (the frontend Leads screen now relies on this);
- buyer_display_name carries a buyer-chosen name only — the phone-number
  default and phone-shaped names are withheld (same rule as the public
  owner name);
- OwnerListingOut.enquiry_count / visit_count reflect the owner's real
  persisted rows and never count another owner's activity;
- unauthenticated / wrong-role / foreign-owner access stays refused.

All rows live in the rollback-isolated client session.
"""
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from tests.conftest import auth_header
from tests.test_owner_leads import fresh_buyer, owner_header, seed_owner_listing

FUTURE = lambda days=7: (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _owner_of(session, lid):
    owner = session.execute(
        text("SELECT user_id FROM property_listings WHERE id=:l"), {"l": lid}
    ).fetchone()[0]
    phone = session.execute(
        text("SELECT provider_identifier FROM auth_identities WHERE user_id=:u AND provider='phone'"),
        {"u": owner},
    ).fetchone()[0]
    return owner, phone


def _second_listing(session, owner_id):
    """Another VERIFIED/AVAILABLE listing for the same owner (newer id)."""
    pp = session.execute(
        text("SELECT physical_property_id FROM property_listings WHERE user_id=:u LIMIT 1"),
        {"u": owner_id},
    ).fetchone()[0]
    session.execute(
        text(
            "INSERT INTO property_listings (physical_property_id, user_id, title, "
            "price_paise, price_period, verification_status, listing_status, transaction_type, "
            "property_type) VALUES (:pp, :u, 'OL2', 200, 'TOTAL', 'VERIFIED', 'AVAILABLE', "
            "'BUY', 'APARTMENT')"
        ),
        {"pp": pp, "u": owner_id},
    )
    session.flush()
    return session.execute(text("SELECT LAST_INSERT_ID()")).fetchone()[0]


def _enquire(c, buyer, lid, message="Is it available?"):
    r = c.post(
        "/buyer/enquiries",
        json={"property_listing_id": lid, "message": message},
        headers=auth_header(buyer["access_token"]),
    )
    assert r.status_code in (200, 201), r.text
    return r.json()


def _lead_for(body, lid):
    return [i for i in body["items"] if i["property_listing_id"] == lid]


# --- lead retrieval ---------------------------------------------------------------

def test_unfiltered_leads_include_older_listing_when_newest_has_none(client):
    c, session = client
    old_lid, owner, phone = seed_owner_listing(session)
    new_lid = _second_listing(session, owner)
    assert new_lid > old_lid
    buyer = fresh_buyer(c)
    enquiry = _enquire(c, buyer, old_lid)
    ho, owner_id = owner_header(c, phone)
    assert owner_id == owner

    body = c.get("/owner/leads", headers=ho).json()
    assert body["total"] == 1 and [i["id"] for i in body["items"]] == [enquiry["id"]]
    assert body["items"][0]["property_listing_id"] == old_lid
    assert body["items"][0]["message"] == "Is it available?"
    assert body["items"][0]["listing"]["title"] == "OL1"
    # The newest listing honestly has none.
    assert c.get(f"/owner/leads?property_listing_id={new_lid}", headers=ho).json()["total"] == 0


def test_new_owner_gets_documented_empty_page(client):
    c, session = client
    _, _, phone = seed_owner_listing(session)
    ho, _ = owner_header(c, phone)
    assert c.get("/owner/leads", headers=ho).json() == {
        "items": [], "page": 1, "page_size": 20, "total": 0,
    }


# --- buyer names --------------------------------------------------------------------

def test_phone_default_name_is_withheld(client):
    c, session = client
    lid, _, phone = seed_owner_listing(session)
    buyer = fresh_buyer(c)  # OTP registration: display_name == phone number
    buyer_phone = session.execute(
        text("SELECT display_name FROM users WHERE id=:u"), {"u": buyer["user_id"]}
    ).fetchone()[0]
    _enquire(c, buyer, lid)
    ho, _ = owner_header(c, phone)

    body = c.get("/owner/leads", headers=ho).json()
    item = _lead_for(body, lid)[0]
    assert item["buyer_user_id"] == buyer["user_id"]
    assert item["buyer_display_name"] is None
    assert buyer_phone not in json.dumps(body)


def test_buyer_chosen_name_is_returned_in_list_and_update(client):
    c, session = client
    lid, _, phone = seed_owner_listing(session)
    buyer = fresh_buyer(c)
    hb = auth_header(buyer["access_token"])
    assert c.patch("/users/me", json={"display_name": "  Priya Raman "}, headers=hb).status_code == 200
    _enquire(c, buyer, lid)
    ho, _ = owner_header(c, phone)

    item = _lead_for(c.get("/owner/leads", headers=ho).json(), lid)[0]
    assert item["buyer_display_name"] == "Priya Raman"
    updated = c.patch(f"/owner/leads/{item['id']}", json={"status": "CONTACTED"}, headers=ho)
    assert updated.status_code == 200
    assert updated.json()["buyer_display_name"] == "Priya Raman"
    assert updated.json()["status"] == "CONTACTED"


def test_phone_shaped_or_embedded_names_are_withheld(client):
    c, session = client
    lid, _, phone = seed_owner_listing(session)
    ho, _ = owner_header(c, phone)
    for name in ("98765 43210", "Ravi 98765-43210", "+91 98765 43210"):
        buyer = fresh_buyer(c)
        hb = auth_header(buyer["access_token"])
        assert c.patch("/users/me", json={"display_name": name}, headers=hb).status_code == 200
        _enquire(c, buyer, lid)
        rows = [
            i for i in c.get("/owner/leads", headers=ho).json()["items"]
            if i["buyer_user_id"] == buyer["user_id"]
        ]
        assert rows and rows[0]["buyer_display_name"] is None, name


# --- per-listing activity counts ---------------------------------------------------

def test_listing_counts_reflect_real_enquiries_and_visits(client):
    c, session = client
    lid, owner, phone = seed_owner_listing(session)
    quiet_lid = _second_listing(session, owner)
    b1, b2 = fresh_buyer(c), fresh_buyer(c)
    _enquire(c, b1, lid)
    _enquire(c, b2, lid)
    r = c.post(
        "/buyer/visits",
        json={"property_listing_id": lid, "scheduled_at": FUTURE()},
        headers=auth_header(b1["access_token"]),
    )
    assert r.status_code == 201, r.text
    ho, _ = owner_header(c, phone)

    cards = {i["listing_id"]: i for i in c.get("/owner/listings", headers=ho).json()["items"]}
    assert (cards[lid]["enquiry_count"], cards[lid]["visit_count"]) == (2, 1)
    assert (cards[quiet_lid]["enquiry_count"], cards[quiet_lid]["visit_count"]) == (0, 0)
    detail = c.get(f"/owner/listings/{lid}", headers=ho).json()
    assert (detail["enquiry_count"], detail["visit_count"]) == (2, 1)

    # Closing a lead keeps it in the total (counts are all-status totals).
    lead_id = _lead_for(c.get("/owner/leads", headers=ho).json(), lid)[0]["id"]
    assert c.patch(f"/owner/leads/{lead_id}", json={"status": "CLOSED", "closing_reason": "Not Converted"}, headers=ho).status_code == 200
    assert c.get(f"/owner/listings/{lid}", headers=ho).json()["enquiry_count"] == 2


def test_counts_and_leads_never_include_another_owners_activity(client):
    c, session = client
    lid_a, _, phone_a = seed_owner_listing(session)
    lid_b, _, phone_b = seed_owner_listing(session)
    buyer = fresh_buyer(c)
    _enquire(c, buyer, lid_b)
    ha, _ = owner_header(c, phone_a)
    hb, _ = owner_header(c, phone_b)

    assert c.get("/owner/leads", headers=ha).json()["total"] == 0
    cards_a = c.get("/owner/listings", headers=ha).json()["items"]
    assert [i["listing_id"] for i in cards_a] == [lid_a]
    assert cards_a[0]["enquiry_count"] == 0
    # Owner A cannot read B's listing, filter by it, or change B's lead.
    assert c.get(f"/owner/listings/{lid_b}", headers=ha).status_code == 404
    assert c.get(f"/owner/leads?property_listing_id={lid_b}", headers=ha).status_code == 404
    lead_b = _lead_for(c.get("/owner/leads", headers=hb).json(), lid_b)[0]
    assert c.patch(f"/owner/leads/{lead_b['id']}", json={"status": "CLOSED", "closing_reason": "Not Converted"},
                   headers=ha).status_code == 404
    still = _lead_for(c.get("/owner/leads", headers=hb).json(), lid_b)[0]
    assert still["status"] == "NEW"


# --- auth, roles, invalid input, contract -----------------------------------------

def test_owner_endpoints_reject_anonymous_and_buyer_role(client):
    c, _ = client
    for path in ("/owner/listings", "/owner/leads", "/owner/visits"):
        assert c.get(path).status_code == 401, path
    buyer = fresh_buyer(c)
    hb = auth_header(buyer["access_token"])
    for path in ("/owner/listings", "/owner/leads", "/owner/visits"):
        r = c.get(path, headers=hb)
        assert r.status_code == 403 and r.json() == {"detail": "OWNER role required"}, path


def test_invalid_ids_and_payloads(client):
    c, session = client
    _, _, phone = seed_owner_listing(session)
    ho, _ = owner_header(c, phone)
    assert c.get("/owner/listings/999999999", headers=ho).status_code == 404
    assert c.get("/owner/leads?property_listing_id=0", headers=ho).json() == {"detail": "Invalid request"}
    assert c.get("/owner/leads?page_size=101", headers=ho).status_code == 422
    assert c.patch("/owner/leads/999999999", json={"status": "CLOSED", "closing_reason": "Not Converted"}, headers=ho).status_code == 404
    assert c.patch("/owner/leads/1", json={"status": "LOST"}, headers=ho).status_code == 422


def test_openapi_documents_new_owner_fields(client):
    c, _ = client
    spec = c.get("/openapi.json").json()
    schemas = spec["components"]["schemas"]
    lead = schemas["OwnerLeadOut"]["properties"]
    assert "buyer_display_name" in lead and "phone" not in lead
    listing = schemas["OwnerListingOut"]["properties"]
    assert listing["enquiry_count"]["type"] == "integer"
    assert listing["visit_count"]["type"] == "integer"
    assert "/owner/leads" in spec["paths"] and "get" in spec["paths"]["/owner/leads"]


# --- historical leads on removed listings ----------------------------------------

def test_lead_on_removed_listing_stays_visible_with_real_details(client):
    c, session = client
    lid, owner, phone = seed_owner_listing(session)
    live_lid = _second_listing(session, owner)
    b1, b2 = fresh_buyer(c), fresh_buyer(c)
    old = _enquire(c, b1, lid, "Still available?")
    _enquire(c, b2, live_lid)
    ho, _ = owner_header(c, phone)
    assert c.delete(f"/owner/listings/{lid}", headers=ho).status_code == 204

    body = c.get("/owner/leads", headers=ho).json()
    assert body["total"] == 2  # leads on BOTH listings
    removed = _lead_for(body, lid)[0]
    assert removed["id"] == old["id"] and removed["message"] == "Still available?"
    assert removed["listing_removed"] is True
    real = {"title": "OL1", "price_paise": 100, "price_period": "TOTAL", "property_type": "APARTMENT"}
    assert {k: removed["listing"][k] for k in real} == real
    assert set(removed["listing"]) == set(real) | {"locality", "city"}
    live = _lead_for(body, live_lid)[0]
    assert live["listing_removed"] is False and live["listing"]["title"] == "OL2"
    # The removed listing's leads stay filterable and updatable by its owner.
    assert c.get(f"/owner/leads?property_listing_id={lid}", headers=ho).json()["total"] == 1
    r = c.patch(f"/owner/leads/{removed['id']}", json={"status": "CONTACTED"}, headers=ho)
    assert r.status_code == 200 and r.json()["listing_removed"] is True
    # Buyers still cannot see the removed listing.
    assert c.get(f"/buyer/listings/{lid}").status_code == 404


def test_owner_listings_flag_removed_rows_and_keep_their_counts(client):
    c, session = client
    lid, owner, phone = seed_owner_listing(session)
    live_lid = _second_listing(session, owner)
    _enquire(c, fresh_buyer(c), lid)
    ho, _ = owner_header(c, phone)
    assert c.delete(f"/owner/listings/{lid}", headers=ho).status_code == 204

    cards = {i["listing_id"]: i for i in c.get("/owner/listings", headers=ho).json()["items"]}
    assert cards[lid]["removed"] is True and cards[lid]["enquiry_count"] == 1
    assert cards[live_lid]["removed"] is False and cards[live_lid]["enquiry_count"] == 0
    assert c.get(f"/owner/listings/{lid}", headers=ho).json()["removed"] is True


def test_removed_listing_leads_stay_private_to_their_owner(client):
    c, session = client
    lid_a, _, phone_a = seed_owner_listing(session)
    _, _, phone_b = seed_owner_listing(session)
    _enquire(c, fresh_buyer(c), lid_a)
    ha, _ = owner_header(c, phone_a)
    hb, _ = owner_header(c, phone_b)
    assert c.delete(f"/owner/listings/{lid_a}", headers=ha).status_code == 204

    assert c.get("/owner/leads", headers=hb).json()["total"] == 0
    assert c.get(f"/owner/leads?property_listing_id={lid_a}", headers=hb).status_code == 404
    assert lid_a not in [i["listing_id"] for i in c.get("/owner/listings", headers=hb).json()["items"]]
    lead = c.get("/owner/leads", headers=ha).json()["items"][0]
    assert c.patch(f"/owner/leads/{lead['id']}", json={"status": "CLOSED", "closing_reason": "Not Converted"}, headers=hb).status_code == 404


def test_openapi_documents_removed_flags(client):
    c, _ = client
    schemas = c.get("/openapi.json").json()["components"]["schemas"]
    assert schemas["OwnerLeadOut"]["properties"]["listing_removed"]["type"] == "boolean"
    assert schemas["OwnerListingOut"]["properties"]["removed"]["type"] == "boolean"


# --- approved automatic-verification policy (read-only pins; unchanged) -------------

def test_auto_verify_policy_constants_unchanged():
    from app import worker
    from app.services import owner_listings as owner_svc

    assert owner_svc._AUTO_VERIFY_MINUTES == 2
    assert worker.WORKER_INTERVAL_SECONDS == 30


def test_removed_listing_is_never_auto_verified(client):
    c, session = client
    from app.services import owner_listings as owner_svc

    lid, _, phone = seed_owner_listing(session, verification="PENDING")
    ho, _ = owner_header(c, phone)
    assert c.delete(f"/owner/listings/{lid}", headers=ho).status_code == 204
    session.execute(text("UPDATE property_listings SET submitted_at='2000-01-01' WHERE id=:i"), {"i": lid})
    assert owner_svc.transition_verification_automatic(session, lid) is None
    status = session.execute(
        text("SELECT verification_status FROM property_listings WHERE id=:i"), {"i": lid}
    ).fetchone()[0]
    assert status == "PENDING"


# --- lead detail: real property type, not title guesses ---------------------------

def test_lead_snapshot_carries_real_property_type_not_title_words(client):
    c, session = client
    lid, _, phone = seed_owner_listing(session)
    # A title that would fool a word-based guess ("Villa"), on a COMMERCIAL listing.
    session.execute(
        text("UPDATE property_listings SET title='Villa-style office', property_type='COMMERCIAL' WHERE id=:l"),
        {"l": lid},
    )
    _enquire(c, fresh_buyer(c), lid)
    ho, _ = owner_header(c, phone)
    item = _lead_for(c.get("/owner/leads", headers=ho).json(), lid)[0]
    assert item["listing"]["property_type"] == "COMMERCIAL"
    assert item["listing"]["title"] == "Villa-style office"
    updated = c.patch(f"/owner/leads/{item['id']}", json={"status": "CONTACTED"}, headers=ho).json()
    assert updated["listing"]["property_type"] == "COMMERCIAL"
    # There is no buyer-budget field: only the listing's own asking price.
    assert "budget" not in json.dumps(item)


def test_buyer_enquiry_snapshot_has_buyer_fields_only(client):
    """Buyer snapshot (CONTRACT §A) gains the Messages-tab fields; owner-only
    lead fields never appear on the buyer side."""
    c, session = client
    lid, _, _ = seed_owner_listing(session)
    buyer = fresh_buyer(c)
    created = _enquire(c, buyer, lid)
    buyer_listing = {"title", "price_paise", "price_period", "price_display", "property_type", "locality", "city", "cover_image_url"}
    assert set(created["listing"]) == buyer_listing
    listed = c.get("/buyer/enquiries", headers=auth_header(buyer["access_token"])).json()
    assert set(listed["items"][0]["listing"]) == buyer_listing
    for owner_only in ("buyer_display_name", "listing_removed", "buyer_user_id"):
        assert owner_only not in created and owner_only not in listed["items"][0]
