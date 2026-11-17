"""RESTAMP owner (seller) plans — saved per user, independent of the current role.

A user can hold an owner plan and an agent plan at the same time; buying one
never removes the other. Codes and prices mirror the app's Owner Plans screen
(src/features/owner/data/ownerPlans.js).

PAYMENT IS SIMULATED: subscribe_simulated() records a provider="SIMULATED"
payment and activates the plan without charging anyone. Replace with a real
gateway (order -> webhook-confirmed payment -> subscription) before billing.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from ..models import Payment, Plan, Subscription


@dataclass(frozen=True)
class OwnerPlan:
    code: str
    app_id: str
    name: str
    price_paise: int
    duration_days: int
    listing_limit: int | None  # None = unlimited


PLANS = (
    OwnerPlan("RESTAMP_OWNER_STARTUP", "owner-startup", "Startup Plan", 49900, 30, 1),
    OwnerPlan("RESTAMP_OWNER_SUPER", "owner-super", "Super Plan", 99900, 45, 3),
    OwnerPlan("RESTAMP_OWNER_PREMIUM", "owner-premium", "Premium Plan", 179900, 60, 5),
    OwnerPlan("RESTAMP_OWNER_ELITE", "owner-elite", "Elite Plan", 249900, 90, 20),
    OwnerPlan("RESTAMP_OWNER_SPECIAL", "owner-special", "Special Plan", 349900, 90, 30),
    OwnerPlan("RESTAMP_OWNER_BUSINESS", "owner-business", "Business Plan", 4999900, 90, 50),
    OwnerPlan("RESTAMP_OWNER_UNLIMITED", "owner-unlimited-business", "Unlimited Business Plan", 9999900, 180, None),
)
BY_APP_ID = {p.app_id: p for p in PLANS}
BY_CODE = {p.code: p for p in PLANS}
CODES = tuple(BY_CODE)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def _plan_row(db: Session, plan: OwnerPlan) -> Plan:
    row = db.query(Plan).filter(Plan.code == plan.code).first()
    if row is None:
        row = Plan(
            code=plan.code,
            name=plan.name,
            description="Owner plan",
            price_paise=plan.price_paise,
            duration_days=plan.duration_days,
            status="ACTIVE",
        )
        db.add(row)
        db.flush()
    elif (row.name, row.price_paise, row.duration_days) != (plan.name, plan.price_paise, plan.duration_days):
        row.name, row.price_paise, row.duration_days = plan.name, plan.price_paise, plan.duration_days
        db.flush()
    return row


def status_for(db: Session, user_id: int) -> dict:
    """The user's active, unexpired owner plan (active=False when none)."""
    now = _now()
    row = (
        db.query(Subscription, Plan.code)
        .join(Plan, Plan.id == Subscription.plan_id)
        .filter(
            Subscription.user_id == user_id,
            Subscription.status == "ACTIVE",
            Plan.code.in_(CODES),
            (Subscription.end_date.is_(None)) | (Subscription.end_date > now),
        )
        .order_by(Subscription.start_date.desc(), Subscription.id.desc())
        .first()
    )
    if row is None:
        return {"active": False}
    sub, code = row
    plan = BY_CODE[code]
    return {
        "active": True,
        "plan_id": plan.app_id,
        "plan_code": plan.code,
        "name": plan.name,
        "price_paise": plan.price_paise,
        "duration_days": plan.duration_days,
        "listing_limit": plan.listing_limit,
        "start_date": sub.start_date.isoformat() if sub.start_date else None,
        "end_date": sub.end_date.isoformat() if sub.end_date else None,
    }


def subscribe_simulated(db: Session, user_id: int, app_id: str) -> dict:
    """Activate an owner plan with a SIMULATED payment (no money is charged)."""
    plan = BY_APP_ID.get(app_id) or BY_CODE.get(app_id)
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    row = _plan_row(db, plan)
    now = _now()
    payment = Payment(
        user_id=user_id,
        provider="SIMULATED",
        provider_payment_id=f"SIM-{uuid.uuid4().hex[:16]}",
        amount_paise=plan.price_paise,
        currency="INR",
        status="SUCCESS",
        plan_id=row.id,
        idempotency_key=uuid.uuid4().hex,
        paid_at=now,
    )
    db.add(payment)
    db.flush()
    # One active owner plan at a time: a new owner purchase replaces the old
    # owner plan only. Agent and buyer plans are untouched.
    (
        db.query(Subscription)
        .filter(
            Subscription.user_id == user_id,
            Subscription.status == "ACTIVE",
            Subscription.plan_id.in_(db.query(Plan.id).filter(Plan.code.in_(CODES)).scalar_subquery()),
        )
        .update({Subscription.status: "EXPIRED"}, synchronize_session=False)
    )
    db.add(
        Subscription(
            user_id=user_id,
            plan_id=row.id,
            payment_id=payment.id,
            start_date=now,
            end_date=now + timedelta(days=plan.duration_days),
            status="ACTIVE",
        )
    )
    db.flush()
    result = status_for(db, user_id)
    result["transaction_id"] = payment.provider_payment_id
    result["simulated"] = True
    return result
