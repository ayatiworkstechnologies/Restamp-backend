"""Two-way site-visit lifecycle across buyer and owner endpoints.

Covers the flows the apps drive end to end under the strict Phase 5
lifecycle: buyer request (PENDING) -> owner receives -> owner confirms;
owner proposes from a lead (PENDING) -> buyer receives -> the poster
confirms; time edits (counter-proposals, reschedules) are refused; the
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
    """How VisitOut.scheduled_at comes back: the UTC instant with an
    explicit "Z" (never a zone-less value a client could read as local)."""
    return moment.replace(tzinfo=None).isoformat() + "Z"


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
    assert r.json()["status"] == "PENDING"
    assert r.json()["scheduled_at"] == _naive(when)

    owner_rows = c.get("/owner/visits", params={"property_listing_id": lid}, headers=oh).json()["items"]
    row = next(v for v in owner_rows if v["id"] == vid)
    assert row["status"] == "PENDING" and row["initiator"] == "BUYER"
    assert row["scheduled_at"] == _naive(when) and row["note"] == "Evening please"
    assert row["buyer_user_id"] == buyer["user_id"]
    assert row["owner_user_id"] == _owner_id(c, oh) and row["property_listing_id"] == lid
    assert c.get("/buyer/visits", headers=bh).json()["items"][0]["id"] == vid


def test_owner_proposal_from_lead_reaches_buyer_and_poster_confirms(client):
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
    assert mine["status"] == "PENDING" and mine["initiator"] == "OWNER"
    assert mine["scheduled_at"] == _naive(when) and mine["note"] == "Morning works"
    assert mine["buyer_user_id"] == buyer["user_id"] and mine["property_listing_id"] == lid

    # Strict Phase 5: the buyer cannot confirm; the poster does.
    assert c.patch(f"/buyer/visits/{vid}", json={"status": "CONFIRMED"}, headers=bh).status_code == 422
    assert c.patch(f"/owner/visits/{vid}", json={"status": "CONFIRMED"}, headers=oh).status_code == 200
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


def test_owner_offers_another_time_by_rejecting_and_proposing(client):
    """Strict Phase 5 has no counter-proposal edit: the owner cannot change
    the time of a buyer's request (the owner can never impose a time on
    it). Offering another time = reject the request, then propose anew."""
    c, _ = client
    lid, buyer, bh, oh = _setup(c)
    when = _at(5)
    vid = c.post("/buyer/visits", json={"property_listing_id": lid, "scheduled_at": _iso(when),
                                         "note": "Evening please"}, headers=bh).json()["id"]
    counter = _at(6)
    r = c.patch(f"/owner/visits/{vid}", json={"scheduled_at": _iso(counter), "note": "Only mornings"},
                headers=oh)
    assert r.status_code == 422, r.text
    unchanged = c.get(f"/buyer/visits/{vid}", headers=bh).json()
    assert unchanged["status"] == "PENDING" and unchanged["scheduled_at"] == _naive(when)
    assert unchanged["note"] == "Evening please"

    assert c.patch(f"/owner/visits/{vid}", json={"status": "REJECTED"}, headers=oh).status_code == 200
    r = c.post("/owner/visits", json={"property_listing_id": lid, "buyer_user_id": buyer["user_id"],
                                       "scheduled_at": _iso(counter), "note": "Only mornings"}, headers=oh)
    assert r.status_code == 201, r.text
    offer = c.get(f"/buyer/visits/{r.json()['id']}", headers=bh).json()
    assert offer["status"] == "PENDING" and offer["initiator"] == "OWNER"
    assert offer["scheduled_at"] == _naive(counter) and offer["note"] == "Only mornings"


def test_time_and_note_edits_stay_refused(client):
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
    assert c.get(f"/buyer/visits/{vid}", headers=bh).json()["status"] == "PENDING"
    # Neither side may move the time; the proposer (here the buyer) is told why.
    assert c.patch(f"/owner/visits/{vid}", json={"scheduled_at": FUTURE(6)}, headers=oh).status_code == 422
    r = c.patch(f"/buyer/visits/{vid}", json={"scheduled_at": FUTURE(7)}, headers=bh)
    assert r.status_code == 422 and r.json()["detail"] == "The proposer cannot edit a submitted visit proposal"


def test_confirmed_visit_cannot_be_rescheduled(client):
    c, _ = client
    lid, _buyer_out, bh, oh = _setup(c)
    vid = c.post("/buyer/visits", json={"property_listing_id": lid, "scheduled_at": FUTURE(5)},
                 headers=bh).json()["id"]
    assert c.patch(f"/owner/visits/{vid}", json={"status": "CONFIRMED"}, headers=oh).status_code == 200
    before = c.get(f"/buyer/visits/{vid}", headers=bh).json()
    moved = _at(9)
    r = c.patch(f"/owner/visits/{vid}", json={"scheduled_at": _iso(moved)}, headers=oh)
    assert r.status_code == 422 and r.json()["detail"] == "A CONFIRMED visit cannot be rescheduled or edited"
    assert c.patch(f"/buyer/visits/{vid}", json={"scheduled_at": _iso(moved)}, headers=bh).status_code == 422
    buyer_view = c.get(f"/buyer/visits/{vid}", headers=bh).json()
    assert buyer_view["scheduled_at"] == before["scheduled_at"] and buyer_view["rescheduled_at"] is None


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


def test_owner_propose_eligibility_follows_listing_state(client):
    """The backend, not the app, decides: an owner may propose only on a
    VERIFIED + AVAILABLE, not-deleted listing. PENDING (awaiting verification),
    REJECTED and non-AVAILABLE listings are refused with the same 422, and no
    visit row is created; the live listing accepts the proposal."""
    from app.models import PropertyListing, PropertyVisit

    c, session = client
    lid, buyer, _bh, oh = _setup(c)
    body = {"property_listing_id": lid, "buyer_user_id": buyer["user_id"], "scheduled_at": FUTURE(3)}
    listing = session.get(PropertyListing, lid)
    assert (listing.verification_status, listing.listing_status) == ("VERIFIED", "AVAILABLE")

    for verification, availability in (("PENDING", "AVAILABLE"), ("REJECTED", "AVAILABLE"),
                                       ("VERIFIED", "CLOSED"), ("VERIFIED", "SOLD")):
        listing.verification_status, listing.listing_status = verification, availability
        session.flush()
        r = c.post("/owner/visits", json=body, headers=oh)
        assert r.status_code == 422, (verification, availability, r.text)
        assert r.json()["detail"] == "Visits can only be scheduled on a live listing"
        assert session.query(PropertyVisit).filter_by(property_listing_id=lid).count() == 0

    listing.verification_status, listing.listing_status = "VERIFIED", "AVAILABLE"
    session.flush()
    r = c.post("/owner/visits", json=body, headers=oh)
    assert r.status_code == 201, r.text
    assert r.json()["initiator"] == "OWNER" and r.json()["property_listing_id"] == lid
    assert session.query(PropertyVisit).filter_by(property_listing_id=lid).count() == 1


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
    assert c.get(f"/buyer/visits/{vid}", headers=bh).json()["status"] == "PENDING"
