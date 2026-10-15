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
    lids = [
        seed_listing(session, title=f"Flat {i}", price=(i + 1) * 10_00_000_00, area=1000 + i * 100)
        for i in range(5)
    ]
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
    # Multi-term text search is AND across terms (every term must appear
    # somewhere in the record): the exact-title row is always recalled, but
    # sibling rows whose generated location names contain the digit may also
    # match, so only membership — not an exact total — is asserted here.
    got = c.get("/buyer/listings", params={"q": "Flat 3"}).json()
    assert got["total"] >= 1
    assert lids[3] in [i["listing_id"] for i in got["items"]]
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


def test_construction_status_filter(client):
    c, session = client
    a = seed_listing(session, title="CS-Ready-Flat")
    b = seed_listing(session, title="CS-New-Flat")
    session.execute(text("UPDATE property_listings SET construction_status='READY_TO_MOVE' WHERE id=:i"), {"i": a})
    session.execute(text("UPDATE property_listings SET construction_status='NEW_LAUNCH' WHERE id=:i"), {"i": b})
    session.flush()
    # Distinctive q scopes past any shared-database rows.
    r = c.get("/buyer/listings", params={"construction_status": "READY_TO_MOVE", "q": "CS-"})
    assert r.status_code == 200, r.text
    assert [i["listing_id"] for i in r.json()["items"]] == [a]
    r = c.get("/buyer/listings", params={"construction_status": "UNDER_CONSTRUCTION", "q": "CS-"})
    assert r.json()["total"] == 0 and r.json()["items"] == []
    assert c.get("/buyer/listings", params={"construction_status": "BOGUS"}).status_code == 422


def test_locality_substring_and_zero_match_for_explore(client):
    # Explore-by-Location preparation: the existing `locality` substring
    # filter is sufficient — no new endpoint required.
    c, session = client
    pc = make_postcode(session, "-explore")
    session.execute(text("UPDATE localities SET name='Sholinganallur' WHERE id=(SELECT locality_id FROM postcodes WHERE id=:p)"), {"p": pc})
    session.flush()
    lid = seed_listing(session, title="EXP-Flat", locality_postcode=pc)
    assert c.get("/buyer/listings", params={"locality": "sholing", "q": "EXP-"}).json()["total"] == 1
    assert [i["listing_id"] for i in c.get("/buyer/listings", params={"locality": "sholing", "q": "EXP-"}).json()["items"]] == [lid]
    r = c.get("/buyer/listings", params={"locality": "zzz-no-such-locality"})
    assert r.status_code == 200 and r.json()["total"] == 0 and r.json()["items"] == []


def _seed_smart(session, tag, title, description, locality_name, address_line):
    """Listing with fully controlled text fields. Returns listing id."""
    pc = make_postcode(session, tag)
    session.execute(
        text("UPDATE localities SET name=:n WHERE id=(SELECT locality_id FROM postcodes WHERE id=:p)"),
        {"n": locality_name, "p": pc},
    )
    lid = seed_listing(session, title=title, locality_postcode=pc)
    session.execute(
        text("UPDATE property_listings SET description=:d WHERE id=:l"), {"d": description, "l": lid}
    )
    session.execute(
        text(
            "UPDATE physical_properties SET address_line=:a WHERE id="
            "(SELECT physical_property_id FROM property_listings WHERE id=:l)"
        ),
        {"a": address_line, "l": lid},
    )
    session.flush()
    return lid


def test_smart_multi_term_search(client):
    # All assertions scope to unique Q9Z tokens, never absolute totals, so
    # unrelated shared-database rows cannot break them.
    c, session = client
    a = _seed_smart(
        session, "-sq1",
        title="Home ECR starter Q9Z 1BHK",
        description="Sunny Q9Z flat near the park",
        locality_name="Thiruvanmiyur",
        address_line="5 Beach Road",
    )
    b = _seed_smart(
        session, "-sq2",
        title="Villa Q9Z Alpha",
        description="Gated community Q9Z",
        locality_name="Anna Nagar Q9Z",
        address_line="12 Main Street",
    )
    d = _seed_smart(
        session, "-sq3",
        title="Villa Q9Z Beta",
        description="Unrelated Q9Z text",
        locality_name="Faraway Q9Z",
        address_line="99 Other Road",
    )

    def ids(**params):
        r = c.get("/buyer/listings", params={"page_size": 100, **params})
        assert r.status_code == 200, r.text
        return [i["listing_id"] for i in r.json()["items"]]

    # 1. Mid-word substring.
    assert a in ids(q="tarte Q9Z")
    # 2. Case-insensitive.
    assert a in ids(q="STARTER q9z")
    # 3. Locality term.
    assert b in ids(q="anna nagar q9z")
    # 4/5. Multi-term across fields; single-term-only listing excluded.
    got = ids(q="villa anna nagar q9z")
    assert b in got and d not in got
    # 6. Leading/trailing + repeated whitespace.
    assert b in ids(q="  villa   anna   nagar   q9z  ")
    # 9. Zero results are an honest empty page.
    r = c.get("/buyer/listings", params={"q": "zzqnope9x"})
    assert r.status_code == 200 and r.json()["total"] == 0 and r.json()["items"] == []
    # 10. Text search composes with existing filters/sort/pagination.
    assert b in ids(q="villa q9z", deal="BUY", sort="price_asc")
    page1 = c.get("/buyer/listings", params={"q": "Q9Z", "page": 1, "page_size": 2}).json()
    page2 = c.get("/buyer/listings", params={"q": "Q9Z", "page": 2, "page_size": 2}).json()
    assert page1["total"] >= 3
    assert not set(i["listing_id"] for i in page1["items"]) & set(
        i["listing_id"] for i in page2["items"]
    )


def test_smart_search_specials_and_blank_query(client):
    # LIKE specials match literally; blank queries add no text filter.
    c, session = client
    p = _seed_smart(
        session, "-sq4",
        title="100% Genuine Q9W Deal",
        description="Sea_view Q9W duplex",
        locality_name="Pctown Q9W",
        address_line="7 Percent Road",
    )
    other = _seed_smart(
        session, "-sq5",
        title="SeaXview Q9W Cottage",
        description="Plain Q9W listing",
        locality_name="Othertown Q9W",
        address_line="8 Other Road",
    )

    def ids(**params):
        r = c.get("/buyer/listings", params={"page_size": 100, **params})
        assert r.status_code == 200, r.text
        return [i["listing_id"] for i in r.json()["items"]]

    # 7. Literal % and _ (unescaped _ would also match "SeaXview").
    assert p in ids(q="100% Q9W")
    got = ids(q="sea_view Q9W")
    assert p in got and other not in got
    # 8. Empty / whitespace-only q behaves like no text filter.
    plain = c.get("/buyer/listings", params={"page_size": 100}).json()["total"]
    blank = c.get("/buyer/listings", params={"page_size": 100, "q": "   "}).json()["total"]
    assert blank == plain


def test_four_deals_seed_faithful_rows_visible(client):
    """Regression for empty Home sections: seed-shaped rows (descriptions,
    construction statuses, NULL bedrooms like LEASE/COMMERCIAL seeds in
    scripts/seed_dev_data.py) must appear in every deal feed, while
    PENDING/SOLD rows stay excluded. All assertions scope to unique SF4D
    tokens, never absolute totals, so unrelated shared-database rows cannot
    break them."""
    c, session = client
    pc = make_postcode(session, "-4deal")
    specs = [
        ("SF4D BUY Flat", "BUY", "APARTMENT", 3, "READY_TO_MOVE"),
        ("SF4D RESALE Villa", "RESALE", "VILLA", 4, "READY_TO_MOVE"),
        ("SF4D RENT House", "RENT", "HOUSE", 2, "UNDER_CONSTRUCTION"),
        ("SF4D LEASE Office", "LEASE", "COMMERCIAL", None, None),
    ]
    wanted = {}
    for title, deal, ptype, beds, cstat in specs:
        lid = seed_listing(
            session, title=title, deal=deal, ptype=ptype, beds=beds,
            locality_postcode=pc,
        )
        session.execute(
            text("UPDATE property_listings SET description=:d WHERE id=:l"),
            {"d": f"{title} seed-shaped description", "l": lid},
        )
        if cstat is not None:
            session.execute(
                text("UPDATE property_listings SET construction_status=:s WHERE id=:l"),
                {"s": cstat, "l": lid},
            )
        session.flush()
        wanted[deal] = lid
    seed_listing(session, title="SF4D-HIDDEN Pending", deal="BUY",
                 locality_postcode=pc, verification="PENDING")
    seed_listing(session, title="SF4D-HIDDEN Sold", deal="BUY",
                 locality_postcode=pc, status="SOLD")
    for deal, lid in wanted.items():
        r = c.get("/buyer/listings", params={"deal": deal, "q": "SF4D", "page_size": 50})
        assert r.status_code == 200, r.text
        body = r.json()
        assert lid in [i["listing_id"] for i in body["items"]], deal
        assert all(i["transaction_type"] == deal for i in body["items"]), deal
    hidden = c.get("/buyer/listings", params={"q": "SF4D-HIDDEN", "page_size": 50}).json()
    assert hidden["total"] == 0 and hidden["items"] == []
