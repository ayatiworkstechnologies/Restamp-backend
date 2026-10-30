"""Account status enforcement (security review, 2026-10-30).

users.status is 'active' | 'suspended' | 'closed'. Before this fix nothing
read it: a suspended or closed account could still log in and use every
endpoint. Now:
- login (OTP verify, Google) issues no token to a non-active account (403);
- every authenticated request from a non-active account is refused (403),
  including tokens issued before the account was disabled;
- the detail is static ("Account disabled") and reveals nothing else.
Rollback-isolated.
"""
import pytest
from sqlalchemy import text

from tests.conftest import auth_header, captured_otp_code
from tests.test_owner_leads import fresh_buyer, owner_header, seed_owner_listing


def _set_status(session, user_id, value):
    session.execute(text("UPDATE users SET status=:s WHERE id=:u"), {"s": value, "u": user_id})
    # The fixture shares one ORM session across requests; drop its cached
    # User rows so the next request reads the new status (production uses
    # a fresh session per request).
    session.expire_all()


@pytest.mark.parametrize("value", ["suspended", "closed"])
def test_disabled_account_is_refused_everywhere(client, value):
    c, session = client
    lid, _, owner_phone = seed_owner_listing(session)
    buyer = fresh_buyer(c)
    hb = auth_header(buyer["access_token"])
    assert c.get("/auth/me", headers=hb).status_code == 200
    _set_status(session, buyer["user_id"], value)
    # Token issued while active: refused once the account is disabled.
    for method, path in (("GET", "/auth/me"), ("GET", "/buyer/enquiries"), ("GET", "/buyer/visits"),
                         ("GET", "/buyer/call-events"), ("GET", "/users/me/role")):
        r = c.request(method, path, headers=hb)
        assert r.status_code == 403 and r.json() == {"detail": "Account disabled"}, (path, r.status_code)
    r = c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": "hi"}, headers=hb)
    assert r.status_code == 403
    assert session.execute(text("SELECT COUNT(*) FROM enquiries WHERE buyer_user_id=:u"),
                           {"u": buyer["user_id"]}).scalar() == 0
    # Owner endpoints too.
    ho, owner_id = owner_header(c, owner_phone)
    _set_status(session, owner_id, value)
    assert c.get("/owner/listings", headers=ho).status_code == 403
    assert c.get("/owner/leads", headers=ho).status_code == 403


def test_disabled_account_cannot_log_in_with_otp(client):
    c, session = client
    buyer = fresh_buyer(c)
    phone = session.execute(text("SELECT provider_identifier FROM auth_identities WHERE user_id=:u AND provider='phone'"),
                            {"u": buyer["user_id"]}).scalar()
    _set_status(session, buyer["user_id"], "suspended")
    assert c.post("/auth/otp/request", json={"phone": phone}).status_code == 200
    r = c.post("/auth/otp/verify", json={"phone": phone, "code": captured_otp_code(phone)})
    assert r.status_code == 403 and r.json() == {"detail": "Account disabled"}
    assert "access_token" not in r.text


def test_disabled_account_cannot_log_in_with_google(client):
    c, session = client
    buyer = fresh_buyer(c)
    hb = auth_header(buyer["access_token"])
    assert c.post("/auth/google/link", json={"id_token": "dev:status-test-sub"}, headers=hb).status_code == 200
    assert c.post("/auth/google", json={"id_token": "dev:status-test-sub"}).status_code == 200
    _set_status(session, buyer["user_id"], "closed")
    r = c.post("/auth/google", json={"id_token": "dev:status-test-sub"})
    assert r.status_code == 403 and "access_token" not in r.text


def test_reactivated_account_works_again(client):
    c, session = client
    buyer = fresh_buyer(c)
    hb = auth_header(buyer["access_token"])
    _set_status(session, buyer["user_id"], "suspended")
    assert c.get("/auth/me", headers=hb).status_code == 403
    _set_status(session, buyer["user_id"], "active")
    assert c.get("/auth/me", headers=hb).status_code == 200


def test_guest_browsing_unaffected(client):
    c, session = client
    lid, _, _ = seed_owner_listing(session)
    assert c.get(f"/buyer/listings/{lid}").status_code == 200
    assert c.get("/buyer/listings").status_code == 200
