"""Enquiry creation timestamps (migration i9j0k1l2m3n4).

Contract:
- new enquiries are stamped with UTC at creation (whole seconds, app-side —
  never the MySQL server clock, which is not UTC on every environment);
- every read (buyer create/list/detail, owner list/PATCH) returns that same
  instant as an explicit UTC string ("...Z");
- idempotent repeats return the ORIGINAL creation time;
- rows from before the column existed stay NULL and are returned as null —
  never backfilled with the migration date or any other guess;
- the column is nullable with no server default; the migration graph keeps
  a single head.

Rollback-isolated (client fixture); historical rows are simulated by
nulling a fresh test row inside the rolled-back transaction.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from tests.conftest import _expected_heads, auth_header
from tests.test_owner_leads import fresh_buyer, owner_header, seed_owner_listing


def _utc(api_value: str) -> datetime:
    assert api_value.endswith("Z"), api_value  # explicit UTC on the wire
    return datetime.fromisoformat(api_value.replace("Z", "+00:00"))


def _stored(session, enquiry_id):
    return session.execute(
        text("SELECT created_at FROM enquiries WHERE id=:i"), {"i": enquiry_id}
    ).fetchone()[0]


def _create(c, buyer, lid, message="Is it available?"):
    r = c.post(
        "/buyer/enquiries",
        json={"property_listing_id": lid, "message": message},
        headers=auth_header(buyer["access_token"]),
    )
    assert r.status_code in (200, 201), r.text
    return r


def test_new_enquiry_gets_a_real_utc_timestamp(client):
    c, session = client
    lid, _, _ = seed_owner_listing(session)
    before = datetime.now(timezone.utc).replace(microsecond=0)
    r = _create(c, fresh_buyer(c), lid)
    after = datetime.now(timezone.utc)
    assert r.status_code == 201
    created = _utc(r.json()["created_at"])
    # UTC "now", not the MySQL server clock (IST on local MySQL) and not a
    # migration date.
    assert before <= created <= after
    assert created.microsecond == 0
    # Persisted as the same instant (naive UTC).
    assert _stored(session, r.json()["id"]) == created.replace(tzinfo=None)


def test_every_read_returns_the_same_instant(client):
    c, session = client
    lid, _, phone = seed_owner_listing(session)
    buyer = fresh_buyer(c)
    hb = auth_header(buyer["access_token"])
    created = _create(c, buyer, lid).json()
    stamp = created["created_at"]

    assert c.get(f"/buyer/enquiries/{created['id']}", headers=hb).json()["created_at"] == stamp
    assert c.get("/buyer/enquiries", headers=hb).json()["items"][0]["created_at"] == stamp
    ho, _ = owner_header(c, phone)
    lead = [i for i in c.get("/owner/leads", headers=ho).json()["items"] if i["id"] == created["id"]][0]
    assert lead["created_at"] == stamp
    patched = c.patch(f"/owner/leads/{created['id']}", json={"status": "CONTACTED"}, headers=ho)
    assert patched.status_code == 200 and patched.json()["created_at"] == stamp  # updates never re-stamp


def test_idempotent_repeat_returns_the_original_time(client):
    c, session = client
    lid, _, _ = seed_owner_listing(session)
    buyer = fresh_buyer(c)
    first = _create(c, buyer, lid).json()
    session.execute(  # make the original clearly older than "now"
        text("UPDATE enquiries SET created_at = created_at - INTERVAL 3 DAY WHERE id=:i"),
        {"i": first["id"]},
    )
    again = _create(c, buyer, lid, "Following up")
    assert again.status_code == 200  # duplicate path (JSONResponse)
    assert again.json()["id"] == first["id"]
    assert _utc(again.json()["created_at"]) == _utc(first["created_at"]) - timedelta(days=3)


def test_contact_reveal_lead_is_stamped_too(client):
    c, session = client
    lid, _, _ = seed_owner_listing(session)
    buyer = fresh_buyer(c)
    assert c.post(f"/buyer/listings/{lid}/contact", headers=auth_header(buyer["access_token"])).status_code == 200
    stored = session.execute(
        text("SELECT created_at FROM enquiries WHERE buyer_user_id=:b AND property_listing_id=:l"),
        {"b": buyer["user_id"], "l": lid},
    ).fetchone()[0]
    assert stored is not None
    assert abs(datetime.now(timezone.utc).replace(tzinfo=None) - stored) < timedelta(minutes=1)


def test_historical_rows_stay_null_everywhere(client):
    c, session = client
    lid, _, phone = seed_owner_listing(session)
    buyer = fresh_buyer(c)
    hb = auth_header(buyer["access_token"])
    eid = _create(c, buyer, lid).json()["id"]
    # Simulate a pre-migration row: its creation time was never recorded.
    session.execute(text("UPDATE enquiries SET created_at = NULL WHERE id=:i"), {"i": eid})

    assert c.get(f"/buyer/enquiries/{eid}", headers=hb).json()["created_at"] is None
    assert c.get("/buyer/enquiries", headers=hb).json()["items"][0]["created_at"] is None
    ho, _ = owner_header(c, phone)
    lead = [i for i in c.get("/owner/leads", headers=ho).json()["items"] if i["id"] == eid][0]
    assert lead["created_at"] is None
    # An owner update never invents a creation time for a historical lead.
    assert c.patch(f"/owner/leads/{eid}", json={"status": "CLOSED"}, headers=ho).json()["created_at"] is None
    assert _stored(session, eid) is None


def test_timestamps_do_not_leak_across_owners(client):
    c, session = client
    lid_a, _, _ = seed_owner_listing(session)
    _, _, phone_b = seed_owner_listing(session)
    _create(c, fresh_buyer(c), lid_a)
    hb, _ = owner_header(c, phone_b)
    assert c.get("/owner/leads", headers=hb).json() == {"items": [], "page": 1, "page_size": 20, "total": 0}


def test_column_is_nullable_without_default(client):
    _, session = client
    row = session.execute(text(
        "SELECT IS_NULLABLE, COLUMN_DEFAULT, DATA_TYPE FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'enquiries' AND COLUMN_NAME = 'created_at'"
    )).fetchone()
    assert row is not None, "enquiries.created_at missing: run alembic upgrade head"
    assert row[0] == "YES" and row[1] is None and row[2] == "datetime"


def test_migration_graph_single_head_and_parent():
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    import os

    here = os.path.dirname(os.path.abspath(__file__))
    script = ScriptDirectory.from_config(Config(os.path.join(here, "..", "alembic.ini")))
    assert _expected_heads() == ("i9j0k1l2m3n4",)
    rev = script.get_revision("i9j0k1l2m3n4")
    assert rev.down_revision == "h8i9j0k1l2m3"
    revisions = [r.revision for r in script.walk_revisions()]
    assert len(revisions) == len(set(revisions))  # unique ids, linear chain


def test_duplicate_enquiry_matches_the_create_contract(client):
    """POST /buyer/enquiries twice: 201 then 200 with the SAME body shape and
    values (id, listing snapshot, created_at); list/detail agree; buyer-role
    and ownership checks unchanged."""
    c, session = client
    lid, _, phone = seed_owner_listing(session)
    buyer = fresh_buyer(c)
    hb = auth_header(buyer["access_token"])
    first = _create(c, buyer, lid)
    dup = _create(c, buyer, lid, "Second message is not a new enquiry")
    assert (first.status_code, dup.status_code) == (201, 200)
    assert set(first.json()) == set(dup.json()) == {
        "id", "property_listing_id", "message", "status", "closing_reason", "created_at", "listing",
    }
    for key in ("id", "property_listing_id", "status", "created_at", "listing"):
        assert dup.json()[key] == first.json()[key], key
    assert dup.json()["message"] == first.json()["message"]  # the original row is returned

    detail = c.get(f"/buyer/enquiries/{first.json()['id']}", headers=hb).json()
    listed = c.get("/buyer/enquiries", headers=hb).json()["items"][0]
    assert detail == listed == first.json()

    other = fresh_buyer(c)
    assert c.get(f"/buyer/enquiries/{first.json()['id']}",
                 headers=auth_header(other["access_token"])).status_code == 404
    ho, _ = owner_header(c, phone)  # an OWNER-mode account cannot create buyer enquiries
    assert c.post("/buyer/enquiries", json={"property_listing_id": lid}, headers=ho).status_code == 403
    assert session.execute(
        text("SELECT COUNT(*) FROM enquiries WHERE buyer_user_id=:b AND property_listing_id=:l"),
        {"b": buyer["user_id"], "l": lid},
    ).fetchone()[0] == 1
