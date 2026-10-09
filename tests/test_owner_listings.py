"""Tests for RESTAMP Owner V1 property listing endpoints:
- POST /owner/listings (create listing)
- GET /owner/listings (list caller's listings)
- GET /owner/listings/{listing_id} (detail)
- Role verification (OWNER required)
- Automatic verification: PENDING -> VERIFIED 2 minutes after submission
  (backend-driven on owner-listings fetch, persisted, idempotent)
"""
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from tests.conftest import auth_header, make_postcode, register_phone


def _setup_owner(client) -> tuple[dict, str]:
    """Register a user and upgrade their role to OWNER."""
    token_out = register_phone(client, "+919876543210")
    token = token_out["access_token"]

    # Upgrade role to OWNER
    r = client.post(
        "/users/me/role",
        json={"role": "OWNER"},
        headers=auth_header(token),
    )
    assert r.status_code == 200, r.text
    # Re-verify profile or use token (role is evaluated from DB / token)
    return token_out, token


def test_create_listing_requires_auth(client):
    c, session = client
    r = c.post("/owner/listings", json={"title": "Test Apartment", "price_rupees": 5000000})
    assert r.status_code == 401


def test_create_listing_requires_owner_role(client):
    c, session = client
    # Default registered user is BUYER
    token_out = register_phone(c, "+919876543211")
    token = token_out["access_token"]

    r = c.post(
        "/owner/listings",
        json={
            "title": "Test Villa",
            "price_rupees": 7500000,
            "locality": "Indiranagar",
            "city": "Bengaluru",
            "pincode": "560038",
        },
        headers=auth_header(token),
    )
    assert r.status_code == 403


def test_create_and_fetch_owner_listing(client):
    c, session = client
    _, token = _setup_owner(c)

    # 1. Create a listing
    payload = {
        "title": "Luxury 3BHK Apartment in Indiranagar",
        "transaction_type": "BUY",
        "property_type": "APARTMENT",
        "price_rupees": 12500000,
        "price_period": "TOTAL",
        "locality": "Indiranagar",
        "city": "Bengaluru",
        "pincode": "560038",
        "address_line": "100 Feet Road, 12th Main",
        "area_value": 1850,
        "area_unit": "sqft",
        "bedrooms": 3,
        "bathrooms": 3,
        "description": "Spacious north-facing apartment with 2 car parks.",
        "construction_status": "READY_TO_MOVE",
        "images": [
            "https://images.unsplash.com/photo-1545324418-cc1a3fa10c00?w=800",
            "https://images.unsplash.com/photo-1512917774080-9991f1c4c750?w=800",
        ],
    }

    r = c.post("/owner/listings", json=payload, headers=auth_header(token))
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["title"] == "Luxury 3BHK Apartment in Indiranagar"
    assert created["price_paise"] == 1250000000
    assert created["verification_status"] == "PENDING"
    assert created["listing_status"] == "AVAILABLE"
    assert created["bedrooms"] == 3
    assert created["bathrooms"] == 3
    assert created["locality"] == "Indiranagar"
    assert created["cover_image_url"] == payload["images"][0]
    listing_id = created["listing_id"]

    # 2. List caller's listings
    r = c.get("/owner/listings", headers=auth_header(token))
    assert r.status_code == 200, r.text
    list_data = r.json()
    assert list_data["total"] >= 1
    found = [item for item in list_data["items"] if item["listing_id"] == listing_id]
    assert len(found) == 1
    assert found[0]["title"] == "Luxury 3BHK Apartment in Indiranagar"

    # 3. Get single listing detail
    r = c.get(f"/owner/listings/{listing_id}", headers=auth_header(token))
    assert r.status_code == 200, r.text
    detail = r.json()
    assert detail["listing_id"] == listing_id
    assert detail["gallery"] == payload["images"]
    assert detail["cover_image_url"] == payload["images"][0]


def test_owner_cannot_access_other_owner_listing(client):
    c, session = client
    # Setup Owner A
    token_out_a = register_phone(c, "+919876543212")
    token_a = token_out_a["access_token"]
    c.post("/users/me/role", json={"role": "OWNER"}, headers=auth_header(token_a))

    # Setup Owner B
    token_out_b = register_phone(c, "+919876543213")
    token_b = token_out_b["access_token"]
    c.post("/users/me/role", json={"role": "OWNER"}, headers=auth_header(token_b))

    # Owner A creates listing
    r = c.post(
        "/owner/listings",
        json={
            "title": "Owner A Villa",
            "price_rupees": 5000000,
            "locality": "Whitefield",
            "city": "Bengaluru",
            "pincode": "560066",
        },
        headers=auth_header(token_a),
    )
    assert r.status_code == 201
    listing_id = r.json()["listing_id"]

    # Owner B tries to read Owner A's listing via /owner/listings/{id}
    r = c.get(f"/owner/listings/{listing_id}", headers=auth_header(token_b))
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Automatic verification: PENDING -> VERIFIED 2 minutes after submission
# ---------------------------------------------------------------------------


def _setup_owner_with_phone(client, phone: str) -> str:
    """Register a fresh user on a dedicated phone and upgrade to OWNER."""
    token_out = register_phone(client, phone)
    token = token_out["access_token"]
    r = client.post("/users/me/role", json={"role": "OWNER"}, headers=auth_header(token))
    assert r.status_code == 200, r.text
    return token


def _create_listing(client, token: str, title: str, area_value=None, area_unit=None) -> dict:
    payload = {
        "title": title,
        "transaction_type": "RENT",
        "property_type": "APARTMENT",
        "price_rupees": 25000,
        "price_period": "MONTHLY",
        "locality": "Oldwashermenpet",
        "city": "Chennai",
        "pincode": "600021",
    }
    if area_value is not None:
        payload["area_value"] = area_value
    if area_unit is not None:
        payload["area_unit"] = area_unit
    r = client.post(
        "/owner/listings",
        json=payload,
        headers=auth_header(token),
    )
    assert r.status_code == 201, r.text
    return r.json()


def _db_status(session, listing_id: int):
    # NOTE: no session.rollback() here — the fixture session is shared with
    # the app, and rolling back would undo the test user's OWNER role.
    return session.execute(
        text("SELECT verification_status, listing_status, submitted_at "
             "FROM property_listings WHERE id=:i"),
        {"i": listing_id},
    ).fetchone()


def test_new_listing_starts_pending_with_submitted_at(client):
    c, session = client
    token = _setup_owner_with_phone(c, "+919876543230")
    created = _create_listing(c, token, "AutoVerify Fresh Flat")
    listing_id = created["listing_id"]
    assert created["verification_status"] == "PENDING"
    assert created["listing_status"] == "AVAILABLE"

    # Immediately after creation the listing is still PENDING, and a
    # submission timestamp was recorded (UTC).
    vstat, lstat, submitted_at = _db_status(session, listing_id)
    assert vstat == "PENDING"
    assert lstat == "AVAILABLE"
    assert submitted_at is not None
    age = datetime.now(timezone.utc).replace(tzinfo=None) - submitted_at
    assert age < timedelta(minutes=2)

    r = c.get("/owner/listings", headers=auth_header(token))
    assert r.status_code == 200, r.text
    found = [i for i in r.json()["items"] if i["listing_id"] == listing_id]
    assert len(found) == 1
    assert found[0]["verification_status"] == "PENDING"


def test_listing_auto_verifies_after_two_minutes_and_persists(client):
    c, session = client
    token = _setup_owner_with_phone(c, "+919876543231")
    listing_id = _create_listing(c, token, "AutoVerify Due Flat")["listing_id"]

    # Simulate the 2-minute window having elapsed (no waiting in tests).
    session.execute(
        text("UPDATE property_listings SET submitted_at=:t WHERE id=:i"),
        {"t": datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=3),
         "i": listing_id},
    )
    session.commit()

    # Next fetch transitions PENDING -> VERIFIED + AVAILABLE ...
    r = c.get("/owner/listings", headers=auth_header(token))
    assert r.status_code == 200, r.text
    found = [i for i in r.json()["items"] if i["listing_id"] == listing_id]
    assert len(found) == 1
    assert found[0]["verification_status"] == "VERIFIED"
    assert found[0]["listing_status"] == "AVAILABLE"

    # ... persisted in MySQL (not just returned temporarily) ...
    vstat, lstat, _ = _db_status(session, listing_id)
    assert vstat == "VERIFIED"
    assert lstat == "AVAILABLE"

    # ... and idempotent: repeated fetches keep it VERIFIED without changes.
    r = c.get("/owner/listings", headers=auth_header(token))
    found = [i for i in r.json()["items"] if i["listing_id"] == listing_id]
    assert found[0]["verification_status"] == "VERIFIED"
    vstat, _, _ = _db_status(session, listing_id)
    assert vstat == "VERIFIED"


def test_listing_detail_fetch_also_auto_verifies(client):
    c, session = client
    token = _setup_owner_with_phone(c, "+919876543232")
    listing_id = _create_listing(c, token, "AutoVerify Detail Flat")["listing_id"]
    session.execute(
        text("UPDATE property_listings SET submitted_at=:t WHERE id=:i"),
        {"t": datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5),
         "i": listing_id},
    )
    session.commit()

    r = c.get(f"/owner/listings/{listing_id}", headers=auth_header(token))
    assert r.status_code == 200, r.text
    assert r.json()["verification_status"] == "VERIFIED"
    vstat, _, _ = _db_status(session, listing_id)
    assert vstat == "VERIFIED"


def test_listing_without_submitted_at_never_auto_verifies(client):
    c, session = client
    token = _setup_owner_with_phone(c, "+919876543233")
    listing_id = _create_listing(c, token, "AutoVerify NullTs Flat")["listing_id"]

    # Rows without a submission time (seed rows, pre-existing listings) are
    # excluded from auto-verification even long after creation.
    session.execute(
        text("UPDATE property_listings SET submitted_at=NULL WHERE id=:i"),
        {"i": listing_id},
    )
    session.commit()

    r = c.get("/owner/listings", headers=auth_header(token))
    found = [i for i in r.json()["items"] if i["listing_id"] == listing_id]
    assert found[0]["verification_status"] == "PENDING"
    vstat, _, _ = _db_status(session, listing_id)
    assert vstat == "PENDING"


def test_seed_listings_unchanged_by_auto_verify(client):
    c, session = client
    # The seeded PENDING row has no submitted_at and must stay PENDING; all
    # other seeded rows must stay exactly as seeded.
    rows = session.execute(text(
        "SELECT title, verification_status, listing_status FROM property_listings "
        "WHERE user_id=(SELECT user_id FROM auth_identities WHERE provider='phone' "
        "AND provider_identifier='+16501110002')"
    )).fetchall()
    by_title = {r[0]: (r[1], r[2]) for r in rows}
    assert by_title["Seed Draft Flat"] == ("PENDING", "AVAILABLE")
    assert by_title["Seed Sold Flat"] == ("VERIFIED", "SOLD")
    assert by_title["Seed Emerald 3BHK"] == ("VERIFIED", "AVAILABLE")


# ---------------------------------------------------------------------------
# RENT DB layer: new columns/tables/constraints (additive, nullable)
# ---------------------------------------------------------------------------


def test_rent_physical_columns_exist_and_nullable(client):
    c, session = client
    cols = {
        r[0]: r[1]
        for r in session.execute(text(
            "SELECT COLUMN_NAME, IS_NULLABLE FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'physical_properties'"
        )).fetchall()
    }
    expected = ["balconies", "built_up_area", "super_built_up_area", "total_floors",
                "floor_on", "is_duplex", "property_age_band", "sub_locality",
                "society_name", "house_no", "landmark", "latitude", "longitude",
                "furnishing", "covered_parking", "open_parking", "open_sides",
                "overlooking", "power_backup", "facing"]
    for col in expected:
        assert col in cols, col
        if col != "is_duplex":
            assert cols[col] == "YES", col


def test_rent_listing_columns_and_lifecycle_enums(client):
    c, session = client
    cols = {
        r[0]: r[1]
        for r in session.execute(text(
            "SELECT COLUMN_NAME, IS_NULLABLE FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'property_listings'"
        )).fetchall()
    }
    for col in ("contact_phone", "contact_email", "closed_reason"):
        assert cols[col] == "YES", col
    vstat = session.execute(text(
        "SELECT COLUMN_TYPE FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'property_listings' "
        "AND COLUMN_NAME = 'verification_status'")).fetchone()[0]
    assert "REJECTED" in vstat
    lstat = session.execute(text(
        "SELECT COLUMN_TYPE FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'property_listings' "
        "AND COLUMN_NAME = 'listing_status'")).fetchone()[0]
    assert "CLOSED" in lstat and "EXPIRED" in lstat


def test_rent_terms_one_to_one(client):
    c, session = client
    # 1:1 is enforced at DDL: UNIQUE KEY on rent_terms.property_listing_id.
    uniq = session.execute(text(
        "SELECT COUNT(*) FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'rent_terms' "
        "AND COLUMN_NAME = 'property_listing_id' AND NON_UNIQUE = 0")).fetchone()[0]
    assert uniq >= 1
    token = _setup_owner_with_phone(c, "+919876543234")
    listing_id = _create_listing(c, token, "AutoVerify Terms Flat")["listing_id"]
    # No terms row is auto-created by publish (API phase writes it later).
    n = session.execute(text(
        "SELECT COUNT(*) FROM rent_terms WHERE property_listing_id=:i"),
        {"i": listing_id}).fetchone()[0]
    assert n == 0
    # A valid terms row persists with FK to the listing.
    session.execute(text(
        "INSERT INTO rent_terms (property_listing_id, lock_in_months, is_negotiable) "
        "VALUES (:i, 6, 1)"), {"i": listing_id})
    session.commit()
    row = session.execute(text(
        "SELECT lock_in_months, is_negotiable FROM rent_terms WHERE property_listing_id=:i"),
        {"i": listing_id}).fetchone()
    assert tuple(row) == (6, 1)


def test_amenity_master_seed_and_link(client):
    c, session = client
    rows = session.execute(text(
        "SELECT name, kind FROM amenity_master")).fetchall()
    by_kind = {}
    for name, kind in rows:
        by_kind.setdefault(kind, []).append(name)
    assert len(by_kind["AMENITY"]) == 16
    assert len(by_kind["FEATURE"]) == 14
    assert len(by_kind["ROOM"]) == 5
    assert "Gym" in by_kind["AMENITY"] and "Vastu Compliant" in by_kind["FEATURE"]
    assert "Pooja Room" in by_kind["ROOM"]
    # Re-seed is a no-op: same count, no duplicates.
    before = len(rows)
    for name, kind in rows:
        session.execute(text(
            "INSERT IGNORE INTO amenity_master (name, kind) VALUES (:n, :k)"),
            {"n": name, "k": kind})
    session.commit()
    after = session.execute(text("SELECT COUNT(*) FROM amenity_master")).fetchone()[0]
    assert after == before
    # Link rows: composite PK (property_listing_id, amenity_id) — verified at
    # DDL below; here a valid link persists and reads back.
    token = _setup_owner_with_phone(c, "+919876543235")
    listing_id = _create_listing(c, token, "AutoVerify Amenity Flat")["listing_id"]
    amenity_id = session.execute(text(
        "SELECT id FROM amenity_master WHERE name='Gym'")).fetchone()[0]
    session.execute(text(
        "INSERT INTO listing_amenities (property_listing_id, amenity_id) VALUES (:l, :a)"),
        {"l": listing_id, "a": amenity_id})
    session.commit()
    n = session.execute(text(
        "SELECT COUNT(*) FROM listing_amenities "
        "WHERE property_listing_id=:l AND amenity_id=:a"),
        {"l": listing_id, "a": amenity_id}).fetchone()[0]
    assert n == 1
    pk_cols = session.execute(text(
        "SELECT COUNT(*) FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'listing_amenities' "
        "AND INDEX_NAME = 'PRIMARY'")).fetchone()[0]
    assert pk_cols == 2


def test_media_columns_exist(client):
    c, session = client
    cols = {r[0] for r in session.execute(text(
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'property_media'")).fetchall()}
    assert {"category", "mime_type", "byte_size"} <= cols


def test_area_unit_normalized_lowercase(client):
    c, session = client
    token = _setup_owner_with_phone(c, "+919876543236")
    lid = _create_listing(c, token, "AutoVerify Unit Flat",
                          area_value=1200, area_unit="SQFT")["listing_id"]
    unit = session.execute(text(
        "SELECT p.area_unit FROM physical_properties p JOIN property_listings l "
        "ON l.physical_property_id = p.id WHERE l.id=:i"), {"i": lid}).fetchone()[0]
    # Uppercase 'SQFT' is canonicalized to lowercase 'sqft' at the
    # persistence boundary; existing 'SQFT' rows are untouched.
    assert unit == "sqft"


# ---------------------------------------------------------------------------
# RENT create/read API: full persistence of the 7-step form
# ---------------------------------------------------------------------------


def _post_listing(client, token: str, title: str, **overrides):
    payload = {
        "title": title,
        "transaction_type": "RENT",
        "property_type": "APARTMENT",
        "price_rupees": 25000,
        "price_period": "MONTHLY",
        "locality": "Oldwashermenpet",
        "city": "Chennai",
        "pincode": "600021",
    }
    payload.update(overrides)
    return client.post("/owner/listings", json=payload, headers=auth_header(token))


def _db_row(session, listing_id: int):
    return session.execute(text(
        "SELECT l.verification_status, l.listing_status, l.contact_phone, l.contact_email,"
        " p.balconies, p.built_up_area, p.super_built_up_area, p.total_floors, p.floor_on,"
        " p.is_duplex, p.property_age_band, p.sub_locality, p.society_name, p.house_no,"
        " p.landmark, p.furnishing, p.covered_parking, p.open_parking, p.open_sides,"
        " p.overlooking, p.power_backup, p.facing, p.ownership_type"
        " FROM property_listings l JOIN physical_properties p"
        " ON p.id = l.physical_property_id WHERE l.id=:i"),
        {"i": listing_id}).fetchone()


def test_full_rent_create_persists_everywhere(client):
    c, session = client
    token = _setup_owner_with_phone(c, "+919876543271")
    body = {
        "district": "Test District 271", "locality": "Test Nagar 271", "pincode": "600271",
        "sub_locality": "5th Avenue", "society_name": "Green Acres", "house_no": "Flat 402",
        "landmark": "Tower Park", "latitude": 13.0850, "longitude": 80.2101,
        "bedrooms": 2, "bathrooms": 2, "balconies": 2, "area_value": 1200,
        "built_up_area": 1380, "super_built_up_area": 1550, "total_floors": 8,
        "floor_on": "3", "is_duplex": False, "property_age_band": "1–5 Years",
        "furnishing": "Semi-Furnished", "covered_parking": 1, "open_parking": 1,
        "open_sides": "2", "overlooking": "Park", "power_backup": "Full",
        "facing": "East", "ownership_type": "Freehold",
        "contact_phone": "+919840123456", "contact_email": "owner@example.com",
        "description": "Corner flat near the park.",
        "rent_terms": {"security_deposit_rupees": 100000, "maintenance_rupees": 3000,
                       "maintenance_period": "Monthly", "is_negotiable": True,
                       "available_from": "Immediately", "tenant_preference": "Family",
                       "lock_in_months": "1 Year", "agreement_months": "11 Months"},
        "amenities": ["Gym", "Lift"], "property_features": ["Vastu Compliant"],
        "other_rooms": ["Pooja Room"],
        "photos": [{"url": "https://t.test/a.jpg", "category": "Living Room"},
                   {"url": "https://t.test/b.jpg", "category": "Bedroom"}],
    }
    r = _post_listing(c, token, "Full Rent Flat 271", **body)
    assert r.status_code == 201, r.text
    created = r.json()
    lid = created["listing_id"]
    assert created["verification_status"] == "PENDING"
    assert created["district"] == "Test District 271"
    assert created["contact_phone"] == "+919840123456"
    assert created["rent_terms"]["security_deposit_paise"] == 10000000
    assert created["rent_terms"]["maintenance_paise"] == 300000
    assert created["rent_terms"]["lock_in_months"] == 12
    assert created["rent_terms"]["agreement_months"] == 11
    assert created["amenities"] == ["Gym", "Lift"] or sorted(created["amenities"]) == ["Gym", "Lift"]
    assert created["photos"][0] == {"url": "https://t.test/a.jpg", "category": "Living Room"}
    assert created["cover_image_url"] == "https://t.test/a.jpg"
    # DB destinations
    row = _db_row(session, lid)
    assert row[2] == "+919840123456" and row[3] == "owner@example.com"
    assert tuple(row[4:12]) == (2, 1380, 1550, 8, "3", 0, "1–5 Years", "5th Avenue")
    assert row[12] == "Green Acres" and row[13] == "Flat 402" and row[14] == "Tower Park"
    assert row[15] == "SEMI_FURNISHED" and row[20] == "FULL" and row[21] == "East"
    assert row[22] == "FREEHOLD"
    terms = session.execute(text(
        "SELECT security_deposit_paise, tenant_preference FROM rent_terms "
        "WHERE property_listing_id=:i"), {"i": lid}).fetchone()
    assert tuple(terms) == (10000000, "FAMILY")
    links = session.execute(text(
        "SELECT m.name FROM listing_amenities la JOIN amenity_master m ON m.id = la.amenity_id "
        "WHERE la.property_listing_id=:i"), {"i": lid}).fetchall()
    assert sorted(n for (n,) in links) == ["Gym", "Lift", "Pooja Room", "Vastu Compliant"]
    media = session.execute(text(
        "SELECT url, category, order_index FROM property_media "
        "WHERE property_listing_id=:i ORDER BY order_index"), {"i": lid}).fetchall()
    assert [(u, cat, idx) for (u, cat, idx) in media] == [
        ("https://t.test/a.jpg", "Living Room", 0),
        ("https://t.test/b.jpg", "Bedroom", 1)]


def test_minimal_rent_create(client):
    c, session = client
    token = _setup_owner_with_phone(c, "+919876543272")
    r = _post_listing(c, token, "Minimal Rent Flat")
    assert r.status_code == 201, r.text
    lid = r.json()["listing_id"]
    assert r.json()["verification_status"] == "PENDING"
    assert r.json()["rent_terms"] is None
    assert r.json()["amenities"] == [] and r.json()["photos"] == []
    n = session.execute(text(
        "SELECT COUNT(*) FROM rent_terms WHERE property_listing_id=:i"),
        {"i": lid}).fetchone()[0]
    assert n == 0
    vstat, _, submitted = session.execute(text(
        "SELECT verification_status, listing_status, submitted_at FROM property_listings "
        "WHERE id=:i"), {"i": lid}).fetchone()
    assert submitted is not None


def test_available_from_edge_cases(client):
    c, session = client
    token = _setup_owner_with_phone(c, "+919876543273")
    today = datetime.now(timezone.utc).date()
    cases = [
        ("Immediately", today), ("Today", today), ("Tomorrow", today + timedelta(days=1)),
        ("Within 1 Week", today + timedelta(days=7)),
        ("Within 15 Days", today + timedelta(days=15)),
        ("Within 1 Month", today + timedelta(days=30)),
        ("2026-12-25", date(2026, 12, 25)), ("25 Dec 2026", date(2026, 12, 25)),
    ]
    for idx, (label, expected) in enumerate(cases):
        r = _post_listing(c, token, f"Avail Flat {idx}",
                          rent_terms={"available_from": label})
        assert r.status_code == 201, (label, r.text)
        assert r.json()["rent_terms"]["available_from"] == expected.isoformat(), label


def test_amenity_unknown_rejected_and_duplicates_idempotent(client):
    c, session = client
    token = _setup_owner_with_phone(c, "+919876543274")
    r = _post_listing(c, token, "Bad Amenity Flat", amenities=["Gym", "No Such Place"])
    assert r.status_code == 422, r.text
    assert "No Such Place" in r.json()["detail"]
    r = _post_listing(c, token, "Dup Amenity Flat",
                      amenities=["Gym", " gym ", "GYM", "Lift"])
    assert r.status_code == 201, r.text
    assert sorted(r.json()["amenities"]) == ["Gym", "Lift"]


def test_legacy_images_fallback_category_null(client):
    c, session = client
    token = _setup_owner_with_phone(c, "+919876543275")
    r = _post_listing(c, token, "Legacy Img Flat",
                      images=["https://t.test/1.jpg", "https://t.test/2.jpg"])
    assert r.status_code == 201, r.text
    assert r.json()["cover_image_url"] == "https://t.test/1.jpg"
    assert r.json()["photos"] == [
        {"url": "https://t.test/1.jpg", "category": None},
        {"url": "https://t.test/2.jpg", "category": None}]


def test_create_validation_errors(client):
    c, session = client
    token = _setup_owner_with_phone(c, "+919876543276")
    bad_bodies = [
        ({"pincode": "12345"}, "pincode"),
        ({"pincode": "abcdef"}, "pincode"),
        ({"rent_terms": {"security_deposit_rupees": -5}}, "deposit"),
        ({"rent_terms": {"maintenance_rupees": -1}}, "maintenance"),
        ({"furnishing": "Gold-Plated"}, "furnishing"),
        ({"rent_terms": {"maintenance_period": "Weekly"}}, "maintenance_period"),
        ({"contact_email": "not-an-email"}, "email"),
        ({"contact_phone": "123"}, "phone"),
        ({"latitude": 91.0}, "latitude"),
        ({"longitude": 200.0}, "longitude"),
        ({"floor_on": "5", "total_floors": 3}, "floor"),
        ({"ownership_type": "Mars"}, "ownership"),
        ({"district": "   "}, "district"),
        ({"photos": [{"url": "https://t.test/x.jpg", "category": "Sauna"}]}, "category"),
    ]
    for idx, (override, _label) in enumerate(bad_bodies):
        r = _post_listing(c, token, f"Invalid Flat {idx}", **override)
        assert r.status_code == 422, (override, r.status_code, r.text)


def test_create_security(client):
    c, session = client
    # Buyer role cannot create.
    buyer_out = register_phone(c, "+919876543277")
    buyer_token = buyer_out["access_token"]
    r = _post_listing(c, buyer_token, "Buyer Sneak Flat")
    assert r.status_code == 403, r.text
    # No token cannot create.
    r = c.post("/owner/listings", json={"title": "Anon", "price_rupees": 5})
    assert r.status_code == 401, r.text
    # Cross-owner read is 404.
    token_a = _setup_owner_with_phone(c, "+919876543278")
    token_b = _setup_owner_with_phone(c, "+919876543279")
    r = _post_listing(c, token_a, "Owner A Private Flat")
    assert r.status_code == 201, r.text
    lid = r.json()["listing_id"]
    r = c.get(f"/owner/listings/{lid}", headers=auth_header(token_b))
    assert r.status_code == 404, r.text


def test_create_rollback_on_invalid_amenity(client):
    c, session = client
    token = _setup_owner_with_phone(c, "+919876543280")
    before_listings = session.execute(text("SELECT COUNT(*) FROM property_listings")).fetchone()[0]
    before_phys = session.execute(text("SELECT COUNT(*) FROM physical_properties")).fetchone()[0]
    owner_id = session.execute(text(
        "SELECT user_id FROM auth_identities WHERE provider='phone' "
        "AND provider_identifier='+919876543280'")).fetchone()[0]
    from app.services import owner_listings as svc
    sp = session.begin_nested()
    try:
        with pytest.raises(ValueError, match="Unknown amenity"):
            svc.create_owner_listing(
                session, owner_id, title="Rollback Flat", transaction_type="RENT",
                property_type="APARTMENT", price_paise=2500000, price_period="MONTHLY",
                locality="Rollback Nagar", city="Chennai", pincode="600280",
                address_line=None, area_value=None, area_unit=None, bedrooms=2,
                bathrooms=2, description=None, construction_status=None,
                image_urls=["https://t.test/r.jpg"], amenities=["Gym", "Bogus Room X"])
    finally:
        sp.rollback()
    assert session.execute(text("SELECT COUNT(*) FROM property_listings")).fetchone()[0] == before_listings
    assert session.execute(text("SELECT COUNT(*) FROM physical_properties")).fetchone()[0] == before_phys
