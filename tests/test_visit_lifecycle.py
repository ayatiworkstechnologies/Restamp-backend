"""Two-way site-visit lifecycle across buyer and owner endpoints.

Covers the flows the apps drive end to end: buyer request -> owner receives
-> owner confirms or counter-proposes; owner proposes from a lead -> buyer
receives -> buyer confirms; reschedules visible to the other side; the
owner's "propose on a listing that is no longer live" case (previously a
bare 404 the app showed as "The visit or listing could not be found");
and cross-user isolation. Rollback-isolated `client` fixture.
"""
from datetime import datetime, timedelta, timezone

from tests.conftest import auth_header
from tests.test_visits import FUTURE, _buyer, _listing, _owner


def _at(days):
    """Whole-second explicit-UTC instant, as the apps send it."""
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0)


def _iso(moment):
    return moment.isoformat()


def _naive(moment):
    """How VisitOut.scheduled_at comes back: naive UTC ISO (no zone)."""
    return moment.replace(tzinfo=None).isoformat()


def _setup(c):
    _, otoken = _owner(c)
    buyer = _buyer(c)
    lid = _listing(c, otoken)
    bh, oh = auth_header(buyer["access_token"]), auth_header(otoken)
    assert c.post("/buyer/enquiries", json={"property_listing_id": lid}, headers=bh).status_code == 201
    return lid, buyer, bh, oh


def _owner_id(c, oh):
    return c.get("/auth/me", headers=oh).json()["id"]


def test_buyer_request_reaches_owner_with_time_and_note(client):
    c, _ = client
    lid, buyer, bh, oh = _setup(c)
    when = _at(4)
    r = c.post("/buyer/visits", json={"property_listing_id": lid, "scheduled_at": _iso(when),
                                       "note": "Evening please"}, headers=bh)
    assert r.status_code == 201, r.text
    vid = r.json()["id"]
    assert r.json()["status"] == "REQUESTED"
    assert r.json()["scheduled_at"] == _naive(when)

    owner_rows = c.get("/owner/visits", params={"property_listing_id": lid}, headers=oh).json()["items"]
    row = next(v for v in owner_rows if v["id"] == vid)
    assert row["status"] == "REQUESTED" and row["initiator"] == "BUYER"
    assert row["scheduled_at"] == _naive(when) and row["note"] == "Evening please"
    assert row["buyer_user_id"] == buyer["user_id"]
    assert row["owner_user_id"] == _owner_id(c, oh) and row["property_listing_id"] == lid
    assert c.get("/buyer/visits", headers=bh).json()["items"][0]["id"] == vid


def test_owner_proposal_from_lead_reaches_buyer_who_confirms(client):
    c, _ = client
    lid, buyer, bh, oh = _setup(c)
    # Exactly what the app sends: ids from the owner's lead row.
    lead = c.get("/owner/leads", params={"property_listing_id": lid}, headers=oh).json()["items"][0]
    when = _at(3)
    r = c.post("/owner/visits", json={"property_listing_id": lead["property_listing_id"],
                                       "buyer_user_id": lead["buyer_user_id"],
                                       "scheduled_at": _iso(when), "note": "Morning works"}, headers=oh)
    assert r.status_code == 201, r.text
    vid = r.json()["id"]

    mine = next(v for v in c.get("/buyer/visits", headers=bh).json()["items"] if v["id"] == vid)
    assert mine["status"] == "PROPOSED" and mine["initiator"] == "OWNER"
    assert mine["scheduled_at"] == _naive(when) and mine["note"] == "Morning works"
    assert mine["buyer_user_id"] == buyer["user_id"] and mine["property_listing_id"] == lid

    assert c.patch(f"/buyer/visits/{vid}", json={"status": "CONFIRMED"}, headers=bh).status_code == 200
    owner_view = c.get(f"/owner/visits/{vid}", headers=oh).json()
    assert owner_view["status"] == "CONFIRMED" and owner_view["scheduled_at"] == _naive(when)


def test_owner_confirms_request_and_buyer_sees_confirmed_same_time(client):
    c, _ = client
    lid, _buyer_out, bh, oh = _setup(c)
    when = _at(5)
    vid = c.post("/buyer/visits", json={"property_listing_id": lid, "scheduled_at": _iso(when)},
                 headers=bh).json()["id"]
    r = c.patch(f"/owner/visits/{vid}", json={"status": "CONFIRMED"}, headers=oh)
    assert r.status_code == 200 and r.json()["status"] == "CONFIRMED"
    buyer_view = c.get(f"/buyer/visits/{vid}", headers=bh).json()
    assert buyer_view["status"] == "CONFIRMED"
    assert buyer_view["scheduled_at"] == r.json()["scheduled_at"] == _naive(when)


def test_owner_counter_proposes_a_new_time_on_a_buyer_request(client):
    c, _ = client
    lid, _buyer_out, bh, oh = _setup(c)
    vid = c.post("/buyer/visits", json={"property_listing_id": lid, "scheduled_at": _iso(_at(5)),
                                         "note": "Evening please"}, headers=bh).json()["id"]
    counter = _at(6)
    r = c.patch(f"/owner/visits/{vid}", json={"scheduled_at": _iso(counter), "note": "Only mornings"},
                headers=oh)
    assert r.status_code == 200, r.text
    body = r.json()
    # Awaiting the buyer — the owner cannot impose a time.
    assert body["status"] == "PROPOSED" and body["initiator"] == "BUYER"
    assert body["scheduled_at"] == _naive(counter) and body["note"] == "Only mornings"
    assert body["rescheduled_at"] is None  # status changed: "Awaiting your reply", not "Rescheduled"

    buyer_view = c.get(f"/buyer/visits/{vid}", headers=bh).json()
    assert buyer_view["status"] == "PROPOSED" and buyer_view["scheduled_at"] == _naive(counter)
    # Buyer accepts the owner's time; both sides agree on the persisted time.
    assert c.patch(f"/buyer/visits/{vid}", json={"status": "CONFIRMED"}, headers=bh).status_code == 200
    assert c.get(f"/owner/visits/{vid}", headers=oh).json()["status"] == "CONFIRMED"
    assert c.get(f"/owner/visits/{vid}", headers=oh).json()["scheduled_at"] == _naive(counter)


def test_counter_proposal_rules_stay_strict(client):
    c, _ = client
    lid, _buyer_out, bh, oh = _setup(c)
    vid = c.post("/buyer/visits", json={"property_listing_id": lid, "scheduled_at": FUTURE(5)},
                 headers=bh).json()["id"]
    # No time + status smuggling, no note-only owner edit, no past time.
    r = c.patch(f"/owner/visits/{vid}", json={"status": "CONFIRMED", "scheduled_at": FUTURE(9)}, headers=oh)
    assert r.status_code == 422
    r = c.patch(f"/owner/visits/{vid}", json={"note": "Owner edit"}, headers=oh)
    assert r.status_code == 422
    r = c.patch(f"/owner/visits/{vid}", json={"scheduled_at": _iso(_at(-1))}, headers=oh)
    assert r.status_code == 422
    assert c.get(f"/buyer/visits/{vid}", headers=bh).json()["status"] == "REQUESTED"
    # After the counter-proposal the buyer still cannot move an owner-proposed time.
    assert c.patch(f"/owner/visits/{vid}", json={"scheduled_at": FUTURE(6)}, headers=oh).status_code == 200
    r = c.patch(f"/buyer/visits/{vid}", json={"scheduled_at": FUTURE(7)}, headers=bh)
    assert r.status_code == 422 and r.json()["detail"] == "Only owner may reschedule a PROPOSED visit"


def test_owner_reschedule_of_confirmed_visit_reaches_buyer(client):
    c, _ = client
    lid, _buyer_out, bh, oh = _setup(c)
    vid = c.post("/buyer/visits", json={"property_listing_id": lid, "scheduled_at": FUTURE(5)},
                 headers=bh).json()["id"]
    assert c.patch(f"/owner/visits/{vid}", json={"status": "CONFIRMED"}, headers=oh).status_code == 200
    moved = _at(9)
    r = c.patch(f"/owner/visits/{vid}", json={"scheduled_at": _iso(moved)}, headers=oh)
    assert r.status_code == 200 and r.json()["status"] == "CONFIRMED"
    buyer_view = c.get(f"/buyer/visits/{vid}", headers=bh).json()
    assert buyer_view["scheduled_at"] == _naive(moved) and buyer_view["rescheduled_at"] is not None


def test_owner_propose_on_own_listing_that_is_no_longer_live(client):
    """Regression: proposing from a lead whose listing was archived used to
    404, shown as "The visit or listing could not be found"."""
    c, _ = client
    lid, buyer, _bh, oh = _setup(c)
    assert c.post(f"/owner/listings/{lid}/archive", headers=oh).status_code == 200
    # The lead is still listed for the owner...
    assert c.get("/owner/leads", params={"property_listing_id": lid}, headers=oh).json()["total"] == 1
    # ...and proposing on it now names the real reason.
    r = c.post("/owner/visits", json={"property_listing_id": lid, "buyer_user_id": buyer["user_id"],
                                       "scheduled_at": FUTURE(3)}, headers=oh)
    assert r.status_code == 422
    assert r.json()["detail"] == "Visits can only be scheduled on a live listing"


def test_owner_propose_on_removed_listing(client):
    c, _ = client
    lid, buyer, _bh, oh = _setup(c)
    assert c.delete(f"/owner/listings/{lid}", headers=oh).status_code == 204
    r = c.post("/owner/visits", json={"property_listing_id": lid, "buyer_user_id": buyer["user_id"],
                                       "scheduled_at": FUTURE(3)}, headers=oh)
    assert r.status_code == 422
    assert r.json()["detail"] == "Visits can only be scheduled on a live listing"


def test_owner_propose_on_foreign_or_missing_listing_stays_404(client):
    c, _ = client
    lid, buyer, _bh, _oh = _setup(c)
    _, other_token = _owner(c)
    oh2 = auth_header(other_token)
    for target in (lid, 999_999_999):
        r = c.post("/owner/visits", json={"property_listing_id": target, "buyer_user_id": buyer["user_id"],
                                           "scheduled_at": FUTURE(3)}, headers=oh2)
        assert r.status_code == 404 and r.json()["detail"] == "Not found"


def test_visits_are_isolated_between_users(client):
    c, _ = client
    lid, _buyer_out, bh, oh = _setup(c)
    vid = c.post("/buyer/visits", json={"property_listing_id": lid, "scheduled_at": FUTURE(5)},
                 headers=bh).json()["id"]
    stranger = auth_header(_buyer(c)["access_token"])
    _, other_token = _owner(c)
    oh2 = auth_header(other_token)
    assert c.get(f"/buyer/visits/{vid}", headers=stranger).status_code == 404
    assert c.patch(f"/buyer/visits/{vid}", json={"status": "CANCELLED"}, headers=stranger).status_code == 404
    assert c.get(f"/owner/visits/{vid}", headers=oh2).status_code == 404
    # Another owner can neither confirm nor counter-propose.
    assert c.patch(f"/owner/visits/{vid}", json={"status": "CONFIRMED"}, headers=oh2).status_code == 404
    assert c.patch(f"/owner/visits/{vid}", json={"scheduled_at": FUTURE(6)}, headers=oh2).status_code == 404
    # Role gates: a buyer token cannot use owner endpoints.
    assert c.patch(f"/owner/visits/{vid}", json={"scheduled_at": FUTURE(6)}, headers=bh).status_code == 403
    assert c.get(f"/buyer/visits/{vid}", headers=bh).json()["status"] == "REQUESTED"
