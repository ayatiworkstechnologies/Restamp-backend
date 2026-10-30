"""CONTRACT §C/§E — visit names, listing details, reschedule state, shared note.

- names (D6): buyer's chosen name and owner's public name on both sides;
  phone-number defaults are never returned (null);
- listing snapshot carries locality/city/property_type;
- rescheduled_at: set when an accepted change moves the time (not when the
  same time is re-sent), cleared when the status changes, set again by a later
  reschedule; returned as explicit UTC;
- note: editable by whoever may edit the time in the current status, "" clears,
  note-only PATCH allowed, terminal visits refuse it; visible to both parties;
- status transitions and isolation unchanged.
Rollback-isolated.
"""
from datetime import datetime, timedelta, timezone

from tests.conftest import auth_header
from tests.test_owner_leads import fresh_buyer, owner_header, seed_owner_listing

FUTURE = (datetime.now(timezone.utc) + timedelta(days=5)).replace(microsecond=0)
LATER = FUTURE + timedelta(days=1)


def _iso(dt):
    return dt.isoformat()


def _setup(c, session, buyer_name=None, owner_name=None, note=None):
    lid, _, phone = seed_owner_listing(session)
    buyer = fresh_buyer(c)
    hb = auth_header(buyer["access_token"])
    if buyer_name:
        assert c.patch("/users/me", json={"display_name": buyer_name}, headers=hb).status_code == 200
    ho, _ = owner_header(c, phone)
    if owner_name:
        assert c.patch("/users/me", json={"display_name": owner_name}, headers=ho).status_code == 200
    body = {"property_listing_id": lid, "scheduled_at": _iso(FUTURE)}
    if note is not None:
        body["note"] = note
    r = c.post("/buyer/visits", json=body, headers=hb)
    assert r.status_code == 201, r.text
    return r.json(), hb, ho


def test_names_and_listing_details_on_both_sides(client):
    c, session = client
    visit, hb, ho = _setup(c, session, buyer_name="Priya Raman", owner_name="Kumar Estates")
    for got in (visit, c.get(f"/owner/visits/{visit['id']}", headers=ho).json(),
                c.get("/buyer/visits", headers=hb).json()["items"][0]):
        assert got["buyer_display_name"] == "Priya Raman"
        assert got["owner_display_name"] == "Kumar Estates"
        assert got["listing"]["locality"] and got["listing"]["city"]
        assert got["listing"]["property_type"] == "APARTMENT"
        assert got["rescheduled_at"] is None


def test_phone_default_names_are_never_returned(client):
    c, session = client
    visit, hb, ho = _setup(c, session)  # the buyer keeps the OTP phone default
    from sqlalchemy import text
    # Give the seeded owner the OTP default too (display_name == own phone).
    session.execute(text(
        "UPDATE users u JOIN auth_identities a ON a.user_id = u.id AND a.provider = 'phone' "
        "SET u.display_name = a.provider_identifier WHERE u.id = :o"), {"o": visit["owner_user_id"]})
    visit = c.get(f"/buyer/visits/{visit['id']}", headers=hb).json()
    owner_view = c.get(f"/owner/visits/{visit['id']}", headers=ho).json()
    assert visit["buyer_display_name"] is None and visit["owner_display_name"] is None
    assert owner_view["buyer_display_name"] is None and owner_view["owner_display_name"] is None
    assert "+91" not in str(owner_view) and "+91" not in str(visit)


def test_reschedule_sets_and_status_change_clears(client):
    c, session = client
    visit, hb, ho = _setup(c, session)
    vid = visit["id"]
    same = c.patch(f"/buyer/visits/{vid}", json={"scheduled_at": _iso(FUTURE)}, headers=hb)
    assert same.status_code == 200 and same.json()["rescheduled_at"] is None  # same time: not a reschedule
    moved = c.patch(f"/buyer/visits/{vid}", json={"scheduled_at": _iso(LATER)}, headers=hb)
    assert moved.status_code == 200 and moved.json()["rescheduled_at"].endswith("Z")
    assert moved.json()["status"] == "REQUESTED"  # status unchanged
    confirmed = c.patch(f"/owner/visits/{vid}", json={"status": "CONFIRMED"}, headers=ho)
    assert confirmed.json()["status"] == "CONFIRMED" and confirmed.json()["rescheduled_at"] is None
    again = c.patch(f"/owner/visits/{vid}", json={"scheduled_at": _iso(LATER + timedelta(hours=2))}, headers=ho)
    assert again.status_code == 200 and again.json()["status"] == "CONFIRMED"
    assert again.json()["rescheduled_at"] is not None  # owner may reschedule after confirming
    assert c.get(f"/buyer/visits/{vid}", headers=hb).json()["rescheduled_at"] == again.json()["rescheduled_at"]
    done = c.patch(f"/owner/visits/{vid}", json={"status": "COMPLETED"}, headers=ho)
    assert done.json()["rescheduled_at"] is None


def test_note_edit_rules(client):
    c, session = client
    visit, hb, ho = _setup(c, session, note="Gate 2 please")
    vid = visit["id"]
    assert visit["note"] == "Gate 2 please"
    # REQUESTED: only the buyer may edit time/note.
    r = c.patch(f"/owner/visits/{vid}", json={"note": "Owner edit"}, headers=ho)
    assert r.status_code == 422 and r.json()["detail"] == "Only buyer may edit the note of a REQUESTED visit"
    r = c.patch(f"/buyer/visits/{vid}", json={"note": "  Gate 3 instead  "}, headers=hb)
    assert r.status_code == 200 and r.json()["note"] == "Gate 3 instead"
    assert r.json()["rescheduled_at"] is None  # note-only edit is not a reschedule
    assert c.get(f"/owner/visits/{vid}", headers=ho).json()["note"] == "Gate 3 instead"  # shared note
    # CONFIRMED: either party; "" clears.
    assert c.patch(f"/owner/visits/{vid}", json={"status": "CONFIRMED"}, headers=ho).status_code == 200
    r = c.patch(f"/owner/visits/{vid}", json={"note": ""}, headers=ho)
    assert r.status_code == 200 and r.json()["note"] is None
    # Terminal: no edits.
    assert c.patch(f"/owner/visits/{vid}", json={"status": "COMPLETED"}, headers=ho).status_code == 200
    r = c.patch(f"/buyer/visits/{vid}", json={"note": "late"}, headers=hb)
    assert r.status_code == 422 and r.json()["detail"] == "Cannot edit the note of a COMPLETED visit"
    assert c.patch(f"/buyer/visits/{vid}", json={}, headers=hb).status_code == 422
    assert c.patch(f"/buyer/visits/{vid}", json={"note": "x" * 2001}, headers=hb).status_code == 422


def test_isolation_unchanged(client):
    c, session = client
    visit, hb, ho = _setup(c, session)
    stranger = fresh_buyer(c)
    hs = auth_header(stranger["access_token"])
    assert c.patch(f"/buyer/visits/{visit['id']}", json={"note": "hijack"}, headers=hs).status_code == 404
    _, _, other_phone = seed_owner_listing(session)
    hx, _ = owner_header(c, other_phone)
    assert c.patch(f"/owner/visits/{visit['id']}", json={"note": "hijack"}, headers=hx).status_code == 404
    assert c.get(f"/buyer/visits/{visit['id']}", headers=hb).json()["note"] is None
