"""P3 Owner Leads MVP: reveal-to-lead linkage + GET /owner/leads (rollback-isolated)."""
from sqlalchemy import text

from tests.conftest import auth_header, make_postcode, register_phone

_owner_n = {"i": 0}
_buyer_n = {"i": 200}


def seed_owner_listing(session, verification="VERIFIED", status="AVAILABLE"):
    """Owner (with phone identity) + AVAILABLE listing. Returns (lid, owner_id, phone)."""
    _owner_n["i"] += 1
    owner_phone = f"+91610010{_owner_n['i']:04d}"
    pc = make_postcode(session, f"-ol-{_owner_n['i']}")
    session.execute(text("INSERT INTO users (display_name) VALUES ('Owner')"))
    owner = session.execute(text("SELECT LAST_INSERT_ID()")).fetchone()[0]
    session.execute(
        text("INSERT INTO auth_identities (user_id, provider, provider_identifier) "
             "VALUES (:u, 'phone', :p)"),
        {"u": owner, "p": owner_phone},
    )
    session.execute(
        text("INSERT INTO physical_properties (postcode_id, user_id) VALUES (:p, :u)"),
        {"p": pc, "u": owner},
    )
    pp = session.execute(text("SELECT LAST_INSERT_ID()")).fetchone()[0]
    session.execute(
        text(
            "INSERT INTO property_listings (physical_property_id, user_id, title, "
            "price_paise, price_period, verification_status, listing_status, transaction_type, "
            "property_type) VALUES (:pp, :u, 'OL1', 100, 'TOTAL', :v, :s, 'BUY', 'APARTMENT')"
        ),
        {"pp": pp, "u": owner, "v": verification, "s": status},
    )
    session.flush()
    lid = session.execute(text("SELECT LAST_INSERT_ID()")).fetchone()[0]
    return lid, owner, owner_phone


def fresh_buyer(c):
    _buyer_n["i"] += 1
    return register_phone(c, f"+91620010{_buyer_n['i']:04d}")


def owner_header(c, phone):
    tok = register_phone(c, phone)
    h = auth_header(tok["access_token"])
    c.post("/users/me/role", json={"role": "OWNER"}, headers=h)
    return h, tok["user_id"]


def enquiry_count(session, buyer_id, listing_id):
    return session.execute(
        text("SELECT COUNT(*) FROM enquiries WHERE buyer_user_id=:b AND property_listing_id=:l"),
        {"b": buyer_id, "l": listing_id},
    ).fetchone()[0]


def audit_count(session, buyer_id, listing_id):
    return session.execute(
        text("SELECT COUNT(*) FROM contact_reveal_audits WHERE user_id=:b AND property_listing_id=:l"),
        {"b": buyer_id, "l": listing_id},
    ).fetchone()[0]


def test_reveal_creates_single_lead_and_reuses_on_repeat(client):
    c, session = client
    lid, owner, owner_phone = seed_owner_listing(session)
    buyer = fresh_buyer(c)
    h = auth_header(buyer["access_token"])

    r = c.post(f"/buyer/listings/{lid}/contact", headers=h)
    assert r.status_code == 200, r.text
    assert r.json() == {"phone": owner_phone}
    assert enquiry_count(session, buyer["user_id"], lid) == 1
    row = session.execute(
        text("SELECT status, owner_user_id, message FROM enquiries "
             "WHERE buyer_user_id=:b AND property_listing_id=:l"),
        {"b": buyer["user_id"], "l": lid},
    ).fetchone()
    assert row[0] == "NEW" and row[1] == owner and row[2] is None
    first_id = session.execute(
        text("SELECT id FROM enquiries WHERE buyer_user_id=:b AND property_listing_id=:l"),
        {"b": buyer["user_id"], "l": lid},
    ).fetchone()[0]
    assert audit_count(session, buyer["user_id"], lid) == 1

    # Repeat reveal: same phone, same single lead, second audit event.
    r = c.post(f"/buyer/listings/{lid}/contact", headers=h)
    assert r.status_code == 200 and r.json() == {"phone": owner_phone}
    assert enquiry_count(session, buyer["user_id"], lid) == 1
    kept = session.execute(
        text("SELECT id FROM enquiries WHERE buyer_user_id=:b AND property_listing_id=:l"),
        {"b": buyer["user_id"], "l": lid},
    ).fetchone()[0]
    assert kept == first_id
    assert audit_count(session, buyer["user_id"], lid) == 2


def test_buyer_cannot_access_owner_endpoints(client):
    c, _ = client
    assert c.get("/owner/leads").status_code == 401
    buyer = fresh_buyer(c)
    h = auth_header(buyer["access_token"])
    assert c.get("/owner/leads", headers=h).status_code == 403
    assert c.get("/owner/leads?property_listing_id=1", headers=h).status_code == 403


def test_owner_isolation_and_foreign_property_404(client):
    c, session = client
    lid_a, _, _ = seed_owner_listing(session)
    lid_b, _, _ = seed_owner_listing(session)
    buyer_a = fresh_buyer(c)
    buyer_b = fresh_buyer(c)
    ha = auth_header(buyer_a["access_token"])
    hb = auth_header(buyer_b["access_token"])
    assert c.post(f"/buyer/listings/{lid_a}/contact", headers=ha).status_code == 200
    assert c.post(f"/buyer/listings/{lid_b}/contact", headers=hb).status_code == 200

    # Resolve owner identities from the seeded rows (no privileged lookup).
    owner_a = session.execute(
        text("SELECT owner_user_id FROM enquiries WHERE property_listing_id=:l"),
        {"l": lid_a},
    ).fetchone()[0]
    owner_b = session.execute(
        text("SELECT owner_user_id FROM enquiries WHERE property_listing_id=:l"),
        {"l": lid_b},
    ).fetchone()[0]
    assert owner_a != owner_b
    phone_a = session.execute(
        text("SELECT provider_identifier FROM auth_identities WHERE user_id=:u AND provider='phone'"),
        {"u": owner_a},
    ).fetchone()[0]
    phone_b = session.execute(
        text("SELECT provider_identifier FROM auth_identities WHERE user_id=:u AND provider='phone'"),
        {"u": owner_b},
    ).fetchone()[0]
    hoa, _ = owner_header(c, phone_a)
    hob, _ = owner_header(c, phone_b)

    mine_a = c.get("/owner/leads", headers=hoa).json()
    assert mine_a["total"] == 1
    assert mine_a["items"][0]["property_listing_id"] == lid_a
    assert mine_a["items"][0]["buyer_user_id"] == buyer_a["user_id"]
    mine_b = c.get("/owner/leads", headers=hob).json()
    assert mine_b["total"] == 1
    assert mine_b["items"][0]["property_listing_id"] == lid_b

    # Foreign property filter: 404, never an empty list.
    r = c.get(f"/owner/leads?property_listing_id={lid_b}", headers=hoa)
    assert r.status_code == 404, r.text
    assert r.json() == {"detail": "Not found"}
    r = c.get("/owner/leads?property_listing_id=999999999", headers=hoa)
    assert r.status_code == 404


def test_leads_isolated_by_selected_property(client):
    c, session = client
    lid1, owner, owner_phone = seed_owner_listing(session)
    # Second listing under the SAME owner (same physical owner authority).
    pc = make_postcode(session, "-ol-second")
    session.execute(
        text("INSERT INTO physical_properties (postcode_id, user_id) VALUES (:p, :u)"),
        {"p": pc, "u": owner},
    )
    pp = session.execute(text("SELECT LAST_INSERT_ID()")).fetchone()[0]
    session.execute(
        text(
            "INSERT INTO property_listings (physical_property_id, user_id, title, "
            "price_paise, price_period, verification_status, listing_status, transaction_type, "
            "property_type) VALUES (:pp, :u, 'OL2', 200, 'TOTAL', 'VERIFIED', 'AVAILABLE', 'BUY', 'APARTMENT')"
        ),
        {"pp": pp, "u": owner},
    )
    session.flush()
    lid2 = session.execute(text("SELECT LAST_INSERT_ID()")).fetchone()[0]

    buyer = fresh_buyer(c)
    h = auth_header(buyer["access_token"])
    assert c.post(f"/buyer/listings/{lid1}/contact", headers=h).status_code == 200
    assert c.post(f"/buyer/listings/{lid2}/contact", headers=h).status_code == 200
    ho, _ = owner_header(c, owner_phone)

    one = c.get(f"/owner/leads?property_listing_id={lid1}", headers=ho).json()
    assert one["total"] == 1
    assert all(i["property_listing_id"] == lid1 for i in one["items"])
    two = c.get(f"/owner/leads?property_listing_id={lid2}", headers=ho).json()
    assert two["total"] == 1
    assert all(i["property_listing_id"] == lid2 for i in two["items"])
    both = c.get("/owner/leads?page_size=100", headers=ho).json()
    ids = {i["property_listing_id"] for i in both["items"]}
    assert {lid1, lid2} <= ids


def test_failed_reveal_leaks_nothing_and_creates_no_lead(client):
    c, session = client
    bad, _, _ = seed_owner_listing(session, verification="PENDING")
    buyer = fresh_buyer(c)
    h = auth_header(buyer["access_token"])
    before_leads = enquiry_count(session, buyer["user_id"], bad)
    r = c.post(f"/buyer/listings/{bad}/contact", headers=h)
    assert r.status_code == 404
    assert r.json() == {"detail": "Not found"}
    assert "phone" not in r.text
    assert enquiry_count(session, buyer["user_id"], bad) == before_leads
    assert audit_count(session, buyer["user_id"], bad) == 0


def test_status_filter_and_lead_shape_has_no_phone(client):
    c, session = client
    lid, _, _ = seed_owner_listing(session)
    buyer = fresh_buyer(c)
    h = auth_header(buyer["access_token"])
    assert c.post(f"/buyer/listings/{lid}/contact", headers=h).status_code == 200
    owner = session.execute(
        text("SELECT owner_user_id FROM enquiries WHERE property_listing_id=:l"),
        {"l": lid},
    ).fetchone()[0]
    phone = session.execute(
        text("SELECT provider_identifier FROM auth_identities WHERE user_id=:u AND provider='phone'"),
        {"u": owner},
    ).fetchone()[0]
    ho, _ = owner_header(c, phone)

    body = c.get("/owner/leads", headers=ho).json()
    assert body["total"] >= 1
    item = [i for i in body["items"] if i["property_listing_id"] == lid][0]
    assert set(item.keys()) == {
        "id", "property_listing_id", "buyer_user_id", "buyer_display_name", "listing_removed", "created_at", "message",
        "message_count", "last_message", "last_message_at",
        "status", "closing_reason", "listing",
    }
    import json as _json

    assert "phone" not in _json.dumps(body)
    assert phone not in _json.dumps(body)
    assert item["status"] == "NEW"
    assert item["listing"]["title"] == "OL1"

    assert c.get("/owner/leads?status=NEW", headers=ho).json()["total"] >= 1
    assert c.get("/owner/leads?status=CLOSED", headers=ho).json()["total"] == 0
    assert c.get("/owner/leads?status=BOGUS", headers=ho).status_code == 422


def _setup_lead(c, session):
    """One owner listing + one buyer reveal. Returns (lid, owner_h, buyer_h, lead_id)."""
    lid, _, owner_phone = seed_owner_listing(session)
    buyer = fresh_buyer(c)
    bh = auth_header(buyer["access_token"])
    assert c.post(f"/buyer/listings/{lid}/contact", headers=bh).status_code == 200
    ho, _ = owner_header(c, owner_phone)
    lead_id = c.get("/owner/leads", headers=ho).json()["items"][0]["id"]
    return lid, ho, bh, lead_id


def test_owner_update_persists_across_refetch(client):
    c, session = client
    lid, ho, _, lead_id = _setup_lead(c, session)
    r = c.patch(f"/owner/leads/{lead_id}", json={"status": "CONTACTED"}, headers=ho)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "CONTACTED" and body["id"] == lead_id
    again = c.get("/owner/leads", headers=ho).json()
    mine = [i for i in again["items"] if i["id"] == lead_id][0]
    assert mine["status"] == "CONTACTED"


def test_update_auth_and_foreign_lead(client):
    c, session = client
    _, ho, _, lead_id = _setup_lead(c, session)
    assert c.patch(f"/owner/leads/{lead_id}", json={"status": "CONTACTED"}).status_code == 401
    buyer = fresh_buyer(c)
    bh = auth_header(buyer["access_token"])
    assert c.patch(f"/owner/leads/{lead_id}", json={"status": "CONTACTED"}, headers=bh).status_code == 403
    lid2, _, other_phone = seed_owner_listing(session)
    ho2, _ = owner_header(c, other_phone)
    assert lid2 != lead_id
    r = c.patch(f"/owner/leads/{lead_id}", json={"status": "CONTACTED"}, headers=ho2)
    assert r.status_code == 404, r.text
    assert r.json() == {"detail": "Not found"}
    assert c.patch("/owner/leads/999999999", json={"status": "CONTACTED"}, headers=ho).status_code == 404


def test_update_rejects_unsupported_status_without_partial_change(client):
    c, session = client
    _, ho, _, lead_id = _setup_lead(c, session)
    r = c.patch(f"/owner/leads/{lead_id}", json={"status": "VISIT_SCHEDULED"}, headers=ho)
    assert r.status_code == 422, r.text
    r = c.patch(f"/owner/leads/{lead_id}", json={"status": "LOST"}, headers=ho)
    assert r.status_code == 422, r.text
    mine = c.get("/owner/leads", headers=ho).json()
    row = [i for i in mine["items"] if i["id"] == lead_id][0]
    assert row["status"] == "NEW"


def test_not_converted_persists_closed_with_reason_and_keeps_listing(client):
    c, session = client
    lid, ho, _, lead_id = _setup_lead(c, session)
    before = session.execute(
        text("SELECT listing_status FROM property_listings WHERE id=:l"), {"l": lid}
    ).fetchone()[0]
    r = c.patch(
        f"/owner/leads/{lead_id}",
        json={"status": "CLOSED", "closing_reason": "Not Converted"},
        headers=ho,
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "CLOSED"
    assert r.json()["closing_reason"] == "Not Converted"
    after = session.execute(
        text("SELECT listing_status FROM property_listings WHERE id=:l"), {"l": lid}
    ).fetchone()[0]
    assert after == before
    import json as _json

    assert "phone" not in _json.dumps(r.json())


def test_buyer_can_reenquire_after_close(client):
    c, session = client
    lid, ho, bh, lead_id = _setup_lead(c, session)
    assert c.patch(f"/owner/leads/{lead_id}", json={"status": "CLOSED", "closing_reason": "Not Converted"}, headers=ho).status_code == 200
    r = c.post("/buyer/enquiries", json={"property_listing_id": lid}, headers=bh)
    assert r.status_code == 201, r.text
    assert r.json()["id"] != lead_id
    assert r.json()["status"] == "NEW"


def test_b2_reveal_then_enquiry_single_owner_visible_lead(client):
    # B2: reveal creates the lead; a later Send Enquiry reuses it (no
    # duplicate active lead); only the correct owner sees it; failures
    # create nothing.
    c, session = client
    lid, owner, owner_phone = seed_owner_listing(session)
    other_lid, _, other_phone = seed_owner_listing(session)
    buyer = fresh_buyer(c)
    bh = auth_header(buyer["access_token"])
    ho, _ = owner_header(c, owner_phone)
    other_ho, _ = owner_header(c, other_phone)

    assert c.post(f"/buyer/listings/{lid}/contact", headers=bh).status_code == 200
    r = c.post("/buyer/enquiries", json={"property_listing_id": lid}, headers=bh)
    assert r.status_code == 200, r.text  # idempotent reuse, not a second row
    assert enquiry_count(session, buyer["user_id"], lid) == 1

    mine = c.get("/owner/leads", headers=ho).json()
    assert [i["property_listing_id"] for i in mine["items"]] == [lid]
    assert c.get("/owner/leads", headers=other_ho).json()["total"] == 0

    # Failed submission (invisible listing) creates no partial records.
    bad, _, _ = seed_owner_listing(session, verification="PENDING")
    before = enquiry_count(session, buyer["user_id"], bad)
    assert c.post("/buyer/enquiries", json={"property_listing_id": bad}, headers=bh).status_code == 404
    assert enquiry_count(session, buyer["user_id"], bad) == before
    assert audit_count(session, buyer["user_id"], bad) == 0
