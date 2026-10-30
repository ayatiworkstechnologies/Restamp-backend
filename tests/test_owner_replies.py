"""Two-way enquiry threads: POST /owner/leads/{id}/messages.

- only the lead's owner (OWNER role, both listing ownership columns) may
  reply; others get 401/403/404 and nothing is stored;
- replies share the buyer's thread: buyer detail/list and owner lead detail
  show both sides in insertion order with sender BUYER/OWNER (derived
  server-side, no user ids exposed to the buyer);
- 1-2000 chars after trimming; closed leads refuse replies;
- an identical owner double tap is stored once (200), and an owner reply
  repeating the buyer's words is NOT mistaken for the buyer's double tap;
- per-sender, per-thread rate limit (429) read from the database;
- private lead notes never appear in any thread response.
Rollback-isolated.
"""
from datetime import datetime

from sqlalchemy import text

from app.services import enquiries as enquiry_svc
from tests.conftest import auth_header
from tests.test_owner_leads import fresh_buyer, owner_header, seed_owner_listing


def _enquire(c, buyer, lid, message):
    r = c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": message},
               headers=auth_header(buyer["access_token"]))
    assert r.status_code in (200, 201), r.text
    return r.json()


def _reply(c, headers, lead_id, message):
    return c.post(f"/owner/leads/{lead_id}/messages", json={"message": message}, headers=headers)


def _rows(session, enquiry_id):
    return session.execute(text(
        "SELECT sender_user_id, body FROM enquiry_messages WHERE enquiry_id=:e ORDER BY id"),
        {"e": enquiry_id}).fetchall()


def _lead(c, session):
    lid, owner_id, phone = seed_owner_listing(session)
    buyer = fresh_buyer(c)
    enquiry = _enquire(c, buyer, lid, "Is it available?")
    ho, oid = owner_header(c, phone)
    assert oid == owner_id
    return lid, owner_id, ho, buyer, enquiry["id"]


def test_owner_reply_is_stored_with_sender_role_and_time(client):
    c, session = client
    _, owner_id, ho, buyer, eid = _lead(c, session)
    r = _reply(c, ho, eid, "  Yes, it is. Visit Saturday?  ")
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == {"id", "body", "sender", "created_at"}
    assert body["body"] == "Yes, it is. Visit Saturday?" and body["sender"] == "OWNER"
    sent = datetime.fromisoformat(body["created_at"].replace("Z", "+00:00"))
    assert sent.utcoffset().total_seconds() == 0
    assert _rows(session, eid) == [(buyer["user_id"], "Is it available?"), (owner_id, "Yes, it is. Visit Saturday?")]
    # Replying does not move the lead through its lifecycle.
    assert session.execute(text("SELECT status FROM enquiries WHERE id=:e"), {"e": eid}).scalar() == "NEW"


def test_buyer_sees_owner_replies_in_order(client):
    c, session = client
    lid, _, ho, buyer, eid = _lead(c, session)
    assert _reply(c, ho, eid, "Yes").status_code == 201
    _enquire(c, buyer, lid, "Great, Saturday 10am?")
    assert _reply(c, ho, eid, "Works for me").status_code == 201
    hb = auth_header(buyer["access_token"])
    detail = c.get(f"/buyer/enquiries/{eid}", headers=hb).json()
    assert [(m["sender"], m["body"]) for m in detail["messages"]] == [
        ("BUYER", "Is it available?"), ("OWNER", "Yes"),
        ("BUYER", "Great, Saturday 10am?"), ("OWNER", "Works for me")]
    assert [m["id"] for m in detail["messages"]] == sorted(m["id"] for m in detail["messages"])
    assert detail["message_count"] == 4 and detail["last_message"] == "Works for me"
    for m in detail["messages"]:
        assert set(m) == {"id", "body", "sender", "created_at"}  # no user ids
    listed = c.get("/buyer/enquiries", headers=hb).json()["items"]
    row = next(i for i in listed if i["id"] == eid)
    assert row["last_message"] == "Works for me" and row["message_count"] == 4


def test_owner_sees_full_conversation(client):
    c, session = client
    lid, _, ho, buyer, eid = _lead(c, session)
    _reply(c, ho, eid, "Yes")
    _enquire(c, buyer, lid, "Price negotiable?")
    lead = c.get(f"/owner/leads/{eid}", headers=ho).json()
    assert [(m["sender"], m["body"]) for m in lead["messages"]] == [
        ("BUYER", "Is it available?"), ("OWNER", "Yes"), ("BUYER", "Price negotiable?")]
    summary = next(i for i in c.get("/owner/leads", headers=ho).json()["items"] if i["id"] == eid)
    assert summary["message_count"] == 3 and summary["last_message"] == "Price negotiable?"


def test_unauthenticated_and_buyer_are_refused(client):
    c, session = client
    _, _, _, buyer, eid = _lead(c, session)
    assert c.post(f"/owner/leads/{eid}/messages", json={"message": "hi"}).status_code == 401
    r = _reply(c, auth_header(buyer["access_token"]), eid, "hi")
    assert r.status_code == 403 and r.json() == {"detail": "OWNER role required"}
    assert len(_rows(session, eid)) == 1


def test_unrelated_owner_cannot_reply_or_probe(client):
    c, session = client
    _, _, _, _, eid = _lead(c, session)
    # Another owner with their own listing and their own lead.
    other_lid, _, other_phone = seed_owner_listing(session)
    other_buyer = fresh_buyer(c)
    other_eid = _enquire(c, other_buyer, other_lid, "Hello")["id"]
    ho2, _ = owner_header(c, other_phone)
    r = _reply(c, ho2, eid, "I am not the owner")
    assert r.status_code == 404 and r.json() == {"detail": "Not found"}
    assert len(_rows(session, eid)) == 1
    assert _reply(c, ho2, other_eid, "My own lead").status_code == 201  # sanity
    assert _reply(c, ho2, 987654321, "nope").status_code == 404  # non-existent


def test_listing_ownership_mismatch_fails_closed(client):
    c, session = client
    lid, _, ho, _, eid = _lead(c, session)
    session.execute(text("INSERT INTO users (display_name) VALUES ('Someone Else')"))
    other = session.execute(text("SELECT LAST_INSERT_ID()")).scalar()
    session.execute(text("UPDATE property_listings SET user_id=:u WHERE id=:l"), {"u": other, "l": lid})
    session.expire_all()
    assert _reply(c, ho, eid, "hi").status_code == 404
    assert len(_rows(session, eid)) == 1


def test_message_validation(client):
    c, session = client
    _, _, ho, _, eid = _lead(c, session)
    for bad in ({"message": ""}, {"message": "   \n\t "}, {}, {"message": "x" * 2001}, {"message": None}):
        r = c.post(f"/owner/leads/{eid}/messages", json=bad, headers=ho)
        assert r.status_code == 422, (bad, r.text)
        assert r.json() == {"detail": "Invalid request"}
    assert len(_rows(session, eid)) == 1
    assert _reply(c, ho, eid, "y" * 2000).status_code == 201


def test_closed_lead_refuses_replies(client):
    c, session = client
    _, _, ho, _, eid = _lead(c, session)
    r = c.patch(f"/owner/leads/{eid}", json={"status": "CLOSED", "closing_reason": "Not Converted"}, headers=ho)
    assert r.status_code == 200, r.text
    r = _reply(c, ho, eid, "Still there?")
    assert r.status_code == 422 and r.json() == {"detail": "Lead is closed"}
    assert len(_rows(session, eid)) == 1


def test_owner_double_tap_stored_once(client):
    c, session = client
    _, _, ho, _, eid = _lead(c, session)
    first = _reply(c, ho, eid, "Yes")
    again = _reply(c, ho, eid, "Yes")
    assert first.status_code == 201 and again.status_code == 200
    assert again.json() == first.json()
    assert len(_rows(session, eid)) == 2
    assert _reply(c, ho, eid, "Yes, call me").status_code == 201  # distinct text is stored


def test_owner_reply_echoing_buyer_words_is_stored(client):
    """Regression: the legacy opening-message double-tap branch ignored the
    sender, so an owner reply identical to a pre-enquiry_messages opening
    message (no thread rows yet) was silently dropped."""
    c, session = client
    lid, owner_id, phone = seed_owner_listing(session)
    buyer = fresh_buyer(c)
    session.execute(text(
        "INSERT INTO enquiries (property_listing_id, buyer_user_id, owner_user_id, message, status, created_at) "
        "VALUES (:l, :b, :o, 'Available?', 'NEW', UTC_TIMESTAMP())"),
        {"l": lid, "b": buyer["user_id"], "o": owner_id})
    eid = session.execute(text("SELECT LAST_INSERT_ID()")).scalar()
    ho, _ = owner_header(c, phone)
    r = _reply(c, ho, eid, "Available?")
    assert r.status_code == 201, r.text
    assert _rows(session, eid) == [(owner_id, "Available?")]
    msgs = c.get(f"/owner/leads/{eid}", headers=ho).json()["messages"]
    assert [(m["sender"], m["body"]) for m in msgs] == [("BUYER", "Available?"), ("OWNER", "Available?")]


def test_per_thread_rate_limit(client, monkeypatch):
    monkeypatch.setattr(enquiry_svc, "_MESSAGE_RATE_LIMIT", 3)
    c, session = client
    lid, _, ho, buyer, eid = _lead(c, session)  # buyer has 1 stored message
    for i in range(3):
        assert _reply(c, ho, eid, f"owner {i}").status_code == 201
    r = _reply(c, ho, eid, "owner 3")
    assert r.status_code == 429 and r.json() == {"detail": "Too many messages"}
    # Limits are per sender: the buyer still has room for two more.
    _enquire(c, buyer, lid, "buyer 1")
    _enquire(c, buyer, lid, "buyer 2")
    r = c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": "buyer 3"},
               headers=auth_header(buyer["access_token"]))
    assert r.status_code == 429
    assert len(_rows(session, eid)) == 6
    # Only messages inside the window count.
    session.execute(text("UPDATE enquiry_messages SET created_at = created_at - INTERVAL 2 HOUR "
                         "WHERE enquiry_id=:e"), {"e": eid})
    session.expire_all()
    assert _reply(c, ho, eid, "owner later").status_code == 201


def test_other_buyer_cannot_read_thread(client):
    c, session = client
    _, _, ho, _, eid = _lead(c, session)
    _reply(c, ho, eid, "Private reply")
    other = fresh_buyer(c)
    r = c.get(f"/buyer/enquiries/{eid}", headers=auth_header(other["access_token"]))
    assert r.status_code == 404
    listed = c.get("/buyer/enquiries", headers=auth_header(other["access_token"])).json()
    assert "Private reply" not in str(listed)


def test_private_notes_never_in_threads(client):
    c, session = client
    _, _, ho, buyer, eid = _lead(c, session)
    assert c.post(f"/owner/leads/{eid}/notes", json={"body": "SECRET-NOTE-7731"}, headers=ho).status_code == 201
    reply = _reply(c, ho, eid, "Hello buyer")
    hb = auth_header(buyer["access_token"])
    for resp in (reply, c.get(f"/buyer/enquiries/{eid}", headers=hb), c.get("/buyer/enquiries", headers=hb),
                 c.get(f"/owner/leads/{eid}", headers=ho), c.get("/owner/leads", headers=ho)):
        assert resp.status_code in (200, 201)
        assert "SECRET-NOTE-7731" not in resp.text
