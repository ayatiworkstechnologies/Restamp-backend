"""Phase 6B-3: GET /buyer/listings/{id}/similar (rollback-isolated)."""
from tests.test_buyer_6b1 import seed_listing


def seed_group(session, n, ptype="APARTMENT", deal="BUY", base_price=100_00_000_00):
    return [
        seed_listing(session, title=f"S{i}", ptype=ptype, deal=deal,
                     price=base_price + i * 10_00_000_00)
        for i in range(n)
    ]


def test_similar_core_rules(client):
    c, session = client
    ids = seed_group(session, 7)
    source = ids[3]
    r = c.get(f"/buyer/listings/{source}/similar")
    assert r.status_code == 200, r.text
    items = r.json()
    assert len(items) == 5  # maximum 5
    got = [i["listing_id"] for i in items]
    assert source not in got  # source excluded
    assert all(i in ids for i in got)
    # Deterministic: repeated calls identical; closest price first.
    again = [i["listing_id"] for i in c.get(f"/buyer/listings/{source}/similar").json()]
    assert again == got
    prices = [i["price_paise"] for i in items]
    assert prices == sorted(prices, key=lambda p: abs(p - 130_00_000_00))


def test_similar_visibility_and_matching(client):
    c, session = client
    src = seed_listing(session, title="Src", ptype="VILLA", deal="RENT")
    seed_listing(session, title="Other type", ptype="APARTMENT", deal="RENT")
    seed_listing(session, title="Other deal", ptype="VILLA", deal="BUY")
    seed_listing(session, title="Pending peer", ptype="VILLA", deal="RENT", verification="PENDING")
    seed_listing(session, title="Sold peer", ptype="VILLA", deal="RENT", status="SOLD")
    r = c.get(f"/buyer/listings/{src}/similar")
    assert r.json() == []  # no eligible peers -> empty, not 404
    assert c.get("/buyer/listings/999999999/similar").status_code == 404
    hidden = seed_listing(session, title="H", verification="PENDING")
    assert c.get(f"/buyer/listings/{hidden}/similar").json() == {"detail": "Not found"}
    assert c.get(f"/buyer/listings/{src}/similar", params={"limit": 2}).status_code == 200
    assert len(c.get(f"/buyer/listings/{src}/similar", params={"limit": 2}).json()) <= 2
    assert c.get(f"/buyer/listings/{src}/similar", params={"limit": 6}).status_code == 422


def test_similar_no_private_info(client):
    c, session = client
    ids = seed_group(session, 3)
    for item in c.get(f"/buyer/listings/{ids[0]}/similar").json():
        assert "user_id" not in item and "phone" not in str(item).lower()
        assert set(item.keys()) <= {
            "listing_id", "title", "price_paise", "price_period", "price_display",
            "transaction_type", "property_type", "construction_status", "description",
            "area_value", "area_unit", "bedrooms", "bathrooms", "address_line",
            "locality", "city", "pincode", "location_label", "cover_image_url",
        }
