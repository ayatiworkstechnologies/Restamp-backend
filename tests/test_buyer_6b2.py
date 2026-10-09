"""Phase 6B-2: GET /buyer/listings/{id} — public detail (rollback-isolated)."""
from tests.test_buyer_6b1 import seed_listing


def test_detail_valid_structure(client):
    c, session = client
    lid = seed_listing(session, title="Detail Flat", media=("u1", "u2", "u3"))
    r = c.get(f"/buyer/listings/{lid}")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["listing_id"] == lid and d["title"] == "Detail Flat"
    assert d["price_paise"] == 10_000_000_00 and d["price_display"].startswith("₹")
    assert (d["bedrooms"], d["bathrooms"], d["area_value"]) == (3, 2, 1500)
    assert d["gallery"] == ["u1", "u2", "u3"]  # order_index order
    assert d["cover_image_url"] == "u1"
    assert d["verification_status"] == "VERIFIED" and d["listing_status"] == "AVAILABLE"
    assert "Anna" in d["location_label"] or "L1" in d["location_label"] or d["locality"]
    assert set(d.keys()) == {
        "listing_id", "title", "price_paise", "price_period", "price_display",
        "transaction_type", "property_type", "construction_status", "description",
        "area_value", "area_unit", "bedrooms", "bathrooms", "address_line",
        "locality", "city", "pincode", "location_label", "cover_image_url",
        "gallery", "verification_status", "listing_status",
    }


def test_detail_visibility_and_404(client):
    c, session = client
    good = seed_listing(session, title="G")
    for kwargs, expect in (
        ({"verification": "PENDING"}, 404),
        ({"status": "SOLD"}, 404),
        ({"status": "RENTED"}, 404),
        ({"status": "LEASED"}, 404),
    ):
        lid = seed_listing(session, title="X", **kwargs)
        r = c.get(f"/buyer/listings/{lid}")
        assert r.status_code == expect, (kwargs, r.text)
        assert r.json() == {"detail": "Not found"}
    assert c.get("/buyer/listings/999999999").json() == {"detail": "Not found"}
    assert c.get("/buyer/listings/not-an-id").status_code == 422
    # No phone/owner leakage anywhere in a valid detail body.
    body = c.get(f"/buyer/listings/{good}").json()
    assert "user_id" not in body and "phone" not in str(body).lower()
