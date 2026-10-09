"""Phase 6B-5: buyer enquiries (rollback-isolated)."""
from sqlalchemy import text

from tests.conftest import auth_header, make_postcode, register_phone

_listing_n = {"i": 0}


def seed_listing(session, verification="VERIFIED", status="AVAILABLE"):
    _listing_n["i"] += 1
    n = _listing_n["i"]
    session.execute(text("INSERT INTO users (display_name) VALUES ('Owner')"))
    owner = session.execute(text("SELECT LAST_INSERT_ID()")).fetchone()[0]
    pc = make_postcode(session, f"-e5-{n}")
    session.execute(
        text("INSERT INTO physical_properties (postcode_id, user_id) VALUES (:p, :u)"),
        {"p": pc, "u": owner},
    )
    pp = session.execute(text("SELECT LAST_INSERT_ID()")).fetchone()[0]
    session.execute(
        text(
            "INSERT INTO property_listings (physical_property_id, user_id, title, "
            "price_paise, verification_status, listing_status, transaction_type, "
            "property_type) VALUES (:pp, :u, 'E1', 100, :v, :s, 'BUY', 'APARTMENT')"
        ),
        {"pp": pp, "u": owner, "v": verification, "s": status},
    )
    session.flush()
    return session.execute(text("SELECT LAST_INSERT_ID()")).fetchone()[0]


def buyer_token(c, phone):
    return register_phone(c, phone)["access_token"]


def test_create_enquiry_new_and_active_row(client):
    c, session = client
    lid = seed_listing(session)
    h = auth_header(buyer_token(c, "+16300000001"))
    r = c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": "Hi"}, headers=h)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "NEW" and body["message"] == "Hi"
    assert body["property_listing_id"] == lid
    assert body["listing"]["title"] == "E1"
    assert session.execute(text("SELECT COUNT(*) FROM active_enquiries")).fetchone()[0] == 1
    # Message optional; extra buyer_user_id in body is ignored (JWT rules).
    lid2 = seed_listing(session)
    r = c.post("/buyer/enquiries", json={"property_listing_id": lid2, "buyer_user_id": 1}, headers=h)
    assert r.status_code == 201
    assert r.json()["message"] is None


def test_create_gates(client):
    c, session = client
    lid = seed_listing(session)
    assert c.post("/buyer/enquiries", json={"property_listing_id": lid}).status_code == 401
    tok = buyer_token(c, "+16300000002")
    h = auth_header(tok)
    c.post("/users/me/role", json={"role": "OWNER"}, headers=h)
    assert c.post("/buyer/enquiries", json={"property_listing_id": lid}, headers=h).status_code == 403
    buyer = buyer_token(c, "+16300000003")
    hb = auth_header(buyer)
    for kwargs in ({"verification": "PENDING"}, {"status": "SOLD"}):
        bad = seed_listing(session, **kwargs)
        assert c.post("/buyer/enquiries", json={"property_listing_id": bad}, headers=hb).status_code == 404
    assert c.post("/buyer/enquiries", json={"property_listing_id": 999999999}, headers=hb).status_code == 404
    assert c.post("/buyer/enquiries", json={"property_listing_id": 0}, headers=hb).status_code == 422
    assert session.execute(text("SELECT COUNT(*) FROM enquiries")).fetchone()[0] == 0


def test_duplicate_prevented_and_closed_allows_new(client):
    c, session = client
    lid = seed_listing(session)
    h = auth_header(buyer_token(c, "+16300000004"))
    first = c.post("/buyer/enquiries", json={"property_listing_id": lid}, headers=h).json()
    r = c.post("/buyer/enquiries", json={"property_listing_id": lid}, headers=h)
    assert r.status_code == 200  # idempotent duplicate, not a second row
    assert r.json()["id"] == first["id"]
    assert session.execute(text("SELECT COUNT(*) FROM active_enquiries")).fetchone()[0] == 1
    # Simulate owner close (status + active-row removal belong to Owner APIs).
    session.execute(text("UPDATE enquiries SET status='CLOSED' WHERE id=:i"), {"i": first["id"]})
    session.execute(text("DELETE FROM active_enquiries WHERE enquiry_id=:i"), {"i": first["id"]})
    session.flush()
    r = c.post("/buyer/enquiries", json={"property_listing_id": lid}, headers=h)
    assert r.status_code == 201 and r.json()["id"] != first["id"]
    assert r.json()["status"] == "NEW"


def test_list_and_detail_scoping(client):
    c, session = client
    ha = auth_header(buyer_token(c, "+16300000005"))
    hb = auth_header(buyer_token(c, "+16300000006"))
    l1, l2 = seed_listing(session), seed_listing(session)
    a1 = c.post("/buyer/enquiries", json={"property_listing_id": l1}, headers=ha).json()["id"]
    b1 = c.post("/buyer/enquiries", json={"property_listing_id": l2}, headers=hb).json()["id"]
    mine = c.get("/buyer/enquiries", headers=ha).json()
    assert mine["total"] == 1 and mine["items"][0]["id"] == a1
    assert c.get("/buyer/enquiries", headers=hb).json()["items"][0]["id"] == b1
    # Deterministic newest-first ordering.
    l3 = seed_listing(session)
    a2 = c.post("/buyer/enquiries", json={"property_listing_id": l3}, headers=ha).json()["id"]
    ids = [i["id"] for i in c.get("/buyer/enquiries", headers=ha).json()["items"]]
    assert ids == sorted(ids, reverse=True) and ids[0] == a2
    assert c.get("/buyer/enquiries", params={"page_size": 101}, headers=ha).status_code == 422
    # Detail: own 200, other's 404, missing 404.
    assert c.get(f"/buyer/enquiries/{a1}", headers=ha).status_code == 200
    assert c.get(f"/buyer/enquiries/{b1}", headers=ha).status_code == 404
    assert c.get("/buyer/enquiries/999999999", headers=ha).status_code == 404
    assert c.get(f"/buyer/enquiries/{a1}").status_code == 401
    # Status filter + canonical statuses only.
    assert c.get("/buyer/enquiries", params={"status": "NEW"}, headers=ha).json()["total"] == 2
    assert {i["status"] for i in c.get("/buyer/enquiries", headers=ha).json()["items"]} == {"NEW"}
    # No leakage: exact key contract.
    item = c.get("/buyer/enquiries", headers=ha).json()["items"][0]
    assert set(item.keys()) == {"id", "property_listing_id", "message", "status", "closing_reason", "listing"}
    assert set(item["listing"].keys()) == {"title", "price_paise", "price_period"}
    assert "owner_user_id" not in str(item) and "phone" not in str(item).lower()
