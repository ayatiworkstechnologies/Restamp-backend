"""Payment hardening regressions (independent audit, 2026-11-16).

- Simulated paid-plan activation is refused outside development: production,
  staging and an unset/unknown RESTAMP_ENV all get 403, no client field can
  re-enable it, nothing is written, and existing subscriptions are untouched.
- Webhook-only agent activation recovers the locality choice from the ORDER
  we created (server-to-server fetch), never from the webhook body; a gateway
  failure records nothing (Razorpay retries); a mismatched order is refused.
- A delivery racing another delivery of the same event gets a clean duplicate
  answer instead of a 500.
- Reva: plan questions, crisis-phrase precision, and provider-error logging.
"""
from __future__ import annotations

import logging
import uuid

import httpx
import pytest
from sqlalchemy import text

from app.config import settings
from app.routers import reva as reva_router
from app.services import payments as payments_svc
from tests.conftest import make_postcode
from tests.test_payments import (  # noqa: F401  (fixtures are used by name)
    _captured_payload,
    _checkout,
    _checkout_sig,
    _user,
    _webhook,
    fake_rzp,
    razorpay,
)


def _sub_rows(session, user_id):
    return session.execute(text(
        "SELECT id, status, end_date FROM subscriptions WHERE user_id=:u ORDER BY id"), {"u": user_id}
    ).fetchall()


def _payment_count(session, user_id):
    return session.execute(text("SELECT COUNT(*) FROM payments WHERE user_id=:u"), {"u": user_id}).scalar()


def _locality(session, tag):
    pid = make_postcode(session, tag=f"-{tag}{uuid.uuid4().hex[:6]}")
    return session.execute(text(
        "SELECT l.name FROM postcodes p JOIN localities l ON l.id=p.locality_id WHERE p.id=:p"), {"p": pid}
    ).scalar()


# --------------------------------------------------------------------------
# Simulated subscriptions: development only
# --------------------------------------------------------------------------
@pytest.mark.parametrize("env", ["production", "prod", "staging", "", "demo-ish"])
def test_simulated_buyer_subscribe_refused_outside_development(client, monkeypatch, env):
    c, session = client
    tok, h = _user(c)
    monkeypatch.setattr(settings, "ENV", env)
    # Client-controlled "flags" in query/body/header must not matter.
    r = c.post("/buyer/plans/RESTAMP_BUYER_UNLIMITED/subscribe?simulated=1&env=local",
               json={"simulated": True, "env": "local"},
               headers={**h, "X-Restamp-Env": "local"})
    assert r.status_code == 403
    assert r.json()["detail"] == "Paid plans can only be activated through a verified online payment."
    assert _payment_count(session, tok["user_id"]) == 0
    assert _sub_rows(session, tok["user_id"]) == []
    plan = c.get("/buyer/plan", headers=h).json()
    assert plan["plan_code"] == "RESTAMP_BUYER_FREE" and plan["is_free"] is True


@pytest.mark.parametrize("env", ["production", "staging", ""])
def test_simulated_agent_subscribe_refused_outside_development(client, monkeypatch, env):
    c, session = client
    tok, h = _user(c)
    name = _locality(session, "sa")
    monkeypatch.setattr(settings, "ENV", env)
    r = c.post("/broker/plans/RESTAMP_AGENT_STARTER/subscribe", json={"localities": [name]}, headers=h)
    assert r.status_code == 403
    assert _payment_count(session, tok["user_id"]) == 0
    assert _sub_rows(session, tok["user_id"]) == []
    assert session.execute(text(
        "SELECT COUNT(*) FROM broker_postcode_access_current WHERE broker_user_id=:u"), {"u": tok["user_id"]}
    ).scalar() == 0


def test_existing_subscription_survives_production_refusal(client, monkeypatch):
    c, session = client
    tok, h = _user(c)
    # Legitimate non-production behaviour still works (development env).
    assert settings.ENV == "local"
    ok = c.post("/buyer/plans/RESTAMP_BUYER_PLUS/subscribe", headers=h)
    assert ok.status_code == 200 and ok.json()["simulated"] is True
    before = _sub_rows(session, tok["user_id"])
    assert len(before) == 1 and before[0].status == "ACTIVE"

    monkeypatch.setattr(settings, "ENV", "production")
    r = c.post("/buyer/plans/RESTAMP_BUYER_UNLIMITED/subscribe", headers=h)
    assert r.status_code == 403
    assert _sub_rows(session, tok["user_id"]) == before  # not revoked, not modified
    plan = c.get("/buyer/plan", headers=h).json()
    assert plan["plan_code"] == "RESTAMP_BUYER_PLUS"


def test_payment_verified_activation_still_works_in_production(client, monkeypatch, razorpay, fake_rzp):
    c, session = client
    tok, h = _user(c)
    monkeypatch.setattr(settings, "ENV", "production")
    co = _checkout(c, h).json()
    r = c.post("/payments/verify", json={
        "razorpay_order_id": co["razorpay_order_id"], "razorpay_payment_id": "pay_PV1",
        "razorpay_signature": _checkout_sig(co["razorpay_order_id"], "pay_PV1")}, headers=h)
    assert r.status_code == 200 and r.json()["activated"] is True
    assert c.get("/buyer/plan", headers=h).json()["plan_code"] == "RESTAMP_BUYER_PLUS"


# --------------------------------------------------------------------------
# Agent webhook localities come from OUR order, never the webhook body
# --------------------------------------------------------------------------
def _grants(session, user_id):
    return session.execute(text(
        "SELECT COUNT(*) FROM broker_postcode_access_current WHERE broker_user_id=:u"), {"u": user_id}
    ).scalar()


def test_webhook_only_agent_activation_multiple_localities_from_order(client, razorpay, fake_rzp):
    c, session = client
    tok, h = _user(c)
    names = [_locality(session, "m1"), _locality(session, "m2"), _locality(session, "m3")]
    co = _checkout(c, h, kind="agent", code="RESTAMP_AGENT_STARTER", localities=names).json()
    order_id = co["razorpay_order_id"]

    # Realistic payload: the payment entity does NOT carry the order notes.
    r = _webhook(c, _captured_payload(order_id, 299900, "pay_MW1", notes={}), event_id="evt_mw1")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert _grants(session, tok["user_id"]) == 3
    subs = _sub_rows(session, tok["user_id"])
    assert len(subs) == 1 and subs[0].status == "ACTIVE"

    # Browser verify afterwards: consistent, idempotent, no second activation.
    v = c.post("/payments/verify", json={
        "razorpay_order_id": order_id, "razorpay_payment_id": "pay_MW1",
        "razorpay_signature": _checkout_sig(order_id, "pay_MW1")}, headers=h)
    assert v.status_code == 200 and v.json()["activated"] is False
    assert _grants(session, tok["user_id"]) == 3 and len(_sub_rows(session, tok["user_id"])) == 1

    # Replays are duplicates.
    again = _webhook(c, _captured_payload(order_id, 299900, "pay_MW1"), event_id="evt_mw1")
    assert again.json() == {"status": "ok", "duplicate": True}


def test_webhook_body_notes_are_never_trusted(client, razorpay, fake_rzp):
    c, session = client
    tok, h = _user(c)
    chosen, injected = _locality(session, "t1"), _locality(session, "t2")
    co = _checkout(c, h, kind="agent", code="RESTAMP_AGENT_STARTER", localities=[chosen]).json()
    forged_notes = {"kind": "agent", "plan_code": "RESTAMP_AGENT_STARTER",
                    "user_id": str(tok["user_id"]), "localities": f"{chosen}|{injected}"}
    r = _webhook(c, _captured_payload(co["razorpay_order_id"], 299900, "pay_T1", notes=forged_notes))
    assert r.status_code == 200
    covered = session.execute(text(
        "SELECT l.name FROM broker_postcode_access_current b JOIN postcodes p ON p.id=b.postcode_id "
        "JOIN localities l ON l.id=p.locality_id WHERE b.broker_user_id=:u"), {"u": tok["user_id"]}
    ).scalars().all()
    assert covered == [chosen]


def test_webhook_order_not_matching_payment_is_refused(client, razorpay, fake_rzp):
    c, session = client
    tok, h = _user(c)
    name = _locality(session, "nm")
    co = _checkout(c, h, kind="agent", code="RESTAMP_AGENT_STARTER", localities=[name]).json()
    fake_rzp.orders[co["razorpay_order_id"]]["notes"]["user_id"] = "999999999"
    r = _webhook(c, _captured_payload(co["razorpay_order_id"], 299900, "pay_NM1"), event_id="evt_nm1")
    assert r.status_code == 200 and r.json()["activation_failed"] is True
    assert _sub_rows(session, tok["user_id"]) == [] and _grants(session, tok["user_id"]) == 0
    assert session.execute(text(
        "SELECT processing_status FROM payment_webhook_events WHERE provider_event_id='evt_nm1'")
    ).scalar() == "FAILED"


def test_webhook_without_order_localities_never_activates(client, razorpay, fake_rzp):
    c, session = client
    tok, h = _user(c)
    name = _locality(session, "nl")
    co = _checkout(c, h, kind="agent", code="RESTAMP_AGENT_STARTER", localities=[name]).json()
    fake_rzp.orders[co["razorpay_order_id"]]["notes"].pop("localities")
    r = _webhook(c, _captured_payload(co["razorpay_order_id"], 299900, "pay_NL1",
                                      notes={"localities": name}), event_id="evt_nl1")
    assert r.status_code == 200 and r.json()["activation_failed"] is True
    assert _sub_rows(session, tok["user_id"]) == [] and _grants(session, tok["user_id"]) == 0


def test_webhook_gateway_failure_records_nothing_then_retry_activates(client, razorpay, fake_rzp):
    c, session = client
    tok, h = _user(c)
    name = _locality(session, "gf")
    co = _checkout(c, h, kind="agent", code="RESTAMP_AGENT_STARTER", localities=[name]).json()
    fake_rzp.get_error = httpx.ConnectTimeout("down")
    r = _webhook(c, _captured_payload(co["razorpay_order_id"], 299900, "pay_GF1"), event_id="evt_gf1")
    assert r.status_code == 502
    assert session.execute(text(
        "SELECT COUNT(*) FROM payment_webhook_events WHERE provider_event_id='evt_gf1'")).scalar() == 0
    assert session.execute(text("SELECT status FROM payments WHERE id=:p"),
                           {"p": co["payment_id"]}).scalar() == "INITIATED"
    fake_rzp.get_error = None
    r = _webhook(c, _captured_payload(co["razorpay_order_id"], 299900, "pay_GF1"), event_id="evt_gf1")
    assert r.status_code == 200 and _grants(session, tok["user_id"]) == 1


def test_racing_duplicate_webhook_gets_duplicate_not_500(client, razorpay, fake_rzp, monkeypatch):
    """The concurrent winner commits its event row while this delivery awaits
    the order fetch (the exact race window); the unique key must yield a
    clean duplicate answer and leave activation to the winner."""
    c, session = client
    tok, h = _user(c)
    co = _checkout(c, h).json()
    real_fetch = payments_svc._fetch_order

    async def fetch_while_winner_commits(order_id):
        session.execute(text(
            "INSERT INTO payment_webhook_events (payment_id, provider, provider_event_id, event_type, "
            "processing_status) VALUES (:p, 'RAZORPAY', 'evt_race', 'payment.captured', 'COMPLETED')"),
            {"p": co["payment_id"]})
        return await real_fetch(order_id)

    monkeypatch.setattr(payments_svc, "_fetch_order", fetch_while_winner_commits)
    r = _webhook(c, _captured_payload(co["razorpay_order_id"], 149900, "pay_RC1"), event_id="evt_race")
    assert r.status_code == 200 and r.json() == {"status": "ok", "duplicate": True}
    assert _sub_rows(session, tok["user_id"]) == []


def test_payment_responses_match_declared_models(client, razorpay, fake_rzp):
    c, _session = client
    _tok, h = _user(c)
    assert set(c.get("/payments/config", headers=h).json()) == {"provider", "razorpay_key_id"}
    co = _checkout(c, h).json()
    assert "unmatched_localities" not in co and "locality_limit" not in co["plan"]
    assert KEY_SECRET_ABSENT(co)
    v = c.post("/payments/verify", json={
        "razorpay_order_id": co["razorpay_order_id"], "razorpay_payment_id": "pay_RM1",
        "razorpay_signature": _checkout_sig(co["razorpay_order_id"], "pay_RM1")}, headers=h).json()
    assert v["activated"] is True and v["plan_code"] == "RESTAMP_BUYER_PLUS"  # passthrough kept
    inv = c.get(f"/payments/{v['payment_id']}/invoice", headers=h).json()
    assert inv["amount_paise"] == 149900 and inv["seller"]["name"] == "RESTAMP"
    hist = c.get("/payments/history", headers=h).json()
    assert hist["items"][0]["status"] == "SUCCESS"


def KEY_SECRET_ABSENT(payload) -> bool:  # noqa: N802
    return settings.RAZORPAY_KEY_SECRET not in str(payload) and settings.RAZORPAY_WEBHOOK_SECRET not in str(payload)


# --------------------------------------------------------------------------
# Reva
# --------------------------------------------------------------------------
@pytest.mark.parametrize("msg", ["what plans do you sell", "What subscription plans do you sell?",
                                 "pricing for buyers"])
def test_reva_plan_questions_answered_from_catalogue(msg):
    reply = reva_router._demo_reply(msg, "Buyer", "en", {})
    assert reply.startswith("Here are the plans Restamp currently sells online")
    assert "RESTAMP PLUS" in reply


def test_reva_sell_question_still_gets_listing_help():
    assert "7-step owner flow" in reva_router._demo_reply("how do I sell my flat", "Owner", "en", {})


@pytest.mark.parametrize("msg", ["Is this area not safe?", "is the locality not safe at night",
                                 "Is it safe to pay online?", "Which area is safest in Chennai?"])
def test_reva_crisis_footer_not_added_to_property_questions(msg):
    assert reva_router._needs_crisis_footer(msg) is False


@pytest.mark.parametrize("msg", ["I am not safe", "I’m not safe at home", "i want to die", "தற்கொலை"])
def test_reva_crisis_footer_still_added_for_first_person_distress(msg):
    assert reva_router._needs_crisis_footer(msg) is True


def test_reva_provider_error_logs_class_only(client, monkeypatch, caplog):
    c, _session = client
    secret_text = "https://secret-endpoint.example/v1?token=SHOULD_NOT_LOG"

    class Boom:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **k):
            raise httpx.ConnectError(secret_text)

    monkeypatch.setattr(settings, "AGARAM_ENABLED", True)
    monkeypatch.setattr(settings, "AGARAM_ENDPOINT", "http://127.0.0.1:9/v1/chat/completions")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "")
    monkeypatch.setattr(reva_router.httpx, "AsyncClient", Boom)
    with caplog.at_level(logging.DEBUG, logger="restamp"):
        r = c.post("/chat", json={"message": "hello"})
    assert r.status_code == 200 and r.json()["demo"] is True
    assert "Agaram connection error (ConnectError)" in caplog.text
    assert "SHOULD_NOT_LOG" not in caplog.text and "secret-endpoint" not in caplog.text
