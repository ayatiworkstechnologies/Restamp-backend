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

Locality names are resolved by services/localities.resolve: canonical
records and aliases, spelling-insensitive, no substring matching, and only
localities an admin has left ENABLED can be newly selected. A plan's usage
is counted in distinct places (locality name keys), so "T Nagar" and
"T. Nagar" rows count once.

Unused allowance (add_localities): an agent who bought N localities but
chose fewer can add the rest later under the SAME subscription. No payment
or subscription row is written; the new grants expire with the plan (its
expiry never changes). The limit is enforced here, not only in the app. The
subscription row is locked FOR UPDATE and re-checked, so two concurrent adds
serialise and cannot both pass the limit check. No active plan -> 409, and
the app falls back to the normal purchase flow.

Admin (routers/admin.py, audited): assign localities to an agent within the
same plan limit (add_localities with actor_id), revoke one (revoke_locality:
the grant is removed, its history row is marked REVOKED, the plan is
untouched), and view an agent's coverage (admin_coverage).

PAYMENT IS SIMULATED, exactly like buyer plans: provider="SIMULATED", no
money is charged. Replace with a real gateway before production billing.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..models import (
    AuditLog,
    BrokerPostcodeAccessCurrent,
    BrokerPostcodeAccessHistory,
    Locality,
    Payment,
    PhysicalProperty,
    Plan,
    Postcode,
    PropertyListing,
    Subscription,
    User,
)
from . import brokers as broker_svc
from . import localities as locality_svc
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

# Agent free trial: ONE area, no payment, no expiry (duration None), owner
# number reveals unlimited (the buyer reveal quota never applies to agents).
# Started once per account with start_trial; never purchasable through
# subscribe_simulated, and upgrading to a paid tier replaces it.
TRIAL = AgentPlan("RESTAMP_AGENT_TRIAL", "agent-trial", "Agent Free Trial", 0, None, 1)
BY_CODE = {p.code: p for p in (*PLANS, TRIAL)}
CODES = tuple(BY_CODE)

AREA_REQUIRED_CODE = "AGENT_AREA_NOT_IN_PLAN"
TRIAL_AREA_MESSAGE = (
    "Your free trial includes properties in one area. "
    "Upgrade your plan to explore properties in more areas."
)
PLAN_AREA_MESSAGE = "This property is outside the areas in your agent plan. Upgrade your plan to add more areas."


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
            # The free trial has no end date; paid plans must not be expired.
            or_(Subscription.end_date.is_(None), Subscription.end_date > _now()),
        )
        .order_by(Subscription.start_date.desc(), Subscription.id.desc())
        .first()
    )
    if row is None:
        return None
    sub, code = row
    return sub, BY_CODE[code]


NOT_SERVED = "None of the chosen localities are served yet."
NOT_AVAILABLE = "None of the chosen localities are available for agents."


def _clean_names(localities: list[str]) -> list[str]:
    return list(dict.fromkeys(n.strip() for n in localities if n and n.strip()))


def _covered(db: Session, user_id: int) -> dict[str, str]:
    """{place key: display name} the agent covers now (unexpired grants)."""
    return locality_svc.postcode_keys(db, broker_svc.active_postcode_ids(db, user_id))


def _split(resolved: list) -> tuple[list, list[str], list[str]]:
    ok = [r for r in resolved if r.status == "ok"]
    unknown = [r.requested for r in resolved if r.status == "unknown"]
    unavailable = [r.requested for r in resolved if r.status == "unavailable"]
    return ok, unknown, unavailable


def _refuse_when_nothing_usable(ok: list, unavailable: list[str]) -> None:
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=NOT_AVAILABLE if unavailable else NOT_SERVED,
        )


def status_for(db: Session, user_id: int) -> dict:
    """The caller's agent plan (or has_plan=False) plus covered localities."""
    active = _active(db, user_id)
    if active is None:
        return {"has_plan": False}
    sub, plan = active
    localities = sorted(_covered(db, user_id).values())
    return {
        "has_plan": True,
        "plan_code": plan.code,
        "plan_id": plan.app_id,
        "plan_name": plan.name,
        "price_paise": plan.price_paise,
        "locality_limit": plan.locality_limit,
        "start_date": sub.start_date,
        "valid_until": sub.end_date,
        "is_trial": plan.code == TRIAL.code,
        "localities": localities,
        "localities_remaining": max(0, plan.locality_limit - len(localities)),
    }


def subscribe_simulated(db: Session, user_id: int, key: str, localities: list[str]) -> dict:
    """Activate an agent plan for the chosen localities. PAYMENT IS SIMULATED."""
    plan = BY_KEY.get(key)
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    names = _clean_names(localities)
    if not names:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Choose at least one locality.",
        )
    resolved = locality_svc.resolve(db, names)
    ok, unmatched, unavailable = _split(resolved)
    if len(names) > plan.locality_limit or len({r.key for r in ok}) > plan.locality_limit:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{plan.name} covers up to {plan.locality_limit} localities.",
        )
    _refuse_when_nothing_usable(ok, unavailable)
    postcode_ids = sorted({pid for r in ok for pid in r.postcode_ids})
    if len(locality_svc.postcode_keys(db, postcode_ids)) > plan.locality_limit:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{plan.name} covers up to {plan.locality_limit} localities.",
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

    # Coverage = exactly the chosen localities, expiring with the plan. The
    # replaced grants stay visible in history (marked EXPIRED, not erased).
    (
        db.query(BrokerPostcodeAccessHistory)
        .filter(
            BrokerPostcodeAccessHistory.broker_user_id == user_id,
            BrokerPostcodeAccessHistory.status == "ACTIVE",
        )
        .update(
            {
                BrokerPostcodeAccessHistory.status: "EXPIRED",
                BrokerPostcodeAccessHistory.revoked_at: now,
                BrokerPostcodeAccessHistory.revoked_by: user_id,
            },
            synchronize_session=False,
        )
    )
    db.query(BrokerPostcodeAccessCurrent).filter(
        BrokerPostcodeAccessCurrent.broker_user_id == user_id
    ).delete(synchronize_session=False)
    _grant(db, user_id, user_id, postcode_ids, now, end, f"Agent plan purchase ({plan.code})")

    users_roles.set_current_role(db, user_id, "BROKER")
    db.flush()

    result = status_for(db, user_id)
    result["transaction_id"] = payment.provider_payment_id
    result["amount_paise"] = plan.price_paise
    result["unmatched_localities"] = unmatched
    result["unavailable_localities"] = unavailable
    result["simulated"] = True
    return result


def _grant(db: Session, user_id: int, actor_id: int, postcode_ids, now, end, reason: str) -> None:
    for pid in postcode_ids:
        db.add(
            BrokerPostcodeAccessCurrent(
                broker_user_id=user_id,
                postcode_id=pid,
                granted_by=actor_id,
                granted_at=now,
                expires_at=end,
            )
        )
        db.add(
            BrokerPostcodeAccessHistory(
                broker_user_id=user_id,
                postcode_id=pid,
                granted_by=actor_id,
                granted_at=now,
                status="ACTIVE",
                reason=reason,
            )
        )


def _lock_active(db: Session, user_id: int):
    """Active plan with its subscription row locked FOR UPDATE, re-checked
    after the lock (a concurrent purchase may have replaced it). 409 if none."""
    active = _active(db, user_id)
    if active is not None:
        sub, plan = active
        sub = db.query(Subscription).filter(Subscription.id == sub.id).with_for_update().one()
        db.refresh(sub)
        if sub.status == "ACTIVE" and (sub.end_date is None or sub.end_date > _now()):
            return sub, plan
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No active agent plan")


def add_localities(db: Session, user_id: int, localities: list[str], *, actor_id: int | None = None) -> dict:
    """Add localities to the agent's ACTIVE plan, within its limit.

    No payment and no new subscription: the unused allowance was already paid
    for, and the plan's expiry is unchanged. Already-covered places are
    skipped (no duplicate grants). Unknown and admin-disabled names are
    listed in the result and never granted. Raises 409 without an active
    plan, 422 when nothing chosen is usable or the plan limit would be
    exceeded (nothing is written in either case). `actor_id` is the admin
    when an admin assigns on the agent's behalf (audited); default: the agent.
    """
    names = _clean_names(localities)
    if not names:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Choose at least one locality.",
        )
    if actor_id is not None and db.query(User.id).filter(User.id == user_id).first() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    sub, plan = _lock_active(db, user_id)

    resolved = locality_svc.resolve(db, names)
    ok, unmatched, unavailable = _split(resolved)
    _refuse_when_nothing_usable(ok, unavailable)

    # LOCKING read: under REPEATABLE READ a plain read would reuse the
    # snapshot taken before the plan lock was granted and miss grants a
    # concurrent add just committed (letting both exceed the limit).
    current_ids = {
        int(r[0])
        for r in db.query(BrokerPostcodeAccessCurrent.postcode_id)
        .filter(BrokerPostcodeAccessCurrent.broker_user_id == user_id, broker_svc._grant_is_active())
        .with_for_update()
        .all()
    }
    current = locality_svc.postcode_keys(db, current_ids)
    new_ids = sorted({pid for r in ok for pid in r.postcode_ids if pid not in current_ids})
    added = {k: n for k, n in locality_svc.postcode_keys(db, new_ids).items() if k not in current}
    if len(current) + len(added) > plan.locality_limit:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{plan.name} covers up to {plan.locality_limit} localities.",
        )

    by_admin = actor_id is not None
    actor = actor_id if by_admin else user_id
    if new_ids:
        now = _now()
        # An old, expired grant for the same postcode would collide with the
        # (broker, postcode) primary key; it no longer counts, so replace it.
        db.query(BrokerPostcodeAccessCurrent).filter(
            BrokerPostcodeAccessCurrent.broker_user_id == user_id,
            BrokerPostcodeAccessCurrent.postcode_id.in_(new_ids),
        ).delete(synchronize_session=False)
        reason = (
            f"Locality added under active plan ({plan.code})"
            if not by_admin
            else f"Assigned by admin under active plan ({plan.code})"
        )
        _grant(db, user_id, actor, new_ids, now, sub.end_date, reason)
        if by_admin:
            db.add(AuditLog(
                user_id=actor,
                action="AGENT_LOCALITY_ASSIGNED",
                entity="broker_coverage",
                before_json={"agent_user_id": user_id, "localities": sorted(current.values())},
                after_json={"agent_user_id": user_id, "added": sorted(added.values()), "plan_code": plan.code},
                created_at=now,
            ))
        db.flush()

    result = status_for(db, user_id)
    result["added_localities"] = sorted(added.values())
    result["unmatched_localities"] = unmatched
    result["unavailable_localities"] = unavailable
    return result


def revoke_locality(db: Session, admin_id: int, agent_id: int, locality_id: int, reason: str | None) -> dict:
    """Admin removes one place from an agent's coverage. The grants are
    removed from the current table only; their history rows are marked
    REVOKED (who/when kept). The plan, its payment and expiry are untouched,
    and the freed slot can be used again. 404 if the agent doesn't cover it."""
    loc = db.query(Locality).filter(Locality.id == locality_id).first()
    if loc is None or db.query(User.id).filter(User.id == agent_id).first() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    key = locality_svc.name_key(loc.name)
    rows = (
        db.query(BrokerPostcodeAccessCurrent.postcode_id, Locality.name)
        .join(Postcode, Postcode.id == BrokerPostcodeAccessCurrent.postcode_id)
        .join(Locality, Locality.id == Postcode.locality_id)
        .filter(BrokerPostcodeAccessCurrent.broker_user_id == agent_id)
        .with_for_update()
        .all()
    )
    ids = sorted(int(pid) for pid, name in rows if locality_svc.name_key(name) == key)
    if not ids:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    now = _now()
    db.query(BrokerPostcodeAccessCurrent).filter(
        BrokerPostcodeAccessCurrent.broker_user_id == agent_id,
        BrokerPostcodeAccessCurrent.postcode_id.in_(ids),
    ).delete(synchronize_session=False)
    marked = (
        db.query(BrokerPostcodeAccessHistory)
        .filter(
            BrokerPostcodeAccessHistory.broker_user_id == agent_id,
            BrokerPostcodeAccessHistory.postcode_id.in_(ids),
            BrokerPostcodeAccessHistory.status == "ACTIVE",
        )
        .update(
            {
                BrokerPostcodeAccessHistory.status: "REVOKED",
                BrokerPostcodeAccessHistory.revoked_at: now,
                BrokerPostcodeAccessHistory.revoked_by: admin_id,
            },
            synchronize_session=False,
        )
    )
    if not marked:  # a grant written outside the app had no history row
        for pid in ids:
            db.add(BrokerPostcodeAccessHistory(
                broker_user_id=agent_id, postcode_id=pid, granted_by=admin_id, granted_at=now,
                revoked_at=now, revoked_by=admin_id, status="REVOKED", reason=(reason or "Revoked by admin")[:500],
            ))
    db.add(AuditLog(
        user_id=admin_id,
        action="AGENT_LOCALITY_REVOKED",
        entity="broker_coverage",
        before_json={"agent_user_id": agent_id, "locality_id": loc.id, "locality": loc.name, "postcode_ids": ids},
        after_json={"agent_user_id": agent_id, "locality_id": loc.id, "revoked": True, "reason": reason},
        created_at=now,
    ))
    db.flush()
    return admin_coverage(db, agent_id)


def admin_coverage(db: Session, agent_id: int) -> dict:
    """An agent's plan + allowance and covered postcodes, for admins."""
    if db.query(User.id).filter(User.id == agent_id).first() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    plan = status_for(db, agent_id)
    ids = broker_svc.active_postcode_ids(db, agent_id)
    meta = {
        int(pid): (int(lid), bool(flag))
        for pid, lid, flag in db.query(Postcode.id, Postcode.locality_id, Locality.agent_enabled)
        .join(Locality, Locality.id == Postcode.locality_id)
        .filter(Postcode.id.in_(ids or [0]))
    }
    coverage = [
        {
            "postcode_id": c["postcode_id"],
            "pincode": c["pincode"],
            "locality_id": meta.get(c["postcode_id"], (0, False))[0],
            "locality": c["locality"],
            "city": c["city"],
            "expires_at": c["expires_at"],
            "agent_enabled": meta.get(c["postcode_id"], (0, False))[1],
        }
        for c in broker_svc.list_covered_locations(db, agent_id)
    ]
    return {"user_id": agent_id, "plan": plan, "coverage": coverage}


def start_trial(db: Session, user_id: int, locality: str) -> dict:
    """Start the agent free trial with ONE area (no payment, no expiry).

    Once per account: refused (409) while any agent plan is active and once a
    trial was ever started, so an agent cannot hop between areas by starting
    trials again; a second area needs a paid plan. The area must resolve to a
    served, agent-enabled locality (422 otherwise). The subscription row needs
    a payment row, so a zero-amount provider="FREE_TRIAL" payment records the
    activation (no money). Coverage = that area's postcodes, no expiry; the
    account's role becomes BROKER."""
    name = (locality or "").strip()
    if not name:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Choose an area.")
    if _active(db, user_id) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent plan already active")
    used = (
        db.query(Subscription.id)
        .join(Plan, Plan.id == Subscription.plan_id)
        .filter(Subscription.user_id == user_id, Plan.code == TRIAL.code)
        .first()
    )
    if used is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Free trial already used")
    ok, unmatched, unavailable = _split(locality_svc.resolve(db, [name]))
    _refuse_when_nothing_usable(ok, unavailable)
    postcode_ids = sorted(set(ok[0].postcode_ids))

    row = _plan_row(db, TRIAL)
    now = _now()
    payment = Payment(
        user_id=user_id,
        provider="FREE_TRIAL",
        provider_payment_id=f"TRIAL-{uuid.uuid4().hex[:16]}",
        amount_paise=0,
        currency="INR",
        status="SUCCESS",
        plan_id=row.id,
        idempotency_key=uuid.uuid4().hex,
        paid_at=now,
    )
    db.add(payment)
    db.flush()
    db.add(Subscription(user_id=user_id, plan_id=row.id, payment_id=payment.id,
                        start_date=now, end_date=None, status="ACTIVE"))
    (
        db.query(BrokerPostcodeAccessHistory)
        .filter(BrokerPostcodeAccessHistory.broker_user_id == user_id,
                BrokerPostcodeAccessHistory.status == "ACTIVE")
        .update({BrokerPostcodeAccessHistory.status: "EXPIRED",
                 BrokerPostcodeAccessHistory.revoked_at: now,
                 BrokerPostcodeAccessHistory.revoked_by: user_id}, synchronize_session=False)
    )
    db.query(BrokerPostcodeAccessCurrent).filter(
        BrokerPostcodeAccessCurrent.broker_user_id == user_id
    ).delete(synchronize_session=False)
    _grant(db, user_id, user_id, postcode_ids, now, None, f"Agent free trial ({TRIAL.code})")
    users_roles.set_current_role(db, user_id, "BROKER")
    db.flush()
    return status_for(db, user_id)


def ensure_listing_in_plan(db: Session, user_id: int, listing_id: int) -> None:
    """Agents may act only on listings inside their covered areas (free trial:
    one area; paid plans: their localities). Raises 402 AGENT_AREA_NOT_IN_PLAN
    otherwise. A missing listing is left to the caller's own 404."""
    row = (
        db.query(PhysicalProperty.postcode_id)
        .join(PropertyListing, PropertyListing.physical_property_id == PhysicalProperty.id)
        .filter(PropertyListing.id == listing_id)
        .first()
    )
    if row is None:
        return
    if int(row[0]) in set(broker_svc.area_postcode_ids(db, user_id)):
        return
    active = _active(db, user_id)
    trial = active is None or active[1].code == TRIAL.code
    raise HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail={
            "code": AREA_REQUIRED_CODE,
            "message": TRIAL_AREA_MESSAGE if trial else PLAN_AREA_MESSAGE,
            "has_plan": active is not None,
            "is_trial": bool(active is not None and active[1].code == TRIAL.code),
        },
    )
