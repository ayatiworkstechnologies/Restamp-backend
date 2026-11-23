"""Subscription management: admin plans, admin subscription changes, temporary
payment bypass, user-facing plan/limits, and owner listing-limit enforcement.

Covered: 401/403 for non-admins and wrong admin roles; plan create / edit
(version + history) / pause / resume / archive / invalid data; assign, upgrade,
downgrade (one current plan per role, history kept), extend, suspend,
reactivate, cancel, audit rows; bypass (role check, idempotent, BYPASS payment
never SUCCESS, never verified); GET /subscriptions/me reflecting admin changes;
owner listing limit (402) following the assigned plan; buyer lead limit
following an admin plan edit. Rollback-isolated.
"""
from sqlalchemy import text

from tests.test_admin import _admin, _user

_n = {"i": 0}


def _code(prefix="T"):
    _n["i"] += 1
    return f"TEST_{prefix}_{_n['i']:04d}"


def _plan(c, h, role="OWNER", **kw):
    body = {"code": _code(role[:2]), "role": role, "name": f"{role} plan {_n['i']}", "price_paise": 49900,
            "duration_days": 30, "listing_limit": 1, **kw}
    r = c.post("/admin/plans", json=body, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def _listing(c, h, n=0):
    return c.post("/owner/listings", headers=h, json={
        "title": f"Limit test {n}", "transaction_type": "BUY", "property_type": "APARTMENT",
        "price_rupees": 5000000, "locality": "Adyar", "city": "Chennai", "pincode": "600020"})


def _audits(session, action):
    return session.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action=:a"), {"a": action}).scalar()


def test_admin_endpoints_refuse_non_admins(client):
    c, session = client
    calls = [("GET", "/admin/plans", None), ("POST", "/admin/plans", {}), ("PATCH", "/admin/plans/1", {}),
             ("POST", "/admin/plans/1/status", {"status": "PAUSED"}), ("GET", "/admin/plan-history", None),
             ("POST", "/admin/users/1/subscription", {"plan_code": "X", "reason": "r"}),
             ("POST", "/admin/subscriptions/1/extend", {"days": 1, "reason": "r"}),
             ("POST", "/admin/subscriptions/1/suspend", {"reason": "r"})]
    for m, p, b in calls:
        assert c.request(m, p, json=b).status_code == 401
        for role in ("BUYER", "OWNER", "BROKER"):
            h, _ = _user(c, role)
            assert c.request(m, p, json=b, headers=h).status_code == 403
    # a Listing Reviewer may not manage plans or subscriptions
    rh, _ = _admin(c, session, "LISTING_REVIEWER")
    assert c.post("/admin/plans", json={}, headers=rh).status_code in (403, 422)
    assert c.post("/admin/users/1/subscription", json={"plan_code": "X", "reason": "r"}, headers=rh).status_code == 403


def test_plan_lifecycle_versions_and_validation(client):
    c, session = client
    ah, _ = _admin(c, session)
    p = _plan(c, ah, "OWNER", listing_limit=3, features=["Unlimited Leads"])
    assert p["status"] == "ACTIVE" and p["version"] == 1 and p["listing_limit"] == 3
    r = c.patch(f"/admin/plans/{p['id']}", headers=ah, json={"price_paise": 99900, "change_note": "price rise"})
    assert r.status_code == 200 and r.json()["version"] == 2 and r.json()["price_paise"] == 99900
    hist = c.get(f"/admin/plan-history?plan_id={p['id']}", headers=ah).json()
    assert [v["version"] for v in hist["items"]] == [2, 1] and hist["items"][0]["change_note"] == "price rise"
    assert hist["items"][1]["snapshot"]["price_paise"] == 49900
    assert c.post(f"/admin/plans/{p['id']}/status", headers=ah, json={"status": "PAUSED"}).json()["status"] == "PAUSED"
    assert c.post(f"/admin/plans/{p['id']}/status", headers=ah, json={"status": "ACTIVE"}).json()["status"] == "ACTIVE"
    assert c.post(f"/admin/plans/{p['id']}/status", headers=ah, json={"status": "ARCHIVED"}).json()["status"] == "ARCHIVED"
    listed = [x["id"] for x in c.get("/admin/plans?role=OWNER", headers=ah).json()["items"]]
    assert p["id"] not in listed
    assert p["id"] in [x["id"] for x in c.get("/admin/plans?role=OWNER&include_archived=true", headers=ah).json()["items"]]
    assert c.patch(f"/admin/plans/{p['id']}", headers=ah, json={"name": "x"}).status_code == 422
    assert c.post(f"/admin/plans/{p['id']}/status", headers=ah, json={"status": "ACTIVE"}).status_code == 422
    # invalid data
    for bad in ({"code": "lower case", "role": "OWNER", "name": "x", "price_paise": 1},
                {"code": "TEST_BAD_ROLE", "role": "ADMIN", "name": "x", "price_paise": 1},
                {"code": "TEST_NEG", "role": "OWNER", "name": "x", "price_paise": -1},
                {"code": "TEST_NO_NAME", "role": "OWNER", "name": "", "price_paise": 1}):
        assert c.post("/admin/plans", json=bad, headers=ah).status_code == 422
    dup = c.post("/admin/plans", headers=ah, json={"code": p["code"], "role": "OWNER", "name": "d", "price_paise": 1})
    assert dup.status_code == 422
    assert _audits(session, "PLAN_CREATED") >= 1 and _audits(session, "PLAN_UPDATED") >= 1


def test_admin_assign_upgrade_downgrade_and_owner_limits(client):
    c, session = client
    ah, admin_id = _admin(c, session)
    oh, owner_id = _user(c, "OWNER")
    startup = _plan(c, ah, "OWNER", name="Startup", listing_limit=1)
    super_ = _plan(c, ah, "OWNER", name="Super", listing_limit=3, duration_days=45)

    r = c.post(f"/admin/users/{owner_id}/subscription", headers=ah, json={"plan_code": startup["code"], "reason": "trial"})
    assert r.status_code == 200, r.text
    s1 = r.json()
    assert s1["plan"]["code"] == startup["code"] and s1["source"] == "ADMIN" and s1["payment_verified"] is False
    me = c.get("/subscriptions/me", headers=oh).json()["subscriptions"]["OWNER"]
    assert me["subscription"]["plan"]["code"] == startup["code"] and me["usage"]["listing_limit"] == 1

    assert _listing(c, oh, 1).status_code == 201
    blocked = _listing(c, oh, 2)
    assert blocked.status_code == 402 and blocked.json()["detail"]["code"] == "OWNER_PLAN_LIMIT"

    # upgrade -> limit follows; one current owner plan; history kept
    s2 = c.post(f"/admin/users/{owner_id}/subscription", headers=ah,
                json={"plan_code": super_["code"], "reason": "upgrade"}).json()
    assert s2["plan"]["code"] == super_["code"]
    me = c.get("/subscriptions/me", headers=oh).json()["subscriptions"]["OWNER"]
    assert me["subscription"]["id"] == s2["id"] and me["usage"]["listings_remaining"] == 2
    assert _listing(c, oh, 3).status_code == 201
    rows = session.execute(text("SELECT id, status FROM subscriptions WHERE user_id=:u ORDER BY id"), {"u": owner_id}).fetchall()
    assert [r[1] for r in rows] == ["EXPIRED", "ACTIVE"]
    # downgrade back
    s3 = c.post(f"/admin/users/{owner_id}/subscription", headers=ah,
                json={"plan_code": startup["code"], "reason": "downgrade"}).json()
    assert s3["plan"]["code"] == startup["code"]
    assert _listing(c, oh, 4).status_code == 402
    assert session.execute(text("SELECT COUNT(*) FROM subscriptions WHERE user_id=:u AND status='ACTIVE'"),
                           {"u": owner_id}).scalar() == 1
    pay = session.execute(text("SELECT provider, status, paid_at FROM payments WHERE id=(SELECT payment_id FROM subscriptions WHERE id=:s)"),
                          {"s": s3["id"]}).fetchone()
    assert pay[0] == "ADMIN" and pay[1] == "INITIATED" and pay[2] is None
    audit = session.execute(text("SELECT user_id FROM audit_logs WHERE action='SUBSCRIPTION_ASSIGNED' ORDER BY id DESC LIMIT 1")).scalar()
    assert audit == admin_id
    # validation
    assert c.post(f"/admin/users/{owner_id}/subscription", headers=ah, json={"plan_code": "NOPE", "reason": "x"}).status_code == 404
    c.post(f"/admin/plans/{super_['id']}/status", headers=ah, json={"status": "PAUSED"})
    assert c.post(f"/admin/users/{owner_id}/subscription", headers=ah,
                  json={"plan_code": super_["code"], "reason": "x"}).status_code == 422


def test_extend_suspend_reactivate_cancel(client):
    c, session = client
    ah, _ = _admin(c, session)
    oh, owner_id = _user(c, "OWNER")
    p = _plan(c, ah, "OWNER", listing_limit=1)
    s = c.post(f"/admin/users/{owner_id}/subscription", headers=ah, json={"plan_code": p["code"], "reason": "r"}).json()
    ext = c.post(f"/admin/subscriptions/{s['id']}/extend", headers=ah, json={"days": 10, "reason": "goodwill"}).json()
    assert ext["end_date"] > s["end_date"]
    assert c.post(f"/admin/subscriptions/{s['id']}/suspend", headers=ah, json={"reason": "abuse"}).json()["status"] == "SUSPENDED"
    me = c.get("/subscriptions/me", headers=oh).json()["subscriptions"]["OWNER"]
    assert me["subscription"] is None  # suspended plan is not current
    assert c.get("/admin/subscriptions?status=SUSPENDED&user_id=%d" % owner_id, headers=ah).json()["total"] == 1
    assert c.post(f"/admin/subscriptions/{s['id']}/reactivate", headers=ah).json()["status"] == "ACTIVE"
    assert c.post(f"/admin/subscriptions/{s['id']}/cancel", headers=ah, json={"reason": "refund"}).json()["status"] == "CANCELLED"
    assert c.post(f"/admin/subscriptions/{s['id']}/cancel", headers=ah, json={"reason": "again"}).status_code == 422
    assert c.post(f"/admin/subscriptions/{s['id']}/suspend", headers=ah, json={"reason": ""}).status_code == 422
    for a in ("SUBSCRIPTION_EXTENDED", "SUBSCRIPTION_SUSPEND", "SUBSCRIPTION_REACTIVATE", "SUBSCRIPTION_CANCEL"):
        assert _audits(session, a) >= 1


def test_payment_bypass_activation(client):
    c, session = client
    ah, _ = _admin(c, session)
    oh, owner_id = _user(c, "OWNER")
    bh, _ = _user(c, "BUYER")
    p = _plan(c, ah, "OWNER", listing_limit=2)
    # role must match the caller's current role (server-side)
    assert c.post("/subscriptions/bypass", headers=bh, json={"plan_code": p["code"]}).status_code == 403
    assert c.post("/subscriptions/bypass", json={"plan_code": p["code"]}).status_code == 401
    r1 = c.post("/subscriptions/bypass", headers=oh, json={"plan_code": p["code"]})
    assert r1.status_code == 200, r1.text
    a = r1.json()
    assert a["source"] == "BYPASS" and a["payment_provider"] == "BYPASS" and a["payment_verified"] is False
    assert a["already_active"] is False
    r2 = c.post("/subscriptions/bypass", headers=oh, json={"plan_code": p["code"]}).json()
    assert r2["id"] == a["id"] and r2["already_active"] is True  # no duplicate
    assert session.execute(text("SELECT COUNT(*) FROM subscriptions WHERE user_id=:u"), {"u": owner_id}).scalar() == 1
    pay = session.execute(text("SELECT status, paid_at FROM payments WHERE user_id=:u"), {"u": owner_id}).fetchall()
    assert pay == [("INITIATED", None)]  # never a successful / paid payment
    adm = c.get(f"/admin/payments?user_id={owner_id}", headers=ah).json()["items"][0]
    assert adm["provider"] == "BYPASS" and adm["simulated"] is True and adm["provider_payment_id"] is None
    me = c.get("/subscriptions/me", headers=oh).json()["subscriptions"]["OWNER"]
    assert me["subscription"]["plan"]["code"] == p["code"] and me["usage"]["listing_limit"] == 2
    # paused / hidden plans cannot be bypass-activated
    c.post(f"/admin/plans/{p['id']}/status", headers=ah, json={"status": "PAUSED"})
    oh2, _ = _user(c, "OWNER")
    assert c.post("/subscriptions/bypass", headers=oh2, json={"plan_code": p["code"]}).status_code == 404
    # public catalogue lists only active, visible plans
    public = [x["code"] for x in c.get("/subscriptions/plans?role=OWNER").json()["items"]]
    assert p["code"] not in public


def test_buyer_limit_follows_admin_plan_edit(client):
    c, session = client
    ah, _ = _admin(c, session)
    bh, buyer_id = _user(c, "BUYER")
    p = _plan(c, ah, "BUYER", listing_limit=None, lead_limit=5)
    c.post(f"/admin/users/{buyer_id}/subscription", headers=ah, json={"plan_code": p["code"], "reason": "r"})
    st = c.get("/buyer/plan", headers=bh).json()
    assert st["plan_code"] == p["code"] and st["lead_limit"] == 5
    c.patch(f"/admin/plans/{p['id']}", headers=ah, json={"lead_limit": 12})
    assert c.get("/buyer/plan", headers=bh).json()["lead_limit"] == 12
    c.patch(f"/admin/plans/{p['id']}", headers=ah, json={"lead_limit": -1})
    assert c.get("/buyer/plan", headers=bh).json()["lead_limit"] is None
    subs = c.get(f"/admin/subscriptions?role=BUYER&plan_code={p['code']}", headers=ah).json()
    assert subs["total"] == 1 and subs["items"][0]["source"] == "ADMIN" and subs["items"][0]["simulated"] is True


def test_bypass_kill_switch(client, monkeypatch):
    c, session = client
    ah, _ = _admin(c, session)
    oh, owner_id = _user(c, "OWNER")
    p = _plan(c, ah, "OWNER", listing_limit=1)
    monkeypatch.setenv("RESTAMP_PAYMENT_BYPASS", "0")
    r = c.post("/subscriptions/bypass", headers=oh, json={"plan_code": p["code"]})
    assert r.status_code == 403
    assert session.execute(text("SELECT COUNT(*) FROM subscriptions WHERE user_id=:u"), {"u": owner_id}).scalar() == 0
    monkeypatch.setenv("RESTAMP_PAYMENT_BYPASS", "1")
    assert c.post("/subscriptions/bypass", headers=oh, json={"plan_code": p["code"]}).status_code == 200


def test_my_payments_own_rows_only_never_paid_for_bypass(client):
    c, session = client
    ah, _ = _admin(c, session)
    oh, owner_id = _user(c, "OWNER")
    other, other_id = _user(c, "OWNER")
    p = _plan(c, ah, "OWNER", listing_limit=1)
    assert c.get("/subscriptions/payments").status_code == 401
    c.post("/subscriptions/bypass", headers=oh, json={"plan_code": p["code"]})
    c.post(f"/admin/users/{other_id}/subscription", headers=ah, json={"plan_code": p["code"], "reason": "r"})
    mine = c.get("/subscriptions/payments", headers=oh).json()
    assert mine["total"] == 1
    row = mine["items"][0]
    assert row["provider"] == "BYPASS" and row["paid"] is False and row["transaction_ref"] is None
    assert row["paid_at"] is None and row["plan_role"] == "OWNER" and row["subscription_id"]
    theirs = c.get("/subscriptions/payments", headers=other).json()["items"]
    assert [r["provider"] for r in theirs] == ["ADMIN"] and theirs[0]["paid"] is False
