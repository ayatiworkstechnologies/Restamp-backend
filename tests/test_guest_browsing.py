"""Guest browsing + login gating: public reads stay open, phones stay hidden,
mutations require auth. Uses the rollback-isolated `client` fixture on local
restamp_dev only. No real SMS/network calls.
"""
import json

from tests.conftest import auth_header, register_phone

OWNER_PHONE = "+919840123456"
LISTING_PHONE = "+919840654321"
DIGIT_VARIANTS = ("919840123456", "9840123456", "919840654321", "9840654321")

_SEQ = {"n": 0}


def _owner(c):
    _SEQ["n"] += 1
    out = register_phone(c, OWNER_PHONE)
    token = out["access_token"]
    r = c.post("/users/me/role", json={"role": "OWNER"}, headers=auth_header(token))
    assert r.status_code == 200, r.text
    return token


def _create(c, token, deal, title=None):
    _SEQ["n"] += 1
    r = c.post(
        "/owner/listings",
        json={
            "title": title or f"Guest {deal} Flat {_SEQ['n']}",
            "transaction_type": deal,
            "property_type": "APARTMENT",
            "price_rupees": 5000000,
            "price_period": "MONTHLY" if deal in ("RENT", "LEASE") else "TOTAL",
            "locality": f"Guest Nagar {_SEQ['n']}",
            "city": "Chennai",
            "pincode": "600080",
            "contact_phone": LISTING_PHONE,
            "photos": [{"url": f"https://t.test/g{_SEQ['n']}.jpg",
                        "category": "Living Room"}],
        },
        headers=auth_header(token),
    )
    assert r.status_code == 201, r.text
    lid = r.json()["listing_id"]
    assert c.post(f"/owner/listings/{lid}/verify",
                  headers=auth_header(token)).status_code == 200
    return lid


def _seed_all_deals(c):
    token = _owner(c)
    return token, {deal: _create(c, token, deal) for deal in ("BUY", "RESALE", "RENT", "LEASE")}


def _walk_keys(obj, found):
    if isinstance(obj, dict):
        for key, value in obj.items():
            found.add(str(key).lower())
            _walk_keys(value, found)
    elif isinstance(obj, list):
        for item in obj:
            _walk_keys(item, found)


def _assert_no_phone_leak(payload, where):
    blob = json.dumps(payload)
    for variant in (OWNER_PHONE, LISTING_PHONE) + DIGIT_VARIANTS:
        assert variant not in blob, f"{where} leaks {variant}"
    keys = set()
    _walk_keys(payload, keys)
    assert "phone" not in keys, f"{where} has a phone key: {sorted(keys)}"
    assert "contact_phone" not in keys, f"{where} has contact_phone"
    assert "provider_identifier" not in keys, f"{where} leaks provider identifier"


# --- Guest browsing stays open ------------------------------------------------

def test_guest_browses_all_four_deal_types(client):
    c, _ = client
    _, lids = _seed_all_deals(c)
    r = c.get("/buyer/listings")
    assert r.status_code == 200, r.text
    assert {i["listing_id"] for i in r.json()["items"]} >= set(lids.values())
    for deal in ("BUY", "RESALE", "RENT", "LEASE"):
        r = c.get("/buyer/listings", params={"deal": deal})
        assert r.status_code == 200, (deal, r.text)
        assert r.json()["items"], deal
        assert {i["transaction_type"] for i in r.json()["items"]} == {deal}
        _assert_no_phone_leak(r.json(), f"feed {deal}")


def test_guest_search_filter_sort_paginate(client):
    c, _ = client
    _seed_all_deals(c)
    for params in (
        {"q": "Guest"},
        {"locality": "Guest Nagar"},
        {"city": "Chennai"},
        {"bedrooms_min": 0},
        {"min_price_paise": 1, "max_price_paise": 10**15},
        {"construction_status": "READY_TO_MOVE"},
        {"sort": "price_asc"},
        {"sort": "price_desc"},
        {"page": 1, "page_size": 2},
        {"count_only": True},
        {"deal": "RENT", "sort": "price_asc", "page": 1, "page_size": 5},
    ):
        r = c.get("/buyer/listings", params=params)
        assert r.status_code == 200, (params, r.text)
        _assert_no_phone_leak(r.json(), f"feed {params}")


def test_guest_detail_similar_photos(client):
    c, _ = client
    _, lids = _seed_all_deals(c)
    lid = lids["BUY"]
    r = c.get(f"/buyer/listings/{lid}")
    assert r.status_code == 200, r.text
    detail = r.json()
    assert detail["gallery"], "expected seeded photo in public gallery"
    assert detail["cover_image_url"] in detail["gallery"]
    _assert_no_phone_leak(detail, "detail")
    r = c.get(f"/buyer/listings/{lid}/similar", params={"limit": 5})
    assert r.status_code == 200, r.text
    _assert_no_phone_leak(r.json(), "similar")
    # Unknown id: 404 with no phone in the error body either.
    r = c.get("/buyer/listings/999999999")
    assert r.status_code == 404
    _assert_no_phone_leak(r.json(), "detail-404")


# --- Guest mutations gated with the consistent auth error ---------------------

def test_guest_mutations_require_login(client):
    c, _ = client
    _, lids = _seed_all_deals(c)
    lid = lids["RENT"]
    guest_calls = [
        ("post", f"/buyer/listings/{lid}/contact", None),
        ("post", "/buyer/enquiries", {"property_listing_id": lid}),
        ("get", "/buyer/enquiries", None),
        ("post", f"/buyer/saved/{lid}", None),
        ("get", "/buyer/saved", None),
        ("delete", f"/buyer/saved/{lid}", None),
    ]
    for method, path, body in guest_calls:
        r = getattr(c, method)(path, json=body) if body else getattr(c, method)(path)
        assert r.status_code == 401, (method, path, r.text)
        assert r.json() == {"detail": "Not authenticated"}, (method, path, r.text)


# --- Authenticated behavior preserved, phone only via contact -----------------

def test_authenticated_flows_preserved_without_leaks(client):
    c, _ = client
    _, lids = _seed_all_deals(c)
    lid = lids["LEASE"]
    buyer = register_phone(c, "+919100888801")["access_token"]
    h = auth_header(buyer)
    # Phone is revealed ONLY through the contact endpoint ...
    r = c.post(f"/buyer/listings/{lid}/contact", headers=h)
    assert r.status_code == 200, r.text
    assert r.json() == {"phone": OWNER_PHONE}
    # ... and appears nowhere else, even authenticated.
    r = c.get(f"/buyer/listings/{lid}", headers=h)
    assert r.status_code == 200, r.text
    _assert_no_phone_leak(r.json(), "authed-detail")
    r = c.post("/buyer/enquiries", json={"property_listing_id": lid}, headers=h)
    assert r.status_code in (200, 201), r.text
    _assert_no_phone_leak(r.json(), "enquiry-create")
    r = c.get("/buyer/enquiries", headers=h)
    assert r.status_code == 200, r.text
    _assert_no_phone_leak(r.json(), "enquiry-list")
    r = c.post(f"/buyer/saved/{lid}", headers=h)
    assert r.status_code in (200, 201), r.text
    _assert_no_phone_leak(r.json(), "saved-create")
    r = c.get("/buyer/saved", headers=h)
    assert r.status_code == 200, r.text
    _assert_no_phone_leak(r.json(), "saved-list")


def test_owner_leads_never_carry_phones(client):
    c, _ = client
    token, lids = _seed_all_deals(c)
    buyer = register_phone(c, "+919100888802")["access_token"]
    c.post(f"/buyer/listings/{lids['BUY']}/contact", headers=auth_header(buyer))
    r = c.get("/owner/leads", headers=auth_header(token))
    assert r.status_code == 200, r.text
    assert r.json()["items"], "expected at least one lead"
    _assert_no_phone_leak(r.json(), "owner-leads")


# --- 403 root-cause matrix: role, not token ----------------------------------
# A 403 on POST /buyer/enquiries (or .../contact) means exactly one thing:
# authenticated, but current role is not BUYER (e.g. an owner/agent-mode
# account tapping Send Enquiry). Missing/invalid tokens are 401 instead.

def test_enquiry_403_means_wrong_role(client):
    c, _ = client
    token, lids = _seed_all_deals(c)
    lid = lids["BUY"]
    # OWNER-role account (e.g. just posted a property) -> 403 with an
    # actionable detail naming the required role.
    r = c.post("/buyer/enquiries", json={"property_listing_id": lid},
               headers=auth_header(token))
    assert r.status_code == 403, r.text
    assert r.json() == {"detail": "BUYER role required"}, r.text
    # Same for contact reveal: eligible buyers only, never a phone on 403.
    r = c.post(f"/buyer/listings/{lid}/contact", headers=auth_header(token))
    assert r.status_code == 403, r.text
    assert "phone" not in r.json()
    # BROKER role likewise refused.
    broker = register_phone(c, "+919100888803")["access_token"]
    c.post("/users/me/role", json={"role": "BROKER"}, headers=auth_header(broker))
    r = c.post("/buyer/enquiries", json={"property_listing_id": lid},
               headers=auth_header(broker))
    assert r.status_code == 403, r.text
    # Switching back to BUYER restores access (self-serve preserved): the
    # role gate now passes. `lid` is this account's OWN listing, so the
    # request reaches the self-enquiry rule (422, added in the 2026-10-30
    # security review) instead of the 403 role refusal.
    c.post("/users/me/role", json={"role": "BUYER"}, headers=auth_header(token))
    r = c.post("/buyer/enquiries", json={"property_listing_id": lid},
               headers=auth_header(token))
    assert r.status_code == 422, r.text
    assert r.json() == {"detail": "Cannot enquire on your own listing"}, r.text
    # The broker switching to BUYER gets the full buyer path on that listing.
    c.post("/users/me/role", json={"role": "BUYER"}, headers=auth_header(broker))
    r = c.post("/buyer/enquiries", json={"property_listing_id": lid},
               headers=auth_header(broker))
    assert r.status_code in (200, 201), r.text
    _assert_no_phone_leak(r.json(), "enquiry-after-role-switch")
