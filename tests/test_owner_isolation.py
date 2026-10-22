"""Owner listing isolation regression (account separation).

The owner-facing collection ("My Properties") and every owner management
endpoint must be scoped to the AUTHENTICATED principal, never to a
client-supplied id and never to "whatever listing exists".

Scenario covered end to end:
  1. Owner A (Roshan) creates a property.
  2. Owner A logs out.
  3. Owner B (Test 1) logs in.
  4. Owner B must NOT see or manage Owner A's property through ANY
     owner-specific endpoint (list, detail, transitions, photos, drafts).
  5. Owner A must still be able to retrieve and manage their own property.
  6. The public buyer marketplace keeps showing eligible (VERIFIED +
     AVAILABLE) listings only.

All client tests use the rollback-isolated `client` fixture on the local
restamp_dev database, so no row created here survives the test.
"""
from sqlalchemy import text

from tests.conftest import auth_header, register_phone

_SEQ = {"n": 0}


def _phone(tag: str) -> str:
    _SEQ["n"] += 1
    return f"+9198{tag}{_SEQ['n']:06d}"


def _owner(c, phone: str | None = None) -> dict:
    """Register a phone identity and switch it to the OWNER role."""
    _SEQ["n"] += 1
    phone = phone or f"+91987{_SEQ['n']:07d}"
    out = register_phone(c, phone)
    token = out["access_token"]
    r = c.post("/users/me/role", json={"role": "OWNER"}, headers=auth_header(token))
    assert r.status_code == 200, r.text
    return {"token": token, "user_id": out["user_id"], "phone": phone}


def _create(c, token: str, title: str | None = None, extra: dict | None = None) -> dict:
    _SEQ["n"] += 1
    body = {
        "title": title or f"Isolation Flat {_SEQ['n']}",
        "transaction_type": "RENT",
        "property_type": "APARTMENT",
        "price_rupees": 25000,
        "price_period": "MONTHLY",
        "locality": f"Isolation Nagar {_SEQ['n']}",
        "city": "Chennai",
        "pincode": "600090",
    }
    if extra:
        body.update(extra)
    r = c.post("/owner/listings", json=body, headers=auth_header(token))
    assert r.status_code == 201, r.text
    return r.json()


def _listing_owner(session, listing_id: int) -> tuple:
    return session.execute(
        text(
            "SELECT l.user_id, p.user_id FROM property_listings l "
            "JOIN physical_properties p ON p.id = l.physical_property_id WHERE l.id=:i"
        ),
        {"i": listing_id},
    ).fetchone()


# --- Creation is attributed to the authenticated owner ----------------------

def test_created_listing_is_owned_by_the_authenticated_principal(client):
    c, session = client
    owner_a = _owner(c)
    owner_b = _owner(c)

    created = _create(c, owner_a["token"], extra={"user_id": owner_b["user_id"]})
    lid = created["listing_id"]

    # The owner id comes from the validated Bearer token, never from the body.
    assert _listing_owner(session, lid) == (owner_a["user_id"], owner_a["user_id"])
    # The response carries the real listing id and the real saved status.
    assert created["listing_id"] > 0
    assert created["verification_status"] == "PENDING"
    assert created["listing_status"] == "AVAILABLE"


# --- The reported scenario: A creates, B logs in, B must not see A's row ----

def test_owner_b_cannot_see_or_manage_owner_a_listing(client):
    c, session = client
    roshan = _owner(c)
    test1 = _owner(c)

    # 1. Roshan creates a property.
    created = _create(c, roshan["token"], title="Roshan 2 BHK")
    lid = created["listing_id"]
    assert created["verification_status"] == "PENDING"

    # 2. Test 1 signs in (a different account, same OWNER role).
    test1_headers = auth_header(test1["token"])

    # 3. Test 1's "My Properties" collection never contains Roshan's listing.
    r = c.get("/owner/listings", headers=test1_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 0
    assert [i["listing_id"] for i in body["items"]] == []

    # 4. Test 1 cannot open the detail of Roshan's listing.
    r = c.get(f"/owner/listings/{lid}", headers=test1_headers)
    assert r.status_code == 404, r.text
    assert r.json() == {"detail": "Not found"}

    # 5. Test 1 cannot drive any lifecycle transition on it.
    for path, payload in [
        (f"/owner/listings/{lid}/verify", None),
        (f"/owner/listings/{lid}/reject", None),
        (f"/owner/listings/{lid}/submit", None),
        (f"/owner/listings/{lid}/publish", None),
        (f"/owner/listings/{lid}/archive", None),
        (f"/owner/listings/{lid}/status", {"status": "CLOSED"}),
    ]:
        if payload is None:
            r = c.post(path, headers=test1_headers)
        else:
            r = c.post(path, json=payload, headers=test1_headers)
        assert r.status_code == 404, (path, r.text)

    # 6. Test 1 cannot edit, attach photos to, or list leads for it.
    r = c.patch(
        f"/owner/listings/{lid}",
        json={"title": "Hijacked"},
        headers=test1_headers,
    )
    assert r.status_code == 404, r.text
    r = c.get(f"/owner/leads?property_listing_id={lid}", headers=test1_headers)
    assert r.status_code == 404, r.text

    # 7. Roshan's listing row is untouched by every foreign attempt.
    assert _listing_owner(session, lid) == (roshan["user_id"], roshan["user_id"])
    row = session.execute(
        text(
            "SELECT verification_status, listing_status, title FROM property_listings WHERE id=:i"
        ),
        {"i": lid},
    ).fetchone()
    assert row[0] == "PENDING"
    assert row[1] == "AVAILABLE"
    assert row[2] == "Roshan 2 BHK"

    # 8. Roshan still sees and manages their own property.
    roshan_headers = auth_header(roshan["token"])
    r = c.get("/owner/listings", headers=roshan_headers)
    assert r.status_code == 200, r.text
    assert [i["listing_id"] for i in r.json()["items"]] == [lid]
    r = c.get(f"/owner/listings/{lid}", headers=roshan_headers)
    assert r.status_code == 200, r.text
    assert r.json()["listing_id"] == lid
    r = c.post(f"/owner/listings/{lid}/verify", headers=roshan_headers)
    assert r.status_code == 200, r.text
    assert r.json()["verification_status"] == "VERIFIED"


def test_owner_lists_are_scoped_after_switching_accounts(client):
    """The owner list must not reuse the previous account's response."""
    c, _session = client
    roshan = _owner(c)
    test1 = _owner(c)

    _create(c, roshan["token"], title="Roshan Only")

    # Roshan's own list contains exactly his listing.
    r = c.get("/owner/listings", headers=auth_header(roshan["token"]))
    assert r.json()["total"] == 1

    # Test 1's list is empty and stays empty (no fallback to "first listing").
    r = c.get("/owner/listings", headers=auth_header(test1["token"]))
    assert r.json()["total"] == 0
    assert r.json()["items"] == []

    # Test 1 creates their own listing: each account sees only its own row.
    created = _create(c, test1["token"], title="Test 1 Only")
    assert created["listing_id"] > 0
    r = c.get("/owner/listings", headers=auth_header(test1["token"]))
    assert [i["listing_id"] for i in r.json()["items"]] == [created["listing_id"]]
    r = c.get("/owner/listings", headers=auth_header(roshan["token"]))
    assert [i["listing_id"] for i in r.json()["items"]] != [created["listing_id"]]


def test_owner_drafts_are_scoped_to_their_owner(client):
    c, _session = client
    roshan = _owner(c)
    test1 = _owner(c)

    r = c.post(
        "/owner/drafts",
        json={"transaction_type": "RENT", "title": "Roshan Draft", "form_data": {"step": 2}},
        headers=auth_header(roshan["token"]),
    )
    assert r.status_code == 201, r.text
    draft_id = r.json()["id"]

    # Test 1 does not see Roshan's draft in the list...
    r = c.get("/owner/drafts", headers=auth_header(test1["token"]))
    assert r.status_code == 200, r.text
    assert r.json()["items"] == []
    # ...and cannot read, update or delete it.
    for method, path, payload in [
        ("get", f"/owner/drafts/{draft_id}", None),
        ("put", f"/owner/drafts/{draft_id}", {"title": "Hijacked"}),
        ("delete", f"/owner/drafts/{draft_id}", None),
    ]:
        if method == "get":
            r = c.get(path, headers=auth_header(test1["token"]))
        elif method == "put":
            r = c.put(path, json=payload, headers=auth_header(test1["token"]))
        else:
            r = c.delete(path, headers=auth_header(test1["token"]))
        assert r.status_code == 404, (path, r.text)

    # Roshan still owns the draft.
    r = c.get(f"/owner/drafts/{draft_id}", headers=auth_header(roshan["token"]))
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Roshan Draft"


# --- Public visibility is preserved (and still gated on VERIFIED) ----------

def test_public_marketplace_shows_only_published_owner_listings(client):
    c, _session = client
    roshan = _owner(c)
    created = _create(c, roshan["token"], title="Roshan Published")
    lid = created["listing_id"]

    # PENDING: not in the public marketplace.
    assert c.get(f"/buyer/listings/{lid}").status_code == 404
    ids = [x["listing_id"] for x in c.get("/buyer/listings").json()["items"]]
    assert lid not in ids

    # VERIFIED (still AVAILABLE): publicly visible to every buyer.
    r = c.post(f"/owner/listings/{lid}/verify", headers=auth_header(roshan["token"]))
    assert r.status_code == 200, r.text
    assert c.get(f"/buyer/listings/{lid}").status_code == 200
    ids = [x["listing_id"] for x in c.get("/buyer/listings").json()["items"]]
    assert lid in ids


def test_owner_endpoints_require_authentication(client):
    """Anonymous callers never reach another account's collection."""
    c, _session = client
    roshan = _owner(c)
    created = _create(c, roshan["token"])
    lid = created["listing_id"]
    assert c.get("/owner/listings").status_code == 401
    assert c.get(f"/owner/listings/{lid}").status_code == 401
    assert c.get("/owner/drafts").status_code == 401
    assert c.post("/owner/listings").status_code == 401
