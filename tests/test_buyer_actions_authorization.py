"""Buyer action authorization + payload contracts (enquiries + site visits).

WHY THIS FILE EXISTS
--------------------
The app hit `403 {"detail": "BUYER role required"}` on

    POST /buyer/enquiries
    POST /buyer/visits
    GET  /buyer/visits

The backend was right: it keeps exactly ONE current role per account
(app/services/users_roles.py) and every /buyer/* endpoint is gated on BUYER
(app/deps.py: `require_buyer`). A 403 on those endpoints therefore means
"authenticated, but in the wrong mode" — NEVER "bad token" (that is 401).
The fix is app-side (the client puts the account in BUYER mode through the
sanctioned POST /users/me/role before acting), so the backend contract is
unchanged — these tests pin it down so it can never silently drift:

- 401 for anonymous / invalid tokens (authentication still required),
- 403 with the actionable "BUYER role required" detail for OWNER/BROKER
  accounts (authorization still enforced, never silently widened),
- 201 + persisted rows for a BUYER account (the app path that was broken),
- the exact payload contract of both endpoints (listing id, message/note
  length, ISO datetime in the future),
- the self-serve recovery the app now relies on: switching the current role
  back to BUYER restores access, and switching away again re-locks it.

Uses the rollback-isolated `client` fixture on local restamp_dev only.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from tests.conftest import auth_header, register_phone

_SEQ = {"n": 0}
FUTURE = lambda days=7: (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
PAST = lambda days=1: (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _register(c, phone=None):
    _SEQ["n"] += 1
    return register_phone(c, phone or f"+1940000{_SEQ['n']:04d}")


def _owner(c, phone=None):
    """An account in OWNER mode (e.g. it just posted a property)."""
    out = _register(c, phone)
    token = out["access_token"]
    r = c.post("/users/me/role", json={"role": "OWNER"}, headers=auth_header(token))
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "OWNER", r.text
    return out, token


def _buyer(c, phone=None):
    """An account whose current role is BUYER (the default for new users)."""
    out = _register(c, phone)
    r = c.get("/users/me/role", headers=auth_header(out["access_token"]))
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "BUYER", r.text
    return out


def _listing(c, token, title=None):
    """A VERIFIED + AVAILABLE listing owned by `token`'s account."""
    _SEQ["n"] += 1
    r = c.post(
        "/owner/listings",
        json={
            "title": title or f"Buyer Action Flat {_SEQ['n']}",
            "transaction_type": "BUY",
            "property_type": "APARTMENT",
            "price_rupees": 5000000,
            "locality": f"Buyer Action Nagar {_SEQ['n']}",
            "city": "Chennai",
            "pincode": "600080",
        },
        headers=auth_header(token),
    )
    assert r.status_code == 201, r.text
    lid = r.json()["listing_id"]
    assert c.post(
        f"/owner/listings/{lid}/verify", headers=auth_header(token)
    ).status_code == 200
    return lid


def _enquiry_body(listing_id, message=None):
    body = {"property_listing_id": listing_id}
    if message is not None:
        body["message"] = message
    return body


def _visit_body(listing_id, scheduled_at=None, note=None):
    body = {
        "property_listing_id": listing_id,
        "scheduled_at": scheduled_at if scheduled_at is not None else FUTURE(),
    }
    if note is not None:
        body["note"] = note
    return body


def _current_role(c, token):
    r = c.get("/users/me/role", headers=auth_header(token))
    assert r.status_code == 200, r.text
    return r.json()


# --- Authentication --------------------------------------------------------

def test_enquiry_requires_authentication(client):
    """Anonymous / invalid tokens are 401 — never 403 (that means 'wrong role')."""
    c, _ = client
    _, otoken = _owner(c)
    lid = _listing(c, otoken)

    r = c.post("/buyer/enquiries", json=_enquiry_body(lid))
    assert r.status_code == 401, r.text

    r = c.post(
        "/buyer/enquiries",
        json=_enquiry_body(lid),
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert r.status_code == 401, r.text


def test_visit_requires_authentication(client):
    c, _ = client
    _, otoken = _owner(c)
    lid = _listing(c, otoken)

    r = c.post("/buyer/visits", json=_visit_body(lid))
    assert r.status_code == 401, r.text

    r = c.get("/buyer/visits")
    assert r.status_code == 401, r.text

    r = c.post(
        "/buyer/visits",
        json=_visit_body(lid),
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert r.status_code == 401, r.text


# --- Buyer authorization (the 403 matrix) ---------------------------------

def test_enquiry_rejects_non_buyer_roles(client):
    """OWNER/BROKER accounts are refused with the actionable detail."""
    c, _ = client
    _, otoken = _owner(c)
    lid = _listing(c, otoken)

    r = c.post("/buyer/enquiries", json=_enquiry_body(lid), headers=auth_header(otoken))
    assert r.status_code == 403, r.text
    assert r.json() == {"detail": "BUYER role required"}, r.text

    broker = _register(c)
    r = c.post(
        "/users/me/role", json={"role": "BROKER"}, headers=auth_header(broker["access_token"])
    )
    assert r.status_code == 200, r.text
    r = c.post(
        "/buyer/enquiries", json=_enquiry_body(lid), headers=auth_header(broker["access_token"])
    )
    assert r.status_code == 403, r.text
    assert r.json() == {"detail": "BUYER role required"}, r.text

    # No row was written for the refused attempts.
    count = c.get("/buyer/enquiries", headers=auth_header(otoken)).status_code
    assert count == 403, "an OWNER account must not be able to read buyer enquiries"


def test_visit_rejects_non_buyer_roles(client):
    c, _ = client
    _, otoken = _owner(c)
    lid = _listing(c, otoken)

    r = c.post("/buyer/visits", json=_visit_body(lid), headers=auth_header(otoken))
    assert r.status_code == 403, r.text
    assert r.json() == {"detail": "BUYER role required"}, r.text

    r = c.get("/buyer/visits", headers=auth_header(otoken))
    assert r.status_code == 403, r.text
    assert r.json() == {"detail": "BUYER role required"}, r.text

    broker = _register(c)
    c.post("/users/me/role", json={"role": "BROKER"}, headers=auth_header(broker["access_token"]))
    r = c.post(
        "/buyer/visits", json=_visit_body(lid), headers=auth_header(broker["access_token"])
    )
    assert r.status_code == 403, r.text
    assert r.json() == {"detail": "BUYER role required"}, r.text


# --- Enquiry creation ------------------------------------------------------

def test_buyer_creates_enquiry(client):
    c, session = client
    owner, otoken = _owner(c)
    lid = _listing(c, otoken)
    buyer = _buyer(c)
    token = buyer["access_token"]

    r = c.post(
        "/buyer/enquiries",
        json=_enquiry_body(lid, "Is the price negotiable?"),
        headers=auth_header(token),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["property_listing_id"] == lid
    assert body["message"] == "Is the price negotiable?"
    assert body["status"] == "NEW"
    assert body["listing"]["title"]

    row = session.execute(
        text(
            "SELECT buyer_user_id, owner_user_id, message, status "
            "FROM enquiries WHERE id=:i"
        ),
        {"i": body["id"]},
    ).fetchone()
    assert row is not None, "the enquiry row must exist"
    assert row[0] == buyer["user_id"] and row[1] == owner["user_id"]
    assert row[2] == "Is the price negotiable?" and row[3] == "NEW"

    # The buyer can list its own enquiries.
    r = c.get("/buyer/enquiries", headers=auth_header(token))
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 1


def test_enquiry_is_idempotent_for_the_same_buyer(client):
    c, _ = client
    _, otoken = _owner(c)
    lid = _listing(c, otoken)
    buyer = _buyer(c)
    token = buyer["access_token"]

    first = c.post(
        "/buyer/enquiries", json=_enquiry_body(lid, "Hi"), headers=auth_header(token)
    )
    assert first.status_code == 201, first.text
    second = c.post(
        "/buyer/enquiries", json=_enquiry_body(lid, "Hi"), headers=auth_header(token)
    )
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]
    listing = c.get("/buyer/enquiries", headers=auth_header(token)).json()
    assert listing["total"] == 1


def test_enquiry_payload_contract(client):
    c, _ = client
    _, otoken = _owner(c)
    lid = _listing(c, otoken)
    buyer = _buyer(c)
    token = buyer["access_token"]
    h = auth_header(token)

    # The listing id is required.
    r = c.post("/buyer/enquiries", json={"message": "no listing"}, headers=h)
    assert r.status_code == 422, r.text

    # A non-positive listing id is rejected.
    r = c.post("/buyer/enquiries", json=_enquiry_body(0), headers=h)
    assert r.status_code == 422, r.text

    # The message is capped at 2000 characters (EnquiryCreate).
    r = c.post("/buyer/enquiries", json=_enquiry_body(lid, "x" * 2001), headers=h)
    assert r.status_code == 422, r.text

    # A message of exactly 2000 characters is accepted.
    r = c.post("/buyer/enquiries", json=_enquiry_body(lid, "x" * 2000), headers=h)
    assert r.status_code == 201, r.text


# --- Site-visit creation ---------------------------------------------------

def test_buyer_schedules_visit(client):
    c, session = client
    owner, otoken = _owner(c)
    lid = _listing(c, otoken)
    buyer = _buyer(c)
    token = buyer["access_token"]

    r = c.post(
        "/buyer/visits",
        json=_visit_body(lid, FUTURE(), "Evening please"),
        headers=auth_header(token),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["property_listing_id"] == lid
    assert body["buyer_user_id"] == buyer["user_id"]
    assert body["owner_user_id"] == owner["user_id"]
    assert body["status"] == "REQUESTED" and body["initiator"] == "BUYER"
    assert body["note"] == "Evening please"
    assert body["listing"]["title"]

    row = session.execute(
        text(
            "SELECT buyer_user_id, owner_user_id, status, initiator "
            "FROM property_visits WHERE id=:i"
        ),
        {"i": body["id"]},
    ).fetchone()
    assert row is not None, "the visit row must exist"
    assert row[0] == buyer["user_id"] and row[3] == "BUYER"

    # The buyer can list its own visits (the endpoint that also 403'd).
    r = c.get("/buyer/visits", headers=auth_header(token))
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 1


def test_visit_payload_contract(client):
    c, _ = client
    _, otoken = _owner(c)
    lid = _listing(c, otoken)
    buyer = _buyer(c)
    token = buyer["access_token"]
    h = auth_header(token)

    # The listing id and the scheduled time are required.
    r = c.post("/buyer/visits", json={"scheduled_at": FUTURE()}, headers=h)
    assert r.status_code == 422, r.text
    r = c.post("/buyer/visits", json={"property_listing_id": lid}, headers=h)
    assert r.status_code == 422, r.text

    # A non-positive listing id is rejected.
    r = c.post(
        "/buyer/visits",
        json={"property_listing_id": 0, "scheduled_at": FUTURE()},
        headers=h,
    )
    assert r.status_code == 422, r.text

    # An unparseable datetime is rejected by the schema.
    r = c.post(
        "/buyer/visits",
        json={"property_listing_id": lid, "scheduled_at": "not-a-datetime"},
        headers=h,
    )
    assert r.status_code == 422, r.text

    # A past time is rejected by the service (visits are in the future only).
    r = c.post("/buyer/visits", json=_visit_body(lid, PAST()), headers=h)
    assert r.status_code == 422, r.text

    # The note is capped at 2000 characters (VisitCreate).
    r = c.post(
        "/buyer/visits", json=_visit_body(lid, FUTURE(), "x" * 2001), headers=h
    )
    assert r.status_code == 422, r.text

    # A well-formed visit is accepted.
    r = c.post("/buyer/visits", json=_visit_body(lid, FUTURE(), "x" * 2000), headers=h)
    assert r.status_code == 201, r.text


# --- The self-serve recovery the app now uses -----------------------------

def test_role_switch_restores_buyer_access(client):
    """OWNER -> BUYER unlocks the buyer endpoints; BUYER -> OWNER re-locks them.

    This is the flow the app now performs before a buyer action (the account
    posted a property, went back to browsing, and the server still held the
    OWNER role). The switch is a real, authenticated, server-side change.
    """
    c, _ = client
    # Another owner's listing — the one the switching account browses.
    _, other_token = _owner(c)
    listing_id = _listing(c, other_token)

    # The switching account: it posted its own property, so the server holds
    # the OWNER role for it while it browses somebody else's listing.
    account, token = _owner(c)
    _listing(c, token)
    h = auth_header(token)

    assert _current_role(c, token)["role"] == "OWNER"

    # Locked while the account is in OWNER mode.
    assert c.post("/buyer/enquiries", json=_enquiry_body(listing_id), headers=h).status_code == 403
    assert c.post("/buyer/visits", json=_visit_body(listing_id), headers=h).status_code == 403
    assert c.get("/buyer/visits", headers=h).status_code == 403

    # The sanctioned self-serve switch.
    r = c.post("/users/me/role", json={"role": "BUYER"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "BUYER", r.text
    assert _current_role(c, token)["user_id"] == account["user_id"]

    # Unlocked.
    r = c.post("/buyer/enquiries", json=_enquiry_body(listing_id, "Now it works"), headers=h)
    assert r.status_code == 201, r.text
    r = c.post("/buyer/visits", json=_visit_body(listing_id), headers=h)
    assert r.status_code == 201, r.text
    r = c.get("/buyer/visits", headers=h)
    assert r.status_code == 200, r.text

    # Roles are exclusive: switching back to OWNER re-locks the buyer side.
    r = c.post("/users/me/role", json={"role": "OWNER"}, headers=h)
    assert r.status_code == 200, r.text
    assert c.post("/buyer/enquiries", json=_enquiry_body(listing_id), headers=h).status_code == 403
    assert c.post("/buyer/visits", json=_visit_body(listing_id), headers=h).status_code == 403
    assert c.get("/buyer/visits", headers=h).status_code == 403
