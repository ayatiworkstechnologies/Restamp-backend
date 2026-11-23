"""RESTAMP subscription management (admin plans, admin subscription changes,
the temporary payment bypass, and every user's current plan/limits).

Source of truth: the `plans` and `subscriptions` tables. Entitlements are read
from the plan row of the user's current subscription:
  BUYER  -> plans.lead_limit      (owner-number reveals; enforced in buyer_plans)
  OWNER  -> plans.listing_limit   (properties posted; enforced on POST /owner/listings)
  BROKER -> plans.location_limit  (localities; enforced in agent_plans)
NULL or -1 limit = unlimited.

Invariants:
- At most one ACTIVE subscription per user per plan role: activating a new one
  ends the previous one (status EXPIRED, end_date = now, ended_reason) so the
  history is preserved and nothing is deleted.
- "Current" = ACTIVE, not suspended, and not past end_date.
- Payments: nothing here moves money. Admin assignment records provider
  "ADMIN" and the temporary bypass records provider "BYPASS", both with status
  INITIATED (never SUCCESS) and paid_at NULL, so neither can be counted as
  verified revenue. Real gateway activation is unaffected (services/payments.py).
- Every admin write is audited (services/admin.record_audit).
- Plan edits bump plans.version and store an immutable plan_versions snapshot.
  Limits apply live to current subscribers of that plan (there is no
  per-subscription snapshot of limits).
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..config import payment_bypass_enabled
from ..models import Payment, Plan, PlanVersion, PropertyListing, Subscription, User
from . import capabilities, users_roles

ROLES = ("BUYER", "OWNER", "BROKER")
BILLING_CYCLES = ("one_time", "monthly", "quarterly", "yearly")
BYPASS_PROVIDER = "BYPASS"
ADMIN_PROVIDER = "ADMIN"
UNPAID_PROVIDERS = ("SIMULATED", BYPASS_PROVIDER, ADMIN_PROVIDER, "FREE_TRIAL")
_CODE_RE = re.compile(r"^[A-Z0-9_]{3,50}$")
# The buyer free tier is the default when no paid plan is active; it is never a subscription.
BUYER_FREE_CODE = "RESTAMP_BUYER_FREE"


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def _bad(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


def _audit(db: Session, admin_id: int, action: str, entity: str, before: dict | None, after: dict | None) -> None:
    from .admin import record_audit  # local import: admin imports this module's readers
    record_audit(db, admin_id, action, entity, before, after)


# --- plans ---------------------------------------------------------------------------

def plan_status(plan: Plan) -> str:
    if plan.archived_at is not None:
        return "ARCHIVED"
    return "ACTIVE" if plan.status == "ACTIVE" else "PAUSED"


def _snapshot(plan: Plan) -> dict:
    return {
        "code": plan.code, "name": plan.name, "description": plan.description, "role": plan.role,
        "price_paise": plan.price_paise, "duration_days": plan.duration_days, "billing_cycle": plan.billing_cycle,
        "listing_limit": plan.listing_limit, "lead_limit": plan.lead_limit, "location_limit": plan.location_limit,
        "features": plan.features or [], "visible": bool(plan.visible), "recommended": bool(plan.recommended),
        "status": plan_status(plan), "version": plan.version,
    }


def _active_counts(db: Session) -> dict[int, int]:
    t = now()
    rows = (
        db.query(Subscription.plan_id, func.count(Subscription.id))
        .filter(Subscription.status == "ACTIVE", Subscription.suspended_at.is_(None),
                (Subscription.end_date.is_(None)) | (Subscription.end_date > t))
        .group_by(Subscription.plan_id)
        .all()
    )
    return {pid: int(n) for pid, n in rows}


def plan_to_dict(plan: Plan, active_subscribers: int = 0) -> dict:
    d = _snapshot(plan)
    d.update({
        "id": plan.id,
        "is_free": plan.price_paise == 0,
        "active_subscribers": active_subscribers,
        "created_at": plan.created_at,
        "updated_at": plan.updated_at,
        "updated_by_user_id": plan.updated_by_user_id,
    })
    return d


def list_plans(db: Session, role: str | None = None, include_archived: bool = False, public: bool = False) -> list[dict]:
    q = db.query(Plan).filter(Plan.role.isnot(None))
    if role:
        q = q.filter(Plan.role == role)
    if public:
        q = q.filter(Plan.status == "ACTIVE", Plan.archived_at.is_(None), Plan.visible.is_(True))
    elif not include_archived:
        q = q.filter(Plan.archived_at.is_(None))
    counts = {} if public else _active_counts(db)
    plans = q.order_by(Plan.role, Plan.sort_order, Plan.price_paise, Plan.id).all()
    return [plan_to_dict(p, counts.get(p.id, 0)) for p in plans]


def _validate_fields(data: dict, partial: bool) -> dict:
    out: dict = {}
    if "name" in data or not partial:
        name = str(data.get("name") or "").strip()
        if not name or len(name) > 255:
            raise _bad("Plan name is required (max 255 characters)")
        out["name"] = name
    if "description" in data:
        out["description"] = (str(data["description"]).strip() or None) if data["description"] is not None else None
    if "price_paise" in data or not partial:
        price = data.get("price_paise")
        if not isinstance(price, int) or isinstance(price, bool) or price < 0 or price > 10_000_000_00:
            raise _bad("price_paise must be a whole number between 0 and 1,00,00,000 rupees")
        out["price_paise"] = price
    if "duration_days" in data or not partial:
        days = data.get("duration_days")
        if days is not None and (not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 3650):
            raise _bad("duration_days must be between 1 and 3650, or null for no expiry")
        out["duration_days"] = days
    if "billing_cycle" in data or not partial:
        cycle = data.get("billing_cycle") or "one_time"
        if cycle not in BILLING_CYCLES:
            raise _bad(f"billing_cycle must be one of {list(BILLING_CYCLES)}")
        out["billing_cycle"] = cycle
    for key in ("listing_limit", "lead_limit", "location_limit"):
        if key in data:
            v = data[key]
            if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < -1 or v > 100000):
                raise _bad(f"{key} must be -1 (unlimited), a positive number, or null")
            out[key] = None if v == -1 else v
    if "features" in data:
        feats = data["features"] or []
        if not isinstance(feats, list) or len(feats) > 30 or any(not isinstance(f, str) or len(f) > 200 for f in feats):
            raise _bad("features must be a list of up to 30 short texts")
        out["features"] = [f.strip() for f in feats if f.strip()]
    for key in ("visible", "recommended"):
        if key in data:
            out[key] = bool(data[key])
    if "sort_order" in data:
        out["sort_order"] = int(data["sort_order"] or 0)
    return out


def create_plan(db: Session, admin_id: int, data: dict) -> dict:
    code = str(data.get("code") or "").strip().upper()
    if not _CODE_RE.match(code):
        raise _bad("code must be 3-50 characters: A-Z, 0-9 and _")
    role = data.get("role")
    if role not in ROLES:
        raise _bad(f"role must be one of {list(ROLES)}")
    if db.query(Plan.id).filter(Plan.code == code).first():
        raise _bad("A plan with this code already exists")
    fields = _validate_fields(data, partial=False)
    initial = data.get("status", "ACTIVE")
    if initial not in ("ACTIVE", "PAUSED"):
        raise _bad("status must be ACTIVE or PAUSED")
    plan = Plan(code=code, role=role, status="ACTIVE" if initial == "ACTIVE" else "INACTIVE", version=1,
                updated_at=now(), updated_by_user_id=admin_id, **fields)
    db.add(plan)
    db.flush()
    db.add(PlanVersion(plan_id=plan.id, version=1, snapshot=_snapshot(plan), change_note="Created", changed_by_user_id=admin_id))
    _audit(db, admin_id, "PLAN_CREATED", f"plan:{plan.id}", None, _snapshot(plan))
    return plan_to_dict(plan)


def _plan_for_update(db: Session, plan_id: int) -> Plan:
    plan = db.query(Plan).filter(Plan.id == plan_id, Plan.role.isnot(None)).with_for_update().first()
    if plan is None:
        raise _not_found()
    return plan


def update_plan(db: Session, admin_id: int, plan_id: int, data: dict, note: str | None) -> dict:
    plan = _plan_for_update(db, plan_id)
    if plan.archived_at is not None:
        raise _bad("Archived plans cannot be edited")
    fields = _validate_fields(data, partial=True)
    before = _snapshot(plan)
    changed = {k: v for k, v in fields.items() if getattr(plan, k) != v}
    if not changed:
        return plan_to_dict(plan, _active_counts(db).get(plan.id, 0))
    for k, v in changed.items():
        setattr(plan, k, v)
    plan.version = (plan.version or 1) + 1
    plan.updated_at, plan.updated_by_user_id = now(), admin_id
    db.flush()
    db.add(PlanVersion(plan_id=plan.id, version=plan.version, snapshot=_snapshot(plan),
                       change_note=(note or "").strip()[:500] or None, changed_by_user_id=admin_id))
    _audit(db, admin_id, "PLAN_UPDATED", f"plan:{plan.id}", before, _snapshot(plan))
    return plan_to_dict(plan, _active_counts(db).get(plan.id, 0))


def set_plan_status(db: Session, admin_id: int, plan_id: int, new_status: str) -> dict:
    if new_status not in ("ACTIVE", "PAUSED", "ARCHIVED"):
        raise _bad("status must be ACTIVE, PAUSED or ARCHIVED")
    plan = _plan_for_update(db, plan_id)
    before = plan_status(plan)
    if before == new_status:
        return plan_to_dict(plan, _active_counts(db).get(plan.id, 0))
    if before == "ARCHIVED":
        raise _bad("Archived plans cannot be reactivated")
    if new_status == "ARCHIVED":
        plan.archived_at = now()
        plan.status = "INACTIVE"
    else:
        plan.status = "ACTIVE" if new_status == "ACTIVE" else "INACTIVE"
    plan.updated_at, plan.updated_by_user_id = now(), admin_id
    db.flush()
    _audit(db, admin_id, "PLAN_STATUS_CHANGED", f"plan:{plan.id}", {"status": before}, {"status": new_status})
    return plan_to_dict(plan, _active_counts(db).get(plan.id, 0))


def plan_history(db: Session, plan_id: int | None = None, page: int = 1, page_size: int = 50) -> tuple[list[dict], int]:
    q = db.query(PlanVersion, Plan.code).join(Plan, Plan.id == PlanVersion.plan_id)
    if plan_id is not None:
        q = q.filter(PlanVersion.plan_id == plan_id)
    total = q.with_entities(func.count(PlanVersion.id)).scalar() or 0
    rows = q.order_by(PlanVersion.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return [
        {"id": v.id, "plan_id": v.plan_id, "plan_code": code, "version": v.version, "snapshot": v.snapshot,
         "change_note": v.change_note, "changed_by_user_id": v.changed_by_user_id, "created_at": v.created_at}
        for v, code in rows
    ], total


# --- current subscription and limits ----------------------------------------------------

def _current_q(db: Session, user_id: int, role: str):
    t = now()
    return (
        db.query(Subscription, Plan)
        .join(Plan, Plan.id == Subscription.plan_id)
        .filter(Subscription.user_id == user_id, Subscription.status == "ACTIVE", Plan.role == role,
                Plan.code != BUYER_FREE_CODE, Subscription.suspended_at.is_(None),
                (Subscription.end_date.is_(None)) | (Subscription.end_date > t))
        .order_by(Subscription.start_date.desc(), Subscription.id.desc())
    )


def current(db: Session, user_id: int, role: str):
    """(Subscription, Plan) of the user's current plan for `role`, or None."""
    return _current_q(db, user_id, role).first()


def owner_listing_count(db: Session, owner_id: int) -> int:
    return db.query(func.count(PropertyListing.id)).filter(
        PropertyListing.user_id == owner_id, PropertyListing.deleted_at.is_(None)).scalar() or 0


def _limit(v: int | None) -> int | None:
    return None if v is None or v < 0 else v


def ensure_owner_can_create(db: Session, owner_id: int) -> None:
    """402 when a poster (Owner or Dealer) on a plan has used its listing
    limit. The plan is the one of the account's poster type (Dealer accounts
    use their Dealer plan's listing_limit; NULL/-1 = unlimited). Whether a
    current paid plan exists at all is decided first by
    capabilities.ensure_can_post (POST /owner/listings)."""
    role = capabilities.get_poster_type(db, owner_id) or users_roles.get_current_role(db, owner_id) or "OWNER"
    cur = current(db, owner_id, role)
    if cur is None:
        return
    _sub, plan = cur
    limit = _limit(plan.listing_limit)
    if limit is None:
        return
    used = owner_listing_count(db, owner_id)
    if used >= limit:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={"code": "OWNER_PLAN_LIMIT", "message": f"Your {plan.name} allows {limit} propert{'y' if limit == 1 else 'ies'}. Upgrade your plan to post more.",
                    "plan_code": plan.code, "listing_limit": limit, "listings_used": used},
        )


def _sub_dict(sub: Subscription, plan: Plan, payment: Payment | None = None) -> dict:
    return {
        "id": sub.id, "user_id": sub.user_id, "plan": plan_to_dict(plan), "status": sub_status(sub),
        "start_date": sub.start_date, "end_date": sub.end_date, "source": sub.source,
        "assigned_by_user_id": sub.assigned_by_user_id, "ended_reason": sub.ended_reason,
        "payment_provider": payment.provider if payment else None,
        "payment_status": payment.status if payment else None,
        "payment_verified": bool(payment and payment.status == "SUCCESS" and payment.provider not in UNPAID_PROVIDERS),
    }


def sub_status(sub: Subscription) -> str:
    if sub.status == "CANCELLED":
        return "CANCELLED"
    if sub.status == "EXPIRED" or (sub.end_date is not None and sub.end_date <= now()):
        return "EXPIRED"
    if sub.suspended_at is not None:
        return "SUSPENDED"
    return "ACTIVE"


def my_subscriptions(db: Session, user_id: int) -> dict:
    """The caller's current plan per role with limits and usage (user-facing)."""
    from . import agent_plans, buyer_plans
    out: dict = {"current_role": users_roles.get_current_role(db, user_id), "subscriptions": {}}
    for role in ROLES:
        cur = current(db, user_id, role)
        entry: dict = {"subscription": None, "usage": {}}
        if cur is not None:
            sub, plan = cur
            pay = db.get(Payment, sub.payment_id)
            entry["subscription"] = _sub_dict(sub, plan, pay)
        if role == "OWNER":
            limit = _limit(cur[1].listing_limit) if cur else None
            used = owner_listing_count(db, user_id)
            entry["usage"] = {"listings_used": used, "listing_limit": limit,
                              "listings_remaining": None if limit is None else max(limit - used, 0)}
        elif role == "BUYER":
            s = buyer_plans.status_for(db, user_id)
            entry["usage"] = {"leads_used": s["leads_used"], "lead_limit": s["lead_limit"], "leads_remaining": s["leads_remaining"],
                              "plan_code": s["plan_code"], "plan_name": s["plan_name"]}
        else:
            try:
                s = agent_plans.status_for(db, user_id)
                if s.get("has_plan"):
                    entry["usage"] = {"locality_limit": s.get("locality_limit"), "localities_used": len(s.get("localities") or []),
                                      "localities_remaining": s.get("localities_remaining")}
            except HTTPException:
                entry["usage"] = {}
        out["subscriptions"][role] = entry
    return out


# --- activation (admin assignment and temporary payment bypass) ---------------------------

def _end_current(db: Session, user_id: int, role: str, reason: str) -> list[int]:
    """End every ACTIVE subscription of this role (history kept)."""
    t = now()
    rows = (
        db.query(Subscription).join(Plan, Plan.id == Subscription.plan_id)
        .filter(Subscription.user_id == user_id, Subscription.status == "ACTIVE", Plan.role == role)
        .with_for_update().all()
    )
    for s in rows:
        s.status, s.ended_reason, s.updated_at = "EXPIRED", reason[:255], t
        if s.end_date is None or s.end_date > t:
            s.end_date = t
    return [s.id for s in rows]


def _activate(db: Session, user_id: int, plan: Plan, provider: str, source: str, amount_paise: int,
              admin_id: int | None, end_reason: str, validity_days: int | None = None) -> tuple[Subscription, list[int]]:
    t = now()
    # Owner XOR Dealer: the first poster-plan activation records the poster
    # type; a plan of the other poster type is refused (409) before anything
    # is written. Buyer plans are unaffected.
    capabilities.claim_poster_type(db, user_id, plan.role)
    ended = _end_current(db, user_id, plan.role, end_reason)
    payment = Payment(user_id=user_id, provider=provider, provider_payment_id=None, amount_paise=amount_paise,
                      currency="INR", status="INITIATED", plan_id=plan.id, idempotency_key=uuid.uuid4().hex, paid_at=None)
    db.add(payment)
    db.flush()
    days = validity_days if validity_days is not None else plan.duration_days
    sub = Subscription(user_id=user_id, plan_id=plan.id, payment_id=payment.id, start_date=t,
                       end_date=(t + timedelta(days=days)) if days else None, status="ACTIVE",
                       source=source, assigned_by_user_id=admin_id, updated_at=t)
    db.add(sub)
    db.flush()
    return sub, ended


def _lock_user(db: Session, user_id: int) -> User:
    user = db.query(User).filter(User.id == user_id).with_for_update().first()
    if user is None:
        raise _not_found()
    return user


def admin_assign(db: Session, admin_id: int, user_id: int, plan_code: str, reason: str,
                 validity_days: int | None = None) -> dict:
    reason = (reason or "").strip()
    if not reason:
        raise _bad("A reason is required")
    if validity_days is not None and not 1 <= validity_days <= 3650:
        raise _bad("validity_days must be between 1 and 3650")
    user = _lock_user(db, user_id)
    if user.status != "active":
        raise _bad("Only active accounts can be given a plan")
    plan = db.query(Plan).filter(Plan.code == plan_code, Plan.role.isnot(None)).first()
    if plan is None:
        raise _not_found()
    if plan_status(plan) != "ACTIVE":
        raise _bad("Only active plans can be assigned")
    if plan.code == BUYER_FREE_CODE:
        raise _bad("The free buyer tier applies automatically; end the paid plan instead")
    before = current(db, user_id, plan.role)
    sub, ended = _activate(db, user_id, plan, ADMIN_PROVIDER, "ADMIN", 0, admin_id,
                           f"Replaced by admin assignment of {plan.code}", validity_days)
    _audit(db, admin_id, "SUBSCRIPTION_ASSIGNED", f"user:{user_id}",
           {"plan_code": before[1].code, "subscription_id": before[0].id} if before else None,
           {"plan_code": plan.code, "subscription_id": sub.id, "ended": ended, "reason": reason[:500],
            "end_date": sub.end_date.isoformat() if sub.end_date else None})
    return _sub_dict(sub, plan, db.get(Payment, sub.payment_id))


def bypass_activate(db: Session, user_id: int, plan_code: str) -> dict:
    """TEMPORARY payment bypass: activate a plan for the caller without payment.
    Payment row: provider BYPASS, status INITIATED (never SUCCESS). Idempotent:
    if the caller's current plan for that role is already this plan, it is returned."""
    if not payment_bypass_enabled():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Payment bypass is disabled")
    _lock_user(db, user_id)  # serialises concurrent Continue presses for one user
    plan = db.query(Plan).filter(Plan.code == plan_code, Plan.role.isnot(None)).first()
    if plan is None or plan_status(plan) != "ACTIVE" or not plan.visible:
        raise _not_found()
    if plan.code == BUYER_FREE_CODE:
        raise _bad("The free plan is already included")
    if plan.code in capabilities.TRIAL_PLAN_CODES:
        # The Dealer free trial has its own once-per-account flow
        # (POST /broker/trial); it is never activated through the bypass.
        raise _not_found()
    # Buyer plans: buyer capability exists in every mode. Poster plans still
    # require the matching mode (existing purchase flow), and the poster type
    # exclusivity is enforced by claim_poster_type in _activate.
    role = users_roles.get_current_role(db, user_id)
    if plan.role != "BUYER" and role != plan.role:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"{plan.role} role required")
    capabilities.ensure_compatible(db, user_id, plan.role)
    existing = current(db, user_id, plan.role)
    if existing is not None and existing[1].id == plan.id:
        return {**_sub_dict(existing[0], plan, db.get(Payment, existing[0].payment_id)), "already_active": True}
    sub, _ended = _activate(db, user_id, plan, BYPASS_PROVIDER, "BYPASS", plan.price_paise, None,
                            f"Replaced by {plan.code} (payment bypass)")
    return {**_sub_dict(sub, plan, db.get(Payment, sub.payment_id)), "already_active": False}


def _sub_for_update(db: Session, sub_id: int) -> tuple[Subscription, Plan]:
    row = (db.query(Subscription, Plan).join(Plan, Plan.id == Subscription.plan_id)
           .filter(Subscription.id == sub_id).with_for_update(of=Subscription).first())
    if row is None:
        raise _not_found()
    return row


def admin_extend(db: Session, admin_id: int, sub_id: int, days: int, reason: str) -> dict:
    if not isinstance(days, int) or not 1 <= days <= 3650:
        raise _bad("days must be between 1 and 3650")
    if not (reason or "").strip():
        raise _bad("A reason is required")
    sub, plan = _sub_for_update(db, sub_id)
    if sub.status != "ACTIVE":
        raise _bad("Only active subscriptions can be extended")
    if sub.end_date is None:
        raise _bad("This subscription has no expiry")
    before = sub.end_date
    sub.end_date = max(sub.end_date, now()) + timedelta(days=days)
    sub.updated_at = now()
    _audit(db, admin_id, "SUBSCRIPTION_EXTENDED", f"subscription:{sub.id}",
           {"end_date": before.isoformat()}, {"end_date": sub.end_date.isoformat(), "days": days, "reason": reason.strip()[:500]})
    return _sub_dict(sub, plan, db.get(Payment, sub.payment_id))


def admin_set_state(db: Session, admin_id: int, sub_id: int, action: str, reason: str) -> dict:
    """action: suspend | reactivate | cancel."""
    if action != "reactivate" and not (reason or "").strip():
        raise _bad("A reason is required")
    sub, plan = _sub_for_update(db, sub_id)
    before = sub_status(sub)
    if action == "suspend":
        if before != "ACTIVE":
            raise _bad("Only active subscriptions can be suspended")
        sub.suspended_at = now()
    elif action == "reactivate":
        if before != "SUSPENDED":
            raise _bad("Only suspended subscriptions can be reactivated")
        sub.suspended_at = None
    elif action == "cancel":
        if before in ("CANCELLED", "EXPIRED"):
            raise _bad("This subscription has already ended")
        sub.status, sub.ended_reason = "CANCELLED", (reason or "").strip()[:255]
    else:
        raise _bad("Unknown action")
    sub.updated_at = now()
    _audit(db, admin_id, f"SUBSCRIPTION_{action.upper()}", f"subscription:{sub.id}",
           {"status": before}, {"status": sub_status(sub), "reason": (reason or "").strip()[:500] or None})
    return _sub_dict(sub, plan, db.get(Payment, sub.payment_id))


def activate_for_payment(db: Session, payment: Payment, plan: Plan) -> dict:
    """Activate an OWNER plan from a VERIFIED Razorpay payment row.

    The caller (payments._confirm_and_activate) holds the payment row FOR
    UPDATE and has already claimed the OWNER poster type (conflicts go to
    manual review there, never here). Idempotent: if ANY subscription already
    references this payment (even one later replaced), nothing is created
    again. Renewal/upgrade = the current OWNER plan is ended (history kept)
    and the new one starts now with the plan row's duration.
    """
    existing = db.query(Subscription).filter(Subscription.payment_id == payment.id).first()
    out = {"transaction_id": payment.provider_payment_id, "amount_paise": payment.amount_paise,
           "plan_code": plan.code}
    if existing is not None:
        return {**out, "activated": False, "subscription_id": existing.id}
    capabilities.claim_poster_type(db, payment.user_id, "OWNER")
    t = now()
    _end_current(db, payment.user_id, "OWNER", f"Replaced by {plan.code} (payment)")
    sub = Subscription(user_id=payment.user_id, plan_id=plan.id, payment_id=payment.id, start_date=t,
                       end_date=(t + timedelta(days=plan.duration_days)) if plan.duration_days else None,
                       status="ACTIVE", source="PAYMENT", updated_at=t)
    db.add(sub)
    db.flush()
    return {**out, "activated": True, "subscription_id": sub.id}



def my_payments(db: Session, user_id: int, page: int, page_size: int) -> tuple[list[dict], int]:
    """The caller's own payment records, newest first (read-only)."""
    base = (
        db.query(Payment, Plan.code, Plan.name, Plan.role, Subscription.id)
        .outerjoin(Plan, Plan.id == Payment.plan_id)
        .outerjoin(Subscription, Subscription.payment_id == Payment.id)
        .filter(Payment.user_id == user_id)
    )
    total = base.with_entities(func.count(Payment.id)).scalar() or 0
    rows = base.order_by(Payment.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    out = []
    for pay, code, name, role, sub_id in rows:
        paid = pay.status == "SUCCESS" and pay.provider not in UNPAID_PROVIDERS
        out.append({
            "id": pay.id, "plan_code": code, "plan_name": name, "plan_role": role, "provider": pay.provider,
            "paid": paid, "transaction_ref": pay.provider_payment_id if paid else None,
            "amount_paise": pay.amount_paise, "currency": pay.currency, "status": pay.status,
            "created_at": pay.created_at, "paid_at": pay.paid_at if paid else None, "subscription_id": sub_id,
        })
    return out, total
