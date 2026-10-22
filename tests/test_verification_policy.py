"""Verification policy: a new listing stays PENDING until an EXPLICIT action.

There is NO automatic verification in this codebase: no scheduler, no worker,
no background task, and no timer-based promotion. A listing is created as
PENDING / AVAILABLE and only advances when an explicit, audited transition is
requested:

    PENDING --verify-->  VERIFIED   (visible to buyers)
    PENDING --reject-->  REJECTED   (owner edits and resubmits)
    REJECTED --submit--> PENDING    (resubmit, refreshes submitted_at)

Time alone never changes the state: these tests assert that repeated reads
(and an explicit wait) leave a fresh listing PENDING, so "two minutes later"
can never flip a listing to VERIFIED.

All client tests use the rollback-isolated `client` fixture on the local
restamp_dev database.
"""
import time

from sqlalchemy import text

from tests.conftest import auth_header, register_phone

_SEQ = {"n": 0}


def _owner(c):
    _SEQ["n"] += 1
    out = register_phone(c, f"+9198{_SEQ['n']:07d}")
    token = out["access_token"]
    r = c.post("/users/me/role", json={"role": "OWNER"}, headers=auth_header(token))
    assert r.status_code == 200, r.text
    return token


def _create(c, token):
    _SEQ["n"] += 1
    r = c.post(
        "/owner/listings",
        json={
            "title": f"Policy Flat {_SEQ['n']}",
            "transaction_type": "RENT",
            "property_type": "APARTMENT",
            "price_rupees": 25000,
            "price_period": "MONTHLY",
            "locality": f"Policy Nagar {_SEQ['n']}",
            "city": "Chennai",
            "pincode": "600091",
        },
        headers=auth_header(token),
    )
    assert r.status_code == 201, r.text
    return r.json()


def _state(session, listing_id):
    return session.execute(
        text(
            "SELECT verification_status, listing_status FROM property_listings WHERE id=:i"
        ),
        {"i": listing_id},
    ).fetchone()


def test_new_listing_is_pending_and_stays_pending_without_an_explicit_action(client):
    """The core policy: waiting does not verify anything."""
    c, session = client
    token = _owner(c)
    created = _create(c, token)
    lid = created["listing_id"]

    # The create response carries the real listing id and the saved status.
    assert created["listing_id"] == lid
    assert created["verification_status"] == "PENDING"
    assert created["listing_status"] == "AVAILABLE"
    assert _state(session, lid) == ("PENDING", "AVAILABLE")

    # Reading the collection (the "My Properties" refresh) never writes.
    for _ in range(3):
        r = c.get("/owner/listings", headers=auth_header(token))
        assert r.status_code == 200, r.text
        row = next(i for i in r.json()["items"] if i["listing_id"] == lid)
        assert row["verification_status"] == "PENDING"
        assert row["listing_status"] == "AVAILABLE"

    # An explicit wait changes nothing either.
    time.sleep(1.1)
    assert _state(session, lid) == ("PENDING", "AVAILABLE")

    # No verification audit row was written by any of the above.
    n = session.execute(
        text("SELECT COUNT(*) FROM verifications WHERE property_listing_id=:i"),
        {"i": lid},
    ).fetchone()[0]
    assert n == 0

    # The listing is NOT in the public marketplace while it is PENDING.
    assert c.get(f"/buyer/listings/{lid}").status_code == 404


def test_only_the_explicit_verify_transition_advances_the_listing(client):
    c, session = client
    token = _owner(c)
    lid = _create(c, token)["listing_id"]

    r = c.post(f"/owner/listings/{lid}/verify", headers=auth_header(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verification_status"] == "VERIFIED"
    assert body["listing_status"] == "AVAILABLE"
    assert _state(session, lid) == ("VERIFIED", "AVAILABLE")

    # The transition is audited with the owner as the actor.
    row = session.execute(
        text(
            "SELECT result, checks_json, triggered_by FROM verifications "
            "WHERE property_listing_id=:i"
        ),
        {"i": lid},
    ).fetchone()
    assert row[0] == "PASS"
    assert "owner" in row[1]
    assert row[2] is not None

    # A verified + available listing IS publicly visible (buyer marketplace).
    assert c.get(f"/buyer/listings/{lid}").status_code == 200


def test_rejected_listing_returns_to_pending_through_explicit_resubmit(client):
    c, session = client
    token = _owner(c)
    lid = _create(c, token)["listing_id"]

    r = c.post(
        f"/owner/listings/{lid}/reject",
        json={"reason": "Ownership document unclear"},
        headers=auth_header(token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["verification_status"] == "REJECTED"
    assert _state(session, lid)[0] == "REJECTED"
    # Rejected listings are not public.
    assert c.get(f"/buyer/listings/{lid}").status_code == 404

    # Resubmit is explicit and brings the listing back to PENDING.
    r = c.post(f"/owner/listings/{lid}/submit", headers=auth_header(token))
    assert r.status_code == 200, r.text
    assert r.json()["verification_status"] == "PENDING"
    assert _state(session, lid)[0] == "PENDING"


def test_publishing_requires_a_verified_listing(client):
    """CLOSED -> AVAILABLE (publish) is only allowed for VERIFIED listings."""
    c, session = client
    token = _owner(c)
    lid = _create(c, token)["listing_id"]

    # Archive first, then try to publish while still PENDING.
    assert c.post(f"/owner/listings/{lid}/archive", headers=auth_header(token)).status_code == 200
    assert _state(session, lid)[1] == "CLOSED"

    r = c.post(f"/owner/listings/{lid}/publish", headers=auth_header(token))
    assert r.status_code == 422, r.text
    # The failed transition committed nothing.
    assert _state(session, lid) == ("PENDING", "CLOSED")

    # After the explicit verification the publish succeeds.
    assert c.post(f"/owner/listings/{lid}/verify", headers=auth_header(token)).status_code == 200
    r = c.post(f"/owner/listings/{lid}/publish", headers=auth_header(token))
    assert r.status_code == 200, r.text
    assert r.json()["listing_status"] == "AVAILABLE"
    assert c.get(f"/buyer/listings/{lid}").status_code == 200
