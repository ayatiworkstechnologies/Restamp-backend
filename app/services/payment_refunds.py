"""RESTAMP manual-review payment administration: list flagged payments and
refund them through Razorpay (admin-only, audited).

Scope (approved Phase 2 rule): a verified (SUCCESS) payment flagged
activation_review_status=PENDING could not be activated safely (e.g. the
account's poster type changed after checkout). The ONLY supported resolution
is a full refund. Nothing here activates, transfers or re-assigns a plan.

Safety:
- The payment row is locked FOR UPDATE for the whole operation, so two admins
  (or a double click) serialise; one payment_refunds row per payment
  (UNIQUE payment_id) means a second request finds the first.
- A refund is REQUESTED before the gateway call. PROCESSED is recorded ONLY
  from the gateway's own answer ("processed") or a signed refund.processed
  webhook — never assumed. "pending" stays PENDING until the webhook.
- A FAILED attempt may be retried. Before calling the gateway again we list
  the payment's refunds at Razorpay and adopt one carrying our refund id in
  its notes, so a request that reached the gateway but whose answer was lost
  is never refunded twice. (Razorpay also refuses refunds beyond the
  captured amount.)
- The original payment keeps status SUCCESS; on PROCESSED its review status
  becomes RESOLVED. An audit_logs row records every admin request.
- No secrets, card data or full gateway payloads are stored or logged:
  only the refund id, its status and an error code.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from ..models import AuditLog, Payment, PaymentRefund, Plan

log = logging.getLogger("restamp.payments")

REFUND_ALREADY_REQUESTED = "REFUND_ALREADY_REQUESTED"
NOT_UNDER_REVIEW = "PAYMENT_NOT_UNDER_REVIEW"
PAYMENTS_URL = "https://api.razorpay.com/v1/payments"
_ACTIVE = ("REQUESTED", "PENDING", "PROCESSED")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def refund_out(r: PaymentRefund | None) -> dict | None:
    if r is None:
        return None
    return {
        "id": r.id, "payment_id": r.payment_id, "user_id": r.user_id, "status": r.status,
        "amount_paise": r.amount_paise, "provider_refund_id": r.provider_refund_id,
        "gateway_status": r.gateway_status, "failure_code": r.failure_code,
        "intended_poster_type": r.intended_poster_type, "original_payment_status": r.original_payment_status,
        "review_reason": r.review_reason, "admin_user_id": r.admin_user_id, "admin_reason": r.admin_reason,
        "attempts": r.attempts, "requested_at": r.requested_at, "processed_at": r.processed_at,
    }


def _intended_type(plan: Plan | None) -> str | None:
    if plan is None:
        return None
    return plan.role if plan.role in ("OWNER", "BROKER") else None


def list_reviews(db: Session, review_status: str | None = "PENDING", page: int = 1, page_size: int = 20):
    """Payments flagged for manual review (+ their refund), newest first."""
    q = db.query(Payment).filter(Payment.activation_review_status.isnot(None))
    if review_status:
        q = q.filter(Payment.activation_review_status == review_status)
    total = q.count()
    rows = q.order_by(Payment.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    ids = [p.id for p in rows]
    refunds = {r.payment_id: r for r in db.query(PaymentRefund).filter(PaymentRefund.payment_id.in_(ids)).all()} if ids else {}
    plans = {p.id: p for p in db.query(Plan).filter(Plan.id.in_({r.plan_id for r in rows if r.plan_id})).all()} if rows else {}
    items = []
    for p in rows:
        plan = plans.get(p.plan_id)
        items.append({
            "payment_id": p.id, "user_id": p.user_id, "provider": p.provider, "status": p.status,
            "amount_paise": p.amount_paise, "provider_payment_id": p.provider_payment_id, "paid_at": p.paid_at,
            "plan_code": plan.code if plan else None, "intended_poster_type": _intended_type(plan),
            "review_status": p.activation_review_status, "review_reason": p.activation_review_reason,
            "flagged_at": p.activation_review_at, "refund": refund_out(refunds.get(p.id)),
        })
    return items, total


def _apply_gateway_refund(refund: PaymentRefund, payment: Payment, entity: dict) -> None:
    refund.provider_refund_id = str(entity.get("id") or "")[:255] or refund.provider_refund_id
    gw = str(entity.get("status") or "").lower()[:30]
    refund.gateway_status = gw or None
    if gw == "processed":
        refund.status = "PROCESSED"
        refund.processed_at = refund.processed_at or _now()
        refund.failure_code = None
        payment.activation_review_status = "RESOLVED"
    elif gw == "failed":
        refund.status = "FAILED"
        refund.failure_code = "GATEWAY_REFUND_FAILED"
    else:
        refund.status = "PENDING"  # gateway accepted; final state comes by webhook
        refund.failure_code = None


async def request_refund(db: Session, admin_id: int, payment_id: int, reason: str) -> dict:
    from . import payments as pay_svc  # gateway config/client live there

    pay_svc._require_configured()
    reason = (reason or "").strip()
    if not reason:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="A reason is required")
    payment = db.query(Payment).filter(Payment.id == payment_id).with_for_update().first()
    if payment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    refund = db.query(PaymentRefund).filter(PaymentRefund.payment_id == payment.id).first()
    if refund is not None and refund.status in _ACTIVE:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail={"code": REFUND_ALREADY_REQUESTED, "refund": _jsonable(refund_out(refund))})
    if (payment.provider != pay_svc.PROVIDER or payment.status != "SUCCESS"
            or payment.activation_review_status != "PENDING" or not payment.provider_payment_id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail={"code": NOT_UNDER_REVIEW,
                                    "message": "Only a verified payment awaiting manual review can be refunded here."})
    now = _now()
    plan = db.get(Plan, payment.plan_id) if payment.plan_id else None
    if refund is None:
        refund = PaymentRefund(
            payment_id=payment.id, user_id=payment.user_id, plan_id=payment.plan_id,
            intended_poster_type=_intended_type(plan), original_payment_status=payment.status,
            review_reason=payment.activation_review_reason, status="REQUESTED",
            amount_paise=payment.amount_paise, provider=payment.provider, admin_user_id=admin_id,
            admin_reason=reason[:500], attempts=0, requested_at=now,
        )
        db.add(refund)
    else:  # retry of a FAILED attempt
        refund.status, refund.admin_user_id, refund.admin_reason = "REQUESTED", admin_id, reason[:500]
    refund.attempts = (refund.attempts or 0) + 1
    refund.updated_at = now
    db.add(AuditLog(user_id=admin_id, action="PAYMENT_REFUND_REQUESTED", entity=f"payment:{payment.id}",
                    before_json={"payment_status": payment.status, "review_status": payment.activation_review_status},
                    after_json={"refund_attempt": refund.attempts, "amount_paise": payment.amount_paise,
                                "reason": reason[:500]}, created_at=now))
    db.flush()

    async with pay_svc._client() as client:
        try:
            if refund.attempts > 1:
                # Adopt a refund that reached the gateway but whose answer we lost.
                listed = await client.get(f"{PAYMENTS_URL}/{payment.provider_payment_id}/refunds")
                if listed.status_code == 200:
                    for item in (listed.json() or {}).get("items") or []:
                        if str((item.get("notes") or {}).get("restamp_refund_id")) == str(refund.id):
                            _apply_gateway_refund(refund, payment, item)
                            refund.updated_at = _now()
                            db.flush()
                            return refund_out(refund)
            resp = await client.post(
                f"{PAYMENTS_URL}/{payment.provider_payment_id}/refund",
                json={"amount": payment.amount_paise, "receipt": f"restamp_refund_{refund.id}",
                      "notes": {"restamp_refund_id": str(refund.id), "restamp_payment_id": str(payment.id)}},
            )
        except httpx.HTTPError:
            refund.status, refund.failure_code, refund.updated_at = "FAILED", "GATEWAY_UNREACHABLE", _now()
            db.flush()
            log.warning("refund %s: gateway unreachable", refund.id)
            return refund_out(refund)
    if resp.status_code not in (200, 201):
        try:
            code = str(((resp.json() or {}).get("error") or {}).get("code") or "")
        except ValueError:
            code = ""
        refund.status = "FAILED"
        refund.failure_code = (code or f"HTTP_{resp.status_code}")[:100]
        refund.updated_at = _now()
        db.flush()
        log.warning("refund %s: gateway refused (%s)", refund.id, refund.failure_code)
        return refund_out(refund)
    _apply_gateway_refund(refund, payment, resp.json() or {})
    refund.updated_at = _now()
    db.flush()
    return refund_out(refund)


def _jsonable(d: dict | None) -> dict | None:
    if d is None:
        return None
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in d.items()}


def apply_refund_webhook(db: Session, event_type: str, entity: dict) -> str:
    """Signed refund.processed / refund.failed: update OUR refund row only.
    Returns 'ignored' (not our refund), 'duplicate' (already final) or 'ok'.
    Never creates a refund and never touches subscriptions."""
    refund_id = str(entity.get("id") or "")
    if not refund_id:
        return "ignored"
    refund = (db.query(PaymentRefund).filter(PaymentRefund.provider_refund_id == refund_id)
              .with_for_update().first())
    if refund is None:
        return "ignored"
    payment = db.query(Payment).filter(Payment.id == refund.payment_id).with_for_update().first()
    if refund.status == "PROCESSED":
        return "duplicate"
    target = "processed" if event_type == "refund.processed" else "failed" if event_type == "refund.failed" else ""
    if not target:
        return "ignored"
    _apply_gateway_refund(refund, payment, {"id": refund_id, "status": target})
    refund.updated_at = _now()
    db.flush()
    return "ok"
