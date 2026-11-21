"""Real online payments (Razorpay) — verification, webhook and fail-closed.

The suite always runs with the provider UNCONFIGURED (no keys in the test
environment), so every test that needs Razorpay patches `settings` directly
(the service reads the values at call time) and swaps the module's HTTP
client for an in-memory fake. Nothing here talks to the real gateway.

Contract highlights:
- Unconfigured provider: every endpoint 503; nothing recorded, nothing
  activated, nothing simulated.
- A subscription is activated only after a verified signature (checkout HMAC
  or webhook HMAC) and an order-amount cross-check — never from a frontend
  response alone.
- verify and webhook are idempotent and race-safe: the second caller sees
  activated=False and no duplicate subscription/event.
- Invoices exist only for SUCCESS payments; other users' payments 404.
"""
import hashlib
import hmac
import json
import uuid

import httpx
import pytest
from sqlalchemy import text

from app.config import settings
from app.services import payments as payments_svc
from tests.conftest import auth_header, make_postcode, register_phone

KEY_ID = "rzp_test_KEYID12345678"
KEY_SECRET = "test_key_secret_0123456789abcdef"
WEBHOOK_SECRET = "test_whsec_0123456789abcdef"


def _phone() -> str:
    _phone.n += 1
    return f"+916400{_phone.n:06d}"


_phone.n = 100000


def _user(c):
    tok = register_phone(c, _phone())
    return tok, auth_header(tok["access_token"])


def _checkout_sig(order_id: str, payment_id: str, secret: str = KEY_SECRET) -> str:
    return hmac.new(secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()


def _webhook_sig(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeRazorpay:
    """`async with client()`-shaped stand-in for httpx.AsyncClient."""

    def __init__(self):
        self.orders: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.post_error: Exception | None = None
        self.get_error: Exception | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        self.calls.append(("POST", url, json))
        if self.post_error is not None:
            raise self.post_error
        order_id = f"order_{len(self.orders) + 1}"
        self.orders[order_id] = {"amount": json["amount"], "notes": dict(json.get("notes") or {})}
        return _Resp(201, {"id": order_id, "amount": json["amount"], "currency": "INR"})

    async def get(self, url):
        self.calls.append(("GET", url, None))
        if self.get_error is not None:
            raise self.get_error
        order_id = url.rsplit("/", 1)[-1]
        order = self.orders.get(order_id)
        if order is None:
            return _Resp(404, {"error": {"description": "not found"}})
        return _Resp(200, {"id": order_id, "amount": order["amount"], "notes": dict(order["notes"])})


@pytest.fixture
def razorpay(monkeypatch):
    """A fully configured provider for the duration of one test."""
    monkeypatch.setattr(settings, "PAYMENT_PROVIDER", "razorpay")
    monkeypatch.setattr(settings, "RAZORPAY_KEY_ID", KEY_ID)
    monkeypatch.setattr(settings, "RAZORPAY_KEY_SECRET", KEY_SECRET)
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", WEBHOOK_SECRET)


@pytest.fixture
def fake_rzp(monkeypatch):
    fake = FakeRazorpay()
    monkeypatch.setattr(payments_svc, "_client", lambda: fake)
    return fake


def _checkout(c, h, kind="buyer", code="RESTAMP_BUYER_PLUS", localities=None):
    body = {"kind": kind, "code": code}
    if localities is not None:
        body["localities"] = localities
    return c.post("/payments/checkout", json=body, headers=h)


def _webhook(c, payload: dict, event_id: str | None = None, signature: str | None = None):
    body = json.dumps(payload).encode()
    headers = {
        "content-type": "application/json",
        "x-razorpay-signature": signature or _webhook_sig(body),
    }
    if event_id:
        headers["x-razorpay-event-id"] = event_id
    return c.post("/payments/webhook", data=body, headers=headers)


def _captured_payload(order_id: str, amount: int, payment_id="pay_TEST1", notes=None):
    return {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": order_id,
                    "amount": amount,
                    "status": "captured",
                    "notes": notes or {},
                }
            }
        },
    }


# --- fail-closed when the provider is not configured -------------------------


def test_unconfigured_provider_fails_closed(client):
    c, session = client
    tok, h = _user(c)
    assert c.get("/payments/config", headers=h).json()["provider"] == "none"

    r = _checkout(c, h)
    assert r.status_code == 503
    assert "not configured" in r.json()["detail"]

    r = c.post("/payments/verify", json={
        "razorpay_order_id": "order_x", "razorpay_payment_id": "pay_x",
        "razorpay_signature": "0" * 64,
    }, headers=h)
    assert r.status_code == 503

    payload = _captured_payload("order_x", 149900)
    body = json.dumps(payload).encode()
    r = c.post("/payments/webhook", data=body, headers={
        "content-type": "application/json",
        "x-razorpay-signature": _webhook_sig(body),
    })
    assert r.status_code == 503

    rows = session.execute(
        text("SELECT COUNT(*) FROM payments WHERE user_id=:u"), {"u": tok["user_id"]}
    ).fetchone()[0]
    assert rows == 0  # nothing recorded, nothing simulated


def test_config_exposes_only_the_public_key(client, razorpay):
    c, _ = client
    tok, h = _user(c)
    r = c.get("/payments/config", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "razorpay" and body["razorpay_key_id"] == KEY_ID
    assert KEY_SECRET not in r.text and WEBHOOK_SECRET not in r.text


# --- checkout ----------------------------------------------------------------


def test_checkout_creates_order_and_initiated_payment(client, razorpay, fake_rzp):
    c, session = client
    tok, h = _user(c)
    r = _checkout(c, h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"] == "razorpay"
    assert body["razorpay_key_id"] == KEY_ID
    assert body["amount_paise"] == 149900 and body["currency"] == "INR"
    assert KEY_SECRET not in r.text

    ((_, url, sent),) = [(m, u, j) for m, u, j in fake_rzp.calls]
    assert url == payments_svc.ORDERS_URL
    assert sent["amount"] == 149900 and sent["currency"] == "INR"
    assert sent["notes"]["kind"] == "buyer" and sent["notes"]["user_id"] == str(tok["user_id"])

    row = session.execute(text(
        "SELECT p.provider, p.status, p.amount_paise, p.provider_order_id, pl.code "
        "FROM payments p JOIN plans pl ON pl.id = p.plan_id WHERE p.user_id=:u"
    ), {"u": tok["user_id"]}).fetchone()
    assert row.provider == "RAZORPAY" and row.status == "INITIATED"
    assert row.amount_paise == 149900
    assert row.provider_order_id == body["razorpay_order_id"]
    assert row.code == "RESTAMP_BUYER_PLUS"


def test_checkout_rejects_unknown_free_and_bad_kind(client, razorpay, fake_rzp):
    c, _ = client
    tok, h = _user(c)
    assert _checkout(c, h, code="NOPE").status_code == 404
    assert _checkout(c, h, code="RESTAMP_BUYER_FREE").status_code == 404
    # Phase 2: "owner" is a valid kind (admin-managed OWNER plan rows); a
    # non-owner code under it is simply not found. Unknown kinds stay 422.
    assert _checkout(c, h, kind="owner", code="RESTAMP_BUYER_PLUS").status_code == 404
    assert _checkout(c, h, kind="seller", code="RESTAMP_BUYER_PLUS").status_code == 422
    assert fake_rzp.calls == []  # no gateway call for any invalid request


def test_checkout_agent_validates_localities_before_the_order(client, razorpay, fake_rzp):
    c, session = client
    tok, h = _user(c)
    r = _checkout(c, h, kind="agent", code="RESTAMP_AGENT_STARTER", localities=["Nowhere Land"])
    assert r.status_code == 422
    assert fake_rzp.calls == []

    pid = make_postcode(session, tag=f"-pay{uuid.uuid4().hex[:6]}")
    name = session.execute(text(
        "SELECT l.name FROM postcodes p JOIN localities l ON l.id=p.locality_id WHERE p.id=:p"
    ), {"p": pid}).scalar()
    r = _checkout(c, h, kind="agent", code="RESTAMP_AGENT_STARTER", localities=[name])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plan"]["locality_limit"] == 5
    ((_, _, sent),) = [(m, u, j) for m, u, j in fake_rzp.calls if m == "POST"]
    assert sent["notes"]["localities"] == name


def test_checkout_gateway_unreachable_writes_nothing(client, razorpay, fake_rzp):
    c, session = client
    tok, h = _user(c)
    fake_rzp.post_error = httpx.ConnectError("boom")
    r = _checkout(c, h)
    assert r.status_code == 502
    # The abandoned INSERT was flushed into the shared test session's
    # savepoint; a rollback discards exactly that uncommitted work —
    # proving nothing was persisted for the failed order.
    session.rollback()
    rows = session.execute(
        text("SELECT COUNT(*) FROM payments WHERE user_id=:u"), {"u": tok["user_id"]}
    ).fetchone()[0]
    assert rows == 0  # the failed order leaves nothing behind


# --- verify ------------------------------------------------------------------


def test_verify_activates_buyer_plan_and_is_idempotent(client, razorpay, fake_rzp):
    c, session = client
    tok, h = _user(c)
    checkout = _checkout(c, h).json()
    order_id, amount = checkout["razorpay_order_id"], checkout["amount_paise"]

    r = c.post("/payments/verify", json={
        "razorpay_order_id": order_id,
        "razorpay_payment_id": "pay_OK1",
        "razorpay_signature": _checkout_sig(order_id, "pay_OK1"),
    }, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "SUCCESS" and body["activated"] is True
    assert body["plan_name"] == "RESTAMP PLUS" and body["lead_limit"] == 10

    row = session.execute(text(
        "SELECT status, provider_payment_id, paid_at FROM payments WHERE user_id=:u"
    ), {"u": tok["user_id"]}).fetchone()
    assert row.status == "SUCCESS" and row.provider_payment_id == "pay_OK1" and row.paid_at is not None

    subs = session.execute(text(
        "SELECT COUNT(*) FROM subscriptions WHERE payment_id IN "
        "(SELECT id FROM payments WHERE user_id=:u)"
    ), {"u": tok["user_id"]}).fetchone()[0]
    assert subs == 1

    # A retry (double submit) must not create a second subscription.
    r2 = c.post("/payments/verify", json={
        "razorpay_order_id": order_id,
        "razorpay_payment_id": "pay_OK1",
        "razorpay_signature": _checkout_sig(order_id, "pay_OK1"),
    }, headers=h)
    assert r2.status_code == 200 and r2.json()["activated"] is False
    subs = session.execute(text(
        "SELECT COUNT(*) FROM subscriptions WHERE payment_id IN "
        "(SELECT id FROM payments WHERE user_id=:u)"
    ), {"u": tok["user_id"]}).fetchone()[0]
    assert subs == 1


def test_verify_rejects_a_bad_signature_and_waits_for_the_webhook(client, razorpay, fake_rzp):
    c, session = client
    tok, h = _user(c)
    checkout = _checkout(c, h).json()
    r = c.post("/payments/verify", json={
        "razorpay_order_id": checkout["razorpay_order_id"],
        "razorpay_payment_id": "pay_OK1",
        "razorpay_signature": "0" * 64,
    }, headers=h)
    assert r.status_code == 400
    row = session.execute(text(
        "SELECT status FROM payments WHERE user_id=:u"), {"u": tok["user_id"]}
    ).fetchone()
    assert row.status == "INITIATED"  # unconfirmed, not failed: the signed webhook decides
    subs = session.execute(text(
        "SELECT COUNT(*) FROM subscriptions WHERE payment_id IN "
        "(SELECT id FROM payments WHERE user_id=:u)"
    ), {"u": tok["user_id"]}).fetchone()[0]
    assert subs == 0


def test_verify_crosschecks_the_order_amount(client, razorpay, fake_rzp):
    c, session = client
    tok, h = _user(c)
    checkout = _checkout(c, h).json()
    order_id = checkout["razorpay_order_id"]
    fake_rzp.orders[order_id]["amount"] = 1  # gateway disagrees with our order
    r = c.post("/payments/verify", json={
        "razorpay_order_id": order_id,
        "razorpay_payment_id": "pay_OK1",
        "razorpay_signature": _checkout_sig(order_id, "pay_OK1"),
    }, headers=h)
    assert r.status_code == 409
    row = session.execute(text(
        "SELECT status FROM payments WHERE user_id=:u"), {"u": tok["user_id"]}
    ).fetchone()
    assert row.status == "INITIATED"


def test_verify_requires_the_owner_and_a_known_order(client, razorpay, fake_rzp):
    c, _ = client
    tok_a, h_a = _user(c)
    tok_b, h_b = _user(c)
    checkout = _checkout(c, h_a).json()
    order_id = checkout["razorpay_order_id"]
    sig = _checkout_sig(order_id, "pay_OK1")

    # Another user cannot verify someone else's order.
    r = c.post("/payments/verify", json={
        "razorpay_order_id": order_id, "razorpay_payment_id": "pay_OK1", "razorpay_signature": sig,
    }, headers=h_b)
    assert r.status_code == 404

    # Unknown orders 404 too.
    r = c.post("/payments/verify", json={
        "razorpay_order_id": "order_missing", "razorpay_payment_id": "pay_OK1",
        "razorpay_signature": _checkout_sig("order_missing", "pay_OK1"),
    }, headers=h_a)
    assert r.status_code == 404


# --- webhook -----------------------------------------------------------------


def test_webhook_captures_and_activates_buyer_plan(client, razorpay, fake_rzp):
    c, session = client
    tok, h = _user(c)
    checkout = _checkout(c, h).json()
    order_id, amount = checkout["razorpay_order_id"], checkout["amount_paise"]

    r = _webhook(c, _captured_payload(order_id, amount, "pay_WH1"), event_id="evt_1")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"

    row = session.execute(text(
        "SELECT status, provider_payment_id FROM payments WHERE user_id=:u"
    ), {"u": tok["user_id"]}).fetchone()
    assert row.status == "SUCCESS" and row.provider_payment_id == "pay_WH1"
    subs = session.execute(text(
        "SELECT COUNT(*) FROM subscriptions WHERE payment_id IN "
        "(SELECT id FROM payments WHERE user_id=:u)"
    ), {"u": tok["user_id"]}).fetchone()[0]
    assert subs == 1

    event = session.execute(text(
        "SELECT processing_status, processed_at, payload FROM payment_webhook_events "
        "WHERE provider_event_id='evt_1'"
    )).fetchone()
    assert event.processing_status == "COMPLETED" and event.processed_at is not None
    # The stored payload is minimal — no contact details from the entity.
    stored = json.loads(event.payload)
    assert set(stored.keys()) == {"event", "payment_id", "order_id", "amount", "status"}

    # Replay of the same event id: acknowledged, never processed twice.
    r2 = _webhook(c, _captured_payload(order_id, amount, "pay_WH1"), event_id="evt_1")
    assert r2.status_code == 200 and r2.json()["duplicate"] is True
    subs = session.execute(text(
        "SELECT COUNT(*) FROM subscriptions WHERE payment_id IN "
        "(SELECT id FROM payments WHERE user_id=:u)"
    ), {"u": tok["user_id"]}).fetchone()[0]
    assert subs == 1


def test_webhook_rejects_a_bad_signature(client, razorpay, fake_rzp):
    c, session = client
    tok, h = _user(c)
    checkout = _checkout(c, h).json()
    r = _webhook(c, _captured_payload(checkout["razorpay_order_id"], 149900), signature="0" * 64)
    assert r.status_code == 400
    assert session.execute(text(
        "SELECT COUNT(*) FROM payment_webhook_events WHERE provider_event_id IN "
        "('evt_1','evt_2')"
    )).fetchone()[0] == 0
    row = session.execute(text(
        "SELECT status FROM payments WHERE user_id=:u"), {"u": tok["user_id"]}
    ).fetchone()
    assert row.status == "INITIATED"


def test_webhook_amount_mismatch_never_activates(client, razorpay, fake_rzp):
    c, session = client
    tok, h = _user(c)
    checkout = _checkout(c, h).json()
    r = _webhook(c, _captured_payload(checkout["razorpay_order_id"], 999, "pay_WH2"), event_id="evt_2")
    assert r.status_code == 200
    event = session.execute(text(
        "SELECT processing_status, error_message FROM payment_webhook_events "
        "WHERE provider_event_id='evt_2'"
    )).fetchone()
    assert event.processing_status == "FAILED" and event.error_message == "amount_mismatch"
    row = session.execute(text(
        "SELECT status FROM payments WHERE user_id=:u"), {"u": tok["user_id"]}
    ).fetchone()
    assert row.status == "INITIATED"  # not activated on a mismatched amount
    assert session.execute(text(
        "SELECT COUNT(*) FROM subscriptions WHERE payment_id IN "
        "(SELECT id FROM payments WHERE user_id=:u)"
    ), {"u": tok["user_id"]}).fetchone()[0] == 0


def test_webhook_payment_failed_marks_only_initiated_payments(client, razorpay, fake_rzp):
    c, session = client
    tok, h = _user(c)
    checkout = _checkout(c, h).json()
    order_id = checkout["razorpay_order_id"]
    failed = {
        "event": "payment.failed",
        "payload": {"payment": {"entity": {
            "id": "pay_F1", "order_id": order_id, "amount": 149900, "status": "failed",
        }}},
    }
    assert _webhook(c, failed, event_id="evt_f1").status_code == 200
    row = session.execute(text(
        "SELECT status FROM payments WHERE user_id=:u"), {"u": tok["user_id"]}
    ).fetchone()
    assert row.status == "FAILED"


def test_webhook_ignores_unknown_orders(client, razorpay, fake_rzp):
    c, session = client
    r = _webhook(c, _captured_payload("order_not_ours", 149900, "pay_X1"), event_id="evt_x1")
    assert r.status_code == 200 and r.json()["ignored"] is True
    assert session.execute(text(
        "SELECT COUNT(*) FROM payment_webhook_events WHERE provider_event_id='evt_x1'"
    )).fetchone()[0] == 0


def test_webhook_alone_activates_an_agent_plan_from_notes(client, razorpay, fake_rzp):
    c, session = client
    tok, h = _user(c)
    pid = make_postcode(session, tag=f"-wa{uuid.uuid4().hex[:6]}")
    name = session.execute(text(
        "SELECT l.name FROM postcodes p JOIN localities l ON l.id=p.locality_id WHERE p.id=:p"
    ), {"p": pid}).scalar()
    checkout = _checkout(c, h, kind="agent", code="RESTAMP_AGENT_STARTER", localities=[name]).json()
    order_id = checkout["razorpay_order_id"]
    notes = fake_rzp.orders[order_id]["notes"]

    # No /verify call: the webhook is the only confirmation.
    r = _webhook(c, _captured_payload(order_id, 299900, "pay_AG1", notes=notes), event_id="evt_a1")
    assert r.status_code == 200, r.text
    row = session.execute(text(
        "SELECT status FROM payments WHERE user_id=:u"), {"u": tok["user_id"]}
    ).fetchone()
    assert row.status == "SUCCESS"
    grants = session.execute(text(
        "SELECT COUNT(*) FROM broker_postcode_access_current WHERE broker_user_id=:u"
    ), {"u": tok["user_id"]}).fetchone()[0]
    assert grants >= 1  # coverage came from the order notes
    role = session.execute(text(
        "SELECT role FROM user_current_role WHERE user_id=:u"), {"u": tok["user_id"]}
    ).fetchone()
    assert role is not None and role.role == "BROKER"


def test_webhook_activation_failure_is_recorded_and_recoverable(client, razorpay, fake_rzp):
    c, session = client
    tok, h = _user(c)
    pid = make_postcode(session, tag=f"-wf{uuid.uuid4().hex[:6]}")
    name = session.execute(text(
        "SELECT l.name FROM postcodes p JOIN localities l ON l.id=p.locality_id WHERE p.id=:p"
    ), {"p": pid}).scalar()
    checkout = _checkout(c, h, kind="agent", code="RESTAMP_AGENT_STARTER", localities=[name]).json()
    order_id = checkout["razorpay_order_id"]
    notes = fake_rzp.orders[order_id]["notes"]

    # The locality is admin-disabled before the money lands.
    session.execute(text("UPDATE localities SET agent_enabled=0 WHERE id="
                         "(SELECT locality_id FROM postcodes WHERE id=:p)"), {"p": pid})

    r = _webhook(c, _captured_payload(order_id, 299900, "pay_AG2", notes=notes), event_id="evt_a2")
    assert r.status_code == 200 and r.json()["activation_failed"] is True
    # Money was received: the payment is SUCCESS — but no subscription yet.
    row = session.execute(text(
        "SELECT status FROM payments WHERE user_id=:u"), {"u": tok["user_id"]}
    ).fetchone()
    assert row.status == "SUCCESS"
    event = session.execute(text(
        "SELECT processing_status FROM payment_webhook_events WHERE provider_event_id='evt_a2'"
    )).fetchone()
    assert event.processing_status == "FAILED"
    assert session.execute(text(
        "SELECT COUNT(*) FROM subscriptions WHERE payment_id IN "
        "(SELECT id FROM payments WHERE user_id=:u)"
    ), {"u": tok["user_id"]}).fetchone()[0] == 0

    # Re-enabling the locality and retrying via /payments/verify recovers.
    session.execute(text("UPDATE localities SET agent_enabled=1 WHERE id="
                         "(SELECT locality_id FROM postcodes WHERE id=:p)"), {"p": pid})
    r = c.post("/payments/verify", json={
        "razorpay_order_id": order_id,
        "razorpay_payment_id": "pay_AG2",
        "razorpay_signature": _checkout_sig(order_id, "pay_AG2"),
    }, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["activated"] is True


# --- history and invoices ----------------------------------------------------


def test_history_lists_own_payments_only(client, razorpay, fake_rzp):
    c, session = client
    tok_a, h_a = _user(c)
    tok_b, h_b = _user(c)
    _checkout(c, h_a)  # INITIATED
    checkout = _checkout(c, h_b).json()
    _webhook(c, _captured_payload(checkout["razorpay_order_id"], checkout["amount_paise"],
                                  "pay_H1"), event_id="evt_h1")

    items = c.get("/payments/history", headers=h_a).json()["items"]
    assert len(items) == 1
    assert items[0]["status"] == "INITIATED" and items[0]["plan_name"] == "RESTAMP PLUS"

    items_b = c.get("/payments/history", headers=h_b).json()["items"]
    assert len(items_b) == 1
    assert items_b[0]["status"] == "SUCCESS" and items_b[0]["provider_payment_id"] == "pay_H1"


def test_invoice_requires_a_success_payment_and_ownership(client, razorpay, fake_rzp):
    c, session = client
    tok_a, h_a = _user(c)
    tok_b, h_b = _user(c)
    checkout = _checkout(c, h_a).json()
    payment_id = checkout["payment_id"]
    order_id = checkout["razorpay_order_id"]

    # INITIATED → no invoice yet.
    r = c.get(f"/payments/{payment_id}/invoice", headers=h_a)
    assert r.status_code == 409

    _webhook(c, _captured_payload(order_id, checkout["amount_paise"], "pay_I1"), event_id="evt_i1")

    r = c.get(f"/payments/{payment_id}/invoice", headers=h_a)
    assert r.status_code == 200, r.text
    inv = r.json()
    assert inv["invoice_number"].startswith("RS-")
    assert inv["plan"]["code"] == "RESTAMP_BUYER_PLUS"
    assert inv["amount_paise"] == 149900 and inv["currency"] == "INR"
    assert inv["payment"]["provider_payment_id"] == "pay_I1"
    assert inv["buyer"]["user_id"] == tok_a["user_id"] and inv["buyer"]["name"]

    # Another user cannot see it at all.
    assert c.get(f"/payments/{payment_id}/invoice", headers=h_b).status_code == 404


# --- the simulated flows stay intact ------------------------------------------


def test_simulated_flows_unchanged_by_the_refactor(client):
    c, session = client
    tok, h = _user(c)
    r = c.post("/buyer/plans/RESTAMP_BUYER_PLUS/subscribe", headers=h)
    assert r.status_code == 200 and r.json()["simulated"] is True
    pay = session.execute(text(
        "SELECT provider, status FROM payments WHERE user_id=:u"), {"u": tok["user_id"]}
    ).fetchone()
    assert pay.provider == "SIMULATED" and pay.status == "SUCCESS"


def test_retry_after_failed_attempt_in_same_order_activates(client, razorpay, fake_rzp):
    """Razorpay allows a second attempt inside the same order: a payment.failed
    for attempt 1 must not block a verified, captured attempt 2 (the customer
    was charged). Unverified data still activates nothing."""
    c, session = client
    tok, h = _user(c)
    checkout = _checkout(c, h).json()
    order_id = checkout["razorpay_order_id"]
    failed = {"event": "payment.failed", "payload": {"payment": {"entity": {
        "id": "pay_R1", "order_id": order_id, "amount": 149900, "status": "failed"}}}}
    assert _webhook(c, failed, event_id="evt_r1").status_code == 200

    def subs():
        return session.execute(text(
            "SELECT COUNT(*) FROM subscriptions WHERE payment_id=:p"), {"p": checkout["payment_id"]}
        ).fetchone()[0]

    bad = c.post("/payments/verify", json={"razorpay_order_id": order_id, "razorpay_payment_id": "pay_R2",
                                           "razorpay_signature": "0" * 64}, headers=h)
    assert bad.status_code == 400 and subs() == 0

    r = c.post("/payments/verify", json={"razorpay_order_id": order_id, "razorpay_payment_id": "pay_R2",
                                         "razorpay_signature": _checkout_sig(order_id, "pay_R2")}, headers=h)
    assert r.status_code == 200 and r.json()["activated"] is True
    assert subs() == 1
    # The late captured webhook for the same attempt is a no-op.
    w = _webhook(c, _captured_payload(order_id, 149900, "pay_R2"), event_id="evt_r2")
    assert w.status_code == 200 and subs() == 1
    # A late payment.failed never downgrades the confirmed payment.
    assert _webhook(c, failed, event_id="evt_r3").status_code == 200
    assert session.execute(text("SELECT status FROM payments WHERE id=:p"),
                           {"p": checkout["payment_id"]}).fetchone()[0] == "SUCCESS"
