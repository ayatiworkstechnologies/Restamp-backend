"""Admin backend: authentication, permissions, listings, users, enquiries,
dashboard, analytics, audit log and staff management.

Admin = an admin_accounts row (read from the DB on every request) plus one
permission of its admin_role (services/admin_rbac.py). Covered here:
401/403 for everyone else, no role-switch path to admin, suspended/revoked
admins refused, the UI role matrix enforced, PENDING-only verify/reject with
required reasons and audit + verifications rows, invalid transitions, user
suspension (token refused at once), read-only enquiries without owner
notes, real dashboard/analytics deltas, staff grant/role/revoke with
self-lockout and last-Super-Admin protection, and the gated legacy
/admin/grants. Rollback-isolated (restamp_test holds persisted rows from
earlier live runs, so counts are asserted as deltas).
"""
import pytest
from sqlalchemy import text

from tests.conftest import auth_header, make_postcode, register_phone
from tests.test_broker_agent import _listing
from tests.test_owner_leads import fresh_buyer, owner_header, seed_owner_listing

_n = {"i": 0}
READ_PATHS = (
    "/admin/me", "/admin/roles", "/admin/dashboard", "/admin/analytics", "/admin/listings",
    "/admin/listings/1", "/admin/users", "/admin/users/1", "/admin/enquiries", "/admin/enquiries/1",
    "/admin/audit-logs", "/admin/staff",
)
WRITE_CALLS = (
    ("POST", "/admin/listings/1/verify", None),
    ("POST", "/admin/listings/1/reject", {"reason": "x"}),
    ("POST", "/admin/users/1/suspend", {"reason": "x"}),
    ("POST", "/admin/users/1/reactivate", None),
    ("POST", "/admin/staff", {"user_id": 1, "admin_role": "SUPPORT_SPECIALIST"}),
    ("PATCH", "/admin/staff/1", {"admin_role": "SUPPORT_SPECIALIST"}),
    ("DELETE", "/admin/staff/1", None),
)


def _phone():
    _n["i"] += 1
    return f"+916880{_n['i']:06d}"


def _user(c, role="BUYER"):
    tok = register_phone(c, _phone())
    h = auth_header(tok["access_token"])
    if role != "BUYER":
        assert c.post("/users/me/role", json={"role": role}, headers=h).status_code == 200
    return h, tok["user_id"]


def _admin(c, session, admin_role="SUPER_ADMIN"):
    h, uid = _user(c)
    session.execute(text("INSERT INTO admin_accounts (user_id, admin_role) VALUES (:u, :r)"),
                    {"u": uid, "r": admin_role})
    session.flush()
    return h, uid


def _audits(session, action, **after):
    rows = session.execute(text("SELECT user_id, before_json, after_json FROM audit_logs WHERE action=:a ORDER BY id"),
                           {"a": action}).fetchall()
    import json
    out = []
    for uid, before, aft in rows:
        b = json.loads(before) if isinstance(before, str) else before
        a = json.loads(aft) if isinstance(aft, str) else aft
        if all((a or {}).get(k) == v for k, v in after.items()):
            out.append((uid, b, a))
    return out


def _call(c, method, path, headers, body=None):
    return c.request(method, path, headers=headers, json=body)


# --- authentication / authorization -----------------------------------------------------

def test_admin_endpoints_require_authentication(client):
    c, _ = client
    for path in READ_PATHS:
        assert c.get(path).status_code == 401, path
    for method, path, body in WRITE_CALLS:
        assert _call(c, method, path, None, body).status_code == 401, (method, path)


@pytest.mark.parametrize("role", ["BUYER", "OWNER", "BROKER"])
def test_non_admins_are_forbidden_everywhere(client, role):
    c, _ = client
    h, _ = _user(c, role)
    for path in READ_PATHS:
        r = c.get(path, headers=h)
        assert r.status_code == 403 and r.json() == {"detail": "Forbidden"}, path
    for method, path, body in WRITE_CALLS:
        r = _call(c, method, path, h, body)
        assert r.status_code == 403 and r.json() == {"detail": "Forbidden"}, (method, path)


def test_role_switching_cannot_create_admin(client):
    c, session = client
    h, uid = _user(c)
    for value in ("ADMIN", "SUPER_ADMIN", "admin"):
        assert c.post("/users/me/role", json={"role": value}, headers=h).status_code == 422
    assert c.get("/admin/me", headers=h).status_code == 403
    assert session.execute(text("SELECT COUNT(*) FROM admin_accounts WHERE user_id=:u"), {"u": uid}).scalar() == 0


def test_suspended_and_revoked_admins_are_refused(client):
    c, session = client
    h, uid = _admin(c, session)
    assert c.get("/admin/me", headers=h).status_code == 200
    session.execute(text("UPDATE users SET status='suspended' WHERE id=:u"), {"u": uid})
    session.expire_all()
    r = c.get("/admin/dashboard", headers=h)
    assert r.status_code == 403 and r.json() == {"detail": "Account disabled"}
    session.execute(text("UPDATE users SET status='active' WHERE id=:u"), {"u": uid})
    session.execute(text("DELETE FROM admin_accounts WHERE user_id=:u"), {"u": uid})
    session.expire_all()
    assert c.get("/admin/dashboard", headers=h).json() == {"detail": "Forbidden"}


def test_unknown_admin_role_has_no_permissions(client):
    c, session = client
    h, _ = _admin(c, session, "MYSTERY_ROLE")
    me = c.get("/admin/me", headers=h).json()
    assert me["admin_role"] == "MYSTERY_ROLE" and me["permissions"] == []
    r = c.get("/admin/listings", headers=h)
    assert r.status_code == 403 and r.json() == {"detail": "properties.view permission required"}


def test_role_matrix_is_enforced(client):
    c, session = client
    hs, _ = _admin(c, session, "SUPPORT_SPECIALIST")
    hr, _ = _admin(c, session, "LISTING_REVIEWER")
    ho, _ = _admin(c, session, "OPERATIONS_MANAGER")
    lid = _listing(session, make_postcode(session, "-adm-mx"), "Matrix Flat", verification="PENDING")
    _, target = _user(c)
    allowed = {
        "SUPPORT": [(hs, "GET", "/admin/listings"), (hs, "GET", "/admin/enquiries"), (hs, "GET", "/admin/users")],
        "REVIEWER": [(hr, "GET", "/admin/dashboard"), (hr, "POST", f"/admin/listings/{lid}/verify")],
        "OPS": [(ho, "GET", "/admin/audit-logs"), (ho, "GET", "/admin/analytics"),
                (ho, "POST", f"/admin/users/{target}/suspend")],
    }
    denied = [
        (hs, "POST", f"/admin/listings/{lid}/verify", "properties.approve"),
        (hs, "POST", f"/admin/users/{target}/suspend", "users.suspend"),
        (hs, "GET", "/admin/audit-logs", "settings.view_audit_logs"),
        (hs, "GET", "/admin/analytics", "reports.view"),
        (hr, "POST", f"/admin/users/{target}/suspend", "users.suspend"),
        (hr, "GET", "/admin/analytics", "reports.view"),
        (ho, "GET", "/admin/staff", "settings.manage_team"),
        (ho, "POST", "/admin/staff", "settings.manage_team"),
    ]
    for group in allowed.values():
        for h, method, path in group:
            body = {"reason": "Fraud check"} if path.endswith("/suspend") else None
            assert _call(c, method, path, h, body).status_code == 200, (method, path)
    for h, method, path, perm in denied:
        body = {"reason": "x"} if path.endswith("/suspend") else (
            {"user_id": target, "admin_role": "SUPPORT_SPECIALIST"} if path == "/admin/staff" and method == "POST" else None)
        r = _call(c, method, path, h, body)
        assert r.status_code == 403 and r.json() == {"detail": f"{perm} permission required"}, (method, path)


def test_admin_me_and_roles(client):
    c, session = client
    h, uid = _admin(c, session, "LISTING_REVIEWER")
    me = c.get("/admin/me", headers=h).json()
    assert me["user_id"] == uid and me["admin_role"] == "LISTING_REVIEWER" and me["role_label"] == "Listing Reviewer"
    assert "properties.approve" in me["permissions"] and "users.suspend" not in me["permissions"]
    roles = {r["code"]: r for r in c.get("/admin/roles", headers=h).json()["items"]}
    assert set(roles) == {"SUPER_ADMIN", "OPERATIONS_MANAGER", "LISTING_REVIEWER", "SUPPORT_SPECIALIST", "ADMIN"}
    assert roles["ADMIN"]["assignable"] is False and roles["SUPER_ADMIN"]["assignable"] is True
    assert "settings.manage_team" in roles["SUPER_ADMIN"]["permissions"]
    assert "settings.manage_team" not in roles["OPERATIONS_MANAGER"]["permissions"]


def test_legacy_grants_endpoint_requires_manage_team(client):
    c, session = client
    hs, _ = _admin(c, session, "SUPPORT_SPECIALIST")
    _, target = _user(c)
    r = c.post("/admin/grants", json={"user_id": target}, headers=hs)
    assert r.status_code == 403 and r.json() == {"detail": "settings.manage_team permission required"}
    assert session.execute(text("SELECT COUNT(*) FROM admin_accounts WHERE user_id=:u"), {"u": target}).scalar() == 0
    hl, _ = _admin(c, session, "ADMIN")  # legacy full-access admins keep working
    assert c.post("/admin/grants", json={"user_id": target}, headers=hl).status_code == 201


# --- listings ------------------------------------------------------------------------

def test_listing_list_filters_search_and_pagination(client):
    c, session = client
    h, _ = _admin(c, session, "LISTING_REVIEWER")
    pc = make_postcode(session, "-adm-ls")
    tok = "Qzadminlistcheck"
    _listing(session, pc, f"{tok} Pending A", verification="PENDING")
    _listing(session, pc, f"{tok} Pending B", verification="PENDING")
    _listing(session, pc, f"{tok} Live", verification="VERIFIED")
    _listing(session, pc, f"{tok} Rejected", verification="REJECTED")
    removed = _listing(session, pc, f"{tok} Removed", verification="PENDING", deleted=True)
    body = c.get(f"/admin/listings?q={tok}", headers=h).json()
    assert body["total"] == 4 and {i["title"] for i in body["items"]} == {
        f"{tok} Pending A", f"{tok} Pending B", f"{tok} Live", f"{tok} Rejected"}
    row = next(i for i in body["items"] if i["title"] == f"{tok} Live")
    assert row["locality"] == "L1-adm-ls" and row["removed"] is False and row["owner_name"] == "Lister"
    pend = c.get(f"/admin/listings?q={tok}&verification_status=PENDING", headers=h).json()
    assert {i["title"] for i in pend["items"]} == {f"{tok} Pending A", f"{tok} Pending B"}
    with_removed = c.get(f"/admin/listings?q={tok}&include_removed=true", headers=h).json()
    assert with_removed["total"] == 5 and any(i["listing_id"] == removed and i["removed"] for i in with_removed["items"])
    page2 = c.get(f"/admin/listings?q={tok}&page=2&page_size=3", headers=h).json()
    assert page2["total"] == 4 and len(page2["items"]) == 1 and page2["page"] == 2
    assert c.get("/admin/listings?page_size=101", headers=h).status_code == 422
    assert c.get("/admin/listings?verification_status=BOGUS", headers=h).status_code == 422
    assert c.get("/admin/listings?sort=submitted_oldest", headers=h).status_code == 200


def test_verify_pending_listing_makes_it_public_and_is_audited(client):
    c, session = client
    h, admin_id = _admin(c, session, "LISTING_REVIEWER")
    lid = _listing(session, make_postcode(session, "-adm-v"), "Verify Me Flat", verification="PENDING")
    assert c.get(f"/buyer/listings/{lid}").status_code == 404
    r = c.post(f"/admin/listings/{lid}/verify", json={"notes": "  Docs checked  "}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["listing"]["verification_status"] == "VERIFIED"
    event = body["verification_history"][-1]
    assert event["actor"] == "admin" and event["action"] == "verified" and event["result"] == "PASS"
    assert event["triggered_by_user_id"] == admin_id
    assert c.get(f"/buyer/listings/{lid}").status_code == 200  # buyer visibility rules unchanged
    audits = _audits(session, "ADMIN_LISTING_VERIFIED", listing_id=lid)
    assert audits == [(admin_id, {"listing_id": lid, "verification_status": "PENDING"},
                       {"listing_id": lid, "verification_status": "VERIFIED", "notes": "Docs checked"})]
    # Idempotent repeat: 200, nothing new written.
    before = session.execute(text("SELECT COUNT(*) FROM verifications WHERE property_listing_id=:l"), {"l": lid}).scalar()
    assert c.post(f"/admin/listings/{lid}/verify", headers=h).status_code == 200
    assert session.execute(text("SELECT COUNT(*) FROM verifications WHERE property_listing_id=:l"), {"l": lid}).scalar() == before
    assert len(_audits(session, "ADMIN_LISTING_VERIFIED", listing_id=lid)) == 1


def test_reject_requires_reason_and_owner_can_resubmit(client):
    c, session = client
    h, admin_id = _admin(c, session, "OPERATIONS_MANAGER")
    lid, owner_id, owner_phone = seed_owner_listing(session, verification="PENDING")
    for bad in ({}, {"reason": ""}, {"reason": "   "}, {"reason": "x" * 501}, {"reason": "ok", "category": "c" * 101}):
        r = c.post(f"/admin/listings/{lid}/reject", json=bad, headers=h)
        assert r.status_code == 422 and r.json() == {"detail": "Invalid request"}, bad
    r = c.post(f"/admin/listings/{lid}/reject",
               json={"reason": " Blurry photos ", "category": "Low Quality / Inappropriate Photographs"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["listing"]["verification_status"] == "REJECTED"
    event = r.json()["verification_history"][-1]
    assert event["result"] == "FAIL" and event["reason"] == "Blurry photos" and event["actor"] == "admin"
    assert _audits(session, "ADMIN_LISTING_REJECTED", listing_id=lid)[0][2] == {
        "listing_id": lid, "verification_status": "REJECTED", "reason": "Blurry photos",
        "category": "Low Quality / Inappropriate Photographs"}
    assert c.get(f"/buyer/listings/{lid}").status_code == 404
    # Existing owner lifecycle unchanged: the owner resubmits REJECTED -> PENDING.
    ho, _ = owner_header(c, owner_phone)
    assert c.post(f"/owner/listings/{lid}/submit", headers=ho).json()["verification_status"] == "PENDING"


def test_invalid_listing_transitions(client):
    c, session = client
    h, _ = _admin(c, session)
    pc = make_postcode(session, "-adm-t")
    verified = _listing(session, pc, "Already Live", verification="VERIFIED")
    rejected = _listing(session, pc, "Already Rejected", verification="REJECTED")
    removed = _listing(session, pc, "Removed Pending", verification="PENDING", deleted=True)
    only_pending = {"detail": "Only pending listings can be verified or rejected"}
    assert c.post(f"/admin/listings/{verified}/reject", json={"reason": "late"}, headers=h).json() == only_pending
    assert c.post(f"/admin/listings/{rejected}/verify", headers=h).json() == only_pending
    r = c.post(f"/admin/listings/{removed}/verify", headers=h)
    assert r.status_code == 422 and r.json() == {"detail": "Listing has been removed"}
    assert c.post("/admin/listings/987654321/verify", headers=h).status_code == 404
    assert c.get("/admin/listings/987654321", headers=h).status_code == 404


def test_listing_detail_includes_owner_and_history(client):
    c, session = client
    h, _ = _admin(c, session, "SUPPORT_SPECIALIST")
    lid, owner_id, _ = seed_owner_listing(session, verification="PENDING")
    body = c.get(f"/admin/listings/{lid}", headers=h).json()
    assert body["listing"]["listing_id"] == lid and body["owner"]["user_id"] == owner_id
    assert body["owner"]["status"] == "active" and body["verification_history"] == []


# --- users ----------------------------------------------------------------------------

def test_user_list_filters_and_phone_privacy(client):
    c, session = client
    h, _ = _admin(c, session, "SUPPORT_SPECIALIST")
    hb, buyer_id = _user(c, "BROKER")
    phone = session.execute(text("SELECT provider_identifier FROM auth_identities WHERE user_id=:u AND provider='phone'"),
                            {"u": buyer_id}).scalar()
    body = c.get(f"/admin/users?q={phone[-10:]}", headers=h).json()
    assert body["total"] == 1
    row = body["items"][0]
    assert row["user_id"] == buyer_id and row["current_role"] == "BROKER" and row["phone_last4"] == phone[-4:]
    assert phone not in str(body)  # list never carries the full number (name is phone-derived -> hidden)
    assert row["display_name"] is None
    assert all(i["current_role"] == "BROKER" for i in c.get("/admin/users?role=BROKER&page_size=100", headers=h).json()["items"])
    assert c.get("/admin/users?status=bogus", headers=h).status_code == 422
    detail = c.get(f"/admin/users/{buyer_id}", headers=h).json()
    assert detail["phone"] == phone and detail["providers"] == ["phone"]
    assert detail["activity"] == {"listings_owned": 0, "enquiries_sent": 0, "enquiries_received": 0,
                                  "visits_requested": 0, "broker_covered_locations": 0}
    assert c.get("/admin/users/987654321", headers=h).status_code == 404


def test_suspend_and_reactivate_user(client):
    c, session = client
    h, admin_id = _admin(c, session, "OPERATIONS_MANAGER")
    hu, uid = _user(c)
    assert c.post(f"/admin/users/{uid}/suspend", json={}, headers=h).status_code == 422
    r = c.post(f"/admin/users/{uid}/suspend", json={"reason": "Spam enquiries"}, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "suspended"
    session.expire_all()
    assert c.get("/auth/me", headers=hu).json() == {"detail": "Account disabled"}  # existing token refused at once
    assert c.post(f"/admin/users/{uid}/suspend", json={"reason": "again"}, headers=h).status_code == 200  # idempotent
    assert len(_audits(session, "ADMIN_USER_SUSPENDED", user_id=uid)) == 1
    r = c.post(f"/admin/users/{uid}/reactivate", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "active"
    session.expire_all()
    assert c.get("/auth/me", headers=hu).status_code == 200
    assert _audits(session, "ADMIN_USER_SUSPENDED", user_id=uid)[0][2] == {"user_id": uid, "status": "suspended", "reason": "Spam enquiries"}
    assert len(_audits(session, "ADMIN_USER_REACTIVATED", user_id=uid)) == 1
    r = c.post(f"/admin/users/{admin_id}/suspend", json={"reason": "self"}, headers=h)
    assert r.status_code == 422 and r.json() == {"detail": "You cannot change your own account status"}


def test_admin_accounts_need_manage_team_to_suspend(client):
    c, session = client
    session.execute(text("UPDATE admin_accounts SET admin_role='SUPPORT_SPECIALIST'"))  # rolled back
    ho, _ = _admin(c, session, "OPERATIONS_MANAGER")
    hsup, sup = _admin(c, session, "SUPER_ADMIN")
    _, reviewer = _admin(c, session, "LISTING_REVIEWER")
    r = c.post(f"/admin/users/{reviewer}/suspend", json={"reason": "x"}, headers=ho)
    assert r.status_code == 403 and r.json() == {"detail": "settings.manage_team permission required"}
    assert session.execute(text("SELECT status FROM users WHERE id=:u"), {"u": reviewer}).scalar() == "active"
    r = c.post(f"/admin/users/{reviewer}/suspend", json={"reason": "x"}, headers=hsup)
    assert r.status_code == 200 and r.json()["status"] == "suspended"


# --- enquiries ----------------------------------------------------------------------

def test_enquiries_are_read_only_and_hide_owner_notes(client):
    c, session = client
    h, _ = _admin(c, session, "SUPPORT_SPECIALIST")
    lid, _, owner_phone = seed_owner_listing(session)
    buyer = fresh_buyer(c)
    eid = c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": "Is parking included?"},
                 headers=auth_header(buyer["access_token"])).json()["id"]
    ho, _ = owner_header(c, owner_phone)
    assert c.post(f"/owner/leads/{eid}/notes", json={"body": "PRIVATE-NOTE-4471"}, headers=ho).status_code == 201
    assert c.post(f"/owner/leads/{eid}/messages", json={"message": "Yes, one slot"}, headers=ho).status_code == 201
    listed = c.get(f"/admin/enquiries?property_listing_id={lid}", headers=h).json()
    assert listed["total"] == 1 and listed["items"][0]["message_count"] == 2
    assert listed["items"][0]["last_message"] == "Yes, one slot" and "messages" not in listed["items"][0]
    detail = c.get(f"/admin/enquiries/{eid}", headers=h)
    assert [(m["sender"], m["body"]) for m in detail.json()["messages"]] == [
        ("BUYER", "Is parking included?"), ("OWNER", "Yes, one slot")]
    assert "PRIVATE-NOTE-4471" not in detail.text and "PRIVATE-NOTE-4471" not in str(listed)
    assert c.get(f"/admin/enquiries?property_listing_id={lid}&status=CLOSED", headers=h).json()["total"] == 0
    for method in ("POST", "PATCH", "PUT", "DELETE"):
        assert c.request(method, f"/admin/enquiries/{eid}", headers=h).status_code == 405
    assert c.get("/admin/enquiries/987654321", headers=h).status_code == 404


# --- dashboard / analytics / audit ----------------------------------------------------

def test_dashboard_counts_are_real(client):
    c, session = client
    h, _ = _admin(c, session, "LISTING_REVIEWER")
    before = c.get("/admin/dashboard", headers=h).json()
    pc = make_postcode(session, "-adm-d")
    _listing(session, pc, "Dash Pending", verification="PENDING")
    _listing(session, pc, "Dash Live", verification="VERIFIED")
    _listing(session, pc, "Dash Rejected", verification="REJECTED")
    _listing(session, pc, "Dash Closed", verification="VERIFIED", status="CLOSED")
    _listing(session, pc, "Dash Removed", verification="PENDING", deleted=True)
    _user(c, "BROKER")
    after = c.get("/admin/dashboard", headers=h).json()
    delta = {k: after[k] - before[k] for k in after if k != "unavailable"}
    # Removed listing excluded everywhere; the CLOSED one counts in total only.
    assert delta["listings_total"] == 4 and delta["listings_live"] == 1
    assert delta["listings_pending"] == 1 and delta["listings_rejected"] == 1
    assert delta["brokers"] == 1 and delta["enquiries_total"] == 0
    assert after["unavailable"] == ["pending_kyc"]


def test_analytics_uses_real_decisions_and_enquiries(client):
    c, session = client
    h, _ = _admin(c, session, "OPERATIONS_MANAGER")
    before = c.get("/admin/analytics?months=1", headers=h).json()
    lid, _, _ = seed_owner_listing(session, verification="PENDING")
    assert c.post(f"/admin/listings/{lid}/verify", headers=h).status_code == 200
    buyer = fresh_buyer(c)
    c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": "hi"}, headers=auth_header(buyer["access_token"]))
    after = c.get("/admin/analytics?months=1", headers=h).json()
    assert len(after["months"]) == 1
    assert after["months"][0]["verified"] == before["months"][0]["verified"] + 1
    loc = session.execute(text(
        "SELECT l.name FROM property_listings pl JOIN physical_properties pp ON pp.id=pl.physical_property_id "
        "JOIN postcodes p ON p.id=pp.postcode_id JOIN localities l ON l.id=p.locality_id WHERE pl.id=:l"), {"l": lid}).scalar()
    assert any(row["locality"] == loc and row["enquiries"] >= 1 for row in after["top_enquiry_localities"]) or \
        len(after["top_enquiry_localities"]) == 10
    assert after["unavailable"] == ["listings_added_per_month", "approval_sla"]
    assert len(c.get("/admin/analytics", headers=h).json()["months"]) == 6
    for bad in (0, 25):
        assert c.get(f"/admin/analytics?months={bad}", headers=h).status_code == 422


def test_audit_log_lists_admin_actions_without_phones(client):
    c, session = client
    h, admin_id = _admin(c, session, "SUPER_ADMIN")
    lid = _listing(session, make_postcode(session, "-adm-a"), "Audit Flat", verification="PENDING")
    c.post(f"/admin/listings/{lid}/reject", json={"reason": "Duplicate"}, headers=h)
    body = c.get(f"/admin/audit-logs?action=ADMIN_LISTING_REJECTED&actor_user_id={admin_id}", headers=h).json()
    assert body["total"] == 1
    row = body["items"][0]
    assert row["actor_user_id"] == admin_id and row["entity"] == "property_listing"
    assert row["after"]["reason"] == "Duplicate" and row["created_at"].endswith(("Z", "+00:00"))
    phone = session.execute(text("SELECT provider_identifier FROM auth_identities WHERE user_id=:u"), {"u": admin_id}).scalar()
    assert phone not in str(body) and row["actor_name"] is None
    assert c.get("/admin/audit-logs?page_size=0", headers=h).status_code == 422


# --- staff management -----------------------------------------------------------------

def test_staff_grant_change_and_revoke(client):
    c, session = client
    session.execute(text("UPDATE admin_accounts SET admin_role='SUPPORT_SPECIALIST'"))  # rolled back
    h, sup = _admin(c, session, "SUPER_ADMIN")
    _, uid = _user(c)
    r = c.post("/admin/staff", json={"user_id": uid, "admin_role": "LISTING_REVIEWER"}, headers=h)
    assert r.status_code == 201 and r.json()["admin_role"] == "LISTING_REVIEWER" and r.json()["status"] == "active"
    assert c.post("/admin/staff", json={"user_id": uid, "admin_role": "LISTING_REVIEWER"}, headers=h).status_code == 409
    for bad in ({"user_id": uid, "admin_role": "ADMIN"}, {"user_id": uid, "admin_role": "GOD"}, {"user_id": 0, "admin_role": "SUPER_ADMIN"}):
        assert c.post("/admin/staff", json=bad, headers=h).status_code == 422
    _, suspended = _user(c)
    session.execute(text("UPDATE users SET status='suspended' WHERE id=:u"), {"u": suspended})
    session.expire_all()
    r = c.post("/admin/staff", json={"user_id": suspended, "admin_role": "SUPPORT_SPECIALIST"}, headers=h)
    assert r.status_code == 422 and r.json() == {"detail": "User account is not active"}
    assert c.post("/admin/staff", json={"user_id": 987654321, "admin_role": "SUPPORT_SPECIALIST"}, headers=h).status_code == 404
    r = c.patch(f"/admin/staff/{uid}", json={"admin_role": "OPERATIONS_MANAGER"}, headers=h)
    assert r.status_code == 200 and r.json()["admin_role"] == "OPERATIONS_MANAGER"
    assert any(s["user_id"] == uid for s in c.get("/admin/staff", headers=h).json()["items"])
    assert c.delete(f"/admin/staff/{uid}", headers=h).status_code == 204
    assert c.delete(f"/admin/staff/{uid}", headers=h).status_code == 404
    assert session.execute(text("SELECT COUNT(*) FROM admin_accounts WHERE user_id=:u"), {"u": uid}).scalar() == 0
    actions = [a for (a,) in session.execute(text(
        "SELECT action FROM audit_logs WHERE user_id=:a AND action LIKE 'ADMIN_STAFF_%' ORDER BY id"), {"a": sup}).all()]
    assert actions == ["ADMIN_STAFF_GRANTED", "ADMIN_STAFF_ROLE_CHANGED", "ADMIN_STAFF_REVOKED"]


def test_staff_lockout_protections(client):
    c, session = client
    session.execute(text("UPDATE admin_accounts SET admin_role='SUPPORT_SPECIALIST'"))  # rolled back
    h1, sup1 = _admin(c, session, "SUPER_ADMIN")
    self_err = {"detail": "You cannot change your own admin access"}
    assert c.patch(f"/admin/staff/{sup1}", json={"admin_role": "SUPPORT_SPECIALIST"}, headers=h1).json() == self_err
    assert c.delete(f"/admin/staff/{sup1}", headers=h1).json() == self_err
    r = c.post(f"/admin/users/{sup1}/suspend", json={"reason": "x"}, headers=h1)
    assert r.status_code == 422 and r.json() == {"detail": "You cannot change your own account status"}
    # Another Super Admin can be demoted while one active Super Admin remains.
    _, sup2 = _admin(c, session, "SUPER_ADMIN")
    assert c.patch(f"/admin/staff/{sup2}", json={"admin_role": "SUPPORT_SPECIALIST"}, headers=h1).status_code == 200
    # Defence in depth (service level, whoever the caller is): the last active
    # full-access admin can never be revoked, demoted or suspended.
    from fastapi import HTTPException
    from app.services import admin as admin_svc
    last = "At least one active Super Admin must remain"
    for attempt in (
        lambda: admin_svc.revoke_staff(session, sup2, sup1),
        lambda: admin_svc.change_staff_role(session, sup2, sup1, "SUPPORT_SPECIALIST"),
    ):
        with pytest.raises(HTTPException) as exc:
            attempt()
        assert exc.value.status_code == 422 and exc.value.detail == last
    assert session.execute(text("SELECT admin_role FROM admin_accounts WHERE user_id=:u"), {"u": sup1}).scalar() == "SUPER_ADMIN"
