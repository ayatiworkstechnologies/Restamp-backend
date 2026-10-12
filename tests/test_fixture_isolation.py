"""Regression tests for test-transaction isolation and postcode fixtures.

Order-dependent pair (pytest runs top-to-bottom within a file): test_01
creates rows through real API routes (whose handlers call db.commit);
test_02 proves the fixture teardown rollback removed them. The listing id
is shared via module state so a stale row from an older run can never cause
a false pass or fail. Run this file whole; test_02 alone is meaningless.
"""
import re

from sqlalchemy import text

from tests.conftest import auth_header, make_postcode, register_phone


_probe = {}


def _setup_owner(client, phone: str) -> str:
    token_out = register_phone(client, phone)
    token = token_out["access_token"]
    r = client.post("/users/me/role", json={"role": "OWNER"}, headers=auth_header(token))
    assert r.status_code == 200, r.text
    return token


def test_01_route_commit_visible_within_test(client):
    c, session = client
    token = _setup_owner(c, "+919876543531")
    r = c.post(
        "/owner/listings",
        json={
            "title": "Isolation Probe Flat",
            "transaction_type": "RENT",
            "property_type": "APARTMENT",
            "price_rupees": 25000,
            "price_period": "MONTHLY",
            "locality": "Isolation Probe Nagar",
            "city": "Chennai",
            "pincode": "600040",
        },
        headers=auth_header(token),
    )
    assert r.status_code == 201, r.text
    lid = r.json()["listing_id"]
    _probe["listing_id"] = lid
    # Multi-request flow within the test: a second request sees the row,
    # proving the savepoint-joined session keeps committed work visible.
    r = c.get(f"/owner/listings/{lid}", headers=auth_header(token))
    assert r.status_code == 200, r.text
    assert r.json()["listing_id"] == lid


def test_02_prior_test_rows_rolled_back(client):
    c, session = client
    lid = _probe.get("listing_id")
    assert lid is not None, "test_01 must run first in the same process"
    n = session.execute(
        text("SELECT COUNT(*) FROM property_listings WHERE id=:i"), {"i": lid}
    ).fetchone()[0]
    assert n == 0
    # The test_01 user row is gone too: the same phone registers cleanly.
    token_out = register_phone(c, "+919876543531")
    assert "access_token" in token_out


def test_03_make_postcode_distinct_in_range(client):
    c, session = client
    a = make_postcode(session, "-iso-a")
    b = make_postcode(session, "-iso-b")
    d = make_postcode(session)
    pins = [
        session.execute(
            text("SELECT pincode FROM postcodes WHERE id=:i"), {"i": x}
        ).fetchone()[0]
        for x in (a, b, d)
    ]
    assert len(set(pins)) == 3
    for p in pins:
        assert re.fullmatch(r"\d{6}", p), p
        assert 600001 <= int(p) <= 600118, p
    # Explicit pincode is honored verbatim (caller owns collision
    # responsibility, so probe a free value instead of hardcoding one that
    # history may already occupy).
    wanted = next(
        p
        for p in (f"{600101 + i}" for i in range(18))
        if session.execute(
            text("SELECT id FROM postcodes WHERE pincode=:p"), {"p": p}
        ).fetchone()
        is None
    )
    e = make_postcode(session, "-iso-e", pincode=wanted)
    assert session.execute(
        text("SELECT pincode FROM postcodes WHERE id=:i"), {"i": e}
    ).fetchone()[0] == wanted
