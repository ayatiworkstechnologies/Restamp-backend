"""Agent (role BROKER) foundation: /broker/profile, /dashboard, /locations, /listings.

- 401 without a token; 403 "BROKER role required" for BUYER/OWNER; 403
  "Account disabled" for a suspended agent.
- A self-switched agent has no coverage: zero locations, zero properties,
  even while visible listings exist elsewhere (never "unrestricted").
- Coverage = unexpired broker_postcode_access_current rows; expired grants
  are ignored everywhere.
- /broker/listings = buyer visibility (VERIFIED + AVAILABLE + not deleted)
  inside covered postcodes, with the buyer filters/paging/count_only.
- Agents are isolated from each other; switching role away revokes access
  and switching back restores it (grants are untouched).
- Unsupported metrics are null and named in `unavailable` (no invented data).
- Buyer search is unchanged (no coverage restriction).
Rollback-isolated.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.services import brokers as broker_svc
from app.services import listings as listing_svc
from tests.conftest import auth_header, make_postcode, register_phone

_n = {"i": 0}
PATHS = ("/broker/profile", "/broker/dashboard", "/broker/locations", "/broker/listings")


def _phone():
    _n["i"] += 1
    return f"+916770{_n['i']:06d}"


def _user(c, role):
    tok = register_phone(c, _phone())
    h = auth_header(tok["access_token"])
    if role != "BUYER":
        assert c.post("/users/me/role", json={"role": role}, headers=h).status_code == 200
    return h, tok["user_id"]


def _granter(session):
    session.execute(text("INSERT INTO users (display_name) VALUES ('Coverage Granter')"))
    return session.execute(text("SELECT LAST_INSERT_ID()")).scalar()


def _grant(session, broker_id, postcode_id, granter, expires_at=None):
    session.execute(
        text("INSERT INTO broker_postcode_access_current (broker_user_id, postcode_id, granted_by, expires_at) "
             "VALUES (:b, :p, :g, :e)"),
        {"b": broker_id, "p": postcode_id, "g": granter, "e": expires_at},
    )
    session.flush()


def _listing(session, postcode_id, title, verification="VERIFIED", status="AVAILABLE", deleted=False,
             deal="BUY", bedrooms=None):
    session.execute(text("INSERT INTO users (display_name) VALUES ('Lister')"))
    owner = session.execute(text("SELECT LAST_INSERT_ID()")).scalar()
    session.execute(
        text("INSERT INTO physical_properties (postcode_id, user_id, bedrooms) VALUES (:p, :u, :b)"),
        {"p": postcode_id, "u": owner, "b": bedrooms},
    )
    pp = session.execute(text("SELECT LAST_INSERT_ID()")).scalar()
    session.execute(
        text("INSERT INTO property_listings (physical_property_id, user_id, title, price_paise, price_period, "
             "verification_status, listing_status, transaction_type, property_type) "
             "VALUES (:pp, :u, :t, 500000, 'TOTAL', :v, :s, :d, 'APARTMENT')"),
        {"pp": pp, "u": owner, "t": title, "v": verification, "s": status, "d": deal},
    )
    lid = session.execute(text("SELECT LAST_INSERT_ID()")).scalar()
    if deleted:
        session.execute(text("UPDATE property_listings SET deleted_at=UTC_TIMESTAMP() WHERE id=:l"), {"l": lid})
    session.flush()
    return lid


def _titles(resp):
    return sorted(i["title"] for i in resp.json()["items"])


def test_unauthenticated_is_401(client):
    c, _ = client
    for path in PATHS:
        assert c.get(path).status_code == 401, path


def test_buyer_and_owner_are_refused(client):
    c, _ = client
    for role in ("BUYER", "OWNER"):
        h, _ = _user(c, role)
        for path in PATHS:
            r = c.get(path, headers=h)
            assert r.status_code == 403 and r.json() == {"detail": "BROKER role required"}, (role, path)


def test_suspended_agent_is_refused(client):
    c, session = client
    h, uid = _user(c, "BROKER")
    session.execute(text("UPDATE users SET status='suspended' WHERE id=:u"), {"u": uid})
    session.expire_all()
    for path in PATHS:
        r = c.get(path, headers=h)
        assert r.status_code == 403 and r.json() == {"detail": "Account disabled"}, path


def test_new_agent_has_no_coverage_and_sees_nothing(client):
    c, session = client
    _listing(session, make_postcode(session, "-ag-free"), "Visible Elsewhere")
    h, uid = _user(c, "BROKER")
    profile = c.get("/broker/profile", headers=h).json()
    assert profile == {"user_id": uid, "display_name": profile["display_name"], "role": "BROKER",
                       "covered_location_count": 0, "unavailable": ["verification", "subscription"]}
    dash = c.get("/broker/dashboard", headers=h).json()
    assert dash == {"available_property_count": 0, "covered_location_count": 0, "coverage_limit": None,
                    "subscription": None, "new_lead_count": None, "earnings": None,
                    "unavailable": ["coverage_limit", "subscription", "new_leads", "earnings"]}
    assert c.get("/broker/locations", headers=h).json() == {"items": [], "total": 0}
    r = c.get("/broker/listings", headers=h)
    assert r.status_code == 200 and r.json()["items"] == [] and r.json()["total"] == 0
    assert c.get("/broker/listings?count_only=true", headers=h).json()["total"] == 0


def test_locations_list_own_unexpired_grants(client):
    c, session = client
    h, uid = _user(c, "BROKER")
    g = _granter(session)
    p1 = make_postcode(session, "-ag-l1")
    p2 = make_postcode(session, "-ag-l2")
    p_expired = make_postcode(session, "-ag-l3")
    pin = {pid: session.execute(text("SELECT pincode FROM postcodes WHERE id=:p"), {"p": pid}).scalar()
           for pid in (p1, p2)}
    future = (datetime.now(timezone.utc) + timedelta(days=30)).replace(tzinfo=None, microsecond=0)
    past = (datetime.now(timezone.utc) - timedelta(days=1)).replace(tzinfo=None, microsecond=0)
    _grant(session, uid, p1, g)
    _grant(session, uid, p2, g, expires_at=future)
    _grant(session, uid, p_expired, g, expires_at=past)
    body = c.get("/broker/locations", headers=h).json()
    assert body["total"] == 2
    assert [i["pincode"] for i in body["items"]] == sorted(pin.values())  # ordered by pincode
    by_id = {i["postcode_id"]: i for i in body["items"]}
    assert set(by_id) == {p1, p2}  # expired grant excluded
    assert by_id[p2]["pincode"] == pin[p2] and by_id[p2]["locality"] == "L1-ag-l2" and by_id[p2]["city"] == "Testville-ag-l2"
    assert datetime.fromisoformat(by_id[p2]["expires_at"].replace("Z", "+00:00")) == future.replace(tzinfo=timezone.utc)
    assert by_id[p1]["expires_at"] is None
    assert set(by_id[p1]) == {"postcode_id", "pincode", "locality", "city", "expires_at"}  # no grantor id
    assert c.get("/broker/profile", headers=h).json()["covered_location_count"] == 2
    assert c.get("/broker/dashboard", headers=h).json()["covered_location_count"] == 2


def test_listings_follow_buyer_visibility_inside_coverage(client):
    c, session = client
    h, uid = _user(c, "BROKER")
    g = _granter(session)
    covered = make_postcode(session, "-ag-cov")
    expired = make_postcode(session, "-ag-exp")
    outside = make_postcode(session, "-ag-out")
    _grant(session, uid, covered, g)
    _grant(session, uid, expired, g,
           expires_at=(datetime.now(timezone.utc) - timedelta(minutes=5)).replace(tzinfo=None))
    _listing(session, covered, "Covered Visible", deal="RENT", bedrooms=2)
    _listing(session, covered, "Covered Visible Two", bedrooms=3)
    _listing(session, covered, "Covered Pending", verification="PENDING")
    _listing(session, covered, "Covered Rejected", verification="REJECTED")
    _listing(session, covered, "Covered Closed", status="CLOSED")
    _listing(session, covered, "Covered Deleted", deleted=True)
    _listing(session, expired, "Expired Grant Visible")
    _listing(session, outside, "Outside Visible")

    r = c.get("/broker/listings", headers=h)
    assert r.status_code == 200
    assert _titles(r) == ["Covered Visible", "Covered Visible Two"]
    assert r.json()["total"] == 2 and r.json()["page"] == 1 and r.json()["page_size"] == 20
    assert set(r.json()["items"][0]) == set(c.get("/buyer/listings").json()["items"][0])  # same card shape
    assert c.get("/broker/dashboard", headers=h).json()["available_property_count"] == 2
    # Buyer filters, paging and count_only behave as on /buyer/listings.
    assert _titles(c.get("/broker/listings?deal=RENT", headers=h)) == ["Covered Visible"]
    assert _titles(c.get("/broker/listings?bedrooms_min=3", headers=h)) == ["Covered Visible Two"]
    assert _titles(c.get("/broker/listings?q=Two", headers=h)) == ["Covered Visible Two"]
    assert _titles(c.get("/broker/listings?q=Outside", headers=h)) == []
    paged = c.get("/broker/listings?page=2&page_size=1", headers=h).json()
    assert len(paged["items"]) == 1 and paged["total"] == 2
    assert c.get("/broker/listings?count_only=true", headers=h).json() == {"items": [], "page": 1, "page_size": 20, "total": 2}
    assert c.get("/broker/listings?min_price_paise=10&max_price_paise=5", headers=h).status_code == 422
    assert c.get("/broker/listings?page_size=101", headers=h).status_code == 422


def test_grant_expiring_exactly_now_is_inactive(client, monkeypatch):
    """Boundary: active means expires_at > now (strict). The service clock is
    pinned, so this is deterministic (no sleeps, no real-time dependence)."""
    c, session = client
    now = datetime(2031, 6, 1, 12, 0, 0)
    monkeypatch.setattr(broker_svc, "_utcnow_naive", lambda: now)
    h, uid = _user(c, "BROKER")
    g = _granter(session)
    p_now = make_postcode(session, "-ag-b0")
    p_later = make_postcode(session, "-ag-b1")
    _grant(session, uid, p_now, g, expires_at=now)
    _grant(session, uid, p_later, g, expires_at=now + timedelta(seconds=1))
    _listing(session, p_now, "Boundary Expired")
    _listing(session, p_later, "Boundary Active")
    assert [i["postcode_id"] for i in c.get("/broker/locations", headers=h).json()["items"]] == [p_later]
    assert _titles(c.get("/broker/listings", headers=h)) == ["Boundary Active"]
    assert c.get("/broker/dashboard", headers=h).json()["covered_location_count"] == 1
    assert c.get("/broker/profile", headers=h).json()["covered_location_count"] == 1


def test_agents_are_isolated_from_each_other(client):
    c, session = client
    ha, a = _user(c, "BROKER")
    hb, b = _user(c, "BROKER")
    g = _granter(session)
    pa = make_postcode(session, "-ag-a")
    pb = make_postcode(session, "-ag-b")
    _grant(session, a, pa, g)
    _grant(session, b, pb, g)
    _listing(session, pa, "Agent A Area")
    _listing(session, pb, "Agent B Area")
    assert _titles(c.get("/broker/listings", headers=ha)) == ["Agent A Area"]
    assert _titles(c.get("/broker/listings", headers=hb)) == ["Agent B Area"]
    assert [i["postcode_id"] for i in c.get("/broker/locations", headers=ha).json()["items"]] == [pa]
    assert [i["postcode_id"] for i in c.get("/broker/locations", headers=hb).json()["items"]] == [pb]


def test_role_switch_revokes_and_restores_access(client):
    c, session = client
    h, uid = _user(c, "BROKER")
    p = make_postcode(session, "-ag-sw")
    _grant(session, uid, p, _granter(session))
    _listing(session, p, "Switch Area")
    assert c.post("/users/me/role", json={"role": "BUYER"}, headers=h).status_code == 200
    assert c.get("/broker/listings", headers=h).status_code == 403
    assert c.post("/users/me/role", json={"role": "BROKER"}, headers=h).status_code == 200
    assert _titles(c.get("/broker/listings", headers=h)) == ["Switch Area"]


def test_broker_routes_are_read_only(client):
    c, _ = client
    h, _ = _user(c, "BROKER")
    for path in PATHS:
        for method in ("POST", "PATCH", "PUT", "DELETE"):
            assert c.request(method, path, headers=h).status_code == 405, (method, path)


def test_buyer_search_is_unchanged(client):
    c, session = client
    p = make_postcode(session, "-ag-buyer")
    token = "Zqxbuyeragentcheck"
    _listing(session, p, f"{token} Flat")
    # Public buyer search (guest, no agent) is not restricted by any coverage.
    assert _titles(c.get(f"/buyer/listings?q={token}")) == [f"{token} Flat"]
    # Service contract: None = unrestricted (buyer); [] = covers nothing.
    _, unrestricted = listing_svc.search_listings(session, page=1, page_size=0, query=token)
    _, nothing = listing_svc.search_listings(session, page=1, page_size=0, postcode_ids=[], query=token)
    assert unrestricted == 1 and nothing == 0


def test_existing_broker_scope_endpoint_unchanged(client):
    c, session = client
    h, uid = _user(c, "BROKER")
    p = make_postcode(session, "-ag-scope")
    _grant(session, uid, p, _granter(session))
    body = c.get("/broker/scope", headers=h).json()
    assert body == {"broker_user_id": uid, "postcodes": [p]}
