"""M9: posters may not send contact details before the buyer's unlock.

Through the real API on (disposable) MySQL. A refused poster reply or
visit-proposal note is a 422 CONTACT_DETAILS_NOT_ALLOWED with NO side effect:
the tables a successful request would touch (enquiry_messages,
property_visits, notifications, audit_logs, contact_reveal_audits,
property_contact_unlocks, contact_credit_ledger, contact_credit_accounts) are
compared before and after. Entitlement is ONLY a PropertyContactUnlock row for
the exact (buyer, listing). Buyer -> owner text is unaffected. Rollback-isolated.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app import config as config_module
from tests.test_communication_audit import _buyer, _dealer_poster, _enquire, _owner

PHONE = "Call me on 98401 23456"
EMAIL = "Mail me at owner.flat@gmail.com"
REFUSAL = {"detail": {"code": "CONTACT_DETAILS_NOT_ALLOWED",
                      "message": "Phone numbers can be shared after the buyer unlocks your contact."}}
TABLES = ["enquiry_messages", "property_visits", "notifications", "audit_logs", "contact_reveal_audits",
          "property_contact_unlocks", "contact_credit_ledger", "contact_credit_accounts", "enquiries"]


def _future(days=3):
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0).isoformat()


def _counts(session):
    return {t: session.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() for t in TABLES}


def _thread(session, eid):
    return session.execute(
        text("SELECT id, sender_user_id, body FROM enquiry_messages WHERE enquiry_id=:e ORDER BY id"), {"e": eid}
    ).fetchall()


def _unlock(c, bh, lid):
    r = c.post(f"/buyer/listings/{lid}/contact", headers=bh)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(params=["owner", "dealer"])
def poster(request, client):
    """An OWNER-mode owner, or a Dealer (BROKER mode) acting as the poster."""
    c, session = client
    if request.param == "owner":
        ph, pid, lid = _owner(c, session)
    else:
        ph, pid, lid = _dealer_poster(c, session)
    bh, bid = _buyer(c)
    eid = _enquire(c, bh, lid, "Is it still available?")["id"]
    return c, session, ph, pid, lid, bh, bid, eid


# --- replies -------------------------------------------------------------------

@pytest.mark.parametrize("body", [PHONE, EMAIL, "wa.me/919840123456", "Landline 044-2345 6789"])
def test_reply_with_contact_details_before_unlock_is_refused_without_side_effects(poster, body):
    c, session, ph, pid, lid, bh, bid, eid = poster
    before, thread_before = _counts(session), _thread(session, eid)
    r = c.post(f"/owner/leads/{eid}/messages", json={"message": body}, headers=ph)
    assert r.status_code == 422 and r.json() == REFUSAL
    assert _counts(session) == before  # no message, notification, audit, credit or unlock change
    assert _thread(session, eid) == thread_before  # thread history untouched
    # The buyer sees nothing new either.
    assert [m["body"] for m in c.get(f"/buyer/enquiries/{eid}", headers=bh).json()["messages"]] == ["Is it still available?"]


def test_reply_allowed_after_the_buyer_unlocks_this_exact_property(poster):
    c, session, ph, pid, lid, bh, bid, eid = poster
    _unlock(c, bh, lid)
    r = c.post(f"/owner/leads/{eid}/messages", json={"message": PHONE}, headers=ph)
    assert r.status_code == 201 and r.json()["body"] == PHONE
    assert c.get(f"/buyer/enquiries/{eid}", headers=bh).json()["messages"][-1]["body"] == PHONE


def test_unlocking_a_different_property_does_not_authorize_disclosure(client):
    c, session = client
    ph, _, lid = _owner(c, session)
    _, _, other_lid = _owner(c, session)
    bh, _ = _buyer(c)
    eid = _enquire(c, bh, lid, "Hi")["id"]
    _unlock(c, bh, other_lid)  # a real unlock, but for ANOTHER listing
    before = _counts(session)
    r = c.post(f"/owner/leads/{eid}/messages", json={"message": PHONE}, headers=ph)
    assert r.status_code == 422 and r.json() == REFUSAL
    assert _counts(session) == before


def test_reveal_audit_row_alone_does_not_qualify(client):
    c, session = client
    ph, _, lid = _owner(c, session)
    bh, bid = _buyer(c)
    eid = _enquire(c, bh, lid, "Hi")["id"]
    # A reveal-audit row without a PropertyContactUnlock (e.g. a Dealer-area reveal).
    session.execute(text("INSERT INTO contact_reveal_audits (user_id, property_listing_id) VALUES (:u, :l)"),
                    {"u": bid, "l": lid})
    session.flush()
    r = c.post(f"/owner/leads/{eid}/messages", json={"message": PHONE}, headers=ph)
    assert r.status_code == 422


def test_ordinary_property_text_is_allowed_before_unlock(poster):
    c, session, ph, pid, lid, bh, bid, eid = poster
    body = "Price ₹45,00,000 (45 lakh), 1200 sqft 2BHK, pincode 600042, visit at 10:30, door no 12"
    r = c.post(f"/owner/leads/{eid}/messages", json={"message": body}, headers=ph)
    assert r.status_code == 201


def test_client_supplied_fields_cannot_spoof_an_unlock(poster):
    c, session, ph, pid, lid, bh, bid, eid = poster
    r = c.post(f"/owner/leads/{eid}/messages",
               json={"message": PHONE, "unlocked": True, "buyer_user_id": pid, "entitled": True,
                     "property_listing_id": 1}, headers=ph)
    assert r.status_code == 422 and r.json() == REFUSAL


# --- buyer -> owner unaffected ----------------------------------------------------

def test_buyer_to_owner_message_with_phone_is_allowed(client):
    c, session = client
    _, _, lid = _owner(c, session)
    bh, _ = _buyer(c)
    first = _enquire(c, bh, lid, PHONE)
    follow = c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": EMAIL}, headers=bh)
    assert follow.status_code == 200
    assert [m["body"] for m in follow.json()["messages"]] == [PHONE, EMAIL]
    assert first["id"] == follow.json()["id"]


def test_buyer_visit_note_with_phone_is_allowed(client):
    c, session = client
    _, _, lid = _owner(c, session)
    bh, _ = _buyer(c)
    r = c.post("/buyer/visits", json={"property_listing_id": lid, "scheduled_at": _future(), "note": PHONE}, headers=bh)
    assert r.status_code == 201 and r.json()["note"] == PHONE


# --- visit proposals ----------------------------------------------------------------

def test_visit_proposal_note_with_contact_details_is_refused_without_side_effects(poster):
    c, session, ph, pid, lid, bh, bid, eid = poster
    before = _counts(session)
    r = c.post("/owner/visits", json={"property_listing_id": lid, "buyer_user_id": bid,
                                      "scheduled_at": _future(), "note": EMAIL}, headers=ph)
    assert r.status_code == 422 and r.json() == REFUSAL
    assert _counts(session) == before  # no visit row, notification, audit or credit change
    assert c.get("/buyer/visits", headers=bh).json()["total"] == 0


def test_visit_proposal_note_allowed_after_unlock_and_plain_notes_always(poster):
    c, session, ph, pid, lid, bh, bid, eid = poster
    plain = c.post("/owner/visits", json={"property_listing_id": lid, "buyer_user_id": bid,
                                          "scheduled_at": _future(), "note": "Gate 2, bring ID"}, headers=ph)
    assert plain.status_code == 201
    vid = plain.json()["id"]
    assert c.patch(f"/owner/visits/{vid}", json={"status": "CANCELLED"}, headers=ph).status_code == 200
    _unlock(c, bh, lid)
    r = c.post("/owner/visits", json={"property_listing_id": lid, "buyer_user_id": bid,
                                      "scheduled_at": _future(4), "note": PHONE}, headers=ph)
    assert r.status_code == 201 and r.json()["note"] == PHONE


def test_visit_proposal_without_note_is_unaffected(poster):
    c, session, ph, pid, lid, bh, bid, eid = poster
    r = c.post("/owner/visits", json={"property_listing_id": lid, "buyer_user_id": bid, "scheduled_at": _future()}, headers=ph)
    assert r.status_code == 201


def test_patch_visit_note_edits_remain_refused(poster):
    c, session, ph, pid, lid, bh, bid, eid = poster
    vid = c.post("/buyer/visits", json={"property_listing_id": lid, "scheduled_at": _future()}, headers=bh).json()["id"]
    for who, h in (("owner", ph), ("buyer", bh)):
        r = c.patch(f"/{who}/visits/{vid}", json={"note": PHONE}, headers=h)
        assert r.status_code == 422, (who, r.text)
    assert session.execute(text("SELECT note FROM property_visits WHERE id=:v"), {"v": vid}).scalar() is None


# --- credits and ordering ---------------------------------------------------------

def test_refusal_consumes_no_contact_credit(poster):
    c, session, ph, pid, lid, bh, bid, eid = poster
    before = c.get("/buyer/contact-credits", headers=bh)
    for _ in range(3):
        assert c.post(f"/owner/leads/{eid}/messages", json={"message": PHONE}, headers=ph).status_code == 422
    after = c.get("/buyer/contact-credits", headers=bh)
    assert before.status_code == after.status_code == 200
    assert before.json() == after.json()  # free unlocks / credits unchanged
    assert session.execute(text("SELECT COUNT(*) FROM property_contact_unlocks WHERE user_id=:u"), {"u": bid}).scalar() == 0


def test_m8_limiter_counts_the_request_before_the_m9_refusal(poster, monkeypatch):
    c, session, ph, pid, lid, bh, bid, eid = poster
    monkeypatch.setattr(config_module.settings, "COMM_RATE_LIMITS_DISABLED", False)
    r = c.post(f"/owner/leads/{eid}/messages", json={"message": PHONE}, headers=ph)
    assert r.status_code == 422
    hits = session.execute(
        text("SELECT hit_count FROM rate_limit_windows WHERE bucket_key = :k"),
        {"k": f"comm:lead_message:user:{pid}"},
    ).scalar()
    assert hits == 1  # counted first (M8), refused second (M9)
