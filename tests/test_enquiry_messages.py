"""CONTRACT §A/§D — every distinct enquiry message is stored (D1).

- a repeat send to the same listing appends to the ONE active enquiry (one
  lead), an identical message within 60 s is a double tap (not stored twice);
- whitespace / contact-reveal create no message;
- enquiries created before enquiry_messages show their opening message with
  id/created_at = null, followed by newer messages;
- buyer list: newest activity first, summary only; detail/POST: full thread;
- owner: summary on GET /owner/leads, full thread on GET /owner/leads/{id};
- isolation: other buyers / other owners get 404; roles enforced.
Rollback-isolated.
"""
from datetime import timedelta

from sqlalchemy import text

from tests.conftest import auth_header
from tests.test_owner_leads import fresh_buyer, owner_header, seed_owner_listing


def _send(c, buyer, lid, message=None):
    body = {"property_listing_id": lid}
    if message is not None:
        body["message"] = message
    r = c.post("/buyer/enquiries", json=body, headers=auth_header(buyer["access_token"]))
    assert r.status_code in (200, 201), r.text
    return r


def _rows(session, enquiry_id):
    return session.execute(
        text("SELECT body FROM enquiry_messages WHERE enquiry_id=:e ORDER BY id"), {"e": enquiry_id}
    ).scalars().all()


def test_repeat_sends_append_to_one_lead(client):
    c, session = client
    lid, _, phone = seed_owner_listing(session)
    buyer = fresh_buyer(c)
    first = _send(c, buyer, lid, "  Is it available?  ")
    second = _send(c, buyer, lid, "Can I visit on Sunday?")
    third = _send(c, buyer, lid, "Is parking included?")
    assert (first.status_code, second.status_code, third.status_code) == (201, 200, 200)
    eid = first.json()["id"]
    assert second.json()["id"] == third.json()["id"] == eid
    assert _rows(session, eid) == ["Is it available?", "Can I visit on Sunday?", "Is parking included?"]
    body = third.json()
    assert body["message_count"] == 3 and body["last_message"] == "Is parking included?"
    assert body["last_message_at"].endswith("Z")
    assert [m["sender"] for m in body["messages"]] == ["BUYER"] * 3
    assert all(isinstance(m["id"], int) and m["created_at"].endswith("Z") for m in body["messages"])
    assert session.execute(
        text("SELECT COUNT(*) FROM enquiries WHERE buyer_user_id=:b AND property_listing_id=:l"),
        {"b": buyer["user_id"], "l": lid},
    ).scalar() == 1  # one lead, not three


def test_identical_double_tap_is_not_stored_twice(client):
    c, session = client
    lid, _, _ = seed_owner_listing(session)
    buyer = fresh_buyer(c)
    eid = _send(c, buyer, lid, "Is it available?").json()["id"]
    again = _send(c, buyer, lid, "Is it available?")
    assert again.status_code == 200 and again.json()["message_count"] == 1
    assert _rows(session, eid) == ["Is it available?"]
    # The same text later (outside the 60 s window) is a real new message.
    # Age the enquiry and its opening message together, as real time would.
    session.execute(
        text("UPDATE enquiry_messages SET created_at = created_at - INTERVAL 2 MINUTE WHERE enquiry_id=:e"),
        {"e": eid},
    )
    session.execute(text("UPDATE enquiries SET created_at = created_at - INTERVAL 2 MINUTE WHERE id=:e"), {"e": eid})
    later = _send(c, buyer, lid, "Is it available?")
    assert later.json()["message_count"] == 2
    assert _rows(session, eid) == ["Is it available?", "Is it available?"]


def test_blank_messages_and_contact_reveal_store_nothing(client):
    c, session = client
    lid, _, _ = seed_owner_listing(session)
    buyer = fresh_buyer(c)
    hb = auth_header(buyer["access_token"])
    assert c.post(f"/buyer/listings/{lid}/contact", headers=hb).status_code == 200  # creates the lead
    eid = session.execute(
        text("SELECT id FROM enquiries WHERE buyer_user_id=:b AND property_listing_id=:l"),
        {"b": buyer["user_id"], "l": lid},
    ).scalar()
    blank = _send(c, buyer, lid, "    ")
    assert blank.json()["id"] == eid and blank.json()["message_count"] == 0
    assert blank.json()["messages"] == [] and blank.json()["last_message"] is None
    assert _rows(session, eid) == []
    assert c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": "x" * 2001},
                  headers=hb).status_code == 422


def test_legacy_opening_message_has_unknown_time_then_new_messages(client):
    c, session = client
    lid, _, phone = seed_owner_listing(session)
    buyer = fresh_buyer(c)
    eid = _send(c, buyer, lid, "Legacy opening").json()["id"]
    # Simulate an enquiry created before enquiry_messages/created_at existed.
    session.execute(text("DELETE FROM enquiry_messages WHERE enquiry_id=:e"), {"e": eid})
    session.execute(text("UPDATE enquiries SET created_at=NULL WHERE id=:e"), {"e": eid})
    follow = _send(c, buyer, lid, "Any update?").json()
    assert follow["message_count"] == 2
    opening, newer = follow["messages"]
    assert opening == {"id": None, "body": "Legacy opening", "sender": "BUYER", "created_at": None}
    assert isinstance(newer["id"], int) and newer["body"] == "Any update?" and newer["created_at"].endswith("Z")
    ho, _ = owner_header(c, phone)
    lead = c.get(f"/owner/leads/{eid}", headers=ho).json()
    assert [m["body"] for m in lead["messages"]] == ["Legacy opening", "Any update?"]


def test_buyer_list_is_newest_activity_first_and_summary_only(client):
    c, session = client
    lid_a, _, _ = seed_owner_listing(session)
    lid_b, _, _ = seed_owner_listing(session)
    buyer = fresh_buyer(c)
    hb = auth_header(buyer["access_token"])
    ea = _send(c, buyer, lid_a, "First listing").json()["id"]
    eb = _send(c, buyer, lid_b, "Second listing").json()["id"]
    # Make A's activity older, then give A a new message: A becomes newest.
    session.execute(text("UPDATE enquiry_messages SET created_at = created_at - INTERVAL 1 DAY WHERE enquiry_id IN (:a, :b)"), {"a": ea, "b": eb})
    session.execute(text("UPDATE enquiries SET created_at = created_at - INTERVAL 1 DAY WHERE id IN (:a, :b)"), {"a": ea, "b": eb})
    session.execute(text("UPDATE enquiry_messages SET created_at = created_at - INTERVAL 1 HOUR WHERE enquiry_id = :a"), {"a": ea})
    session.execute(text("UPDATE enquiries SET created_at = created_at - INTERVAL 1 HOUR WHERE id = :a"), {"a": ea})
    assert [i["id"] for i in c.get("/buyer/enquiries", headers=hb).json()["items"]] == [eb, ea]
    _send(c, buyer, lid_a, "Back on A")
    items = c.get("/buyer/enquiries", headers=hb).json()["items"]
    assert [i["id"] for i in items] == [ea, eb]
    assert all(i["messages"] is None for i in items)
    assert items[0]["last_message"] == "Back on A" and items[0]["message_count"] == 2
    listing = items[0]["listing"]
    assert listing["price_display"] and listing["locality"] and listing["city"]
    assert "cover_image_url" in listing and listing["property_type"] == "APARTMENT"


def test_threads_are_private_to_their_buyer_and_owner(client):
    c, session = client
    lid, _, phone = seed_owner_listing(session)
    _, _, other_phone = seed_owner_listing(session)
    buyer, other = fresh_buyer(c), fresh_buyer(c)
    eid = _send(c, buyer, lid, "Private question").json()["id"]
    assert c.get(f"/buyer/enquiries/{eid}", headers=auth_header(other["access_token"])).status_code == 404
    assert c.get("/buyer/enquiries", headers=auth_header(other["access_token"])).json()["total"] == 0
    ho, _ = owner_header(c, phone)
    leads = c.get("/owner/leads", headers=ho).json()
    item = [i for i in leads["items"] if i["id"] == eid][0]
    assert item["last_message"] == "Private question" and item["message_count"] == 1
    assert "messages" not in item  # list = summary only
    assert item["listing"]["locality"] and item["listing"]["city"]
    hx, _ = owner_header(c, other_phone)
    assert c.get(f"/owner/leads/{eid}", headers=hx).status_code == 404
    assert c.get(f"/owner/leads/{eid}").status_code == 401
    assert c.get(f"/owner/leads/{eid}", headers=auth_header(buyer["access_token"])).status_code == 403
    assert c.get("/owner/leads/999999999", headers=ho).status_code == 404


def test_owner_cannot_enquire_on_own_listing_even_in_buyer_mode(client):
    """Security review fix (2026-10-30): same rule as visits and call taps.
    An owner switched to BUYER mode gets 422 on their own listing, no lead,
    no message and no change to their lead list; other listings still work."""
    c, session = client
    own_lid, owner_id, owner_phone = seed_owner_listing(session)
    other_lid, _, _ = seed_owner_listing(session)
    ho, _ = owner_header(c, owner_phone)
    assert c.post("/users/me/role", json={"role": "BUYER"}, headers=ho).status_code == 200
    r = c.post("/buyer/enquiries", json={"property_listing_id": own_lid, "message": "Self lead?"}, headers=ho)
    assert r.status_code == 422 and r.json() == {"detail": "Cannot enquire on your own listing"}
    assert session.execute(text("SELECT COUNT(*) FROM enquiries WHERE buyer_user_id=:u"), {"u": owner_id}).scalar() == 0
    assert session.execute(text("SELECT COUNT(*) FROM enquiry_messages WHERE sender_user_id=:u"), {"u": owner_id}).scalar() == 0
    assert c.post("/buyer/enquiries", json={"property_listing_id": other_lid, "message": "Hi"}, headers=ho).status_code == 201
    assert c.post("/users/me/role", json={"role": "OWNER"}, headers=ho).status_code == 200
    leads = c.get("/owner/leads", headers=ho).json()
    assert all(i["property_listing_id"] != own_lid for i in leads["items"])
