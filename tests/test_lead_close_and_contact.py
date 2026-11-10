"""Closed-lead lifecycle and the owner's buyer-contact reveal.

- POST /owner/leads/{id}/contact: the lead's owner gets the buyer's verified
  phone (audited, number never logged); everyone else gets 404/403/401, and
  lead list/detail responses stay phone-free.
- Closing a lead cancels its active visits (REQUESTED/PROPOSED/CONFIRMED) in
  the same transaction, leaves terminal history alone, and makes the lead and
  its visits read-only for both parties ("Lead is closed" /
  "Lead is already closed", 422). A new enquiry opens a fresh lead.

Rollback-isolated `client` fixture; local test DB only.
"""
import json

from sqlalchemy import text

from app.services import visits as visit_svc
from tests.conftest import auth_header
from tests.test_visits import FUTURE, _SEQ, _buyer, _listing, _no_phone, _owner

_CLOSE = {"status": "CLOSED", "closing_reason": "Not Converted"}


def _setup(c):
    """Owner + live listing + buyer with an open lead. Returns a dict."""
    _, otoken = _owner(c)
    _SEQ["n"] += 1
    buyer_phone = f"+91940000{_SEQ['n']:04d}"
    buyer = _buyer(c, buyer_phone)
    lid = _listing(c, otoken)
    bh, oh = auth_header(buyer["access_token"]), auth_header(otoken)
    r = c.post("/buyer/enquiries", json={"property_listing_id": lid}, headers=bh)
    assert r.status_code == 201, r.text
    return {"lid": lid, "lead": r.json()["id"], "buyer": buyer, "buyer_phone": buyer_phone,
            "bh": bh, "oh": oh}


def _request(c, s, days=5):
    r = c.post("/buyer/visits", json={"property_listing_id": s["lid"], "scheduled_at": FUTURE(days)},
               headers=s["bh"])
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _propose(c, s, days=6):
    r = c.post("/owner/visits", json={"property_listing_id": s["lid"], "buyer_user_id": s["buyer"]["user_id"],
                                      "scheduled_at": FUTURE(days)}, headers=s["oh"])
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _status(c, s, vid):
    buyer_side = c.get(f"/buyer/visits/{vid}", headers=s["bh"])
    owner_side = c.get(f"/owner/visits/{vid}", headers=s["oh"])
    assert buyer_side.status_code == 200 and owner_side.status_code == 200
    assert buyer_side.json()["status"] == owner_side.json()["status"]
    return buyer_side.json()["status"]


def _audits(session, action, lead_id):
    rows = session.execute(
        text("SELECT before_json, after_json FROM audit_logs WHERE action=:a "
             "AND JSON_EXTRACT(after_json, '$.enquiry_id') = :e"), {"a": action, "e": lead_id}
    ).fetchall()
    return [tuple(json.loads(v) if isinstance(v, str) else v for v in row) for row in rows]


# --- buyer contact for Call / WhatsApp -------------------------------------------

def test_owner_gets_buyer_phone_for_own_lead_audited_without_the_number(client):
    c, session = client
    s = _setup(c)
    r = c.post(f"/owner/leads/{s['lead']}/contact", headers=s["oh"])
    assert r.status_code == 200, r.text
    assert r.json() == {"phone": s["buyer_phone"]}
    audit = _audits(session, "LEAD_CONTACT_REVEALED", s["lead"])
    assert len(audit) == 1
    assert audit[0][1] == {"enquiry_id": s["lead"], "buyer_user_id": s["buyer"]["user_id"]}
    assert s["buyer_phone"].lstrip("+") not in json.dumps(audit, default=str)
    # Lead list and detail stay phone-free: the reveal is the only way in.
    _no_phone(c.get("/owner/leads", headers=s["oh"]).json(), "lead list", [s["buyer_phone"]])
    _no_phone(c.get(f"/owner/leads/{s['lead']}", headers=s["oh"]).json(), "lead detail", [s["buyer_phone"]])


def test_buyer_phone_is_not_reachable_by_anyone_else(client):
    c, _ = client
    s = _setup(c)
    _, other_token = _owner(c)
    _listing(c, other_token)  # a real owner with listings of their own
    other = c.post(f"/owner/leads/{s['lead']}/contact", headers=auth_header(other_token))
    assert other.status_code == 404 and other.json() == {"detail": "Not found"}
    assert "phone" not in other.text
    assert c.post(f"/owner/leads/{s['lead']}/contact", headers=s["bh"]).status_code == 403
    assert c.post(f"/owner/leads/{s['lead']}/contact").status_code == 401
    assert c.post("/owner/leads/999999999/contact", headers=s["oh"]).status_code == 404
    # GET is not a reveal route (reads never write the audit row).
    assert c.get(f"/owner/leads/{s['lead']}/contact", headers=s["oh"]).status_code == 405


def test_buyer_without_a_phone_identity_is_contact_not_available(client):
    c, session = client
    s = _setup(c)
    session.execute(text("DELETE FROM auth_identities WHERE user_id=:u AND provider='phone'"),
                    {"u": s["buyer"]["user_id"]})
    r = c.post(f"/owner/leads/{s['lead']}/contact", headers=s["oh"])
    assert r.status_code == 404 and r.json() == {"detail": "Contact not available"}
    assert _audits(session, "LEAD_CONTACT_REVEALED", s["lead"]) == []


def test_closed_lead_contact_stays_readable(client):
    c, _ = client
    s = _setup(c)
    assert c.patch(f"/owner/leads/{s['lead']}", json=_CLOSE, headers=s["oh"]).status_code == 200
    r = c.post(f"/owner/leads/{s['lead']}/contact", headers=s["oh"])
    assert r.status_code == 200 and r.json()["phone"] == s["buyer_phone"]


# --- closing a lead closes its visit ----------------------------------------------

def test_close_cancels_a_requested_visit(client):
    c, session = client
    s = _setup(c)
    vid = _request(c, s)
    closed = c.patch(f"/owner/leads/{s['lead']}", json=_CLOSE, headers=s["oh"])
    assert closed.status_code == 200 and closed.json()["status"] == "CLOSED"
    assert _status(c, s, vid) == "CANCELLED"
    audit = _audits(session, "LEAD_VISITS_CANCELLED", s["lead"])
    assert audit == [({"enquiry_id": s["lead"], "visits": [{"id": vid, "status": "REQUESTED"}]},
                      {"enquiry_id": s["lead"], "visits": [{"id": vid, "status": "CANCELLED"}]})]


def test_close_cancels_a_proposed_visit(client):
    c, _ = client
    s = _setup(c)
    vid = _propose(c, s)
    assert c.patch(f"/owner/leads/{s['lead']}", json={"status": "CLOSED", "closing_reason": "Sold"},
                   headers=s["oh"]).status_code == 200
    assert _status(c, s, vid) == "CANCELLED"


def test_close_cancels_a_confirmed_and_rescheduled_visit(client):
    c, _ = client
    s = _setup(c)
    vid = _request(c, s)
    assert c.patch(f"/owner/visits/{vid}", json={"status": "CONFIRMED"}, headers=s["oh"]).status_code == 200
    moved = c.patch(f"/owner/visits/{vid}", json={"scheduled_at": FUTURE(9)}, headers=s["oh"])
    assert moved.status_code == 200 and moved.json()["rescheduled_at"] is not None
    assert c.patch(f"/owner/leads/{s['lead']}", json={"status": "CLOSED", "closing_reason": "Rented"},
                   headers=s["oh"]).status_code == 200
    row = c.get(f"/buyer/visits/{vid}", headers=s["bh"]).json()
    assert row["status"] == "CANCELLED" and row["rescheduled_at"] is None


def test_close_with_no_visit_succeeds_without_a_visit_audit(client):
    c, session = client
    s = _setup(c)
    r = c.patch(f"/owner/leads/{s['lead']}", json={"status": "CLOSED", "closing_reason": "Leased"},
                headers=s["oh"])
    assert r.status_code == 200 and r.json()["closing_reason"] == "Leased"
    assert len(_audits(session, "LEAD_CLOSED", s["lead"])) == 1
    assert _audits(session, "LEAD_VISITS_CANCELLED", s["lead"]) == []


def test_close_keeps_historical_visits_and_cancels_only_the_active_one(client):
    c, _ = client
    s = _setup(c)
    done = _request(c, s, days=3)
    assert c.patch(f"/owner/visits/{done}", json={"status": "CONFIRMED"}, headers=s["oh"]).status_code == 200
    assert c.patch(f"/owner/visits/{done}", json={"status": "COMPLETED"}, headers=s["oh"]).status_code == 200
    declined = _request(c, s, days=4)
    assert c.patch(f"/owner/visits/{declined}", json={"status": "DECLINED"}, headers=s["oh"]).status_code == 200
    active = _request(c, s, days=5)
    assert c.patch(f"/owner/visits/{active}", json={"status": "CONFIRMED"}, headers=s["oh"]).status_code == 200
    assert c.patch(f"/owner/leads/{s['lead']}", json=_CLOSE, headers=s["oh"]).status_code == 200
    assert (_status(c, s, done), _status(c, s, declined), _status(c, s, active)) == (
        "COMPLETED", "DECLINED", "CANCELLED")
    # History stays readable on both sides (lists included).
    assert {done, declined, active} <= {v["id"] for v in c.get("/buyer/visits", headers=s["bh"]).json()["items"]}
    owner_rows = c.get("/owner/visits", params={"property_listing_id": s["lid"]}, headers=s["oh"]).json()["items"]
    assert {done, declined, active} <= {v["id"] for v in owner_rows}
    lead = c.get(f"/owner/leads/{s['lead']}", headers=s["oh"])
    assert lead.status_code == 200 and lead.json()["status"] == "CLOSED"


def test_close_leaves_other_buyers_visits_alone(client):
    c, _ = client
    s = _setup(c)
    mine = _request(c, s)
    other = _buyer(c)
    oh2 = auth_header(other["access_token"])
    assert c.post("/buyer/enquiries", json={"property_listing_id": s["lid"]}, headers=oh2).status_code == 201
    theirs = c.post("/buyer/visits", json={"property_listing_id": s["lid"], "scheduled_at": FUTURE(5)},
                    headers=oh2).json()["id"]
    assert c.patch(f"/owner/leads/{s['lead']}", json=_CLOSE, headers=s["oh"]).status_code == 200
    assert _status(c, s, mine) == "CANCELLED"
    assert c.get(f"/buyer/visits/{theirs}", headers=oh2).json()["status"] == "REQUESTED"


def test_close_and_visit_cancel_are_one_transaction(client, monkeypatch):
    c, session = client
    s = _setup(c)
    vid = _request(c, s)
    real = visit_svc.close_visits_for_lead

    def _fail_after_cancelling(db, enquiry):
        real(db, enquiry)  # visits already flipped to CANCELLED in this transaction...
        raise ValueError("simulated failure")  # ...then the close fails

    monkeypatch.setattr(visit_svc, "close_visits_for_lead", _fail_after_cancelling)
    assert c.patch(f"/owner/leads/{s['lead']}", json=_CLOSE, headers=s["oh"]).status_code == 422
    monkeypatch.setattr(visit_svc, "close_visits_for_lead", real)
    session.expire_all()
    # Neither half landed: the lead is still open and active, the visit still REQUESTED.
    assert c.get(f"/owner/leads/{s['lead']}", headers=s["oh"]).json()["status"] == "NEW"
    assert session.execute(text("SELECT COUNT(*) FROM active_enquiries WHERE enquiry_id=:e"),
                           {"e": s["lead"]}).scalar() == 1
    assert _status(c, s, vid) == "REQUESTED"
    assert _audits(session, "LEAD_CLOSED", s["lead"]) == []


# --- a closed lead is read-only ----------------------------------------------------

def test_closed_lead_cannot_be_closed_or_changed_again(client):
    c, session = client
    s = _setup(c)
    assert c.patch(f"/owner/leads/{s['lead']}", json=_CLOSE, headers=s["oh"]).status_code == 200
    for body in (_CLOSE, {"status": "CLOSED", "closing_reason": "Sold"}, {"status": "NEW"}, {"status": "CONTACTED"}):
        r = c.patch(f"/owner/leads/{s['lead']}", json=body, headers=s["oh"])
        assert r.status_code == 422 and r.json() == {"detail": "Lead is already closed"}, (body, r.text)
    assert len(_audits(session, "LEAD_CLOSED", s["lead"])) == 1
    lead = c.get(f"/owner/leads/{s['lead']}", headers=s["oh"]).json()
    assert (lead["status"], lead["closing_reason"]) == ("CLOSED", "Not Converted")
    # Another owner still cannot learn the lead exists.
    _, other_token = _owner(c)
    assert c.patch(f"/owner/leads/{s['lead']}", json=_CLOSE, headers=auth_header(other_token)).status_code == 404


def test_closed_lead_gets_no_new_visit_from_either_side(client):
    c, _ = client
    s = _setup(c)
    assert c.patch(f"/owner/leads/{s['lead']}", json=_CLOSE, headers=s["oh"]).status_code == 200
    proposal = c.post("/owner/visits", json={"property_listing_id": s["lid"], "buyer_user_id": s["buyer"]["user_id"],
                                             "scheduled_at": FUTURE(6)}, headers=s["oh"])
    assert proposal.status_code == 422 and proposal.json() == {"detail": "Lead is closed"}
    request = c.post("/buyer/visits", json={"property_listing_id": s["lid"], "scheduled_at": FUTURE(6)},
                     headers=s["bh"])
    assert request.status_code == 422 and request.json() == {"detail": "Lead is closed"}


def test_neither_party_can_touch_a_closed_leads_visit(client):
    c, _ = client
    s = _setup(c)
    vid = _request(c, s)
    assert c.patch(f"/owner/visits/{vid}", json={"status": "CONFIRMED"}, headers=s["oh"]).status_code == 200
    assert c.patch(f"/owner/leads/{s['lead']}", json=_CLOSE, headers=s["oh"]).status_code == 200
    attempts = [
        ("owner", {"status": "CONFIRMED"}), ("owner", {"status": "COMPLETED"}),
        ("owner", {"scheduled_at": FUTURE(10)}), ("owner", {"status": "CANCELLED"}),
        ("owner", {"note": "late note"}),
        ("buyer", {"status": "CONFIRMED"}), ("buyer", {"scheduled_at": FUTURE(10)}),
        ("buyer", {"status": "CANCELLED"}),  # even the same-state repeat from a stale screen
    ]
    for who, body in attempts:
        url = f"/{who}/visits/{vid}"
        r = c.patch(url, json=body, headers=s["oh"] if who == "owner" else s["bh"])
        assert r.status_code == 422 and r.json() == {"detail": "Lead is closed"}, (who, body, r.text)
    assert _status(c, s, vid) == "CANCELLED"
    # Scope checks still come first: a stranger learns nothing.
    stranger = auth_header(_buyer(c)["access_token"])
    assert c.patch(f"/buyer/visits/{vid}", json={"status": "CANCELLED"}, headers=stranger).status_code == 404
    _, other_token = _owner(c)
    assert c.patch(f"/owner/visits/{vid}", json={"status": "CANCELLED"},
                   headers=auth_header(other_token)).status_code == 404


def test_new_enquiry_opens_a_fresh_lead_that_can_schedule_again(client):
    c, _ = client
    s = _setup(c)
    old = _request(c, s)
    assert c.patch(f"/owner/leads/{s['lead']}", json=_CLOSE, headers=s["oh"]).status_code == 200
    again = c.post("/buyer/enquiries", json={"property_listing_id": s["lid"]}, headers=s["bh"])
    assert again.status_code == 201 and again.json()["id"] != s["lead"]
    vid = _propose(c, s)
    assert c.patch(f"/buyer/visits/{vid}", json={"status": "CONFIRMED"}, headers=s["bh"]).status_code == 200
    # The old lead and its visit stay closed history.
    assert _status(c, s, old) == "CANCELLED"
    assert c.patch(f"/owner/leads/{s['lead']}", json=_CLOSE, headers=s["oh"]).status_code == 422
