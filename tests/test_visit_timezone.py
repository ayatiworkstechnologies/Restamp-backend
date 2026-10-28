"""Visit times follow one contract: UTC everywhere.

property_visits.scheduled_at is a timezone-naive DATETIME holding UTC. The
service normalizes timezone-aware inputs to UTC, treats naive inputs as
UTC, validates "in the future" against UTC now, and the API returns the
stored naive UTC value (no suffix) — which clients must read as UTC.

These tests pin that contract for buyer creation, owner proposals and both
reschedule paths: the persisted row and the API response describe the same
instant as the request. Rollback-isolated; no stored values are shifted.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from tests.conftest import auth_header
from tests.test_owner_leads import fresh_buyer, owner_header, seed_owner_listing

IST = timezone(timedelta(hours=5, minutes=30))
# 30 Dec 2030, 4:30 PM in Chennai == 11:00 UTC.
CHENNAI_430PM = "2030-12-30T16:30:00+05:30"
UTC_11AM = "2030-12-30T11:00:00"


def _stored(session, visit_id):
    return session.execute(
        text("SELECT scheduled_at FROM property_visits WHERE id=:i"), {"i": visit_id}
    ).fetchone()[0]


def _as_utc_instant(api_value: str) -> datetime:
    """Read an API value the way clients must: naive means UTC."""
    moment = datetime.fromisoformat(api_value)
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment


def _buyer_visit(c, session, scheduled_at):
    lid, _, phone = seed_owner_listing(session)
    buyer = fresh_buyer(c)
    r = c.post(
        "/buyer/visits",
        json={"property_listing_id": lid, "scheduled_at": scheduled_at},
        headers=auth_header(buyer["access_token"]),
    )
    return r, lid, buyer, phone


def test_offset_input_is_stored_and_returned_as_the_same_utc_instant(client):
    c, session = client
    r, _, _, _ = _buyer_visit(c, session, CHENNAI_430PM)
    assert r.status_code == 201, r.text
    body = r.json()
    assert _stored(session, body["id"]) == datetime(2030, 12, 30, 11, 0, 0)
    assert body["scheduled_at"] == UTC_11AM  # naive UTC on the wire
    assert _as_utc_instant(body["scheduled_at"]) == datetime.fromisoformat(CHENNAI_430PM)


def test_z_input_is_not_shifted(client):
    c, session = client
    r, _, _, _ = _buyer_visit(c, session, "2030-12-30T11:00:00Z")
    assert r.status_code == 201, r.text
    assert _stored(session, r.json()["id"]) == datetime(2030, 12, 30, 11, 0, 0)
    assert r.json()["scheduled_at"] == UTC_11AM


def test_naive_input_is_treated_as_utc(client):
    c, session = client
    r, _, _, _ = _buyer_visit(c, session, UTC_11AM)
    assert r.status_code == 201, r.text
    assert _stored(session, r.json()["id"]) == datetime(2030, 12, 30, 11, 0, 0)


def test_get_returns_the_same_instant_as_create(client):
    c, session = client
    r, _, buyer, _ = _buyer_visit(c, session, CHENNAI_430PM)
    got = c.get(f"/buyer/visits/{r.json()['id']}", headers=auth_header(buyer["access_token"]))
    assert got.status_code == 200 and got.json()["scheduled_at"] == UTC_11AM


def test_past_instant_is_rejected_whatever_its_offset(client):
    c, session = client
    one_hour_ago_ist = (datetime.now(IST) - timedelta(hours=1)).replace(microsecond=0)
    r, _, _, _ = _buyer_visit(c, session, one_hour_ago_ist.isoformat())
    assert r.status_code == 422, r.text
    one_hour_ago_utc = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0)
    r, _, _, _ = _buyer_visit(c, session, one_hour_ago_utc.isoformat())
    assert r.status_code == 422, r.text
    # Naive values are UTC: an hour ago in UTC, written without a zone, is past too.
    r, _, _, _ = _buyer_visit(c, session, one_hour_ago_utc.replace(tzinfo=None).isoformat())
    assert r.status_code == 422, r.text


def test_near_future_offset_instant_is_accepted(client):
    c, session = client
    in_ten_minutes_ist = (datetime.now(IST) + timedelta(minutes=10)).replace(microsecond=0)
    r, _, _, _ = _buyer_visit(c, session, in_ten_minutes_ist.isoformat())
    assert r.status_code == 201, r.text
    expected = in_ten_minutes_ist.astimezone(timezone.utc).replace(tzinfo=None)
    assert _stored(session, r.json()["id"]) == expected


def test_reschedules_and_owner_proposals_normalize_to_utc(client):
    c, session = client
    r, lid, buyer, phone = _buyer_visit(c, session, CHENNAI_430PM)
    vid = r.json()["id"]
    hb = auth_header(buyer["access_token"])

    # Buyer reschedule with an offset.
    r = c.patch(f"/buyer/visits/{vid}", json={"scheduled_at": "2030-12-31T09:15:00+05:30"}, headers=hb)
    assert r.status_code == 200, r.text
    assert _stored(session, vid) == datetime(2030, 12, 31, 3, 45, 0)
    assert r.json()["scheduled_at"] == "2030-12-31T03:45:00"

    # Owner reschedule of the same visit (owners may edit times once it is
    # CONFIRMED — existing state-machine rule, unchanged).
    ho, _ = owner_header(c, phone)
    assert c.patch(f"/owner/visits/{vid}", json={"status": "CONFIRMED"}, headers=ho).status_code == 200
    r = c.patch(f"/owner/visits/{vid}", json={"scheduled_at": "2031-01-02T18:00:00+05:30"}, headers=ho)
    assert r.status_code == 200, r.text
    assert _stored(session, vid) == datetime(2031, 1, 2, 12, 30, 0)
    owner_view = c.get(f"/owner/visits/{vid}", headers=ho).json()
    assert _as_utc_instant(owner_view["scheduled_at"]) == datetime.fromisoformat("2031-01-02T18:00:00+05:30")


def test_owner_proposal_normalizes_to_utc_and_rejects_past(client):
    c, session = client
    lid, _, phone = seed_owner_listing(session)
    buyer = fresh_buyer(c)
    assert c.post("/buyer/enquiries", json={"property_listing_id": lid},
                  headers=auth_header(buyer["access_token"])).status_code == 201
    ho, _ = owner_header(c, phone)
    payload = {"property_listing_id": lid, "buyer_user_id": buyer["user_id"]}

    past = (datetime.now(IST) - timedelta(minutes=5)).replace(microsecond=0).isoformat()
    assert c.post("/owner/visits", json={**payload, "scheduled_at": past}, headers=ho).status_code == 422
    r = c.post("/owner/visits", json={**payload, "scheduled_at": CHENNAI_430PM}, headers=ho)
    assert r.status_code == 201, r.text
    assert _stored(session, r.json()["id"]) == datetime(2030, 12, 30, 11, 0, 0)
    assert r.json()["scheduled_at"] == UTC_11AM
