"""RESTAMP buyer plans — phone-reveal (lead) quota over the real API.

Contract:
- FREE: 3 DISTINCT properties per rolling 30 days; the 4th new property is
  refused with 402 {code: BUYER_PLAN_REQUIRED} and nothing is revealed or
  audited; re-revealing an already-counted property stays free;
- GET /buyer/plans lists the four RESTAMP plans; GET /buyer/plan reports usage;
- POST /buyer/plans/{code}/subscribe activates a paid plan with a SIMULATED
  payment (provider SIMULATED, status SUCCESS) and lifts the quota;
- FREE / unknown codes cannot be "purchased" (404).
"""
from sqlalchemy import text

from tests.conftest import auth_header
from tests.test_owner_leads import audit_count, fresh_buyer, seed_owner_listing


def _reveal(c, h, lid):
    return c.post(f"/buyer/listings/{lid}/contact", headers=h)


def test_catalogue_lists_four_restamp_plans(client):
    c, _ = client
    items = c.get("/buyer/plans").json()["items"]
    assert [(i["name"], i["price_paise"], i["duration_days"], i["lead_limit"]) for i in items] == [
        ("RESTAMP FREE", 0, 30, 3),
        ("RESTAMP PLUS", 149900, 45, 10),
        ("RESTAMP PRO", 249900, 60, 20),
        ("RESTAMP UNLIMITED", 499900, 90, None),
    ]


def test_free_plan_allows_three_distinct_then_402(client):
    c, session = client
    buyer = fresh_buyer(c)
    h = auth_header(buyer["access_token"])
    lids = [seed_owner_listing(session)[0] for _ in range(4)]

    for lid in lids[:3]:
        assert _reveal(c, h, lid).status_code == 200
    plan = c.get("/buyer/plan", headers=h).json()
    assert plan["plan_name"] == "RESTAMP FREE" and plan["leads_used"] == 3 and plan["leads_remaining"] == 0

    fourth_phone = session.execute(
        text("SELECT ai.provider_identifier FROM auth_identities ai "
             "JOIN property_listings pl ON pl.user_id = ai.user_id "
             "WHERE pl.id=:l AND ai.provider='phone'"), {"l": lids[3]}
    ).fetchone()[0]
    r = _reveal(c, h, lids[3])
    assert r.status_code == 402
    detail = r.json()["detail"]
    assert detail["code"] == "BUYER_PLAN_REQUIRED"
    # Only the last 4 digits are disclosed — never the full number.
    assert detail["phone_last4"] == fourth_phone[-4:]
    assert fourth_phone not in r.text and fourth_phone[:-4] not in r.text
    assert audit_count(session, buyer["user_id"], lids[3]) == 0  # nothing revealed

    # Re-revealing an already counted property stays free.
    assert _reveal(c, h, lids[0]).status_code == 200


def test_simulated_purchase_lifts_quota(client):
    c, session = client
    buyer = fresh_buyer(c)
    h = auth_header(buyer["access_token"])
    lids = [seed_owner_listing(session)[0] for _ in range(4)]
    for lid in lids[:3]:
        assert _reveal(c, h, lid).status_code == 200
    # Pre-purchase reveals happened before activation.
    session.execute(
        text("UPDATE contact_reveal_audits SET revealed_at = revealed_at - INTERVAL 1 MINUTE WHERE user_id=:u"),
        {"u": buyer["user_id"]},
    )

    r = c.post("/buyer/plans/RESTAMP_BUYER_PLUS/subscribe", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plan_name"] == "RESTAMP PLUS" and body["lead_limit"] == 10 and body["simulated"] is True
    pay = session.execute(
        text("SELECT provider, status, amount_paise FROM payments WHERE user_id=:u"), {"u": buyer["user_id"]}
    ).fetchall()
    assert [tuple(p) for p in pay] == [("SIMULATED", "SUCCESS", 149900)]

    assert _reveal(c, h, lids[3]).status_code == 200


def test_free_and_unknown_codes_cannot_be_purchased(client):
    c, _ = client
    h = auth_header(fresh_buyer(c)["access_token"])
    assert c.post("/buyer/plans/RESTAMP_BUYER_FREE/subscribe", headers=h).status_code == 404
    assert c.post("/buyer/plans/NOPE/subscribe", headers=h).status_code == 404


def test_owner_viewing_own_listing_gets_number_without_lead_or_quota(client):
    """An owner switched to BUYER mode revealing their OWN listing gets their
    number back (200) — no 422, no lead, no audit, no quota use."""
    from tests.test_owner_leads import enquiry_count, owner_header

    c, session = client
    lid, owner_id, phone = seed_owner_listing(session)
    h, uid = owner_header(c, phone)
    c.post("/users/me/role", json={"role": "BUYER"}, headers=h)
    r = _reveal(c, h, lid)
    assert r.status_code == 200, r.text
    assert r.json()["phone"] == phone
    assert enquiry_count(session, uid, lid) == 0
    assert audit_count(session, uid, lid) == 0
    assert c.get("/buyer/plan", headers=h).json()["leads_used"] == 0
