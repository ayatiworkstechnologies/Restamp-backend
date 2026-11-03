"""RESTAMP agent (BROKER) plans — locality coverage subscriptions.

Tiers (validity from activation), mirroring the app's Agent Plan cards:
    AGENT STARTER   5 localities   ₹2,999   30 days
    AGENT PRO      10 localities   ₹6,999   30 days
    AGENT ELITE    15 localities  ₹14,999   30 days

Buying a plan (subscribe_simulated):
  1. records a payment + an ACTIVE subscription (one active agent plan at a
     time; a new purchase expires the previous one),
  2. replaces the agent's coverage with grants for every postcode of the
     chosen localities, expiring with the subscription,
  3. switches the account's current role to BROKER.

So a returning agent is recognised purely from server data: an ACTIVE,
unexpired agent subscription (status_for). Coverage stays the single source
of what the agent may browse (services/brokers.py).

PAYMENT IS SIMULATED, exactly like buyer plans: provider="SIMULATED", no
money is charged. Replace with a real gateway before production billing.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import (
    BrokerPostcodeAccessCurrent,
    BrokerPostcodeAccessHistory,
    Locality,
    Payment,
    Plan,
    Postcode,
    Subscription,
)
from . import brokers as broker_svc
from . import users_roles


@dataclass(frozen=True)
class AgentPlan:
    code: str
    app_id: str  # the id the app uses for this plan card
    name: str
    price_paise: int
    duration_days: int
    locality_limit: int


PLANS = (
    AgentPlan("RESTAMP_AGENT_STARTER", "agent-starter", "Agent Starter Plan", 299900, 30, 5),
    AgentPlan("RESTAMP_AGENT_PRO", "agent-pro", "Agent Pro Plan", 699900, 30, 10),
    AgentPlan("RESTAMP_AGENT_ELITE", "agent-elite", "Agent Elite Plan", 1499900, 30, 15),
)
# Accept either the server code or the app's card id.
BY_KEY = {**{p.code: p for p in PLANS}, **{p.app_id: p for p in PLANS}}
CODES = tuple(p.code for p in PLANS)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def _plan_row(db: Session, plan: AgentPlan) -> Plan:
    """Get-or-create the catalogue row for an agent plan (keeps price/name in sync)."""
    row = db.query(Plan).filter(Plan.code == plan.code).first()
    label = f"{plan.locality_limit} localities"
    if row is None:
        row = Plan(
            code=plan.code,
            name=plan.name,
            description=label,
            price_paise=plan.price_paise,
            duration_days=plan.duration_days,
            status="ACTIVE",
        )
        db.add(row)
        db.flush()
    elif (row.name, row.price_paise, row.duration_days) != (plan.name, plan.price_paise, plan.duration_days):
        row.name, row.price_paise, row.duration_days = plan.name, plan.price_paise, plan.duration_days
        row.description = label
        db.flush()
    return row


def _active(db: Session, user_id: int):
    """Latest ACTIVE, unexpired agent subscription -> (Subscription, AgentPlan) or None."""
    row = (
        db.query(Subscription, Plan.code)
        .join(Plan, Plan.id == Subscription.plan_id)
        .filter(
            Subscription.user_id == user_id,
            Subscription.status == "ACTIVE",
            Plan.code.in_(CODES),
            Subscription.end_date > _now(),
        )
        .order_by(Subscription.start_date.desc(), Subscription.id.desc())
        .first()
    )
    if row is None:
        return None
    sub, code = row
    return sub, BY_KEY[code]


def _resolve_postcodes(db: Session, names: list[str]) -> tuple[list[int], list[str], list[str]]:
    """Locality names -> (postcode ids, matched names, unmatched names).

    Case-insensitive exact name match first; when none, a contains match
    (e.g. "Anna Nagar" also covers "Anna Nagar West"). Only real postcodes
    are ever granted — an unknown name grants nothing.
    """
    postcode_ids: set[int] = set()
    matched: list[str] = []
    unmatched: list[str] = []
    for raw in names:
        name = (raw or "").strip()
        if not name:
            continue
        rows = (
            db.query(Postcode.id)
            .join(Locality, Locality.id == Postcode.locality_id)
            .filter(func.lower(Locality.name) == name.lower())
            .all()
        )
        if not rows:
            rows = (
                db.query(Postcode.id)
                .join(Locality, Locality.id == Postcode.locality_id)
                .filter(Locality.name.ilike(f"%{name}%"))
                .all()
            )
        if rows:
            postcode_ids.update(int(r[0]) for r in rows)
            matched.append(name)
        else:
            unmatched.append(name)
    return sorted(postcode_ids), matched, unmatched


def status_for(db: Session, user_id: int) -> dict:
    """The caller's agent plan (or has_plan=False) plus covered localities."""
    active = _active(db, user_id)
    if active is None:
        return {"has_plan": False}
    sub, plan = active
    locations = broker_svc.list_covered_locations(db, user_id)
    localities = sorted({loc.get("locality") for loc in locations if loc.get("locality")})
    return {
        "has_plan": True,
        "plan_code": plan.code,
        "plan_id": plan.app_id,
        "plan_name": plan.name,
        "price_paise": plan.price_paise,
        "locality_limit": plan.locality_limit,
        "start_date": sub.start_date,
        "valid_until": sub.end_date,
        "localities": localities,
    }


def subscribe_simulated(db: Session, user_id: int, key: str, localities: list[str]) -> dict:
    """Activate an agent plan for the chosen localities. PAYMENT IS SIMULATED."""
    plan = BY_KEY.get(key)
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    names = list(dict.fromkeys(n.strip() for n in localities if n and n.strip()))
    if not names:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Choose at least one locality.",
        )
    if len(names) > plan.locality_limit:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{plan.name} covers up to {plan.locality_limit} localities.",
        )
    postcode_ids, matched, unmatched = _resolve_postcodes(db, names)
    if not postcode_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="None of the chosen localities are served yet.",
        )

    row = _plan_row(db, plan)
    now = _now()
    end = now + timedelta(days=plan.duration_days)
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

    # One active agent plan at a time: the new purchase replaces the old one.
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
            end_date=end,
            status="ACTIVE",
        )
    )

    # Coverage = exactly the chosen localities, expiring with the plan.
    db.query(BrokerPostcodeAccessCurrent).filter(
        BrokerPostcodeAccessCurrent.broker_user_id == user_id
    ).delete(synchronize_session=False)
    for pid in postcode_ids:
        db.add(
            BrokerPostcodeAccessCurrent(
                broker_user_id=user_id,
                postcode_id=pid,
                granted_by=user_id,
                granted_at=now,
                expires_at=end,
            )
        )
        db.add(
            BrokerPostcodeAccessHistory(
                broker_user_id=user_id,
                postcode_id=pid,
                granted_by=user_id,
                granted_at=now,
                status="ACTIVE",
                reason=f"Agent plan purchase ({plan.code})",
            )
        )

    users_roles.set_current_role(db, user_id, "BROKER")
    db.flush()

    result = status_for(db, user_id)
    result["transaction_id"] = payment.provider_payment_id
    result["amount_paise"] = plan.price_paise
    result["unmatched_localities"] = unmatched
    result["simulated"] = True
    return result
