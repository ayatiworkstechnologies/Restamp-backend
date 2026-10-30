"""CONTRACT §B — call-button taps (D2, activity_logs).

- POST records "Call button tapped" (201); a repeat within 60 s returns the
  same event (200, no new row); later taps are new events;
- only publicly visible listings (else 404); buyer role only;
- GET lists only the caller's taps, newest first, with the listing card
  (null once the listing is no longer public);
- owners see per-listing call_count only — never which buyer tapped.
Rollback-isolated.
"""
from sqlalchemy import text

from tests.conftest import auth_header
from tests.test_owner_leads import fresh_buyer, owner_header, seed_owner_listing


def _tap(c, buyer, lid):
    return c.post(f"/buyer/listings/{lid}/call-events", headers=auth_header(buyer["access_token"]))


def _events(session, buyer_id, lid):
    return session.execute(
        text("SELECT action, entity_type, entity_id, details FROM activity_logs "
             "WHERE user_id=:u AND entity_id=:l ORDER BY id"),
        {"u": buyer_id, "l": lid},
    ).fetchall()


def test_tap_is_recorded_and_deduplicated(client):
    c, session = client
    lid, _, _ = seed_owner_listing(session)
    buyer = fresh_buyer(c)
    first = _tap(c, buyer, lid)
    assert first.status_code == 201, first.text
    body = first.json()
    assert set(body) == {"id", "property_listing_id", "created_at"}
    assert body["property_listing_id"] == lid and body["created_at"].endswith("Z")
    retry = _tap(c, buyer, lid)
    assert retry.status_code == 200 and retry.json() == body  # same event, no new row
    rows = _events(session, buyer["user_id"], lid)
    assert len(rows) == 1
    assert tuple(rows[0][:3]) == ("CALL_BUTTON_TAP", "property_listing", lid) and rows[0][3] is None
    session.execute(text("UPDATE activity_logs SET created_at = created_at - INTERVAL 2 MINUTE WHERE user_id=:u"),
                    {"u": buyer["user_id"]})
    later = _tap(c, buyer, lid)
    assert later.status_code == 201 and later.json()["id"] != body["id"]
    assert len(_events(session, buyer["user_id"], lid)) == 2


def test_only_visible_listings_and_buyers(client):
    c, session = client
    hidden, _, phone = seed_owner_listing(session, verification="PENDING")
    buyer = fresh_buyer(c)
    assert _tap(c, buyer, hidden).status_code == 404
    assert _tap(c, buyer, 999999999).status_code == 404
    lid, _, _ = seed_owner_listing(session)
    assert c.post(f"/buyer/listings/{lid}/call-events").status_code == 401
    ho, _ = owner_header(c, phone)
    assert c.post(f"/buyer/listings/{lid}/call-events", headers=ho).status_code == 403
    assert c.get("/buyer/call-events").status_code == 401
    assert _events(session, buyer["user_id"], hidden) == []


def test_history_is_own_newest_first_with_cards(client):
    c, session = client
    lid_a, _, _ = seed_owner_listing(session)
    lid_b, _, _ = seed_owner_listing(session)
    buyer, other = fresh_buyer(c), fresh_buyer(c)
    _tap(c, buyer, lid_a)
    _tap(c, buyer, lid_b)
    _tap(c, other, lid_a)
    page = c.get("/buyer/call-events", headers=auth_header(buyer["access_token"])).json()
    assert page["total"] == 2 and [i["property_listing_id"] for i in page["items"]] == [lid_b, lid_a]
    card = page["items"][0]["listing"]
    assert card["listing_id"] == lid_b and card["title"] and "price_display" in card
    assert "phone" not in str(page)
    session.execute(text("UPDATE property_listings SET listing_status='SOLD' WHERE id=:i"), {"i": lid_a})
    page = c.get("/buyer/call-events", headers=auth_header(buyer["access_token"])).json()
    gone = [i for i in page["items"] if i["property_listing_id"] == lid_a][0]
    assert gone["listing"] is None  # event kept, listing no longer public
    assert c.get("/buyer/call-events?page_size=101", headers=auth_header(buyer["access_token"])).status_code == 422


def test_owner_sees_counts_only_for_own_listings(client):
    c, session = client
    lid, _, phone = seed_owner_listing(session)
    other_lid, _, other_phone = seed_owner_listing(session)
    b1, b2 = fresh_buyer(c), fresh_buyer(c)
    _tap(c, b1, lid)
    _tap(c, b1, lid)  # deduplicated
    _tap(c, b2, lid)
    _tap(c, b1, other_lid)
    ho, _ = owner_header(c, phone)
    cards = {i["listing_id"]: i for i in c.get("/owner/listings", headers=ho).json()["items"]}
    assert cards[lid]["call_count"] == 2
    assert other_lid not in cards
    assert c.get(f"/owner/listings/{lid}", headers=ho).json()["call_count"] == 2
    hx, _ = owner_header(c, other_phone)
    assert c.get(f"/owner/listings/{other_lid}", headers=hx).json()["call_count"] == 1
    assert "buyer" not in str(c.get(f"/owner/listings/{lid}", headers=ho).json().get("call_count"))


def test_owner_cannot_record_a_call_on_own_listing_even_in_buyer_mode(client):
    """Approved follow-up: server-side self-call block. An owner who switches
    to BUYER mode still cannot tap Call on their own listing (422, no row,
    count unchanged); real buyers on the same listing are unaffected, and the
    owner may still record calls on OTHER owners' listings."""
    c, session = client
    own_lid, owner_id, owner_phone = seed_owner_listing(session)
    other_lid, _, _ = seed_owner_listing(session)
    ho, logged_in_id = owner_header(c, owner_phone)
    assert logged_in_id == owner_id
    assert c.post("/users/me/role", json={"role": "BUYER"}, headers=ho).status_code == 200  # buyer mode

    r = c.post(f"/buyer/listings/{own_lid}/call-events", headers=ho)
    assert r.status_code == 422 and r.json() == {"detail": "Cannot record a call on your own listing"}
    assert _events(session, owner_id, own_lid) == []

    # The same account, as a buyer, may still record calls on someone else's listing.
    assert c.post(f"/buyer/listings/{other_lid}/call-events", headers=ho).status_code == 201

    # A genuine buyer's call on the owner's listing still works and is counted.
    buyer = fresh_buyer(c)
    assert _tap(c, buyer, own_lid).status_code == 201
    assert c.post("/users/me/role", json={"role": "OWNER"}, headers=ho).status_code == 200
    card = c.get(f"/owner/listings/{own_lid}", headers=ho).json()
    assert card["call_count"] == 1  # only the real buyer's tap
