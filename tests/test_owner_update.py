"""Tests for owner listing Edit mode (Phase 2):
- PATCH /owner/listings/{id} (in-place update, no duplicates)
- DELETE /owner/listings/{id}/photos/{media_id} (removal + renumber + cloud)
- LEASE terms persistence; RESALE/BUY terms rejection (explicit 422)
- Partial/explicit-null/zero/false semantics; ownership enforcement.
"""
import io

import pytest
from sqlalchemy import text
from tests.conftest import auth_header, register_phone
from tests.test_owner_photos import JPEG_BYTES, PNG_1PX, _mock_cloud


def _setup_owner_with_phone(client, phone: str) -> str:
    token_out = register_phone(client, phone)
    token = token_out["access_token"]
    r = client.post("/users/me/role", json={"role": "OWNER"}, headers=auth_header(token))
    assert r.status_code == 200, r.text
    return token


def _post_listing(client, token, title, **overrides):
    payload = {
        "title": title, "transaction_type": "RENT", "property_type": "APARTMENT",
        "price_rupees": 25000, "price_period": "MONTHLY",
        "locality": "Edit Nagar", "city": "Chennai", "pincode": "600040",
        "bedrooms": 2, "contact_phone": "+919840100001",
        "rent_terms": {"security_deposit_rupees": 100000, "maintenance_rupees": 3000,
                       "maintenance_period": "Monthly", "is_negotiable": True,
                       "available_from": "Immediately", "tenant_preference": "Family",
                       "lock_in_months": "1 Year", "agreement_months": "11 Months"},
        "amenities": ["Gym", "Lift"], "property_features": ["Vastu Compliant"],
        "other_rooms": ["Pooja Room"],
        "photos": [{"url": "https://t.test/e1.jpg", "category": "Living Room"},
                   {"url": "https://t.test/e2.jpg", "category": "Bedroom"}],
    }
    payload.update(overrides)
    return client.post("/owner/listings", json=payload, headers=auth_header(token))


def _counts(session):
    l = session.execute(text("SELECT COUNT(*) FROM property_listings")).fetchone()[0]
    p = session.execute(text("SELECT COUNT(*) FROM physical_properties")).fetchone()[0]
    return l, p


def test_patch_rent_update_same_ids_no_duplicates(client):
    c, session = client
    token = _setup_owner_with_phone(c, "+919876543401")
    r = _post_listing(c, token, "Edit Base Flat")
    assert r.status_code == 201, r.text
    lid = r.json()["listing_id"]
    phys_before = session.execute(text(
        "SELECT physical_property_id FROM property_listings WHERE id=:i"),
        {"i": lid}).fetchone()[0]
    before = _counts(session)
    r = c.patch(f"/owner/listings/{lid}",
                json={"title": "Edit Base Flat Revised", "price_rupees": 30000,
                      "bedrooms": 3, "description": "Updated desc"},
                headers=auth_header(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["listing_id"] == lid
    assert body["title"] == "Edit Base Flat Revised"
    assert body["price_paise"] == 3000000
    assert body["bedrooms"] == 3
    assert body["description"] == "Updated desc"
    assert body["verification_status"] == "PENDING"
    assert body["listing_status"] == "AVAILABLE"
    assert _counts(session) == before
    phys_after = session.execute(text(
        "SELECT physical_property_id FROM property_listings WHERE id=:i"),
        {"i": lid}).fetchone()[0]
    assert phys_after == phys_before
    # Omitted fields preserved.
    assert body["contact_phone"] == "+919840100001"
    assert body["rent_terms"]["security_deposit_paise"] == 10000000
    assert sorted(body["amenities"]) == ["Gym", "Lift"]
    assert [p["url"] for p in body["photos"]] == [
        "https://t.test/e1.jpg", "https://t.test/e2.jpg"]


def test_patch_partial_and_null_zero_false_semantics(client):
    c, session = client
    token = _setup_owner_with_phone(c, "+919876543402")
    lid = _post_listing(c, token, "Semantics Flat").json()["listing_id"]
    # Explicit null clears nullable; 0/False preserved; omitted kept.
    r = c.patch(f"/owner/listings/{lid}",
                json={"description": None, "balconies": 0, "is_duplex": False,
                      "covered_parking": 0, "landmark": None},
                headers=auth_header(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["description"] is None
    assert body["landmark"] is None
    assert body["balconies"] == 0
    assert body["is_duplex"] is False
    assert body["covered_parking"] == 0
    assert body["bedrooms"] == 2  # omitted
    # Explicit null on non-nullable columns is a 422.
    for bad in ({"title": None}, {"price_rupees": None}, {"locality": None},
                {"city": None}, {"pincode": None}):
        r = c.patch(f"/owner/listings/{lid}", json=bad, headers=auth_header(token))
        assert r.status_code == 422, (bad, r.status_code, r.text)


def test_patch_invalid_values_rejected_nothing_committed(client):
    c, session = client
    token = _setup_owner_with_phone(c, "+919876543403")
    lid = _post_listing(c, token, "Invalid Patch Flat").json()["listing_id"]
    before_title = c.get(f"/owner/listings/{lid}",
                         headers=auth_header(token)).json()["title"]
    bad = [
        {"pincode": "12345"}, {"pincode": "abcdef"},
        {"pincode": "600000"}, {"pincode": "600119"}, {"pincode": "600500"},
        {"floor_on": "9", "total_floors": 3},
        {"contact_phone": "123"}, {"contact_email": "nope"},
        {"furnishing": "Gold-Plated"}, {"ownership_type": "Mars"},
        {"photos": [{"url": "https://t.test/x.jpg", "category": "Sauna"}]},
        {"amenities": ["Gym", "Bogus Place X"]},
        {"rent_terms": {"security_deposit_rupees": -5}},
    ]
    for override in bad:
        r = c.patch(f"/owner/listings/{lid}", json=override, headers=auth_header(token))
        assert r.status_code == 422, (override, r.status_code, r.text)
    after = c.get(f"/owner/listings/{lid}", headers=auth_header(token)).json()
    assert after["title"] == before_title
    assert after["contact_phone"] == "+919840100001"


def test_patch_amenities_diff_no_duplicates_retry_idempotent(client):
    c, session = client
    token = _setup_owner_with_phone(c, "+919876543404")
    lid = _post_listing(c, token, "Amenity Diff Flat").json()["listing_id"]

    def links():
        return sorted(n for (n,) in session.execute(text(
            "SELECT m.name FROM listing_amenities la JOIN amenity_master m "
            "ON m.id = la.amenity_id WHERE la.property_listing_id=:i"),
            {"i": lid}).fetchall())

    assert links() == ["Gym", "Lift", "Pooja Room", "Vastu Compliant"]
    r = c.patch(f"/owner/listings/{lid}",
                json={"amenities": ["Lift", "Swimming Pool"]},
                headers=auth_header(token))
    assert r.status_code == 200, r.text
    assert sorted(r.json()["amenities"]) == ["Lift", "Swimming Pool"]
    assert links() == ["Lift", "Pooja Room", "Swimming Pool", "Vastu Compliant"]
    # Retry with identical body: no new links.
    n_before = session.execute(text(
        "SELECT COUNT(*) FROM listing_amenities WHERE property_listing_id=:i"),
        {"i": lid}).fetchone()[0]
    r = c.patch(f"/owner/listings/{lid}",
                json={"amenities": ["Lift", "Swimming Pool"]},
                headers=auth_header(token))
    assert r.status_code == 200, r.text
    n_after = session.execute(text(
        "SELECT COUNT(*) FROM listing_amenities WHERE property_listing_id=:i"),
        {"i": lid}).fetchone()[0]
    assert n_after == n_before
    # Clearing one kind leaves other kinds intact.
    r = c.patch(f"/owner/listings/{lid}", json={"other_rooms": []},
                headers=auth_header(token))
    assert r.status_code == 200, r.text
    assert r.json()["other_rooms"] == []
    assert sorted(r.json()["amenities"]) == ["Lift", "Swimming Pool"]


def test_patch_lease_terms_persist_resale_terms_rejected(client):
    c, session = client
    token = _setup_owner_with_phone(c, "+919876543405")
    # LEASE create persists terms (previously silently dropped).
    r = _post_listing(c, token, "Lease Terms Flat", transaction_type="LEASE",
                      price_period="MONTHLY",
                      rent_terms={"security_deposit_rupees": 50000,
                                  "agreement_months": "2 Years"})
    assert r.status_code == 201, r.text
    lease_id = r.json()["listing_id"]
    assert r.json()["rent_terms"]["security_deposit_paise"] == 5000000
    assert r.json()["rent_terms"]["agreement_months"] == 24
    # LEASE partial terms update merges.
    r = c.patch(f"/owner/listings/{lease_id}",
                json={"rent_terms": {"maintenance_rupees": 2000}},
                headers=auth_header(token))
    assert r.status_code == 200, r.text
    assert r.json()["rent_terms"]["maintenance_paise"] == 200000
    assert r.json()["rent_terms"]["security_deposit_paise"] == 5000000
    # RESALE create with non-empty terms is an explicit 422.
    r = _post_listing(c, token, "Resale Terms Flat", transaction_type="RESALE",
                      price_period="TOTAL",
                      rent_terms={"security_deposit_rupees": 1000})
    assert r.status_code == 422, r.text
    # RESALE create with null terms succeeds.
    r = _post_listing(c, token, "Resale Clean Flat", transaction_type="RESALE",
                      price_period="TOTAL", rent_terms=None)
    assert r.status_code == 201, r.text
    resale_id = r.json()["listing_id"]
    assert r.json()["rent_terms"] is None
    # RESALE PATCH with terms is a 422; null terms PATCH is fine.
    r = c.patch(f"/owner/listings/{resale_id}",
                json={"rent_terms": {"security_deposit_rupees": 5}},
                headers=auth_header(token))
    assert r.status_code == 422, r.text
    r = c.patch(f"/owner/listings/{resale_id}",
                json={"price_rupees": 9000000}, headers=auth_header(token))
    assert r.status_code == 200, r.text
    # RENT -> RESALE type change drops the now-inapplicable terms row.
    rent_id = _post_listing(c, token, "Type Change Flat").json()["listing_id"]
    r = c.patch(f"/owner/listings/{rent_id}",
                json={"transaction_type": "RESALE", "price_period": "TOTAL",
                      "price_rupees": 8000000},
                headers=auth_header(token))
    assert r.status_code == 200, r.text
    assert r.json()["transaction_type"] == "RESALE"
    assert r.json()["rent_terms"] is None
    n = session.execute(text(
        "SELECT COUNT(*) FROM rent_terms WHERE property_listing_id=:i"),
        {"i": rent_id}).fetchone()[0]
    assert n == 0


def test_patch_location_change_keeps_submitted_pincode(client):
    c, session = client
    token = _setup_owner_with_phone(c, "+919876543406")
    lid = _post_listing(c, token, "Location Change Flat").json()["listing_id"]
    # NOTE: 600110 is unused by seeds/other suites; exact-pincode-first
    # lookup creates a fresh chain paired with this locality, so reruns of
    # this test resolve identically.
    r = c.patch(f"/owner/listings/{lid}",
                json={"locality": "New Nagar Xyz", "pincode": "600110"},
                headers=auth_header(token))
    assert r.status_code == 200, r.text
    assert r.json()["pincode"] == "600110"
    assert r.json()["locality"] == "New Nagar Xyz"
    stored = session.execute(text(
        "SELECT pc.pincode FROM property_listings l "
        "JOIN physical_properties p ON p.id = l.physical_property_id "
        "JOIN postcodes pc ON pc.id = p.postcode_id WHERE l.id=:i"),
        {"i": lid}).fetchone()[0]
    assert stored == "600110"
    # Locality-only change keeps the existing pincode.
    r = c.patch(f"/owner/listings/{lid}", json={"locality": "Edit Nagar"},
                headers=auth_header(token))
    assert r.status_code == 200, r.text
    assert r.json()["pincode"] == "600110"


def test_patch_media_endstate_reorder_remove_insert(client):
    c, session = client
    token = _setup_owner_with_phone(c, "+919876543407")
    lid = _post_listing(c, token, "Media Reconcile Flat").json()["listing_id"]
    before = _counts(session)
    # Reorder + category change + insert + implicit removal of e2.
    r = c.patch(f"/owner/listings/{lid}",
                json={"photos": [
                    {"url": "https://t.test/e3.jpg", "category": "Kitchen"},
                    {"url": "https://t.test/e1.jpg", "category": "Balcony"}]},
                headers=auth_header(token))
    assert r.status_code == 200, r.text
    photos = r.json()["photos"]
    assert [(p["url"], p["category"], p["order_index"], p["is_cover"])
            for p in photos] == [
        ("https://t.test/e3.jpg", "Kitchen", 0, True),
        ("https://t.test/e1.jpg", "Balcony", 1, False)]
    assert r.json()["cover_image_url"] == "https://t.test/e3.jpg"
    assert _counts(session) == before
    rows = session.execute(text(
        "SELECT url, category, order_index FROM property_media "
        "WHERE property_listing_id=:i ORDER BY order_index"), {"i": lid}).fetchall()
    assert [(u, cat, idx) for (u, cat, idx) in rows] == [
        ("https://t.test/e3.jpg", "Kitchen", 0),
        ("https://t.test/e1.jpg", "Balcony", 1)]
    # Retry identical end-state: media row count stable (idempotent).
    n_before = session.execute(text(
        "SELECT COUNT(*) FROM property_media WHERE property_listing_id=:i"),
        {"i": lid}).fetchone()[0]
    r = c.patch(f"/owner/listings/{lid}",
                json={"photos": [
                    {"url": "https://t.test/e3.jpg", "category": "Kitchen"},
                    {"url": "https://t.test/e1.jpg", "category": "Balcony"}]},
                headers=auth_header(token))
    assert r.status_code == 200, r.text
    n_after = session.execute(text(
        "SELECT COUNT(*) FROM property_media WHERE property_listing_id=:i"),
        {"i": lid}).fetchone()[0]
    assert n_after == n_before
    # Explicit [] clears the gallery.
    r = c.patch(f"/owner/listings/{lid}", json={"photos": []},
                headers=auth_header(token))
    assert r.status_code == 200, r.text
    assert r.json()["photos"] == []
    assert r.json()["cover_image_url"] is None


def test_delete_photo_renumbers_cover_and_destroys_cloud(client, monkeypatch):
    calls = []
    _mock_cloud(monkeypatch, calls)
    c, session = client
    token = _setup_owner_with_phone(c, "+919876543408")

    def _upload(lid, content, name, mime, category="Living Room"):
        return c.post(f"/owner/listings/{lid}/photos",
                      files={"file": (name, io.BytesIO(content), mime)},
                      data={"category": category}, headers=auth_header(token))

    payload = {"title": "Delete Photo Flat", "transaction_type": "RENT",
               "property_type": "APARTMENT", "price_rupees": 25000,
               "price_period": "MONTHLY", "locality": "Del Nagar",
               "city": "Chennai", "pincode": "600040"}
    lid = c.post("/owner/listings", json=payload,
                 headers=auth_header(token)).json()["listing_id"]
    first = _upload(lid, PNG_1PX, "a.png", "image/png", "Living Room").json()
    second = _upload(lid, JPEG_BYTES, "b.jpg", "image/jpeg", "Kitchen").json()
    # public_id persisted for cloud cleanup.
    stored_pid = session.execute(text(
        "SELECT public_id FROM property_media WHERE id=:i"),
        {"i": first["id"]}).fetchone()[0]
    assert stored_pid is not None
    # Delete the cover: sibling promoted, cover invariant holds.
    r = c.delete(f"/owner/listings/{lid}/photos/{first['id']}",
                 headers=auth_header(token))
    assert r.status_code == 204, r.text
    assert ("destroy", stored_pid) in calls
    detail = c.get(f"/owner/listings/{lid}", headers=auth_header(token)).json()
    assert [p["url"] for p in detail["photos"]] == [second["url"]]
    assert detail["photos"][0]["order_index"] == 0
    assert detail["photos"][0]["is_cover"] is True
    assert detail["cover_image_url"] == second["url"]


def test_delete_photo_cloud_failure_still_succeeds_row_gone(client, monkeypatch):
    from app.services import photo_storage as photo_svc
    calls = []
    _mock_cloud(monkeypatch, calls)
    c, session = client
    token = _setup_owner_with_phone(c, "+919876543409")
    payload = {"title": "Cloud Fail Flat", "transaction_type": "RENT",
               "property_type": "APARTMENT", "price_rupees": 25000,
               "price_period": "MONTHLY", "locality": "Clf Nagar",
               "city": "Chennai", "pincode": "600040"}
    lid = c.post("/owner/listings", json=payload,
                 headers=auth_header(token)).json()["listing_id"]
    r = c.post(f"/owner/listings/{lid}/photos",
               files={"file": ("a.png", io.BytesIO(PNG_1PX), "image/png")},
               data={"category": "Living Room"}, headers=auth_header(token))
    mid = r.json()["id"]

    def boom(public_id):
        calls.append(("destroy-fail", public_id))
        raise OSError("cloud down")

    monkeypatch.setattr(photo_svc, "delete_uploaded_image", boom)
    r = c.delete(f"/owner/listings/{lid}/photos/{mid}",
                 headers=auth_header(token))
    assert r.status_code == 204, r.text
    n = session.execute(text(
        "SELECT COUNT(*) FROM property_media WHERE id=:i"), {"i": mid}).fetchone()[0]
    assert n == 0


def test_patch_and_delete_ownership_enforcement(client):
    c, session = client
    token_a = _setup_owner_with_phone(c, "+919876543410")
    token_b = _setup_owner_with_phone(c, "+919876543411")
    buyer_out = register_phone(c, "+919876543412")
    buyer_token = buyer_out["access_token"]
    lid = _post_listing(c, token_a, "Ownership Guard Flat").json()["listing_id"]
    mid = c.get(f"/owner/listings/{lid}",
                headers=auth_header(token_a)).json()["photos"][0]["id"]
    # PATCH: anon 401, buyer 403, cross-owner 404, unknown id 404.
    assert c.patch(f"/owner/listings/{lid}", json={"title": "X"}).status_code == 401
    assert c.patch(f"/owner/listings/{lid}", json={"title": "X"},
                   headers=auth_header(buyer_token)).status_code == 403
    assert c.patch(f"/owner/listings/{lid}", json={"title": "X"},
                   headers=auth_header(token_b)).status_code == 404
    assert c.patch("/owner/listings/99999999", json={"title": "X"},
                   headers=auth_header(token_a)).status_code == 404
    # DELETE photo: same matrix.
    assert c.delete(f"/owner/listings/{lid}/photos/{mid}").status_code == 401
    assert c.delete(f"/owner/listings/{lid}/photos/{mid}",
                    headers=auth_header(buyer_token)).status_code == 403
    assert c.delete(f"/owner/listings/{lid}/photos/{mid}",
                    headers=auth_header(token_b)).status_code == 404
    assert c.delete(f"/owner/listings/{lid}/photos/99999999",
                    headers=auth_header(token_a)).status_code == 404
    # Failed attempts changed nothing.
    assert c.get(f"/owner/listings/{lid}",
                 headers=auth_header(token_a)).json()["title"] == "Ownership Guard Flat"


def test_patch_single_null_term_clears_column_keeps_rest(client):
    c, session = client
    token = _setup_owner_with_phone(c, "+919876543420")
    lid = _post_listing(c, token, "Null Term Flat").json()["listing_id"]
    # Clear one sub-key; omitted sub-keys must keep stored values.
    r = c.patch(f"/owner/listings/{lid}",
                json={"rent_terms": {"security_deposit_rupees": None}},
                headers=auth_header(token))
    assert r.status_code == 200, r.text
    terms = r.json()["rent_terms"]
    assert terms is not None
    assert terms["security_deposit_paise"] is None
    assert terms["maintenance_paise"] == 300000
    assert terms["tenant_preference"] == "FAMILY"
    assert terms["lock_in_months"] == 12
    # Zero/False supplied alongside a null are preserved, not treated as missing.
    r = c.patch(f"/owner/listings/{lid}",
                json={"rent_terms": {"maintenance_rupees": 0,
                                     "is_negotiable": False,
                                     "tenant_preference": None}},
                headers=auth_header(token))
    assert r.status_code == 200, r.text
    terms = r.json()["rent_terms"]
    assert terms["maintenance_paise"] == 0
    assert terms["is_negotiable"] is False
    assert terms["tenant_preference"] is None
    assert terms["security_deposit_paise"] is None  # still cleared
    assert terms["lock_in_months"] == 12  # still preserved


def test_patch_null_only_terms_without_row_is_noop(client):
    c, session = client
    token = _setup_owner_with_phone(c, "+919876543421")
    r = _post_listing(c, token, "No Terms Flat", rent_terms=None)
    assert r.status_code == 201, r.text
    lid = r.json()["listing_id"]
    assert r.json()["rent_terms"] is None
    r = c.patch(f"/owner/listings/{lid}",
                json={"rent_terms": {"security_deposit_rupees": None}},
                headers=auth_header(token))
    assert r.status_code == 200, r.text
    assert r.json()["rent_terms"] is None
    n = session.execute(text(
        "SELECT COUNT(*) FROM rent_terms WHERE property_listing_id=:i"),
        {"i": lid}).fetchone()[0]
    assert n == 0
    # Whole-object null on a terms-less listing is likewise a no-op success.
    r = c.patch(f"/owner/listings/{lid}", json={"rent_terms": None},
                headers=auth_header(token))
    assert r.status_code == 200, r.text
    assert r.json()["rent_terms"] is None


def test_patch_empty_terms_object_clears_row(client):
    c, session = client
    token = _setup_owner_with_phone(c, "+919876543422")
    lid = _post_listing(c, token, "Empty Terms Flat").json()["listing_id"]
    r = c.patch(f"/owner/listings/{lid}", json={"rent_terms": {}},
                headers=auth_header(token))
    assert r.status_code == 200, r.text
    assert r.json()["rent_terms"] is None
    n = session.execute(text(
        "SELECT COUNT(*) FROM rent_terms WHERE property_listing_id=:i"),
        {"i": lid}).fetchone()[0]
    assert n == 0


def test_service_boundary_rejects_out_of_range_pincode(client):
    c, session = client
    token = _setup_owner_with_phone(c, "+919876543423")
    owner_id = session.execute(text(
        "SELECT user_id FROM auth_identities WHERE provider='phone' "
        "AND provider_identifier='+919876543423'")).fetchone()[0]
    from app.services import owner_listings as svc
    before = session.execute(text(
        "SELECT COUNT(*) FROM property_listings")).fetchone()[0]
    # Direct create with out-of-range pincodes fails before any mutation.
    for pin in ["600000", "600119", "600500", "560038", "abcdef", ""]:
        sp = session.begin_nested()
        try:
            with pytest.raises(ValueError, match="600001 and 600118"):
                svc.create_owner_listing(
                    session, owner_id, title="Svc Pin Flat",
                    transaction_type="RENT", property_type="APARTMENT",
                    price_paise=2500000, price_period="MONTHLY",
                    locality="Svc Nagar", city="Chennai", pincode=pin,
                    address_line=None, area_value=None, area_unit=None,
                    bedrooms=1, bathrooms=1, description=None,
                    construction_status=None, image_urls=[])
        finally:
            sp.rollback()
    assert session.execute(text(
        "SELECT COUNT(*) FROM property_listings")).fetchone()[0] == before
    # Direct update with an out-of-range pincode fails; omitted preserves.
    lid = _post_listing(c, token, "Svc Update Flat").json()["listing_id"]
    sp = session.begin_nested()
    try:
        with pytest.raises(ValueError, match="600001 and 600118"):
            svc.update_owner_listing(session, owner_id, lid, {"pincode": "600119"})
    finally:
        sp.rollback()
    assert c.get(f"/owner/listings/{lid}",
                 headers=auth_header(token)).json()["pincode"] == "600040"
    # In-range direct service update still works (trimmed, string kept).
    sp = session.begin_nested()
    try:
        listing, _ = svc.update_owner_listing(
            session, owner_id, lid,
            {"locality": "Svc Nagar 2", "pincode": " 600089 "})
        assert listing is not None
    finally:
        sp.rollback()
