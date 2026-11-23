"""M7: Dealer (BROKER-mode poster) visit endpoints, action flags and isolation.

The website's Dealer dashboard calls /owner/leads, /owner/visits and
/owner/listings while the account stays in BROKER mode (never switched to
OWNER). These tests pin that contract against the real API:

- a BROKER-mode Dealer lists and reads visits on its own listings;
- the Dealer receives OWNER-side `actions` flags (it is the poster);
- PENDING: confirm/reject/cancel; CONFIRMED: cancel/complete; terminal: none;
- the Dealer stays in BROKER mode throughout (no role switch is needed);
- another Dealer / owner / buyer can neither list nor read nor change the
  Dealer's visits or leads (404, never a leak).

Rollback-isolated (tests/conftest.py client fixture). Test-only: no feature
code is touched.
"""
from __future__ import annotations

from tests.test_communication_audit import _buyer, _dealer_poster, _enquire, _owner, _request

OWNER_PENDING = {"can_confirm": True, "can_reject": True, "can_decline": False, "can_cancel": True, "can_complete": False}
OWNER_CONFIRMED = {"can_confirm": False, "can_reject": False, "can_decline": False, "can_cancel": True, "can_complete": True}
NONE = {"can_confirm": False, "can_reject": False, "can_decline": False, "can_cancel": False, "can_complete": False}


def _role(c, h):
    return c.get("/users/me/role", headers=h).json()["role"]


def _dealer_with_visit(c, session):
    dh, did, lid = _dealer_poster(c, session)
    bh, bid = _buyer(c)
    r = _request(c, bh, lid)
    assert r.status_code == 201, r.text
    return dh, did, lid, bh, bid, r.json()["id"]


def test_dealer_lists_and_reads_own_visits_in_broker_mode_with_owner_flags(client):
    c, session = client
    dh, did, lid, bh, bid, vid = _dealer_with_visit(c, session)
    body = c.get("/owner/visits", params={"page": 1, "page_size": 100}, headers=dh).json()
    assert [v["id"] for v in body["items"]] == [vid] and body["total"] == 1
    row = body["items"][0]
    assert row["status"] == "PENDING" and row["owner_user_id"] == did
    assert row["actions"] == OWNER_PENDING  # poster-side flags, not buyer-side
    assert c.get(f"/owner/visits/{vid}", headers=dh).json()["actions"] == OWNER_PENDING
    # The buyer sees buyer-side flags on the same visit.
    assert c.get(f"/buyer/visits/{vid}", headers=bh).json()["actions"] == {**NONE, "can_cancel": True}
    assert _role(c, dh) == "BROKER"  # nothing switched the Dealer's mode


def test_dealer_confirm_then_complete_flags_follow_status(client):
    c, session = client
    dh, _, _, bh, _, vid = _dealer_with_visit(c, session)
    r = c.patch(f"/owner/visits/{vid}", json={"status": "CONFIRMED"}, headers=dh)
    assert r.status_code == 200 and r.json()["actions"] == OWNER_CONFIRMED
    r = c.patch(f"/owner/visits/{vid}", json={"status": "COMPLETED"}, headers=dh)
    assert r.status_code == 200 and r.json()["status"] == "COMPLETED" and r.json()["actions"] == NONE
    assert _role(c, dh) == "BROKER"


def test_dealer_reject_pending_is_terminal_and_decline_is_refused(client):
    c, session = client
    dh, _, _, _, _, vid = _dealer_with_visit(c, session)
    # DECLINED is the legacy verb: never valid for a PENDING visit.
    assert c.patch(f"/owner/visits/{vid}", json={"status": "DECLINED"}, headers=dh).status_code == 422
    r = c.patch(f"/owner/visits/{vid}", json={"status": "REJECTED"}, headers=dh)
    assert r.status_code == 200 and r.json()["status"] == "REJECTED" and r.json()["actions"] == NONE
    assert c.patch(f"/owner/visits/{vid}", json={"status": "CONFIRMED"}, headers=dh).status_code == 422


def test_dealer_cannot_reschedule(client):
    c, session = client
    dh, _, _, _, _, vid = _dealer_with_visit(c, session)
    r = c.patch(f"/owner/visits/{vid}", json={"scheduled_at": "2031-01-01T10:00:00Z"}, headers=dh)
    assert r.status_code == 422
    assert c.get(f"/owner/visits/{vid}", headers=dh).json()["status"] == "PENDING"


def test_another_dealer_cannot_see_or_change_a_dealers_visits(client):
    c, session = client
    dh, _, _, _, _, vid = _dealer_with_visit(c, session)
    other_dh, _, _ = _dealer_poster(c, session)
    assert c.get("/owner/visits", headers=other_dh).json()["items"] == []
    assert c.get(f"/owner/visits/{vid}", headers=other_dh).status_code == 404
    assert c.patch(f"/owner/visits/{vid}", json={"status": "CONFIRMED"}, headers=other_dh).status_code == 404
    oh, _, _ = _owner(c, session)
    assert c.get(f"/owner/visits/{vid}", headers=oh).status_code == 404
    assert c.get(f"/owner/visits/{vid}", headers=dh).json()["status"] == "PENDING"  # unchanged


def test_another_dealer_cannot_see_a_dealers_leads(client):
    c, session = client
    dh, _, lid = _dealer_poster(c, session)
    bh, _ = _buyer(c)
    eid = _enquire(c, bh, lid, "Is it available?")["id"]
    assert [l["id"] for l in c.get("/owner/leads", headers=dh).json()["items"]] == [eid]
    other_dh, _, _ = _dealer_poster(c, session)
    assert c.get("/owner/leads", headers=other_dh).json()["items"] == []
    assert c.get(f"/owner/leads/{eid}", headers=other_dh).status_code == 404
    assert c.post(f"/owner/leads/{eid}/messages", json={"message": "x"}, headers=other_dh).status_code == 404


def test_dealer_lists_its_own_listings_in_broker_mode(client):
    c, session = client
    dh, _, lid = _dealer_poster(c, session)
    other_dh, _, other_lid = _dealer_poster(c, session)
    mine = [l["listing_id"] for l in c.get("/owner/listings", headers=dh).json()["items"]]
    assert lid in mine and other_lid not in mine
    assert _role(c, dh) == "BROKER"


def test_buyer_cannot_use_owner_visit_endpoints(client):
    c, session = client
    _, _, _, bh, _, vid = _dealer_with_visit(c, session)
    assert c.get("/owner/visits", headers=bh).status_code == 403
    assert c.patch(f"/owner/visits/{vid}", json={"status": "CONFIRMED"}, headers=bh).status_code == 403
