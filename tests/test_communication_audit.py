"""End-to-end communication audit: every supported role pair, isolation,
notifications, site-visit lifecycle (strict Phase 5) and timestamps.

What "messaging" is in the live API: the property-anchored enquiry thread
(enquiries + enquiry_messages). The buyer side is /buyer/enquiries (any
account: buyer capability is mode-independent; an agent = BROKER mode); the
poster side is /owner/leads (OWNER mode or Dealer = BROKER mode, via
require_poster, scoped by the enquiry's stored owner_user_id). The separate
/conversations module is NOT routed (see test_conversations_module_is_not_routed).

Every flow is checked through the real HTTP API against real MySQL:
sender action -> auth -> role gate -> DB rows -> recipient notification ->
recipient retrieval -> read state -> timestamps. Rollback-isolated
(tests/conftest.py client fixture).

Delivery here means "persisted and returned by the recipient's API".
Push/email delivery and on-device display are NOT covered (providers are
disabled in tests; see the audit report).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from tests.conftest import auth_header, grant_posting_plan_for_user, register_phone
from tests.test_agent_buyer_activity import _start_trial_for
from tests.test_owner_leads import fresh_buyer, owner_header, seed_owner_listing

_n = {"i": 0}


def _future(days: int = 3) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0).isoformat()


def _buyer(c):
    tok = fresh_buyer(c)
    return auth_header(tok["access_token"]), tok["user_id"]


def _owner(c, session):
    """OWNER-mode poster with one live listing. -> (headers, user_id, listing_id)."""
    lid, _, phone = seed_owner_listing(session)
    h, uid = owner_header(c, phone)
    return h, uid, lid


def _dealer_poster(c, session):
    """Dealer (poster type BROKER, BROKER mode, paid Dealer plan) who owns a
    live listing -> manages it through /owner/* exactly like an owner."""
    lid, uid, phone = seed_owner_listing(session)
    tok = register_phone(c, phone)
    h = auth_header(tok["access_token"])
    grant_posting_plan_for_user(session, uid, role="BROKER")  # claims poster type BROKER
    assert c.post("/users/me/role", json={"role": "BROKER"}, headers=h).status_code == 200
    assert session.execute(text("SELECT poster_type FROM users WHERE id=:u"), {"u": uid}).scalar() == "BROKER"
    return h, uid, lid


def _agent_for(c, session, lid):
    """Dealer acting buyer-side (agent, BROKER mode) whose trial area covers `lid`."""
    _n["i"] += 1
    tok = register_phone(c, f"+916991{_n['i']:06d}")
    h = auth_header(tok["access_token"])
    _start_trial_for(c, session, h, lid)
    return h, tok["user_id"]


def _notifs(c, h, **params):
    r = c.get("/notifications", params={"page_size": 100, **params}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def _types(c, h, **params):
    return [n["type"] for n in _notifs(c, h, **params)["items"]]


def _enquire(c, h, lid, message):
    r = c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": message}, headers=h)
    assert r.status_code in (200, 201), r.text
    return r.json()


def _reply(c, h, lead_id, message, expect=201):
    r = c.post(f"/owner/leads/{lead_id}/messages", json={"message": message}, headers=h)
    assert r.status_code == expect, r.text
    return r.json()


def _stored_senders(session, enquiry_id):
    return [tuple(r) for r in session.execute(
        text("SELECT sender_user_id, body FROM enquiry_messages WHERE enquiry_id=:e ORDER BY id"),
        {"e": enquiry_id}).fetchall()]


# --- A. OWNER <-> BUYER ---------------------------------------------------------------------

def test_owner_buyer_two_way_thread_order_persistence_and_notifications(client):
    c, session = client
    oh, owner_id, lid = _owner(c, session)
    bh, buyer_id = _buyer(c)

    enq = _enquire(c, bh, lid, "B1 is it available?")
    eid = enq["id"]
    # Follow-ups sent quickly: all stored, in order, never duplicated.
    _enquire(c, bh, lid, "B2 second")
    _enquire(c, bh, lid, "B3 third")
    for body in ("O1 yes", "O2 come Sunday", "O3 bring ID"):
        _reply(c, oh, eid, body)
    _enquire(c, bh, lid, "B4 thanks")

    expected = [
        (buyer_id, "B1 is it available?"), (buyer_id, "B2 second"), (buyer_id, "B3 third"),
        (owner_id, "O1 yes"), (owner_id, "O2 come Sunday"), (owner_id, "O3 bring ID"),
        (buyer_id, "B4 thanks"),
    ]
    assert _stored_senders(session, eid) == expected  # DB: correct sender ids, order, no dupes

    roles = ["BUYER"] * 3 + ["OWNER"] * 3 + ["BUYER"]
    for thread in (
        c.get(f"/buyer/enquiries/{eid}", headers=bh).json()["messages"],
        c.get(f"/owner/leads/{eid}", headers=oh).json()["messages"],
    ):
        assert [m["body"] for m in thread] == [b for _, b in expected]
        assert [m["sender"] for m in thread] == roles
        ids = [m["id"] for m in thread]
        assert ids == sorted(ids) and len(set(ids)) == len(ids)
        assert all(m["created_at"].endswith("Z") for m in thread)

    # Owner: one ENQUIRY_NEW; the quick follow-ups are coalesced while it is
    # unread (no flood) -> exactly one notification, pointing at the lead.
    owner_items = _notifs(c, oh)["items"]
    assert [n["type"] for n in owner_items] == ["ENQUIRY_NEW"]
    assert owner_items[0]["target"] == {"kind": "OWNER_LEAD", "id": eid}
    assert owner_items[0]["property_listing_id"] == lid
    # Buyer: one ENQUIRY_REPLY per stored owner reply, all targeting the enquiry.
    buyer_items = _notifs(c, bh)["items"]
    assert [n["type"] for n in buyer_items] == ["ENQUIRY_REPLY"] * 3
    assert {n["target"]["id"] for n in buyer_items} == {eid}
    assert all(n["audience"] == "BUYER" for n in buyer_items)

    # Survives a fresh login (new token = "logout/login"): server-side state.
    phone = session.execute(text(
        "SELECT provider_identifier FROM auth_identities WHERE user_id=:u AND provider='phone'"),
        {"u": buyer_id}).scalar()
    bh2 = auth_header(register_phone(c, phone)["access_token"])
    assert len(c.get(f"/buyer/enquiries/{eid}", headers=bh2).json()["messages"]) == 7
    assert c.get("/notifications/unread-count", headers=bh2).json()["unread_count"] == 3


def test_read_state_persists_and_unread_count_updates(client):
    c, session = client
    oh, _, lid = _owner(c, session)
    bh, _ = _buyer(c)
    eid = _enquire(c, bh, lid, "hello")["id"]
    for body in ("r1", "r2"):
        _reply(c, oh, eid, body)
    items = _notifs(c, bh)["items"]
    assert c.get("/notifications/unread-count", headers=bh).json()["unread_count"] == 2

    first = c.post(f"/notifications/{items[0]['id']}/read", headers=bh).json()
    assert first["is_read"] is True and first["read_at"].endswith("Z")
    again = c.post(f"/notifications/{items[0]['id']}/read", headers=bh).json()
    assert again["read_at"] == first["read_at"]  # idempotent: first read time kept
    assert c.get("/notifications/unread-count", headers=bh).json()["unread_count"] == 1
    # Re-fetch ("app restart"): persisted.
    refreshed = {n["id"]: n["is_read"] for n in _notifs(c, bh)["items"]}
    assert refreshed[items[0]["id"]] is True and refreshed[items[1]["id"]] is False

    r = c.post("/notifications/read-all", headers=bh).json()
    assert r == {"updated": 1, "unread_count": 0}
    # Owner's own counter is untouched by the buyer's reads.
    assert c.get("/notifications/unread-count", headers=oh).json()["unread_count"] == 1


# --- B/C. DEALER <-> OWNER, DEALER (owner mode) <-> BUYER ------------------------------------

def test_dealer_in_owner_mode_and_buyer_two_way_with_isolation(client):
    c, session = client
    dh, dealer_id, lid = _dealer_poster(c, session)
    bh, buyer_id = _buyer(c)

    eid = _enquire(c, bh, lid, "Dealer listing, still open?")["id"]
    lead = session.execute(text("SELECT owner_user_id, buyer_user_id FROM enquiries WHERE id=:e"),
                           {"e": eid}).fetchone()
    assert tuple(lead) == (dealer_id, buyer_id)  # the dealer IS the lead's poster

    leads = c.get("/owner/leads", headers=dh).json()["items"]  # BROKER mode, /owner/*
    assert [l["id"] for l in leads] == [eid]
    assert _types(c, dh) == ["ENQUIRY_NEW"]
    _reply(c, dh, eid, "Yes, open")
    assert _types(c, bh) == ["ENQUIRY_REPLY"]
    thread = c.get(f"/buyer/enquiries/{eid}", headers=bh).json()["messages"]
    assert [(m["sender"], m["body"]) for m in thread] == [
        ("BUYER", "Dealer listing, still open?"), ("OWNER", "Yes, open")]
    assert _stored_senders(session, eid)[-1] == (dealer_id, "Yes, open")

    # Another dealer and another owner can neither read nor write this thread.
    other_dh, _, _ = _dealer_poster(c, session)
    other_oh, _, _ = _owner(c, session)
    for h in (other_dh, other_oh):
        assert c.get(f"/owner/leads/{eid}", headers=h).status_code == 404
        assert c.post(f"/owner/leads/{eid}/messages", json={"message": "x"}, headers=h).status_code == 404
        assert c.get(f"/buyer/enquiries/{eid}", headers=h).status_code == 404
        assert eid not in [l["id"] for l in c.get("/owner/leads", headers=h).json()["items"]]
    assert len(_stored_senders(session, eid)) == 2


def test_dealer_buyer_side_to_owner_and_owner_reply(client):
    """Dealer -> Owner: a dealer (agent, BROKER mode, area plan) enquires on
    an owner's listing; the owner sees an agent lead and replies; the dealer
    receives the reply on its own buyer-side thread."""
    c, session = client
    oh, owner_id, lid = _owner(c, session)
    ah, agent_id = _agent_for(c, session, lid)

    eid = _enquire(c, ah, lid, "Agent here, client interested")["id"]
    lead = c.get(f"/owner/leads/{eid}", headers=oh).json()
    assert lead["source_role"] == "BROKER" and lead["buyer_user_id"] == agent_id
    assert _types(c, oh) == ["ENQUIRY_NEW"]
    _reply(c, oh, eid, "Send your client Sunday")
    assert _types(c, ah) == ["ENQUIRY_REPLY"]
    assert c.get(f"/buyer/enquiries/{eid}", headers=ah).json()["messages"][-1]["sender"] == "OWNER"
    assert _stored_senders(session, eid) == [
        (agent_id, "Agent here, client interested"), (owner_id, "Send your client Sunday")]


def test_dealer_to_dealer_listing_is_area_gated(client):
    """A dealer (agent mode) messaging another dealer's listing works only
    inside its plan area; outside it the API refuses before any write."""
    c, session = client
    _, _, lid = _dealer_poster(c, session)
    _, _, other_lid = _owner(c, session)  # a different locality
    ah, _ = _agent_for(c, session, lid)
    assert c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": "hi"},
                  headers=ah).status_code == 201
    before = session.execute(text("SELECT COUNT(*) FROM enquiries")).scalar()
    r = c.post("/buyer/enquiries", json={"property_listing_id": other_lid, "message": "hi"}, headers=ah)
    assert r.status_code == 402
    assert session.execute(text("SELECT COUNT(*) FROM enquiries")).scalar() == before


# --- D. Unsupported directions are refused safely -------------------------------------------

def test_conversations_module_is_not_routed(client):
    """Direct (non-listing) messaging is not part of the live API. The
    /conversations code in the tree has no listing-owner/recipient checks,
    so it must stay unrouted until it is hardened (see the audit report)."""
    c, _ = client
    bh, _ = _buyer(c)
    assert c.post("/conversations/", params={"other_user_id": 1, "conversation_type": "DIRECT"},
                  headers=bh).status_code == 404
    assert c.get("/conversations", headers=bh).status_code == 404
    assert c.get("/conversations/unread-count", headers=bh).status_code == 404


def test_poster_cannot_message_own_listing_and_buyer_cannot_reach_owner_endpoints(client):
    c, session = client
    oh, _, lid = _owner(c, session)
    r = c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": "me"}, headers=oh)
    assert r.status_code == 422
    bh, _ = _buyer(c)
    eid = _enquire(c, bh, lid, "hi")["id"]
    # A plain buyer-mode account cannot use the poster reply endpoint at all.
    assert c.post(f"/owner/leads/{eid}/messages", json={"message": "x"}, headers=bh).status_code == 403


# --- E. Cross-account isolation and identity spoofing ---------------------------------------

def test_cross_account_isolation_and_spoofed_identity_fields_are_ignored(client):
    c, session = client
    oh, owner_id, lid = _owner(c, session)
    bh_a, buyer_a = _buyer(c)
    bh_b, buyer_b = _buyer(c)

    # Spoof attempt: extra identity fields are ignored; sender = token holder.
    r = c.post("/buyer/enquiries", json={
        "property_listing_id": lid, "message": "spoof?", "buyer_user_id": buyer_b,
        "sender_user_id": buyer_b, "owner_user_id": buyer_b}, headers=bh_a)
    assert r.status_code == 201
    eid = r.json()["id"]
    row = session.execute(text("SELECT buyer_user_id, owner_user_id FROM enquiries WHERE id=:e"),
                          {"e": eid}).fetchone()
    assert tuple(row) == (buyer_a, owner_id)
    r = c.post(f"/owner/leads/{eid}/messages",
               json={"message": "reply", "sender_user_id": buyer_a, "sender": "BUYER"}, headers=oh)
    assert r.status_code == 201 and r.json()["sender"] == "OWNER"
    assert _stored_senders(session, eid) == [(buyer_a, "spoof?"), (owner_id, "reply")]

    # Buyer B cannot read buyer A's thread (404: no existence leak) nor see it listed.
    assert c.get(f"/buyer/enquiries/{eid}", headers=bh_b).status_code == 404
    assert c.get("/buyer/enquiries", headers=bh_b).json()["total"] == 0
    # Nor read, mark or delete buyer A's notification.
    nid = _notifs(c, bh_a)["items"][0]["id"]
    assert c.post(f"/notifications/{nid}/read", headers=bh_b).status_code == 404
    assert c.delete(f"/notifications/{nid}", headers=bh_b).status_code == 404
    assert _notifs(c, bh_a)["items"][0]["is_read"] is False
    # Owner B cannot touch owner A's lead (status, notes, contact reveal).
    other_oh, _, _ = _owner(c, session)
    assert c.patch(f"/owner/leads/{eid}", json={"status": "CONTACTED"}, headers=other_oh).status_code == 404
    assert c.get(f"/owner/leads/{eid}/notes", headers=other_oh).status_code == 404
    assert c.post(f"/owner/leads/{eid}/contact", headers=other_oh).status_code == 404
    # Unauthenticated access is refused everywhere.
    for method, url in (("get", f"/buyer/enquiries/{eid}"), ("get", f"/owner/leads/{eid}"),
                        ("get", "/notifications"), ("post", f"/owner/leads/{eid}/messages")):
        assert getattr(c, method)(url).status_code == 401


def test_duplicate_retries_store_once_and_notify_once(client):
    c, session = client
    oh, _, lid = _owner(c, session)
    bh, _ = _buyer(c)
    first = c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": "same"}, headers=bh)
    retry = c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": "same"}, headers=bh)
    assert (first.status_code, retry.status_code) == (201, 200)
    assert first.json()["id"] == retry.json()["id"]
    eid = first.json()["id"]
    _reply(c, oh, eid, "ok")
    _reply(c, oh, eid, "ok", expect=200)  # double tap: stored reply returned, nothing new
    assert [b for _, b in _stored_senders(session, eid)] == ["same", "ok"]
    assert _types(c, oh) == ["ENQUIRY_NEW"]
    assert _types(c, bh) == ["ENQUIRY_REPLY"]


def test_failed_reply_on_closed_lead_writes_nothing(client):
    c, session = client
    oh, _, lid = _owner(c, session)
    bh, _ = _buyer(c)
    eid = _enquire(c, bh, lid, "hi")["id"]
    assert c.patch(f"/owner/leads/{eid}", json={"status": "CLOSED", "closing_reason": "Not Converted"},
                   headers=oh).status_code == 200
    before = _notifs(c, bh)["total"]
    r = c.post(f"/owner/leads/{eid}/messages", json={"message": "late"}, headers=oh)
    assert r.status_code == 422
    assert [b for _, b in _stored_senders(session, eid)] == ["hi"]
    assert _notifs(c, bh)["total"] == before


# --- Enquiry eligibility --------------------------------------------------------------------

def test_enquiry_refused_for_ineligible_listings(client):
    c, session = client
    bh, _ = _buyer(c)
    before = session.execute(text("SELECT COUNT(*) FROM enquiries")).scalar()
    for verification, listing_status in (("PENDING", "AVAILABLE"), ("REJECTED", "AVAILABLE"),
                                         ("VERIFIED", "CLOSED"), ("VERIFIED", "SOLD")):
        lid, _, _ = seed_owner_listing(session, verification=verification, status=listing_status)
        r = c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": "x"}, headers=bh)
        assert r.status_code == 404, (verification, listing_status, r.text)
    lid, _, _ = seed_owner_listing(session)
    session.execute(text("UPDATE property_listings SET deleted_at=UTC_TIMESTAMP() WHERE id=:l"), {"l": lid})
    assert c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": "x"},
                  headers=bh).status_code == 404
    assert c.post("/buyer/enquiries", json={"property_listing_id": 999999999, "message": "x"},
                  headers=bh).status_code == 404
    assert session.execute(text("SELECT COUNT(*) FROM enquiries")).scalar() == before


# --- Site visits (strict Phase 5: PENDING, owner-only confirm/reject) -----------------------

def _visit_setup(c, session, poster="owner"):
    ph, pid, lid = (_owner if poster == "owner" else _dealer_poster)(c, session)
    bh, bid = _buyer(c)
    return ph, pid, lid, bh, bid


def _request(c, bh, lid, when=None, note=None):
    body = {"property_listing_id": lid, "scheduled_at": when or _future()}
    if note is not None:
        body["note"] = note
    return c.post("/buyer/visits", json=body, headers=bh)


def _patch(c, h, who, vid, **body):
    return c.patch(f"/{'buyer' if who == 'buyer' else 'owner'}/visits/{vid}", json=body, headers=h)


def test_visit_request_confirm_complete_with_notifications(client):
    c, session = client
    oh, owner_id, lid, bh, buyer_id = _visit_setup(c, session)
    r = _request(c, bh, lid, note="after 5pm")
    assert r.status_code == 201, r.text
    v = r.json()
    assert (v["status"], v["initiator"], v["buyer_user_id"], v["owner_user_id"], v["property_listing_id"]) == (
        "PENDING", "BUYER", buyer_id, owner_id, lid)
    vid = v["id"]
    assert _types(c, oh) == ["VISIT_REQUESTED"]
    owner_visit = c.get(f"/owner/visits/{vid}", headers=oh).json()
    assert owner_visit["note"] == "after 5pm"
    assert [x["id"] for x in c.get("/owner/visits", params={"status": "PENDING"}, headers=oh).json()["items"]] == [vid]

    # The requester can neither confirm nor edit their own request.
    assert _patch(c, bh, "buyer", vid, status="CONFIRMED").status_code == 422
    assert _patch(c, bh, "buyer", vid, scheduled_at=_future(5)).status_code == 422
    assert _patch(c, bh, "buyer", vid, note="changed").status_code == 422
    # Owner may not move the time of a pending request either (only confirm/reject).
    assert _patch(c, oh, "owner", vid, scheduled_at=_future(5)).status_code == 422

    r = _patch(c, oh, "owner", vid, status="CONFIRMED")
    assert r.status_code == 200 and r.json()["status"] == "CONFIRMED"
    assert _types(c, bh) == ["VISIT_CONFIRMED"]
    # Confirmed: immutable schedule; repeat confirm is an idempotent no-op.
    assert _patch(c, oh, "owner", vid, scheduled_at=_future(6)).status_code == 422
    assert _patch(c, bh, "buyer", vid, note="x").status_code == 422
    assert _patch(c, oh, "owner", vid, status="CONFIRMED").status_code == 200
    assert _types(c, bh) == ["VISIT_CONFIRMED"]  # no second notification
    assert _patch(c, bh, "buyer", vid, status="COMPLETED").status_code == 422  # owner only

    r = _patch(c, oh, "owner", vid, status="COMPLETED")
    assert r.status_code == 200 and r.json()["status"] == "COMPLETED"
    # Completed is terminal: cannot be cancelled by anyone; history kept.
    assert _patch(c, bh, "buyer", vid, status="CANCELLED").status_code == 422
    assert _patch(c, oh, "owner", vid, status="CANCELLED").status_code == 422
    assert session.execute(text("SELECT status FROM property_visits WHERE id=:v"), {"v": vid}).scalar() == "COMPLETED"
    assert c.get(f"/buyer/visits/{vid}", headers=bh).json()["status"] == "COMPLETED"


def test_visit_owner_reject_notifies_buyer_and_is_terminal(client):
    c, session = client
    oh, _, lid, bh, _ = _visit_setup(c, session)
    vid = _request(c, bh, lid).json()["id"]
    r = _patch(c, oh, "owner", vid, status="REJECTED")
    assert r.status_code == 200 and r.json()["status"] == "REJECTED"
    items = _notifs(c, bh)["items"]
    assert [n["type"] for n in items] == ["VISIT_REJECTED"]
    assert items[0]["target"] == {"kind": "BUYER_VISIT", "id": vid}
    for who, h, st in (("owner", oh, "CONFIRMED"), ("owner", oh, "COMPLETED"),
                       ("buyer", bh, "CANCELLED"), ("owner", oh, "CANCELLED")):
        assert _patch(c, h, who, vid, status=st).status_code == 422, (who, st)
    assert _patch(c, oh, "owner", vid, status="REJECTED").status_code == 200  # idempotent
    assert _types(c, bh) == ["VISIT_REJECTED"]
    assert [x["id"] for x in c.get("/buyer/visits", params={"status": "REJECTED"}, headers=bh).json()["items"]] == [vid]
    # A rejection frees the pair: the buyer may request again.
    assert _request(c, bh, lid).status_code == 201


def test_visit_buyer_cancels_pending_and_confirmed_owner_notified(client):
    c, session = client
    oh, _, lid, bh, _ = _visit_setup(c, session)
    vid = _request(c, bh, lid).json()["id"]
    r = _patch(c, bh, "buyer", vid, status="CANCELLED")
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED"
    assert _types(c, oh) == ["VISIT_CANCELLED", "VISIT_REQUESTED"]
    assert _patch(c, oh, "owner", vid, status="CONFIRMED").status_code == 422  # accept a cancelled visit

    vid2 = _request(c, bh, lid).json()["id"]
    assert _patch(c, oh, "owner", vid2, status="CONFIRMED").status_code == 200
    r = _patch(c, bh, "buyer", vid2, status="CANCELLED")
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED"
    assert _types(c, oh).count("VISIT_CANCELLED") == 2


def test_visit_owner_proposal_lifecycle(client):
    """Owner (or dealer) proposes -> PENDING (initiator OWNER) -> buyer is
    notified; strict Phase 5: only the poster confirms/rejects; the buyer
    may cancel; the proposer cannot edit the proposal."""
    c, session = client
    dh, dealer_id, lid, bh, buyer_id = _visit_setup(c, session, poster="dealer")
    _enquire(c, bh, lid, "interested")  # the relationship that allows a proposal
    r = c.post("/owner/visits", json={"property_listing_id": lid, "buyer_user_id": buyer_id,
                                      "scheduled_at": _future()}, headers=dh)
    assert r.status_code == 201, r.text
    v = r.json()
    assert (v["status"], v["initiator"], v["owner_user_id"]) == ("PENDING", "OWNER", dealer_id)
    assert "VISIT_PROPOSED" in _types(c, bh)
    assert _patch(c, bh, "buyer", v["id"], status="CONFIRMED").status_code == 422
    assert _patch(c, dh, "owner", v["id"], scheduled_at=_future(4)).status_code == 422  # proposer edit
    assert _patch(c, dh, "owner", v["id"], note="n").status_code == 422
    assert _patch(c, dh, "owner", v["id"], status="CONFIRMED").status_code == 200
    assert "VISIT_CONFIRMED" in _types(c, bh)
    assert _patch(c, bh, "buyer", v["id"], status="CANCELLED").status_code == 200
    assert "VISIT_CANCELLED" in _types(c, dh)


def test_visit_isolation_duplicates_and_lead_close(client):
    c, session = client
    oh, _, lid, bh, buyer_id = _visit_setup(c, session)
    other_oh, _, _ = _owner(c, session)
    other_bh, _ = _buyer(c)
    _enquire(c, bh, lid, "hi")
    vid = _request(c, bh, lid).json()["id"]
    # Accepting/updating someone else's visit: 404, nothing changes.
    assert _patch(c, other_oh, "owner", vid, status="CONFIRMED").status_code == 404
    assert _patch(c, other_bh, "buyer", vid, status="CANCELLED").status_code == 404
    assert c.get(f"/owner/visits/{vid}", headers=other_oh).status_code == 404
    assert c.get(f"/buyer/visits/{vid}", headers=other_bh).status_code == 404
    # Duplicate request while one is pending: refused.
    assert _request(c, bh, lid).status_code == 422
    # Past time and own listing: refused.
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    assert _request(c, other_bh, lid, when=past).status_code == 422
    assert _request(c, oh, lid).status_code == 422
    # Closing the lead cancels the PENDING visit in the same transaction.
    eid = session.execute(text("SELECT id FROM enquiries WHERE buyer_user_id=:b AND property_listing_id=:l"),
                          {"b": buyer_id, "l": lid}).scalar()
    assert c.patch(f"/owner/leads/{eid}", json={"status": "CLOSED", "closing_reason": "Not Converted"},
                   headers=oh).status_code == 200
    assert session.execute(text("SELECT status FROM property_visits WHERE id=:v"), {"v": vid}).scalar() == "CANCELLED"
    assert _patch(c, oh, "owner", vid, status="CONFIRMED").status_code == 422


def test_visit_status_enum_model_matches_database(client):
    """The ORM enum must list exactly the DB enum values (REJECTED was in
    the migration but missing from the model)."""
    from app.models.visits import PropertyVisit

    _, session = client
    col = session.execute(text(
        "SELECT COLUMN_TYPE, COLUMN_DEFAULT FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
        "AND TABLE_NAME='property_visits' AND COLUMN_NAME='status'")).fetchone()
    db_values = {v.strip("'") for v in col[0][len("enum("):-1].split(",")}
    assert db_values == set(PropertyVisit.__table__.c.status.type.enums)
    assert {"PENDING", "REJECTED", "CONFIRMED", "CANCELLED", "COMPLETED"} <= db_values
    assert col[1] == PropertyVisit.__table__.c.status.server_default.arg


# --- Timestamps (UTC storage, explicit-UTC API, IST display) --------------------------------

def test_visit_9pm_ist_is_stored_and_returned_as_the_same_instant(client):
    c, session = client
    oh, _, lid, bh, _ = _visit_setup(c, session)
    r = _request(c, bh, lid, when="2030-11-21T21:00:00+05:30")
    assert r.status_code == 201, r.text
    v = r.json()
    stored = session.execute(text("SELECT scheduled_at FROM property_visits WHERE id=:v"), {"v": v["id"]}).scalar()
    assert stored == datetime(2030, 11, 21, 15, 30, 0)  # UTC instant, naive DATETIME
    assert v["scheduled_at"] == "2030-11-21T15:30:00Z"  # explicit UTC on the wire
    assert datetime.fromisoformat(v["scheduled_at"]) == datetime.fromisoformat("2030-11-21T21:00:00+05:30")
    body = _notifs(c, oh)["items"][0]["body"]
    assert "21 Nov 2030, 9:00 PM IST" in body  # converted exactly once for display
    for key in ("created_at", "updated_at"):
        assert v[key].endswith("Z")


def test_visit_after_ist_midnight_keeps_the_ist_date_and_sorts_by_instant(client):
    c, session = client
    oh, _, lid, bh, _ = _visit_setup(c, session)
    # 22 Nov 00:30 IST is still 21 Nov in UTC (19:00Z).
    v = _request(c, bh, lid, when="2030-11-22T00:30:00+05:30").json()
    assert v["scheduled_at"] == "2030-11-21T19:00:00Z"
    assert "22 Nov 2030, 12:30 AM IST" in _notifs(c, oh)["items"][0]["body"]
    # 21 Nov 23:30 IST (18:00Z) is earlier, even though both are "21 Nov" in UTC.
    other_bh, _ = _buyer(c)
    w = _request(c, other_bh, lid, when="2030-11-21T23:30:00+05:30").json()
    assert w["scheduled_at"] == "2030-11-21T18:00:00Z"
    assert datetime.fromisoformat(w["scheduled_at"]) < datetime.fromisoformat(v["scheduled_at"])


def test_visit_created_at_is_stable_and_updated_at_moves(client):
    c, session = client
    oh, _, lid, bh, _ = _visit_setup(c, session)
    v = _request(c, bh, lid).json()
    session.execute(text("UPDATE property_visits SET created_at=created_at - INTERVAL 1 HOUR, "
                         "updated_at=updated_at - INTERVAL 1 HOUR WHERE id=:v"), {"v": v["id"]})
    before = c.get(f"/owner/visits/{v['id']}", headers=oh).json()
    after = _patch(c, oh, "owner", v["id"], status="CONFIRMED").json()
    assert after["created_at"] == before["created_at"]
    assert datetime.fromisoformat(after["updated_at"]) > datetime.fromisoformat(before["updated_at"])
    # updated_at is a UTC instant close to now (never an IST wall-clock value).
    drift = abs(datetime.fromisoformat(after["updated_at"]) - datetime.now(timezone.utc))
    assert drift < timedelta(minutes=2)


def test_message_and_notification_timestamps_are_utc_instants_near_now(client):
    c, session = client
    oh, _, lid = _owner(c, session)
    bh, _ = _buyer(c)
    now = datetime.now(timezone.utc)
    enq = _enquire(c, bh, lid, "time check")
    for value in (enq["created_at"], enq["messages"][0]["created_at"],
                  _notifs(c, oh)["items"][0]["created_at"]):
        assert value.endswith("Z")
        assert abs(datetime.fromisoformat(value) - now) < timedelta(minutes=2)
