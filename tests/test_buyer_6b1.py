"""Phase 6B-1: GET /buyer/listings — public discovery (rollback-isolated)."""
from sqlalchemy import text

from tests.conftest import make_postcode


_seed_n = {"i": 0}


def seed_listing(
    session,
    title="T1",
    price=10_000_000_00,
    period="TOTAL",
    deal="BUY",
    ptype="APARTMENT",
    verification="VERIFIED",
    status="AVAILABLE",
    beds=3,
    baths=2,
    area=1500,
    locality_postcode=None,
    media=(),
):
    """Insert owner + physical + listing (+media). Returns listing id."""
    _seed_n["i"] += 1
    tag = f"-t{_seed_n['i']}"
    session.execute(text("INSERT INTO users (display_name) VALUES ('Owner')"))
    owner = session.execute(text("SELECT LAST_INSERT_ID()")).fetchone()[0]
    pc = locality_postcode or make_postcode(session, tag)
    session.execute(
        text("INSERT INTO physical_properties (postcode_id, user_id) VALUES (:p, :u)"),
        {"p": pc, "u": owner},
    )
    pp = session.execute(text("SELECT LAST_INSERT_ID()")).fetchone()[0]
    session.execute(
        text(
            "INSERT INTO property_listings (physical_property_id, user_id, title, "
            "price_paise, price_period, verification_status, listing_status, "
            "transaction_type, property_type) VALUES "
            "(:pp, :u, :t, :pr, :per, :v, :s, :d, :pt)"
        ),
        {"pp": pp, "u": owner, "t": title, "pr": price, "per": period,
         "v": verification, "s": status, "d": deal, "pt": ptype},
    )
    lid = session.execute(text("SELECT LAST_INSERT_ID()")).fetchone()[0]
    session.execute(
        text("UPDATE physical_properties SET area_value=:a, bedrooms=:b, bathrooms=:ba WHERE id=:i"),
        {"a": area, "b": beds, "ba": baths, "i": pp},
    )
    for i, url in enumerate(media):
        session.execute(
            text("INSERT INTO property_media (property_listing_id, url, media_type, order_index) "
                 "VALUES (:l, :u, 'image', :o)"),
            {"l": lid, "u": url, "o": i},
        )
    session.flush()
    return lid


def test_public_access_and_visibility(client):
    c, session = client
    good = seed_listing(session, title="Good Flat")
    seed_listing(session, title="Pending Flat", verification="PENDING")
    seed_listing(session, title="Sold Flat", status="SOLD")
    seed_listing(session, title="Rented Flat", status="RENTED")
    seed_listing(session, title="Leased Flat", status="LEASED")
    r = c.get("/buyer/listings")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    assert [i["listing_id"] for i in body["items"]] == [good]
    assert body["page"] == 1 and body["page_size"] == 20


def test_deal_and_type_filters(client):
    c, session = client
    b = seed_listing(session, title="B", deal="BUY")
    seed_listing(session, title="R", deal="RESALE")
    seed_listing(session, title="Rent", deal="RENT")
    seed_listing(session, title="Lease", deal="LEASE")
    seed_listing(session, title="Villa", ptype="VILLA")
    assert c.get("/buyer/listings", params={"deal": "BUY"}).json()["total"] == 2
    for deal, expect in (("BUY", 2), ("RESALE", 1), ("RENT", 1), ("LEASE", 1)):
        r = c.get("/buyer/listings", params={"deal": deal})
        assert r.json()["total"] == expect, deal
        assert all(i["transaction_type"] == deal for i in r.json()["items"])
    assert b in {i["listing_id"] for i in c.get("/buyer/listings", params={"deal": "BUY"}).json()["items"]}
    r = c.get("/buyer/listings", params={"property_type": "VILLA"})
    assert r.json()["total"] == 1
    assert c.get("/buyer/listings", params={"deal": "NOPE"}).status_code == 422


def test_bedrooms_price_locality_filters(client):
    c, session = client
    pc = make_postcode(session, "-loc")
    session.execute(text("UPDATE localities SET name='Guindy' WHERE id=(SELECT locality_id FROM postcodes WHERE id=:p)"), {"p": pc})
    session.flush()
    a = seed_listing(session, title="A", beds=2, price=50_00_000_00, locality_postcode=pc)
    seed_listing(session, title="B", beds=4, price=200_00_000_00, locality_postcode=pc)
    assert [i["listing_id"] for i in c.get("/buyer/listings", params={"bedrooms": 2}).json()["items"]] == [a]
    assert c.get("/buyer/listings", params={"bedrooms_min": 4}).json()["total"] == 1
    r = c.get("/buyer/listings", params={"min_price_paise": 100_00_000_00})
    assert r.json()["total"] == 1
    r = c.get("/buyer/listings", params={"max_price_paise": 100_00_000_00})
    assert r.json()["total"] == 1
    assert c.get("/buyer/listings", params={"min_price_paise": 200, "max_price_paise": 100}).status_code == 422
    # Locality substring via hierarchy chain.
    assert c.get("/buyer/listings", params={"locality": "guin"}).json()["total"] == 2
    assert c.get("/buyer/listings", params={"locality": "zzz-nope"}).json()["total"] == 0


def test_pagination_and_sort_and_search(client):
    c, session = client
    for i in range(5):
        seed_listing(session, title=f"Flat {i}", price=(i + 1) * 10_00_000_00, area=1000 + i * 100)
    r = c.get("/buyer/listings", params={"page": 2, "page_size": 2})
    assert (r.json()["page"], r.json()["page_size"], r.json()["total"]) == (2, 2, 5)
    assert len(r.json()["items"]) == 2
    assert c.get("/buyer/listings", params={"page": 0}).status_code == 422
    assert c.get("/buyer/listings", params={"page_size": 101}).status_code == 422
    asc = [i["price_paise"] for i in c.get("/buyer/listings", params={"sort": "price_asc", "page_size": 100}).json()["items"]]
    assert asc == sorted(asc)
    desc = [i["price_paise"] for i in c.get("/buyer/listings", params={"sort": "price_desc", "page_size": 100}).json()["items"]]
    assert desc == sorted(desc, reverse=True)
    areas = [i["area_value"] for i in c.get("/buyer/listings", params={"sort": "area_desc", "page_size": 100}).json()["items"]]
    assert areas == sorted(areas, reverse=True)
    assert c.get("/buyer/listings", params={"q": "Flat 3"}).json()["total"] == 1
    r = c.get("/buyer/listings", params={"count_only": True})
    assert r.json() == {"items": [], "page": 1, "page_size": 20, "total": 5}


def test_no_private_info_media_and_schema(client):
    c, session = client
    lid = seed_listing(session, title="M", media=("http://img/2", "http://img/1"))
    seed_listing(session, title="Nomed")
    r = c.get("/buyer/listings", params={"page_size": 100})
    allowed = {"listing_id", "title", "price_paise", "price_period", "price_display",
               "transaction_type", "property_type", "construction_status", "description",
               "area_value", "area_unit", "bedrooms", "bathrooms", "address_line",
               "locality", "city", "pincode", "location_label", "cover_image_url"}
    for item in r.json()["items"]:
        assert set(item.keys()) <= allowed, set(item.keys()) - allowed
        blob = str(item).lower()
        assert "phone" not in blob and "98765" not in blob
    covers = {i["title"]: i["cover_image_url"] for i in r.json()["items"]}
    assert covers["M"] == "http://img/2"  # first by order_index, single query
    assert covers["Nomed"] is None
    assert r.json()["items"][0]["price_display"].startswith("₹")
