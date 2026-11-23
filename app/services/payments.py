"""Real online payments (Razorpay) for buyer and agent plans.

Flow — a subscription is NEVER activated by a checkout click or by anything
the frontend alone claims:

  1. POST /payments/checkout   validates the plan (and, for agent plans, the
     locality choice server-side), records an INITIATED payment row and
     creates a Razorpay order (amount = the server's plan price, notes carry
     the validated selection). Returns only the PUBLIC key id + order id.
  2. POST /payments/verify     the client relays Razorpay's checkout
     signature (HMAC-SHA256 of "order_id|payment_id" with the SERVER's key
     secret — unforgeable by the client, which never sees the secret). The
     order is then re-fetched server-to-server to confirm the amount, and
     only then is the payment marked SUCCESS and the plan activated.
  3. POST /payments/webhook    Razorpay's signed event is the authoritative
     confirmation (HMAC of the raw body with the webhook secret). It can
     activate a plan on its own and is fully idempotent via the
     payment_webhook_events unique event id. Agent localities are NEVER read
     from the webhook body: the ORDER we created at checkout is re-fetched
     server-to-server (same source as /verify) and its notes must name this
     payment's user and plan. Razorpay's payment entity carries the notes of
     the checkout call, not reliably the order's notes, so the webhook body is
     not a dependable (or trusted) source for the selection.

verify and webhook race safely: both confirm under a FOR UPDATE lock on the
payment row, and the plan services' activate_for_payment() skips activation
when a subscription already references the payment.

Fail-closed posture (mirrors the email providers): PAYMENT_PROVIDER !=
"razorpay" or any missing secret → every endpoint returns 503, nothing is
simulated, no subscription can be created through this module. Secrets are
read only from the server environment and never logged or returned.

The simulated flows (buyer_plans/agent_plans subscribe_simulated) remain the
development/demo paths and are untouched.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Payment, PaymentWebhookEvent, Plan, Subscription, User, UserEmail
from . import agent_plans as agent_plan_svc
from . import capabilities
from . import subscriptions as sub_svc
from . import payment_refunds as refund_svc
from . import contact_credits as credit_svc
from . import buyer_plans as buyer_plan_svc

PROVIDER = "RAZORPAY"
ORDERS_URL = "https://api.razorpay.com/v1/orders"

_NOT_CONFIGURED = "Online payments are not configured yet. Please try again later."


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def configured() -> bool:
    """True only when Razorpay is fully configured (fail closed otherwise)."""
    return (
        settings.PAYMENT_PROVIDER == "razorpay"
        and bool(settings.RAZORPAY_KEY_ID)
        and bool(settings.RAZORPAY_KEY_SECRET)
        and bool(settings.RAZORPAY_WEBHOOK_SECRET)
    )


def _require_configured() -> None:
    if not configured():
        # Never tell the caller which part is missing.
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=_NOT_CONFIGURED)


def _client() -> httpx.AsyncClient:
    """The Razorpay HTTP client (basic auth with the server's key pair).
    Patched in tests; production timeout comes from settings."""
    return httpx.AsyncClient(
        timeout=settings.RAZORPAY_TIMEOUT_SECONDS,
        auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET),
    )


def config_public() -> dict:
    """What the frontend needs to render checkout: provider name and the
    PUBLIC key id. The key secret and webhook secret never leave the server."""
    if configured():
        return {"provider": "razorpay", "razorpay_key_id": settings.RAZORPAY_KEY_ID}
    return {"provider": "none", "razorpay_key_id": None}


def _plan_and_kind(db: Session, kind: str, code: str):
    """Resolve the client's plan choice: (plan_obj, plan_row, kind)."""
    if kind == "buyer":
        plan = buyer_plan_svc.BY_CODE.get(code)
        if plan is None or plan is buyer_plan_svc.FREE:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        return plan, buyer_plan_svc._plan_row(db, plan), "buyer"
    if kind == "agent":
        plan = agent_plan_svc.BY_KEY.get(code)
        if plan is None or plan.code in capabilities.TRIAL_PLAN_CODES:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        return plan, agent_plan_svc._plan_row(db, plan), "agent"
    if kind == "credits":
        # The 25-credit contact pack: only purchasable once an admin has
        # priced and activated it (the commercial price is not decided).
        row = credit_svc.pack_row(db)
        if row is None or code != credit_svc.PACK_CODE or row.status != "ACTIVE" or (row.price_paise or 0) <= 0:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        return row, row, "credits"
    if kind == "owner":
        # Owner plans are admin-managed rows (role OWNER); only a purchasable
        # (ACTIVE, visible, not archived) plan can be bought, at the ROW price.
        row = db.query(Plan).filter(Plan.code == code, Plan.role == "OWNER").first()
        if row is None or row.status != "ACTIVE" or not row.visible or row.archived_at is not None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        return row, row, "owner"
    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid request")


log = logging.getLogger("restamp.payments")
_POSTER_ROLE = {"agent": "BROKER", "owner": "OWNER"}
MANUAL_REVIEW_POSTER_CONFLICT = "POSTER_TYPE_CONFLICT"


def _plan_by_code(db: Session, code: str):
    """(plan_obj, kind) from OUR plan-row code — the authoritative record of
    what was bought (the client's declared kind is only a checkout hint)."""
    if code.startswith("RESTAMP_BUYER_"):
        return buyer_plan_svc.BY_CODE[code], "buyer"
    if code.startswith("RESTAMP_AGENT_"):
        return agent_plan_svc.BY_KEY[code], "agent"
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="This payment is not linked to a purchasable plan.",
    )


async def create_checkout(
    db: Session, user_id: int, kind: str, code: str, localities: list[str] | None = None
) -> dict:
    """Create a Razorpay order for a plan and record the INITIATED payment.

    The amount always comes from the server's plan constants — the client
    never sends an amount. For agent plans the locality choice is validated
    server-side at checkout and recorded in the ORDER notes (the webhook
    path later re-validates the same names). If the gateway call fails,
    nothing is written: the insert rolls back with the raised error.
    """
    _require_configured()
    plan, plan_row, kind = _plan_and_kind(db, kind, code)
    # Owner XOR Dealer: refuse before any order or money exists (409), so a
    # captured payment can never land on an incompatible account type.
    if kind in _POSTER_ROLE:
        capabilities.ensure_compatible(db, user_id, _POSTER_ROLE[kind])

    notes: dict[str, str] = {
        "kind": kind,
        "plan_code": plan.code,
        "user_id": str(user_id),
    }
    result_extra: dict = {}
    if kind == "agent":
        # Validate now; refuse before any order exists. The canonical names
        # (not the raw input) are what the webhook will re-validate.
        _postcode_ids, canonical, unmatched, unavailable = agent_plan_svc._resolve_choice(
            db, plan, list(localities or [])
        )
        notes["localities"] = "|".join(canonical)
        result_extra = {"unmatched_localities": unmatched, "unavailable_localities": unavailable}

    idempotency_key = uuid.uuid4().hex
    payment = Payment(
        user_id=user_id,
        provider=PROVIDER,
        amount_paise=plan.price_paise,
        currency="INR",
        status="INITIATED",
        plan_id=plan_row.id,
        idempotency_key=idempotency_key,
    )
    db.add(payment)
    db.flush()

    async with _client() as client:
        try:
            resp = await client.post(
                ORDERS_URL,
                json={
                    "amount": plan.price_paise,  # the server's price, in paise
                    "currency": "INR",
                    "receipt": idempotency_key[:40],
                    "notes": notes,
                },
            )
        except httpx.HTTPError:
            # Connection/timeout errors: nothing is persisted (the session
            # rolls back), so no half-created orders are left behind.
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="The payment gateway is unreachable. Please try again.",
            )
    if resp.status_code != 201:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The payment gateway rejected the order. Please try again.",
        )
    order = resp.json()
    order_id = order.get("id")
    if not order_id:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The payment gateway returned an unexpected response.",
        )
    payment.provider_order_id = order_id
    db.flush()

    return {
        "payment_id": payment.id,
        "provider": "razorpay",
        "razorpay_key_id": settings.RAZORPAY_KEY_ID,  # PUBLIC checkout key
        "razorpay_order_id": order_id,
        "amount_paise": plan.price_paise,
        "currency": "INR",
        "plan": {
            "kind": kind,
            "code": plan.code,
            "name": plan.name,
            "price_paise": plan.price_paise,
            "duration_days": plan.duration_days or 0,
            **({"locality_limit": plan.locality_limit} if kind == "agent" else {}),
        },
        **result_extra,
    }


def _signature(order_id: str, payment_id: str) -> str:
    """Razorpay checkout signature: HMAC-SHA256 over "order|payment" with the
    SERVER's key secret. The client can relay it but can never compute it."""
    digest = hmac.new(
        settings.RAZORPAY_KEY_SECRET.encode(),
        f"{order_id}|{payment_id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return digest


async def _fetch_order(order_id: str) -> dict:
    """Re-fetch the order server-to-server: the client's relayed data is not
    trusted on its own (only the signature is; the order confirms the amount
    and carries the notes recorded at checkout)."""
    async with _client() as client:
        try:
            resp = await client.get(f"{ORDERS_URL}/{order_id}")
        except httpx.HTTPError:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="The payment gateway is unreachable. Please try again.",
            )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The payment could not be confirmed. Please try again.",
        )
    return resp.json()


def _trusted_order_notes(order: dict, payment: Payment, plan_code: str | None) -> dict:
    """Notes of an order WE created, cross-checked against our payment row.

    The order is fetched with the server's credentials, so its notes are the
    selection recorded at checkout. They must name this payment's user and
    plan; anything else is refused (409) rather than guessed."""
    notes = order.get("notes") or {}
    if not isinstance(notes, dict):
        notes = {}
    if str(notes.get("user_id", "")) != str(payment.user_id) or (
        plan_code is not None and notes.get("plan_code") != plan_code
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This payment's order does not match its record. Contact support.",
        )
    return notes


def _plan_code_of(db: Session, payment: Payment) -> str | None:
    plan_row = db.get(Plan, payment.plan_id) if payment.plan_id else None
    return plan_row.code if plan_row else None


def _confirm_and_activate(db: Session, payment: Payment, provider_payment_id: str, notes: dict) -> dict:
    """Mark the payment SUCCESS and activate its plan — under a row lock, so
    a concurrent webhook and verify serialise and cannot double-activate.
    Returns the plan-activation result (with activated=True/False)."""
    locked = db.query(Payment).filter(Payment.id == payment.id).with_for_update().first()
    if locked is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    # FAILED is not terminal: Razorpay lets the customer retry inside the
    # SAME order, so a failed first attempt (payment.failed webhook) can be
    # followed by a captured second attempt. Both callers reach this point
    # only after a verified signature and an amount cross-check, so money
    # really moved and the plan must be activated (refusing here charged the
    # customer with no plan).
    if locked.status in ("INITIATED", "FAILED"):
        # (provider, provider_payment_id) is unique: a different row already
        # holding this id means the event belongs elsewhere — refuse rather
        # than guess.
        duplicate = (
            db.query(Payment.id)
            .filter(
                Payment.provider == PROVIDER,
                Payment.provider_payment_id == provider_payment_id,
                Payment.id != locked.id,
            )
            .first()
        )
        if duplicate is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This payment is already recorded on another order.",
            )
        locked.status = "SUCCESS"
        locked.provider_payment_id = provider_payment_id
        locked.paid_at = _now()
        db.flush()

    plan_row = db.get(Plan, locked.plan_id) if locked.plan_id else None
    if plan_row is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This payment is not linked to a purchasable plan.",
        )
    if plan_row.role == "OWNER":
        plan_obj, kind = plan_row, "owner"
    elif plan_row.code == credit_svc.PACK_CODE and plan_row.role is None:
        plan_obj, kind = plan_row, "credits"
    else:
        plan_obj, kind = _plan_by_code(db, plan_row.code)

    def _review_result() -> dict:
        return {"payment_id": locked.id, "status": locked.status, "activated": False,
                "manual_review": True, "review_reason": locked.activation_review_reason,
                "transaction_id": locked.provider_payment_id, "amount_paise": locked.amount_paise}

    # Already flagged: retries return the same answer and write nothing.
    if locked.activation_review_status == "PENDING":
        return _review_result()
    # Owner XOR Dealer at activation time. The poster type may have changed
    # since checkout. The payment is real (verified above), so it stays
    # SUCCESS; activating would grant the wrong poster type and raising would
    # roll the SUCCESS record back. Flag it for manual review instead: no
    # subscription, no coverage, no posting right. A payment that already has
    # a subscription is never re-flagged (idempotent replays).
    target = _POSTER_ROLE.get(kind)
    if target is not None:
        has_sub = db.query(Subscription.id).filter(Subscription.payment_id == locked.id).first() is not None
        if not has_sub and not capabilities.try_claim(db, locked.user_id, target):
            locked.activation_review_status = "PENDING"
            locked.activation_review_reason = MANUAL_REVIEW_POSTER_CONFLICT
            locked.activation_review_at = _now()
            db.flush()
            log.warning("payment %s flagged for manual review: poster type conflict", locked.id)
            return _review_result()

    if kind == "buyer":
        result = buyer_plan_svc.activate_for_payment(db, locked, plan_obj)
    elif kind == "owner":
        result = sub_svc.activate_for_payment(db, locked, plan_obj)
    elif kind == "credits":
        result = credit_svc.grant_for_payment(db, locked)
    else:
        localities = [n for n in str(notes.get("localities", "")).split("|") if n]
        result = agent_plan_svc.activate_for_payment(db, locked, plan_obj, localities)
    result["payment_id"] = locked.id
    result["status"] = locked.status
    return result


async def verify(
    db: Session,
    user_id: int,
    razorpay_order_id: str,
    razorpay_payment_id: str,
    razorpay_signature: str,
) -> dict:
    """Verify a checkout payment and activate the plan.

    Signature first (cheap, unforgeable), then the server-to-server order
    fetch for the amount. A bad signature raises 400 and leaves the payment
    INITIATED — a later signed webhook can still confirm it; nothing is
    guessed from the client's data alone."""
    _require_configured()
    row = (
        db.query(Payment)
        .filter(Payment.provider == PROVIDER, Payment.provider_order_id == razorpay_order_id)
        .first()
    )
    if row is None or row.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    expected = _signature(razorpay_order_id, razorpay_payment_id)
    if not hmac.compare_digest(expected, str(razorpay_signature or "")):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Payment verification failed")

    order = await _fetch_order(razorpay_order_id)
    if int(order.get("amount", -1)) != row.amount_paise:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Payment amount does not match the order. Contact support.",
        )
    notes = _trusted_order_notes(order, row, _plan_code_of(db, row))
    result = _confirm_and_activate(db, row, razorpay_payment_id, notes)
    db.flush()
    return result


async def handle_webhook(db: Session, signature: str | None, body: bytes, event_id_header: str | None) -> dict:
    """Process a signed Razorpay webhook. Public endpoint: the ONLY gate is
    the HMAC of the raw body with the webhook secret (64 KB body cap in
    main.py). Idempotent per provider event id; unknown orders are ignored
    (Razorpay gets a 200 either way)."""
    _require_configured()
    expected = hmac.new(settings.RAZORPAY_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid signature")

    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid payload")
    if not isinstance(data, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid payload")

    event_type = str(data.get("event") or "")
    if event_type.startswith("refund."):
        # Admin refunds of manual-review payments (services/payment_refunds):
        # only OUR refund rows are updated; idempotent by the refund's state.
        refund_entity = (data.get("payload") or {}).get("refund", {}).get("entity") or {}
        outcome = refund_svc.apply_refund_webhook(db, event_type, refund_entity)
        if outcome == "ignored":
            return {"status": "ok", "ignored": True}
        if outcome == "duplicate":
            return {"status": "ok", "duplicate": True}
        return {"status": "ok"}
    entity = (data.get("payload") or {}).get("payment", {}).get("entity") or {}
    order_id = entity.get("order_id")
    provider_payment_id = entity.get("id")
    event_id = event_id_header or f"{event_type}:{provider_payment_id or order_id}"

    row = (
        db.query(Payment)
        .filter(Payment.provider == PROVIDER, Payment.provider_order_id == order_id)
        .first()
        if order_id
        else None
    )
    if row is None:
        # Not our order (or not a payment event we track): acknowledge so
        # Razorpay stops retrying; nothing is recorded or changed.
        return {"status": "ok", "ignored": True}

    duplicate = (
        db.query(PaymentWebhookEvent)
        .filter(
            PaymentWebhookEvent.provider == PROVIDER,
            PaymentWebhookEvent.provider_event_id == event_id,
        )
        .first()
    )
    if duplicate is not None:
        return {"status": "ok", "duplicate": True}

    # A capture we may act on: recover the checkout selection from the ORDER
    # we created (server-to-server, our credentials) BEFORE recording the
    # event. A gateway failure raises 502 with nothing recorded, so Razorpay
    # retries the event later. Notes in the webhook body are never used.
    amount_ok = event_type == "payment.captured" and _int_or_none(entity.get("amount")) == row.amount_paise
    order_notes: dict | None = None
    order_error: HTTPException | None = None
    if amount_ok:
        order = await _fetch_order(order_id)
        try:
            if _int_or_none(order.get("amount")) != row.amount_paise:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="order_amount_mismatch")
            order_notes = _trusted_order_notes(order, row, _plan_code_of(db, row))
        except HTTPException as exc:
            order_error = exc

    # Store a MINIMAL payload only: ids, amount, status — never the full
    # entity (it carries buyer contact details we have no reason to keep).
    event = PaymentWebhookEvent(
        payment_id=row.id,
        provider=PROVIDER,
        provider_event_id=event_id[:255],
        event_type=(event_type or "unknown")[:100],
        processing_status="PENDING",
        payload={
            "event": event_type,
            "payment_id": provider_payment_id,
            "order_id": order_id,
            "amount": entity.get("amount"),
            "status": entity.get("status"),
        },
    )
    # Two deliveries of the same event can race past the duplicate check
    # above; the unique (provider, provider_event_id) key decides. The insert
    # runs in a savepoint so the loser gets a clean duplicate answer (MySQL
    # 1062) instead of a 500, and the winner's processing is untouched.
    try:
        with db.begin_nested():
            db.add(event)
            db.flush()
    except IntegrityError as exc:
        if getattr(exc.orig, "args", (None,))[0] != 1062:
            raise
        return {"status": "ok", "duplicate": True}

    try:
        if event_type == "payment.captured":
            if not amount_ok:
                event.processing_status = "FAILED"
                event.error_message = "amount_mismatch"
            elif order_error is not None:
                raise order_error
            else:
                _confirm_and_activate(db, row, provider_payment_id, order_notes or {})
                event.processing_status = "COMPLETED"
        elif event_type == "payment.failed":
            # Never overwrite a confirmed payment: FAILED only from INITIATED.
            if row.status == "INITIATED":
                row.status = "FAILED"
            event.processing_status = "COMPLETED"
        else:
            event.processing_status = "COMPLETED"
    except HTTPException as exc:
        # Money moved but activation failed (e.g. the plan's localities can
        # no longer be granted). The event is marked FAILED and the caller
        # can still re-activate via POST /payments/verify (idempotent).
        event.processing_status = "FAILED"
        event.error_message = str(exc.detail)[:500]
        db.flush()
        return {"status": "ok", "activation_failed": True}
    except Exception:
        # Unexpected errors bubble up (500): Razorpay will retry the event,
        # and the unique event id keeps the retry from double-processing.
        db.rollback()
        raise
    event.processed_at = _now()
    db.flush()
    return {"status": "ok"}


def _int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def history(db: Session, user_id: int) -> list[dict]:
    """The caller's own payments, newest first (real flow + simulated
    purchases alike; both are real records of what the account paid)."""
    rows = (
        db.query(Payment, Plan.code, Plan.name)
        .outerjoin(Plan, Plan.id == Payment.plan_id)
        .filter(Payment.user_id == user_id)
        .order_by(Payment.id.desc())
        .limit(50)
        .all()
    )

    def _dt(value):
        return value.replace(tzinfo=timezone.utc) if value is not None else None

    return [
        {
            "payment_id": p.id,
            "provider": p.provider,
            "status": p.status,
            "amount_paise": p.amount_paise,
            "currency": p.currency,
            "plan_code": code,
            "plan_name": name,
            "provider_payment_id": p.provider_payment_id,
            "provider_order_id": p.provider_order_id,
            "created_at": _dt(p.created_at),
            "paid_at": _dt(p.paid_at),
        }
        for p, code, name in rows
    ]


def invoice(db: Session, user_id: int, payment_id: int) -> dict:
    """Invoice details for ONE SUCCESS payment. Invoices are derived from
    verified payments only — there is no separate invoice table and no way
    to view one for an INITIATED/FAILED payment. Other users' payments 404."""
    row = db.get(Payment, int(payment_id))
    if row is None or row.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if row.status != "SUCCESS":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An invoice is available once the payment has succeeded.",
        )
    plan = db.get(Plan, row.plan_id) if row.plan_id else None
    user = db.get(User, user_id)
    email = (
        db.query(UserEmail.email)
        .filter(UserEmail.user_id == user_id, UserEmail.verified_at.isnot(None))
        .order_by(UserEmail.verified_at.desc())
        .scalar()
    )
    issued = row.paid_at or row.created_at
    return {
        "invoice_number": f"RS-{issued:%Y}-{row.id:06d}",
        "issued_at": issued.replace(tzinfo=timezone.utc),
        "seller": {
            "name": "RESTAMP",
            "support": settings.EMAIL_SUPPORT or None,
        },
        "buyer": {
            "user_id": user_id,
            "name": user.display_name if user else None,
            "email": email,
        },
        "plan": {
            "code": plan.code if plan else None,
            "name": plan.name if plan else None,
            "description": plan.description if plan else None,
        },
        "payment": {
            "provider": row.provider,
            "provider_payment_id": row.provider_payment_id,
            "provider_order_id": row.provider_order_id,
            "paid_at": row.paid_at.replace(tzinfo=timezone.utc) if row.paid_at else None,
        },
        "amount_paise": row.amount_paise,
        "currency": row.currency,
        "note": "This invoice reflects a payment verified by the payment provider.",
    }
