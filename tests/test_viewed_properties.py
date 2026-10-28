"""Viewed-property history: record, repeat-view, retention, isolation.

All rows live in the rollback-isolated client session.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from tests.conftest import auth_header, make_postcode, register_phone

_view_n = {"i": 0}


def _buyer_view_header(c, phone):
    tok = register_phone(c, phone)
    h = auth_header(tok["access_token"])
    return h, tok["user_id"]


def _seed_listing(session, title="Viewed Listing", status="AVAILABLE",
                  verification="VERIFIED"):
    """Seed a verified/available listing owned by a created user. Returns lid."""
    _view_n["i"] += 1
    pc = make_postcode(session, f"-vl-{_view_n['i']}")
    session.execute(text("INSERT INTO users (display_name) VALUES ('Owner')"))
    owner = session.execute(text("SELECT LAST_INSERT_ID()")).fetchone()[0]
    session.execute(
        text("INSERT INTO auth_identities (user_id, provider, provider_identifier) "
             "VALUES (:u, 'phone', :p)"),
        {"u": owner, "p": f"+91900{owner:07d}"},
    )
    session.execute(
        text("INSERT INTO physical_properties (postcode_id, user_id) VALUES (:p, :u)"),
        {"p": pc, "u": owner},
    )
    pp = session.execute(text("SELECT LAST_INSERT_ID()")).fetchone()[0]
    session.execute(
        text(
            "INSERT INTO property_listings "
            "(physical_property_id, user_id, title, price_paise, price_period, "
            "verification_status, listing_status, transaction_type, property_type) "
            "VALUES (:pp, :u, :t, 100, 'TOTAL', :v, :s, 'BUY', 'APARTMENT')"
        ),
        {"pp": pp, "u": owner, "t": title, "v": verification, "s": status},
    )
    session.flush()
    return session.execute(text("SELECT LAST_INSERT_ID()")).fetchone()[0]


def _view_count(session, buyer_id, listing_id):
    return session.execute(
        text("SELECT COUNT(*) FROM viewed_properties WHERE buyer_user_id=:b AND property_listing_id=:l"),
        {"b": buyer_id, "l": listing_id},
    ).fetchone()[0]


def _viewed_at(session, buyer_id, listing_id):
    return session.execute(
        text("SELECT viewed_at FROM viewed_properties WHERE buyer_user_id=:b AND property_listing_id=:l"),
        {"b": buyer_id, "l": listing_id},
    ).fetchone()


def test_record_view_first_time(client):
    c, session = client
    lid = _seed_listing(session)
    h, buyer_id = _buyer_view_header(c, "+919100100001")

    r = c.post(f"/buyer/viewed/{lid}", headers=h)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["property_listing_id"] == lid
    assert body["buyer_user_id"] == buyer_id
    assert "viewed_at" in body
    assert body["listing"]["title"] == "Viewed Listing"

    # Persisted exactly once
    assert _view_count(session, buyer_id, lid) == 1


def test_repeat_view_updates_timestamp_no_duplicate(client):
    c, session = client
    lid = _seed_listing(session)
    h, buyer_id = _buyer_view_header(c, "+919100100002")

    r1 = c.post(f"/buyer/viewed/{lid}", headers=h)
    assert r1.status_code == 201
    v1 = _viewed_at(session, buyer_id, lid)

    # Second view shortly after
    r2 = c.post(f"/buyer/viewed/{lid}", headers=h)
    assert r2.status_code == 201, r2.text
    v2 = _viewed_at(session, buyer_id, lid)

    # Still exactly one row
    assert _view_count(session, buyer_id, lid) == 1
    # Timestamp refreshed (same second is acceptable)
    assert v2 >= v1


def test_viewed_most_recent_first(client):
    c, session = client
    lids = [_seed_listing(session, title=f"Listing {i}") for i in range(3)]
    h, buyer_id = _buyer_view_header(c, "+919100100003")

    # View in order L0, L1, L2 -> most recent first should be L2, L1, L0
    for lid in lids:
        c.post(f"/buyer/viewed/{lid}", headers=h)

    r = c.get("/buyer/viewed", headers=h)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["total"] == 3
    ids = [it["property_listing_id"] for it in data["items"]]
    assert ids == [lids[2], lids[1], lids[0]]


def test_retention_max_20_per_buyer(client):
    c, session = client
    h, buyer_id = _buyer_view_header(c, "+919100100004")

    # View 25 distinct listings. viewed_at has whole-second resolution, so
    # these usually tie; retention must still keep exactly 20 (ties are
    # broken by row id — see the tied-timestamp tests below).
    lids = [_seed_listing(session, title=f"Listing {i}") for i in range(25)]
    for lid in lids:
        assert c.post(f"/buyer/viewed/{lid}", headers=h).status_code == 201

    r = c.get("/buyer/viewed", headers=h)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["total"] == 20
    assert len(data["items"]) == 20

    # Verify DB directly: exactly 20 rows for this buyer
    count = session.execute(
        text("SELECT COUNT(*) FROM viewed_properties WHERE buyer_user_id=:b"),
        {"b": buyer_id},
    ).fetchone()[0]
    assert count == 20


def test_isolation_between_buyers(client):
    c, session = client
    lid1 = _seed_listing(session, title="Buyer A Listing")
    lid2 = _seed_listing(session, title="Buyer B Listing")
    h_a, buyer_a = _buyer_view_header(c, "+919100100005")
    h_b, buyer_b = _buyer_view_header(c, "+919100100006")

    # Buyer A views lid1, Buyer B views lid2
    c.post(f"/buyer/viewed/{lid1}", headers=h_a)
    c.post(f"/buyer/viewed/{lid2}", headers=h_b)

    # Buyer A sees only lid1
    r_a = c.get("/buyer/viewed", headers=h_a)
    assert r_a.status_code == 200
    ids_a = {it["property_listing_id"] for it in r_a.json()["items"]}
    assert ids_a == {lid1}

    # Buyer B sees only lid2
    r_b = c.get("/buyer/viewed", headers=h_b)
    assert r_b.status_code == 200
    ids_b = {it["property_listing_id"] for it in r_b.json()["items"]}
    assert ids_b == {lid2}

    # DB-level isolation
    count_a = session.execute(
        text("SELECT COUNT(*) FROM viewed_properties WHERE buyer_user_id=:b"),
        {"b": buyer_a},
    ).fetchone()[0]
    count_b = session.execute(
        text("SELECT COUNT(*) FROM viewed_properties WHERE buyer_user_id=:b"),
        {"b": buyer_b},
    ).fetchone()[0]
    assert count_a == 1
    assert count_b == 1


def test_unauthenticated_viewed_rejected(client):
    c, session = client
    lid = _seed_listing(session)
    # No auth header - request directly without authentication
    r = c.post(f"/buyer/viewed/{lid}")
    assert r.status_code == 401, r.text

    r = c.get("/buyer/viewed")
    assert r.status_code == 401, r.text


def test_wrong_role_cannot_view(client):
    c, session = client
    lid = _seed_listing(session)
    # Register as buyer then switch to OWNER role
    tok = register_phone(c, "+919100100007")
    h = auth_header(tok["access_token"])
    c.post("/users/me/role", json={"role": "OWNER"}, headers=h)

    r = c.post(f"/buyer/viewed/{lid}", headers=h)
    assert r.status_code == 403, r.text

    r = c.get("/buyer/viewed", headers=h)
    assert r.status_code == 403, r.text


def test_view_nonexistent_listing_404(client):
    c, session = client
    h, buyer_id = _buyer_view_header(c, "+919100100008")
    r = c.post("/buyer/viewed/99999999", headers=h)
    assert r.status_code == 404, r.text


def test_view_unavailable_listing_404(client):
    c, session = client
    lid = _seed_listing(session, status="CLOSED")
    h, buyer_id = _buyer_view_header(c, "+919100100009")
    r = c.post(f"/buyer/viewed/{lid}", headers=h)
    assert r.status_code == 404, r.text


def test_view_unverified_listing_404(client):
    c, session = client
    lid = _seed_listing(session, verification="REJECTED")
    h, buyer_id = _buyer_view_header(c, "+919100100010")
    r = c.post(f"/buyer/viewed/{lid}", headers=h)
    assert r.status_code == 404, r.text


# --- Deterministic retention with tied timestamps + response contract ------------

TIED = "2001-01-01 00:00:00"  # one shared (past) viewed_at for every seeded row


def _seed_tied_views(session, buyer_id, n):
    """n viewed rows for buyer_id, all with the SAME viewed_at. Returns
    [(viewed_id, listing_id)] in insertion (id) order."""
    out = []
    for i in range(n):
        lid = _seed_listing(session, title=f"Tied {i}")
        session.execute(
            text("INSERT INTO viewed_properties (buyer_user_id, property_listing_id, viewed_at) "
                 "VALUES (:b, :l, :t)"),
            {"b": buyer_id, "l": lid, "t": TIED},
        )
        vid = session.execute(text("SELECT LAST_INSERT_ID()")).fetchone()[0]
        out.append((vid, lid))
    session.flush()
    return out


def _kept_listing_ids(session, buyer_id):
    return {r[0] for r in session.execute(
        text("SELECT property_listing_id FROM viewed_properties WHERE buyer_user_id=:b"), {"b": buyer_id}
    ).fetchall()}


def test_retention_with_tied_timestamps_is_deterministic(client):
    c, session = client
    h, buyer_id = _buyer_view_header(c, "+919100100101")
    tied = _seed_tied_views(session, buyer_id, 25)
    new_lid = _seed_listing(session, title="Newest")

    r = c.post(f"/buyer/viewed/{new_lid}", headers=h)
    assert r.status_code == 201, r.text  # previously could 500 (row trimmed mid-request)
    # Kept: the new view + the 19 newest tied rows by id (deterministic).
    expected = {new_lid} | {lid for _, lid in tied[-19:]}
    assert _kept_listing_ids(session, buyer_id) == expected

    items = c.get("/buyer/viewed?page_size=100", headers=h).json()["items"]
    assert [i["property_listing_id"] for i in items] == [new_lid] + [lid for _, lid in reversed(tied[-19:])]


def test_tied_rows_page_in_a_stable_order(client):
    c, session = client
    h, buyer_id = _buyer_view_header(c, "+919100100102")
    tied = _seed_tied_views(session, buyer_id, 6)
    first = c.get("/buyer/viewed?page=1&page_size=3", headers=h).json()["items"]
    second = c.get("/buyer/viewed?page=2&page_size=3", headers=h).json()["items"]
    order = [i["property_listing_id"] for i in first + second]
    assert order == [lid for _, lid in reversed(tied)]  # no duplicates or gaps across pages


def test_repeat_view_of_oldest_tied_row_is_kept_and_moves_first(client):
    c, session = client
    h, buyer_id = _buyer_view_header(c, "+919100100103")
    tied = _seed_tied_views(session, buyer_id, 20)
    oldest_lid = tied[0][1]

    r = c.post(f"/buyer/viewed/{oldest_lid}", headers=h)
    assert r.status_code == 201, r.text
    assert len(_kept_listing_ids(session, buyer_id)) == 20  # nothing trimmed, no duplicate
    assert _view_count(session, buyer_id, oldest_lid) == 1
    first = c.get("/buyer/viewed", headers=h).json()["items"][0]
    assert first["property_listing_id"] == oldest_lid


def test_retention_never_touches_other_buyers(client):
    c, session = client
    ha, buyer_a = _buyer_view_header(c, "+919100100104")
    _, buyer_b = _buyer_view_header(c, "+919100100105")
    b_rows = _seed_tied_views(session, buyer_b, 20)
    a_rows = _seed_tied_views(session, buyer_a, 20)
    assert c.post(f"/buyer/viewed/{_seed_listing(session)}", headers=ha).status_code == 201
    assert len(_kept_listing_ids(session, buyer_a)) == 20
    assert _kept_listing_ids(session, buyer_b) == {lid for _, lid in b_rows}
    assert a_rows[0][1] not in _kept_listing_ids(session, buyer_a)  # A's own oldest trimmed


def test_response_carries_the_listing_card_and_utc_time(client):
    c, session = client
    lid = _seed_listing(session, title="Card Contract")
    h, _ = _buyer_view_header(c, "+919100100106")
    before = datetime.now(timezone.utc).replace(microsecond=0)
    posted = c.post(f"/buyer/viewed/{lid}", headers=h).json()
    after = datetime.now(timezone.utc)
    listed = c.get("/buyer/viewed", headers=h).json()["items"][0]

    for item in (posted, listed):
        card = item["listing"]
        assert card["listing_id"] == lid == item["property_listing_id"]
        assert card["title"] == "Card Contract"
        assert {"price_display", "cover_image_url", "locality", "city"} <= set(card)
        assert "phone" not in str(item)
        assert item["viewed_at"].endswith("Z")
    stamp = datetime.fromisoformat(posted["viewed_at"].replace("Z", "+00:00"))
    # App-side UTC (the local MySQL clock is IST): within this request window.
    assert before <= stamp <= after
    assert listed["viewed_at"] == posted["viewed_at"]


def test_listing_that_loses_visibility_is_omitted_not_leaked(client):
    c, session = client
    keep, gone = _seed_listing(session, title="Still listed"), _seed_listing(session, title="Sold since")
    h, buyer_id = _buyer_view_header(c, "+919100100107")
    assert c.post(f"/buyer/viewed/{gone}", headers=h).status_code == 201
    assert c.post(f"/buyer/viewed/{keep}", headers=h).status_code == 201
    session.execute(text("UPDATE property_listings SET listing_status='SOLD' WHERE id=:i"), {"i": gone})
    body = c.get("/buyer/viewed", headers=h).json()
    assert [i["property_listing_id"] for i in body["items"]] == [keep]
    assert body["total"] == 1
    assert _view_count(session, buyer_id, gone) == 1  # history row kept, just not exposed


# --- Migrated schema matches the model (DB level) ---------------------------------

def test_migrated_table_enforces_uniqueness_and_cascades(client):
    c, session = client
    lid = _seed_listing(session, title="Cascade")
    h, buyer_id = _buyer_view_header(c, "+919100100108")
    assert c.post(f"/buyer/viewed/{lid}", headers=h).status_code == 201
    rules = {r[0]: (r[1], r[2]) for r in session.execute(text(
        "SELECT kcu.REFERENCED_TABLE_NAME, rc.DELETE_RULE, rc.UPDATE_RULE "
        "FROM information_schema.REFERENTIAL_CONSTRAINTS rc "
        "JOIN information_schema.KEY_COLUMN_USAGE kcu "
        "  ON kcu.CONSTRAINT_NAME = rc.CONSTRAINT_NAME AND kcu.CONSTRAINT_SCHEMA = rc.CONSTRAINT_SCHEMA "
        "WHERE rc.CONSTRAINT_SCHEMA = DATABASE() AND rc.TABLE_NAME = 'viewed_properties'"
    )).fetchall()}
    assert rules == {"users": ("CASCADE", "CASCADE"), "property_listings": ("CASCADE", "CASCADE")}
    # The unique pair is enforced by the database, not only by the service.
    import pytest
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.execute(text(
                "INSERT INTO viewed_properties (buyer_user_id, property_listing_id) VALUES (:b, :l)"
            ), {"b": buyer_id, "l": lid})
    # Hard-deleting the listing removes its history rows (ON DELETE CASCADE).
    session.execute(text("DELETE FROM property_listings WHERE id=:i"), {"i": lid})
    assert _view_count(session, buyer_id, lid) == 0
