"""Phase 6B-6: saved/wishlist (rollback-isolated)."""
from sqlalchemy import text

from tests.conftest import auth_header, register_phone
from tests.test_buyer_6b1 import seed_listing


def buyer(c, phone):
    return auth_header(register_phone(c, phone)["access_token"])


def test_save_list_unsave_flow(client):
    c, session = client
    h = buyer(c, "+16400000001")
    l1, l2 = seed_listing(session, title="S1"), seed_listing(session, title="S2")
    assert c.post(f"/buyer/saved/{l1}", headers=h).status_code == 201
    assert c.post(f"/buyer/saved/{l2}", headers=h).status_code == 201
    body = c.get("/buyer/saved", headers=h).json()
    assert body["total"] == 2 and [i["listing_id"] for i in body["items"]] == [l2, l1]
    assert c.delete(f"/buyer/saved/{l1}", headers=h).status_code == 204
    assert [i["listing_id"] for i in c.get("/buyer/saved", headers=h).json()["items"]] == [l2]
    assert c.delete(f"/buyer/saved/{l1}", headers=h).status_code == 204  # idempotent
    assert session.execute(text("SELECT COUNT(*) FROM saved_properties")).fetchone()[0] == 1


def test_save_gates_and_duplicates(client):
    c, session = client
    lid = seed_listing(session)
    assert c.post(f"/buyer/saved/{lid}").status_code == 401
    tok = register_phone(c, "+16400000002")
    h = auth_header(tok["access_token"])
    c.post("/users/me/role", json={"role": "OWNER"}, headers=h)
    assert c.post(f"/buyer/saved/{lid}", headers=h).status_code == 403
    hb = buyer(c, "+16400000003")
    for kwargs in ({"verification": "PENDING"}, {"status": "SOLD"}):
        bad = seed_listing(session, **kwargs)
        assert c.post(f"/buyer/saved/{bad}", headers=hb).status_code == 404
    assert c.post("/buyer/saved/999999999", headers=hb).status_code == 404
    r = c.post(f"/buyer/saved/{lid}", headers=hb)
    assert r.status_code == 201
    assert c.post(f"/buyer/saved/{lid}", headers=hb).status_code == 200  # idempotent dup
    assert session.execute(text("SELECT COUNT(*) FROM saved_properties")).fetchone()[0] == 1


def test_saved_isolation_hidden_and_bounds(client):
    c, session = client
    ha, hb = buyer(c, "+16400000004"), buyer(c, "+16400000005")
    mine = seed_listing(session)
    theirs = seed_listing(session)
    assert c.post(f"/buyer/saved/{mine}", headers=ha).status_code == 201
    assert c.post(f"/buyer/saved/{theirs}", headers=hb).status_code == 201
    assert [i["listing_id"] for i in c.get("/buyer/saved", headers=ha).json()["items"]] == [mine]
    # Deleting mine does not touch theirs; deleting theirs as me is a safe no-op.
    assert c.delete(f"/buyer/saved/{theirs}", headers=ha).status_code == 204
    assert [i["listing_id"] for i in c.get("/buyer/saved", headers=hb).json()["items"]] == [theirs]
    # Hidden listings vanish from list results (never leaked as normal results).
    session.execute(text("UPDATE property_listings SET listing_status='SOLD' WHERE id=:i"), {"i": mine})
    session.flush()
    assert c.get("/buyer/saved", headers=ha).json() == {"items": [], "page": 1, "page_size": 20, "total": 0}
    assert c.get("/buyer/saved", params={"page_size": 101}, headers=ha).status_code == 422
    # No private fields in saved cards.
    c.post(f"/buyer/saved/{theirs}", headers=ha)
    for item in c.get("/buyer/saved", headers=ha).json()["items"]:
        assert "user_id" not in item and "phone" not in str(item).lower()
