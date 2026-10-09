"""Phase 6B-4: POST /buyer/listings/{id}/contact (rollback-isolated)."""
from sqlalchemy import text

from tests.conftest import auth_header, make_postcode, register_phone

OWNER_PHONE = "+16100000001"

_owner_n = {"i": 0}


def seed_owner_listing(session, verification="VERIFIED", status="AVAILABLE", broker_listed=False):
    """Owner (with phone identity) + listing. broker_listed=True puts a different
    user_id on the listing row to prove owner resolution ignores it."""
    _owner_n["i"] += 1
    owner_phone = f"+1610000{_owner_n['i']:04d}"
    pc = make_postcode(session, f"-c4-{_owner_n['i']}")
    session.execute(text("INSERT INTO users (display_name) VALUES ('Owner')"))
    owner = session.execute(text("SELECT LAST_INSERT_ID()")).fetchone()[0]
    session.execute(
        text("INSERT INTO auth_identities (user_id, provider, provider_identifier) "
             "VALUES (:u, 'phone', :p)"),
        {"u": owner, "p": owner_phone},
    )
    lister = owner
    if broker_listed:
        session.execute(text("INSERT INTO users (display_name) VALUES ('Broker')"))
        lister = session.execute(text("SELECT LAST_INSERT_ID()")).fetchone()[0]
    session.execute(
        text("INSERT INTO physical_properties (postcode_id, user_id) VALUES (:p, :u)"),
        {"p": pc, "u": owner},
    )
    pp = session.execute(text("SELECT LAST_INSERT_ID()")).fetchone()[0]
    session.execute(
        text(
            "INSERT INTO property_listings (physical_property_id, user_id, title, "
            "price_paise, price_period, verification_status, listing_status, transaction_type, "
            "property_type) VALUES (:pp, :u, 'C1', 100, 'TOTAL', :v, :s, 'BUY', 'APARTMENT')"
        ),
        {"pp": pp, "u": lister, "v": verification, "s": status},
    )
    session.flush()
    lid = session.execute(text("SELECT LAST_INSERT_ID()")).fetchone()[0]
    return lid, owner, owner_phone


def audit_count(session):
    return session.execute(text("SELECT COUNT(*) FROM contact_reveal_audits")).fetchone()[0]


def test_buyer_reveal_and_audit(client):
    c, session = client
    lid, _owner, owner_phone = seed_owner_listing(session)
    tok = register_phone(c, "+16200000002")
    r = c.post(f"/buyer/listings/{lid}/contact", headers=auth_header(tok["access_token"]))
    assert r.status_code == 200, r.text
    assert r.json() == {"phone": owner_phone}
    row = session.execute(
        text("SELECT user_id, property_listing_id FROM contact_reveal_audits")
    ).fetchone()
    assert (row[0], row[1]) == (tok["user_id"], lid)


def test_auth_and_visibility_gates(client):
    c, session = client
    lid, _, _ = seed_owner_listing(session)
    assert c.post(f"/buyer/listings/{lid}/contact").status_code == 401
    tok = register_phone(c, "+16200000003")
    h = auth_header(tok["access_token"])
    c.post("/users/me/role", json={"role": "OWNER"}, headers=h)
    assert c.post(f"/buyer/listings/{lid}/contact", headers=h).status_code == 403
    before = audit_count(session)
    for kwargs in ({"verification": "PENDING"}, {"status": "SOLD"}, {"status": "RENTED"}, {"status": "LEASED"}):
        bad, _, _ = seed_owner_listing(session, **kwargs)
        buyer = register_phone(c, f"+1620000{100 + before % 800:04d}")
        r = c.post(f"/buyer/listings/{bad}/contact", headers=auth_header(buyer["access_token"]))
        assert r.status_code == 404, kwargs
        assert r.json() == {"detail": "Not found"}
    assert c.post("/buyer/listings/999999999/contact", headers=h).status_code == 403  # role first
    fresh = register_phone(c, "+16200000999")
    assert c.post("/buyer/listings/999999999/contact", headers=auth_header(fresh["access_token"])).status_code == 404
    assert audit_count(session) == before  # failures leave no audit rows


def test_owner_resolution_ignores_listing_user_and_repeat_audited(client):
    c, session = client
    lid, _owner, owner_phone = seed_owner_listing(session, broker_listed=True)
    tok = register_phone(c, "+16200000004")
    h = auth_header(tok["access_token"])
    assert c.post(f"/buyer/listings/{lid}/contact", headers=h).json()["phone"] == owner_phone
    assert c.post(f"/buyer/listings/{lid}/contact", headers=h).json()["phone"] == owner_phone
    n = session.execute(
        text("SELECT COUNT(*) FROM contact_reveal_audits WHERE user_id=:u AND property_listing_id=:l"),
        {"u": tok["user_id"], "l": lid},
    ).fetchone()[0]
    assert n == 2  # repeats allowed, each audited
    # Public endpoints never leak phones.
    assert "1610000" not in str(c.get("/buyer/listings").json())
    assert "1610000" not in str(c.get(f"/buyer/listings/{lid}").json())
