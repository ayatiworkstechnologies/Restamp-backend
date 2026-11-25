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

PAYMENT IS SIMULATED for subscribe_simulated(), exactly like buyer plans:
provider="SIMULATED", no money is charged. The real gateway flow (Razorpay
order → signed payment → subscription + coverage) lives in
services/payments.py and activates through activate_for_payment() below.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
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
from . import capabilities, users_roles


@dataclass(frozen=True)
class AgentPlan:
    code: str
    app_id: str  # the id the app uses for this plan card
    name: str
    price_paise: int
    duration_days: int
    locality_limit: int


PLANS = (
    AgentPlan("RESTAMP_AGENT_STARTER", "agent-starter", "Dealer Starter Plan", 299900, 30, 5),
    AgentPlan("RESTAMP_AGENT_PRO", "agent-pro", "Dealer Pro Plan", 699900, 30, 10),
    AgentPlan("RESTAMP_AGENT_ELITE", "agent-elite", "Dealer Elite Plan", 1499900, 30, 15),
)
# Accept either the server code or the app's card id.
BY_KEY = {**{p.code: p for p in PLANS}, **{p.app_id: p for p in PLANS}}

# Agent free trial: ONE area, no payment, no expiry (duration None), owner
# number reveals unlimited (the buyer reveal quota never applies to agents).
# Started once per account with start_trial; never purchasable through
# subscribe_simulated, and upgrading to a paid tier replaces it.
TRIAL = AgentPlan("RESTAMP_AGENT_TRIAL", "agent-trial", "Dealer Free Trial", 0, None, 1)
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
    """Get-or-create the catalogue row for an agent plan. An existing row is never
    overwritten: admins manage it (services/subscriptions.py)."""
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
            role="BROKER",
            location_limit=plan.locality_limit,
        )
        db.add(row)
        db.flush()
    return row


def _from_row(row: Plan) -> AgentPlan:
    """The agent plan as stored: admin edits to name/price/locality limit apply."""
    known = BY_CODE.get(row.code)
    limit = row.location_limit if row.location_limit is not None else (known.locality_limit if known else 0)
    if known is not None:
        return replace(known, name=row.name, price_paise=row.price_paise, locality_limit=limit)
    return AgentPlan(row.code, row.code.lower(), row.name, row.price_paise, row.duration_days, limit)


def _active(db: Session, user_id: int):
    """Latest ACTIVE, unexpired agent subscription -> (Subscription, AgentPlan) or None."""
    row = (
        db.query(Subscription, Plan)
        .join(Plan, Plan.id == Subscription.plan_id)
        .filter(
            Subscription.user_id == user_id,
            Subscription.status == "ACTIVE",
            or_(Plan.role == "BROKER", Plan.code.in_(CODES)),
            Subscription.suspended_at.is_(None),
            # The free trial has no end date; paid plans must not be expired.
            or_(Subscription.end_date.is_(None), Subscription.end_date > _now()),
        )
        .order_by(Subscription.start_date.desc(), Subscription.id.desc())
        .first()
    )
    if row is None:
        return None
    sub, plan_row = row
    return sub, _from_row(plan_row)


NOT_SERVED = "None of the chosen localities are served yet."
NOT_AVAILABLE = "None of the chosen localities are available for agents."


def _clean_names(localities: list[str]) -> list[str]:
    return list(dict.fromkeys(n.strip() for n in localities if n and n.strip()))


def _slots(db: Session, postcode_ids) -> dict[str, str]:
    """{slot key: label} for granted postcodes — what a plan's location
    limit counts. ONE slot per Agent Location Master PIN-code group, however
    many localities that PIN code has (adding localities to a PIN code never
    uses more slots). Postcodes outside the master (older grants) keep the
    previous per-locality key, so existing entitlements are never reduced."""
    from . import location_master as lm_svc

    ids = sorted({int(i) for i in postcode_ids})
    groups = lm_svc.groups_for_postcodes(db, ids)
    out = {f"pin:{g.pincode}": f"{g.primary_name} - {g.pincode}" for g in groups.values()}
    out.update(locality_svc.postcode_keys(db, [i for i in ids if i not in groups]))
    return out


def _covered(db: Session, user_id: int) -> dict[str, str]:
    """{slot key: label} the agent covers now (unexpired grants)."""
    return _slots(db, broker_svc.active_postcode_ids(db, user_id))


def _covered_groups(db: Session, user_id: int) -> list[dict]:
    """The Agent Location Master groups the agent covers (stable ids)."""
    from . import location_master as lm_svc

    groups = lm_svc.groups_for_postcodes(db, broker_svc.active_postcode_ids(db, user_id))
    return sorted(
        ({"id": int(g.id), "pincode": g.pincode, "primary_name": g.primary_name, "zone": g.zone}
         for g in groups.values()),
        key=lambda g: g["pincode"],
    )


def _group_choice(db: Session, group_ids) -> tuple[list[int], list[str], list[str]]:
    """Chosen Agent Location Master groups -> (postcode ids, labels, unknown
    ids as strings). Ids are the stable group ids from /locations/agent-groups."""
    from . import location_master as lm_svc

    found, unknown = lm_svc.resolve_group_ids(db, group_ids)
    return (
        sorted(int(g.postcode_id) for g in found),
        [f"{g.primary_name} - {g.pincode}" for g in found],
        [str(i) for i in unknown],
    )


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


def catalogue() -> list[dict]:
    """The purchasable agent plan cards (the free trial is not sold)."""
    return [
        {
            "code": p.code,
            "app_id": p.app_id,
            "name": p.name,
            "price_paise": p.price_paise,
            "duration_days": p.duration_days,
            "locality_limit": p.locality_limit,
        }
        for p in PLANS
    ]


def status_for(db: Session, user_id: int) -> dict:
    """The caller's agent plan (or has_plan=False) plus covered localities."""
    active = _active(db, user_id)
    if active is None:
        return {"has_plan": False}
    sub, plan = active
    localities = sorted(_covered(db, user_id).values())
    groups = _covered_groups(db, user_id)
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
        "location_groups": groups,
        "group_ids": [g["id"] for g in groups],
        "localities_remaining": max(0, plan.locality_limit - len(localities)),
    }


def _resolve_choice(
    db: Session, plan: AgentPlan, localities: list[str], group_ids=None
) -> tuple[list[int], list[str], list[str], list[str]]:
    """Validate a locality choice against the plan's limit: (postcode_ids,
    canonical names, unmatched, unavailable). Raises 422 when nothing is
    usable or the limit would be exceeded — nothing is written. ``group_ids``
    (Agent Location Master PIN groups) take precedence over names."""
    if group_ids:
        postcode_ids, labels, unknown = _group_choice(db, group_ids)
        if not postcode_ids:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=NOT_SERVED)
        if len(_slots(db, postcode_ids)) > plan.locality_limit:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{plan.name} covers up to {plan.locality_limit} locations.",
            )
        return postcode_ids, sorted(labels), unknown, []
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
    if len(_slots(db, postcode_ids)) > plan.locality_limit:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{plan.name} covers up to {plan.locality_limit} localities.",
        )
    canonical = sorted({r.name for r in ok if r.name})
    return postcode_ids, canonical, unmatched, unavailable


def _activate(
    db: Session, user_id: int, plan: AgentPlan, postcode_ids: list[int],
    payment_id: int, now: datetime,
) -> None:
    """Activate a paid agent plan from a payment row: one active agent plan at
    a time (the new purchase replaces the old one), coverage = exactly the
    chosen localities (replaced grants stay visible in history, marked
    EXPIRED), role switches to BROKER."""
    # Dealer XOR Owner: refuse (409) before any write when the account is an
    # Owner; otherwise record BROKER as the poster type.
    capabilities.claim_poster_type(db, user_id, "BROKER")
    (
        db.query(Subscription)
        .filter(
            Subscription.user_id == user_id,
            Subscription.status == "ACTIVE",
            Subscription.plan_id.in_(db.query(Plan.id).filter(Plan.code.in_(CODES)).scalar_subquery()),
        )
        .update({Subscription.status: "EXPIRED"}, synchronize_session=False)
    )
    end = now + timedelta(days=plan.duration_days)
    db.add(
        Subscription(
            user_id=user_id,
            plan_id=db.query(Plan.id).filter(Plan.code == plan.code).scalar(),
            payment_id=payment_id,
            start_date=now,
            end_date=end,
            status="ACTIVE",
        )
    )

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


def subscribe_simulated(db: Session, user_id: int, key: str, localities: list[str], group_ids=None) -> dict:
    """Activate an agent plan for the chosen localities. PAYMENT IS SIMULATED.
    Development environments only: refused (403) in production/staging."""
    from .buyer_plans import require_simulated_purchase_allowed

    require_simulated_purchase_allowed()
    plan = BY_KEY.get(key)
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    postcode_ids, _canonical, unmatched, unavailable = _resolve_choice(db, plan, localities, group_ids)

    row = _plan_row(db, plan)
    now = _now()
    payment = Payment(
        user_id=user_id,
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

    _activate(db, user_id, plan, postcode_ids, payment.id, now)

    result = status_for(db, user_id)
    result["transaction_id"] = payment.provider_payment_id
    result["amount_paise"] = plan.price_paise
    result["unmatched_localities"] = unmatched
    result["unavailable_localities"] = unavailable
    result["simulated"] = True
    return result


def activate_for_payment(
    db: Session, payment: Payment, plan: AgentPlan, localities: list[str], group_ids=None
) -> dict:
    """Activate an agent plan from a verified payment row (real payment flow).

    The locality choice is re-validated here against the plan's limit — the
    server decides what is grantable, never the client. Idempotent: an ACTIVE
    subscription already tied to this payment returns the current status
    (activated=False) without touching coverage again. The caller must hold
    the payment row locked (FOR UPDATE) so a concurrent verify and webhook
    cannot both activate."""
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
    postcode_ids, _canonical, unmatched, unavailable = _resolve_choice(db, plan, localities, group_ids)
    now = _now()
    _activate(db, payment.user_id, plan, postcode_ids, payment.id, now)
    result = status_for(db, payment.user_id)
    result["transaction_id"] = payment.provider_payment_id
    result["amount_paise"] = payment.amount_paise
    result["unmatched_localities"] = unmatched
    result["unavailable_localities"] = unavailable
    result["activated"] = True
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


def add_localities(
    db: Session, user_id: int, localities: list[str], *, actor_id: int | None = None, group_ids=None
) -> dict:
    """Add localities to the agent's ACTIVE plan, within its limit.

    No payment and no new subscription: the unused allowance was already paid
    for, and the plan's expiry is unchanged. Already-covered places are
    skipped (no duplicate grants). Unknown and admin-disabled names are
    listed in the result and never granted. Raises 409 without an active
    plan, 422 when nothing chosen is usable or the plan limit would be
    exceeded (nothing is written in either case). `actor_id` is the admin
    when an admin assigns on the agent's behalf (audited); default: the agent.
    """
    names = _clean_names(localities or [])
    if not names and not group_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Choose at least one locality.",
        )
    if actor_id is not None and db.query(User.id).filter(User.id == user_id).first() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    sub, plan = _lock_active(db, user_id)

    if group_ids:
        chosen_ids, _labels, unmatched = _group_choice(db, group_ids)
        unavailable: list[str] = []
        if not chosen_ids:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=NOT_SERVED)
    else:
        resolved = locality_svc.resolve(db, names)
        ok, unmatched, unavailable = _split(resolved)
        _refuse_when_nothing_usable(ok, unavailable)
        chosen_ids = [pid for r in ok for pid in r.postcode_ids]

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
    current = _slots(db, current_ids)
    new_ids = sorted({pid for pid in chosen_ids if pid not in current_ids})
    added = {k: n for k, n in _slots(db, new_ids).items() if k not in current}
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


def start_trial(db: Session, user_id: int, locality: str, group_id: int | None = None) -> dict:
    """Start the agent free trial with ONE area (no payment, no expiry).

    Once per account: refused (409) while any agent plan is active and once a
    trial was ever started, so an agent cannot hop between areas by starting
    trials again; a second area needs a paid plan. The area must resolve to a
    served, agent-enabled locality (422 otherwise). The subscription row needs
    a payment row, so a zero-amount provider="FREE_TRIAL" payment records the
    activation (no money). Coverage = that area's postcodes, no expiry; the
    account's role becomes BROKER."""
    name = (locality or "").strip()
    if not name and not group_id:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Choose an area.")
    capabilities.ensure_compatible(db, user_id, "BROKER")
    if _active(db, user_id) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Dealer plan already active")
    used = (
        db.query(Subscription.id)
        .join(Plan, Plan.id == Subscription.plan_id)
        .filter(Subscription.user_id == user_id, Plan.code == TRIAL.code)
        .first()
    )
    if used is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Free trial already used")
    if group_id:
        # One Agent Location Master PIN group = the trial's one area.
        postcode_ids, _labels, _unknown = _group_choice(db, [group_id])
        if not postcode_ids:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=NOT_SERVED)
    else:
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
    capabilities.claim_poster_type(db, user_id, "BROKER")
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
