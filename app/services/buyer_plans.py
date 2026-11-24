"""RESTAMP buyer plans — phone-reveal (lead) quota.

A "lead" is one DISTINCT property whose owner phone the buyer revealed.
Re-revealing a property already counted in the current window is free.

Tiers (validity from activation; FREE is a rolling window):
    RESTAMP FREE       0 leads..3     free     15 days (rolling)
    RESTAMP PLUS       up to 10       ₹1,499   30 days
    RESTAMP PRO        up to 30       ₹2,499   60 days
    RESTAMP UNLIMITED  unlimited      ₹4,999   90 days

Usage is derived from contact_reveal_audits (one row per successful reveal),
so no separate counter can drift from what was actually revealed.

PAYMENT IS SIMULATED for subscribe_simulated(): it records a
provider="SIMULATED" payment and activates the plan without charging anyone.
The real gateway flow (Razorpay order → signed payment → subscription) lives
in services/payments.py and activates through activate_for_payment() below.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..config import is_development
from ..models import ContactRevealAudit, Payment, Plan, Subscription


@dataclass(frozen=True)
class BuyerPlan:
    code: str
    name: str
    price_paise: int
    duration_days: int
    lead_limit: int | None  # None = unlimited
    leads_label: str


FREE = BuyerPlan("RESTAMP_BUYER_FREE", "RESTAMP FREE", 0, 15, 3, "0 – 3 leads")
PAID = (
    BuyerPlan("RESTAMP_BUYER_PLUS", "RESTAMP PLUS", 149900, 30, 10, "4 – 10 leads"),
    BuyerPlan("RESTAMP_BUYER_PRO", "RESTAMP PRO", 249900, 60, 30, "11 – 30 leads"),
    BuyerPlan("RESTAMP_BUYER_UNLIMITED", "RESTAMP UNLIMITED", 499900, 90, None, "Unlimited leads"),
)
ALL = (FREE, *PAID)
BY_CODE = {p.code: p for p in ALL}
PAID_CODES = tuple(p.code for p in PAID)

PLAN_REQUIRED_CODE = "BUYER_PLAN_REQUIRED"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def _plan_row(db: Session, plan: BuyerPlan) -> Plan:
    """Get-or-create the catalogue row for a buyer plan. An existing row is never
    overwritten: once it exists, admins manage its name/price/limits
    (services/subscriptions.py) and the row is the source of truth."""
    row = db.query(Plan).filter(Plan.code == plan.code).first()
    if row is None:
        row = Plan(
            code=plan.code,
            name=plan.name,
            description=plan.leads_label,
            price_paise=plan.price_paise,
            duration_days=plan.duration_days,
            status="ACTIVE",
            role="BUYER",
            lead_limit=plan.lead_limit,
        )
        db.add(row)
        db.flush()
    return row


def _from_row(row: Plan) -> BuyerPlan:
    """The buyer plan as currently stored (admin edits apply). Rows created
    before role/limit columns existed fall back to the code catalogue."""
    known = BY_CODE.get(row.code)
    if row.role is None and known is not None:
        return known
    limit = row.lead_limit if row.lead_limit is None or row.lead_limit >= 0 else None
    label = "Unlimited leads" if limit is None else f"Up to {limit} leads"
    return BuyerPlan(row.code, row.name, row.price_paise, row.duration_days or 0, limit, row.description or label)


def _active_paid(db: Session, buyer_id: int):
    """Latest ACTIVE, unexpired paid buyer subscription → (Subscription, BuyerPlan) or None."""
    now = _now()
    row = (
        db.query(Subscription, Plan)
        .join(Plan, Plan.id == Subscription.plan_id)
        .filter(
            Subscription.user_id == buyer_id,
            Subscription.status == "ACTIVE",
            # Any buyer plan (code catalogue or admin-created), never the free tier.
            or_(Plan.role == "BUYER", Plan.code.in_(PAID_CODES)),
            Plan.code != FREE.code,
            Subscription.suspended_at.is_(None),
            or_(Subscription.end_date.is_(None), Subscription.end_date > now),
        )
        .order_by(Subscription.start_date.desc(), Subscription.id.desc())
        .first()
    )
    if row is None:
        return None
    sub, plan_row = row
    return sub, _from_row(plan_row)


def _window(db: Session, buyer_id: int):
    """(plan, window_start, window_end, subscription|None) for the buyer's current plan."""
    active = _active_paid(db, buyer_id)
    if active:
        sub, plan = active
        return plan, sub.start_date, sub.end_date, sub
    now = _now()
    return FREE, now - timedelta(days=FREE.duration_days), None, None


def _revealed_ids(db: Session, buyer_id: int, since: datetime) -> set[int]:
    rows = (
        db.query(ContactRevealAudit.property_listing_id)
        .filter(
            ContactRevealAudit.user_id == buyer_id,
            ContactRevealAudit.revealed_at >= since,
            ContactRevealAudit.property_listing_id.isnot(None),
        )
        .distinct()
        .all()
    )
    return {r[0] for r in rows}


def status_for(db: Session, buyer_id: int) -> dict:
    plan, start, end, _sub = _window(db, buyer_id)
    used = len(_revealed_ids(db, buyer_id, start))
    limit = plan.lead_limit
    return {
        "plan_code": plan.code,
        "plan_name": plan.name,
        "is_free": plan is FREE,
        "lead_limit": limit,
        "leads_used": used,
        "leads_remaining": None if limit is None else max(limit - used, 0),
        "valid_until": end,
    }


def ensure_can_reveal(
    db: Session, buyer_id: int, listing_id: int, phone_last4: str | None = None
) -> None:
    """Raise 402 BUYER_PLAN_REQUIRED when revealing a NEW property exceeds the quota.

    The 402 may carry `phone_last4` (masked teaser) — never the full number.
    """
    plan, start, _end, _sub = _window(db, buyer_id)
    if plan.lead_limit is None:
        return
    revealed = _revealed_ids(db, buyer_id, start)
    if listing_id in revealed:
        return  # already counted in this window — re-reveal is free
    if len(revealed) >= plan.lead_limit:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "code": PLAN_REQUIRED_CODE,
                "message": (
                    f"You've used all {plan.lead_limit} leads on {plan.name}. "
                    "Choose a RESTAMP buyer plan to view more owner numbers."
                ),
                "plan_code": plan.code,
                "lead_limit": plan.lead_limit,
                "leads_used": len(revealed),
                "phone_last4": phone_last4,
            },
        )


def catalogue(db: Session) -> list[dict]:
    return [
        {
            "code": p.code,
            "name": p.name,
            "price_paise": p.price_paise,
            "duration_days": p.duration_days,
            "lead_limit": p.lead_limit,
            "leads_label": p.leads_label,
            "is_free": p is FREE,
        }
        for p in ALL
    ]


SIMULATED_REFUSED_DETAIL = (
    "Paid plans can only be activated through a verified online payment."
)


def require_simulated_purchase_allowed() -> None:
    """Refuse SIMULATED paid-plan activation outside an explicit development
    environment (RESTAMP_ENV local/development/dev/test).

    Fail closed: production, staging, an unset or unknown RESTAMP_ENV are all
    refused. The decision is server-side only (no request field, header or
    query flag is consulted), so a client can never re-enable free paid
    plans. Existing subscriptions are untouched: this only stops NEW
    simulated activations; payment-verified activation (services/payments.py)
    is unaffected."""
    if not is_development():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=SIMULATED_REFUSED_DETAIL)


def subscribe_simulated(db: Session, buyer_id: int, code: str) -> dict:
    """Activate a paid buyer plan with a SIMULATED payment (no money is charged).
    Development environments only (see require_simulated_purchase_allowed)."""
    require_simulated_purchase_allowed()
    plan = BY_CODE.get(code)
    if plan is None or plan is FREE:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    row = _plan_row(db, plan)
    now = _now()
    payment = Payment(
        user_id=buyer_id,
        provider="SIMULATED",
        provider_payment_id=f"SIM-{uuid.uuid4().hex[:16]}",
        # The plan row is admin-managed: record its current price, not the code default.
        amount_paise=row.price_paise,
        currency="INR",
        status="SUCCESS",
        plan_id=row.id,
        idempotency_key=uuid.uuid4().hex,
        paid_at=now,
    )
    db.add(payment)
    db.flush()
    # One active buyer plan at a time: the new purchase replaces the old one.
    _expire_previous(db, buyer_id)
    db.add(
        Subscription(
            user_id=buyer_id,
            plan_id=row.id,
            payment_id=payment.id,
            start_date=now,
            end_date=now + timedelta(days=row.duration_days or plan.duration_days),
            status="ACTIVE",
        )
    )
    db.flush()
    result = status_for(db, buyer_id)
    result["transaction_id"] = payment.provider_payment_id
    result["amount_paise"] = plan.price_paise
    result["simulated"] = True
    return result


def _expire_previous(db: Session, buyer_id: int) -> None:
    """Expire the buyer's current ACTIVE paid subscription (a new purchase
    replaces the old one)."""
    (
        db.query(Subscription)
        .filter(
            Subscription.user_id == buyer_id,
            Subscription.status == "ACTIVE",
            Subscription.plan_id.in_(
                db.query(Plan.id).filter(Plan.code.in_(PAID_CODES)).scalar_subquery()
            ),
        )
        .update({Subscription.status: "EXPIRED"}, synchronize_session=False)
    )


def activate_for_payment(db: Session, payment: Payment, plan: BuyerPlan) -> dict:
    """Activate a paid buyer plan from a verified payment row.

    Used by the real payment flow (services/payments.py) after the provider
    signature is verified; subscribe_simulated keeps its own inline path.
    Idempotent: an ACTIVE subscription already tied to this payment returns
    the current status (activated=False) without creating a duplicate. The
    caller must hold the payment row locked (FOR UPDATE) so a concurrent
    verify and webhook cannot both activate.
    """
    existing = (
        db.query(Subscription)
        # ANY subscription for this payment (even one later replaced/expired):
        # a re-sent verify or webhook must never activate the same payment twice.
        .filter(Subscription.payment_id == payment.id)
        .first()
    )
    if existing is not None:
        result = status_for(db, payment.user_id)
        result["transaction_id"] = payment.provider_payment_id
        result["amount_paise"] = payment.amount_paise
        result["activated"] = False
        return result
    now = _now()
    _expire_previous(db, payment.user_id)
    db.add(
        Subscription(
            user_id=payment.user_id,
            plan_id=payment.plan_id,
            payment_id=payment.id,
            start_date=now,
            end_date=now + timedelta(days=plan.duration_days),
            status="ACTIVE",
        )
    )
    db.flush()
    result = status_for(db, payment.user_id)
    result["transaction_id"] = payment.provider_payment_id
    result["amount_paise"] = payment.amount_paise
    result["activated"] = True
    return result
