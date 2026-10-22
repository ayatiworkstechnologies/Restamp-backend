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
        "gallery", "verification_status", "listing_status", "owner_name",
    }
    # Seeded owners use display_name 'Owner' (a safe saved name).
    assert d["owner_name"] == "Owner"


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


def test_detail_owner_name_real_vs_phone_default(client):
    """owner_name exposes a safe saved name, never a phone default."""
    import json
    from sqlalchemy import text

    from tests.conftest import make_postcode

    c, session = client

    def _seed(owner_name, phone_identity=None, physical_owner_name=None):
        session.execute(text("INSERT INTO users (display_name) VALUES (:n)"), {"n": owner_name})
        owner = session.execute(text("SELECT LAST_INSERT_ID()")).fetchone()[0]
        if phone_identity:
            session.execute(
                text("INSERT INTO auth_identities (user_id, provider, provider_identifier) "
                     "VALUES (:u, 'phone', :p)"), {"u": owner, "p": phone_identity})
        physical_owner = owner
        if physical_owner_name is not None:
            session.execute(text("INSERT INTO users (display_name) VALUES (:n)"),
                            {"n": physical_owner_name})
            physical_owner = session.execute(text("SELECT LAST_INSERT_ID()")).fetchone()[0]
        pc = make_postcode(session, f"-own-{owner}")
        session.execute(text("INSERT INTO physical_properties (postcode_id, user_id) "
                             "VALUES (:p, :u)"), {"p": pc, "u": physical_owner})
        pp = session.execute(text("SELECT LAST_INSERT_ID()")).fetchone()[0]
        session.execute(text(
            "INSERT INTO property_listings (physical_property_id, user_id, title, "
            "price_paise, verification_status, listing_status, transaction_type, "
            "property_type) VALUES (:pp, :u, 'ON', 100, 'VERIFIED', 'AVAILABLE', 'BUY', 'APARTMENT')"
        ), {"pp": pp, "u": owner})
        session.flush()
        return session.execute(text("SELECT LAST_INSERT_ID()")).fetchone()[0]

    # Real saved name is exposed (and feed cards carry no owner_name at all).
    real = _seed("Isaac Vineeth")
    assert c.get(f"/buyer/listings/{real}").json()["owner_name"] == "Isaac Vineeth"
    feed = c.get("/buyer/listings").json()
    assert all("owner_name" not in item for item in feed["items"])

    # Phone-default display names stay hidden, with no digits anywhere.
    for name, identity in (("+19998887771", "+19998887771"), ("19998887772", "+19998887772")):
        lid = _seed(name, identity)
        body = c.get(f"/buyer/listings/{lid}").json()
        assert body["owner_name"] is None
        blob = json.dumps(body)
        assert identity not in blob and identity.lstrip("+") not in blob

    # Embedded phone numbers are rejected too, not only all-digit names.
    embedded = _seed("Call 98765 43210", "+19998887773")
    body = c.get(f"/buyer/listings/{embedded}").json()
    assert body["owner_name"] is None
    assert "98765" not in json.dumps(body) and "43210" not in json.dumps(body)

    # Listing and physical-property ownership must agree.
    mismatch = _seed("Listing Owner", physical_owner_name="Property Owner")
    assert c.get(f"/buyer/listings/{mismatch}").json()["owner_name"] is None
