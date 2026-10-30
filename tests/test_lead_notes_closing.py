"""CONTRACT §F/§G — private lead notes (D4) and lead closing outcomes (D5).

Notes: owner-only, append-only, trimmed 1-2000 chars, allowed on closed
leads, never present in any buyer, visit or lead-list/detail response.
Closing: exactly Rented/Sold/Leased/Not Converted, required with CLOSED and
refused otherwise, terminal (same repeat = no-op, different = 422), one
audit_logs row, listing status untouched.
Rollback-isolated.
"""
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from tests.conftest import auth_header
from tests.test_owner_leads import fresh_buyer, owner_header, seed_owner_listing

SECRET = "PRIVATE-NOTE-7f3a: buyer seems price sensitive"


def _lead(c, session):
    lid, _, phone = seed_owner_listing(session)
    buyer = fresh_buyer(c)
    hb = auth_header(buyer["access_token"])
    eid = c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": "Hi"}, headers=hb).json()["id"]
    ho, _ = owner_header(c, phone)
    return lid, eid, hb, ho


def test_notes_add_list_and_validation(client):
    c, session = client
    _, eid, _, ho = _lead(c, session)
    assert c.get(f"/owner/leads/{eid}/notes", headers=ho).json() == {"items": [], "total": 0}
    first = c.post(f"/owner/leads/{eid}/notes", json={"body": "  Called, no answer  "}, headers=ho)
    assert first.status_code == 201
    assert first.json()["body"] == "Called, no answer" and first.json()["created_at"].endswith("Z")
    second = c.post(f"/owner/leads/{eid}/notes", json={"body": "Visit Saturday"}, headers=ho)
    listed = c.get(f"/owner/leads/{eid}/notes", headers=ho).json()
    assert listed["total"] == 2
    assert [n["id"] for n in listed["items"]] == [first.json()["id"], second.json()["id"]]  # oldest first, kept
    for bad in ("", "   ", "x" * 2001):
        assert c.post(f"/owner/leads/{eid}/notes", json={"body": bad}, headers=ho).status_code == 422


def test_notes_are_owner_only(client):
    c, session = client
    lid, eid, hb, ho = _lead(c, session)
    _, _, other_phone = seed_owner_listing(session)
    hx, _ = owner_header(c, other_phone)
    assert c.get(f"/owner/leads/{eid}/notes").status_code == 401
    assert c.get(f"/owner/leads/{eid}/notes", headers=hb).status_code == 403
    assert c.post(f"/owner/leads/{eid}/notes", json={"body": "x"}, headers=hb).status_code == 403
    assert c.get(f"/owner/leads/{eid}/notes", headers=hx).status_code == 404
    assert c.post(f"/owner/leads/{eid}/notes", json={"body": "x"}, headers=hx).status_code == 404
    assert c.get("/owner/leads/999999999/notes", headers=ho).status_code == 404


def test_notes_never_leak_into_other_responses(client):
    c, session = client
    lid, eid, hb, ho = _lead(c, session)
    assert c.post(f"/owner/leads/{eid}/notes", json={"body": SECRET}, headers=ho).status_code == 201
    future = (datetime.now(timezone.utc) + timedelta(days=3)).replace(microsecond=0).isoformat()
    assert c.post("/buyer/visits", json={"property_listing_id": lid, "scheduled_at": future}, headers=hb).status_code == 201
    responses = [
        c.get("/buyer/enquiries", headers=hb), c.get(f"/buyer/enquiries/{eid}", headers=hb),
        c.get("/buyer/visits", headers=hb), c.get(f"/buyer/listings/{lid}"),
        c.get("/owner/leads", headers=ho), c.get(f"/owner/leads/{eid}", headers=ho),
        c.get("/owner/visits", headers=ho), c.get(f"/owner/listings/{lid}", headers=ho),
    ]
    for r in responses:
        assert r.status_code == 200, r.url
        assert "PRIVATE-NOTE-7f3a" not in r.text, r.url


def test_close_requires_one_of_four_outcomes(client):
    c, session = client
    _, eid, _, ho = _lead(c, session)
    for body in ({"status": "CLOSED"}, {"status": "CLOSED", "closing_reason": "Lost"},
                 {"status": "CONTACTED", "closing_reason": "Sold"}, {"status": "CLOSED", "closing_reason": "sold"}):
        assert c.patch(f"/owner/leads/{eid}", json=body, headers=ho).status_code == 422, body
    for outcome in ("Rented", "Sold", "Leased", "Not Converted"):
        _, fresh, _, hf = _lead(c, session)
        r = c.patch(f"/owner/leads/{fresh}", json={"status": "CLOSED", "closing_reason": outcome}, headers=hf)
        assert r.status_code == 200, outcome
        assert r.json()["status"] == "CLOSED" and r.json()["closing_reason"] == outcome


def test_close_is_terminal_audited_and_leaves_listing_alone(client):
    c, session = client
    lid, eid, _, ho = _lead(c, session)
    before_listing = session.execute(
        text("SELECT verification_status, listing_status FROM property_listings WHERE id=:i"), {"i": lid}
    ).fetchone()
    assert c.patch(f"/owner/leads/{eid}", json={"status": "CONTACTED"}, headers=ho).status_code == 200
    closed = c.patch(f"/owner/leads/{eid}", json={"status": "CLOSED", "closing_reason": "Sold"}, headers=ho)
    assert closed.status_code == 200 and closed.json()["closing_reason"] == "Sold"
    audit = session.execute(
        text("SELECT action, entity, before_json, after_json FROM audit_logs "
             "WHERE action='LEAD_CLOSED' AND JSON_EXTRACT(after_json, '$.enquiry_id') = :e"), {"e": eid}
    ).fetchall()
    assert len(audit) == 1
    action, entity, before, after = audit[0]
    before, after = (json.loads(v) if isinstance(v, str) else v for v in (before, after))
    assert (action, entity) == ("LEAD_CLOSED", "enquiry")
    assert before == {"enquiry_id": eid, "status": "CONTACTED", "closing_reason": None}
    assert after == {"enquiry_id": eid, "status": "CLOSED", "closing_reason": "Sold"}
    repeat = c.patch(f"/owner/leads/{eid}", json={"status": "CLOSED", "closing_reason": "Sold"}, headers=ho)
    assert repeat.status_code == 200  # idempotent, no second audit row
    other = c.patch(f"/owner/leads/{eid}", json={"status": "CLOSED", "closing_reason": "Rented"}, headers=ho)
    assert other.status_code == 422 and other.json()["detail"] == "Lead is already closed"
    assert c.patch(f"/owner/leads/{eid}", json={"status": "NEW"}, headers=ho).status_code == 422
    assert session.execute(
        text("SELECT COUNT(*) FROM audit_logs WHERE action='LEAD_CLOSED' AND JSON_EXTRACT(after_json, '$.enquiry_id') = :e"),
        {"e": eid},
    ).scalar() == 1
    after_listing = session.execute(
        text("SELECT verification_status, listing_status FROM property_listings WHERE id=:i"), {"i": lid}
    ).fetchone()
    assert tuple(after_listing) == tuple(before_listing)
    # Notes remain possible on a closed lead.
    assert c.post(f"/owner/leads/{eid}/notes", json={"body": "Deal done"}, headers=ho).status_code == 201
