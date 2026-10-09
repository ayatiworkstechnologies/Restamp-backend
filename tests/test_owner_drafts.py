"""Tests for owner draft endpoints (persistent JSON snapshots):
- POST /owner/drafts (create)
- GET /owner/drafts (list, owner-scoped, updated-first)
- GET /owner/drafts/{id} (detail, 404 cross-owner)
- PUT /owner/drafts/{id} (replace, idempotent repeat saves)
- DELETE /owner/drafts/{id} (discard, 404 cross-owner)
"""
from sqlalchemy import text
from tests.conftest import auth_header, register_phone


def _setup_owner(c, phone: str) -> str:
    token = register_phone(c, phone)["access_token"]
    r = c.post("/users/me/role", json={"role": "OWNER"}, headers=auth_header(token))
    assert r.status_code == 200, r.text
    return token


def _form(step=3):
    return {
        "lookingTo": "Rent", "category": "Residential", "propertyType": "Apartment",
        "bhk": "2 BHK", "phoneNumber": "+919840100001", "email": "d@example.com",
        "city": "Chennai", "district": "Chennai", "locality": "Anna Nagar",
        "pincode": "600040", "subLocality": "Shanthi Colony",
        "bedrooms": "2", "bathrooms": "2", "carpetArea": "1200",
        "monthlyRent": "25000", "securityDeposit": "100000",
        "availableFrom": "Immediately",
        "photosList": [
            {"id": "local-1", "url": "https://d.test/a.jpg",
             "category": "Living Room", "isCover": True},
            {"id": "local-2", "url": "file:///device/b.jpg",
             "category": "Bedroom", "isCover": False},
        ],
        "selectedAmenities": ["Gym", "Lift"],
        "propertyFeatures": ["Vastu Compliant"],
        "otherRooms": ["Pooja Room"],
        "currentStep": step,
    }


def test_create_draft(client):
    c, _ = client
    token = _setup_owner(c, "+919876543401")
    r = c.post("/owner/drafts", json={
        "transaction_type": "RENT", "title": "2 BHK Apartment",
        "current_step": 3, "form_data": _form()},
        headers=auth_header(token))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["transaction_type"] == "RENT"
    assert body["title"] == "2 BHK Apartment"
    assert body["current_step"] == 3
    assert body["form_data"]["locality"] == "Anna Nagar"
    assert body["form_data"]["photosList"][0]["category"] == "Living Room"
    assert body["created_at"] and body["updated_at"]


def test_get_draft(client):
    c, _ = client
    token = _setup_owner(c, "+919876543402")
    did = c.post("/owner/drafts", json={"title": "Get Me", "form_data": _form()},
                 headers=auth_header(token)).json()["id"]
    r = c.get(f"/owner/drafts/{did}", headers=auth_header(token))
    assert r.status_code == 200, r.text
    assert r.json()["form_data"]["pincode"] == "600040"


def test_list_drafts_owner_scoped_and_ordered(client):
    c, _ = client
    token_a = _setup_owner(c, "+919876543403")
    token_b = _setup_owner(c, "+919876543404")
    c.post("/owner/drafts", json={"title": "A First"}, headers=auth_header(token_a))
    c.post("/owner/drafts", json={"title": "A Second"}, headers=auth_header(token_a))
    c.post("/owner/drafts", json={"title": "B Only"}, headers=auth_header(token_b))
    body = c.get("/owner/drafts", headers=auth_header(token_a)).json()
    assert body["total"] == 2
    assert [i["title"] for i in body["items"]] == ["A Second", "A First"]
    body_b = c.get("/owner/drafts", headers=auth_header(token_b)).json()
    assert body_b["total"] == 1 and body_b["items"][0]["title"] == "B Only"


def test_update_same_draft_no_duplicate(client):
    c, session = client
    token = _setup_owner(c, "+919876543405")
    did = c.post("/owner/drafts", json={"title": "V1", "current_step": 1,
                                        "form_data": _form(1)},
                 headers=auth_header(token)).json()["id"]
    for step in (2, 3, 3):
        r = c.put(f"/owner/drafts/{did}",
                  json={"title": "V1", "current_step": step, "form_data": _form(step)},
                  headers=auth_header(token))
        assert r.status_code == 200, r.text
        assert r.json()["current_step"] == step
    total = c.get("/owner/drafts", headers=auth_header(token)).json()["total"]
    assert total == 1
    row = session.execute(text(
        "SELECT current_step, updated_at, created_at FROM listing_drafts WHERE id=:i"),
        {"i": did}).fetchone()
    assert row[0] == 3 and row[1] >= row[2]


def test_current_step_and_payload_persist(client):
    c, session = client
    token = _setup_owner(c, "+919876543406")
    did = c.post("/owner/drafts",
                 json={"title": "Step5", "current_step": 5, "form_data": _form(5)},
                 headers=auth_header(token)).json()["id"]
    row = session.execute(text(
        "SELECT current_step, form_data FROM listing_drafts WHERE id=:i"),
        {"i": did}).fetchone()
    import json as _json
    assert row[0] == 5
    assert _json.loads(row[1])["monthlyRent"] == "25000"


def test_multiple_drafts_supported(client):
    c, _ = client
    token = _setup_owner(c, "+919876543407")
    ids = {c.post("/owner/drafts", json={"title": f"D{i}"},
                  headers=auth_header(token)).json()["id"] for i in range(3)}
    assert len(ids) == 3
    assert c.get("/owner/drafts", headers=auth_header(token)).json()["total"] == 3


def test_cross_owner_is_404(client):
    c, _ = client
    token_a = _setup_owner(c, "+919876543408")
    token_b = _setup_owner(c, "+919876543409")
    did = c.post("/owner/drafts", json={"title": "Private"},
                 headers=auth_header(token_a)).json()["id"]
    assert c.get(f"/owner/drafts/{did}", headers=auth_header(token_b)).status_code == 404
    assert c.put(f"/owner/drafts/{did}", json={"title": "Hijack"},
                 headers=auth_header(token_b)).status_code == 404
    assert c.delete(f"/owner/drafts/{did}", headers=auth_header(token_b)).status_code == 404
    # Untouched.
    assert c.get(f"/owner/drafts/{did}", headers=auth_header(token_a)).status_code == 200


def test_anonymous_is_401(client):
    c, _ = client
    assert c.post("/owner/drafts", json={"title": "X"}).status_code == 401
    assert c.get("/owner/drafts").status_code == 401
    assert c.get("/owner/drafts/1").status_code == 401
    assert c.put("/owner/drafts/1", json={"title": "X"}).status_code == 401
    assert c.delete("/owner/drafts/1").status_code == 401


def test_buyer_is_403(client):
    c, _ = client
    buyer_token = register_phone(c, "+919876543410")["access_token"]
    assert c.post("/owner/drafts", json={"title": "X"},
                  headers=auth_header(buyer_token)).status_code == 403
    assert c.get("/owner/drafts", headers=auth_header(buyer_token)).status_code == 403


def test_delete_draft(client):
    c, session = client
    token = _setup_owner(c, "+919876543411")
    did = c.post("/owner/drafts", json={"title": "Gone"},
                 headers=auth_header(token)).json()["id"]
    r = c.delete(f"/owner/drafts/{did}", headers=auth_header(token))
    assert r.status_code == 204, r.text
    assert c.get(f"/owner/drafts/{did}", headers=auth_header(token)).status_code == 404
    assert c.get("/owner/drafts", headers=auth_header(token)).json()["total"] == 0
    n = session.execute(text("SELECT COUNT(*) FROM listing_drafts WHERE id=:i"),
                        {"i": did}).fetchone()[0]
    assert n == 0


def test_delete_missing_is_404(client):
    c, _ = client
    token = _setup_owner(c, "+919876543412")
    assert c.delete("/owner/drafts/999999999", headers=auth_header(token)).status_code == 404


def test_invalid_draft_data_handled(client):
    c, _ = client
    token = _setup_owner(c, "+919876543413")
    assert c.post("/owner/drafts", json={"current_step": 9},
                  headers=auth_header(token)).status_code == 422
    assert c.post("/owner/drafts", json={"transaction_type": "MARS"},
                  headers=auth_header(token)).status_code == 422
    # Minimal create works with defaults.
    r = c.post("/owner/drafts", json={}, headers=auth_header(token))
    assert r.status_code == 201, r.text
    assert r.json()["current_step"] == 1 and r.json()["transaction_type"] == "RENT"


def test_drafts_do_not_create_listings(client):
    c, session = client
    token = _setup_owner(c, "+919876543414")
    before = session.execute(text("SELECT COUNT(*) FROM property_listings")).fetchone()[0]
    c.post("/owner/drafts", json={"title": "Not A Listing", "form_data": _form()},
           headers=auth_header(token))
    after = session.execute(text("SELECT COUNT(*) FROM property_listings")).fetchone()[0]
    assert after == before
