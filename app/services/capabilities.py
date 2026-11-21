"""RESTAMP account capabilities (backend-authoritative).

Model (Phase 1):
- Buyer capability: every active account, in every app mode. Buyer endpoints
  never depend on the current mode.
- Poster capability: users.poster_type = OWNER or BROKER (Dealer), mutually
  exclusive, NULL = Buyer only. It is set ONLY by:
    * registration (the declared account type of a brand-new account), or
    * the first Owner/Dealer plan activation (claim_poster_type, called from
      every activation path: payment, admin assignment, bypass, simulated,
      agent trial).
  It is NEVER set or changed by a mode switch or by any client-supplied
  field on an existing account. Changing an existing poster type is refused
  (409 POSTER_TYPE_CONFLICT); the Owner<->Dealer conversion process is a
  pending business decision and is not implemented.
- Poster plan state: derived at read time from the subscriptions table
  (ACTIVE, not suspended, not past end_date) for the poster type's role.
  The poster type alone grants no posting right.
- Posting (Phase 2, ensure_can_post): restricted listing operations need the
  poster type of the current mode AND a current, NON-TRIAL plan of that type
  (a Dealer free trial is never a posting plan). Otherwise 402
  POSTER_PLAN_REQUIRED. Reading listings, leads, visits and history never
  needs a plan. Expiry never unpublishes or deletes anything.
- Conversion (Phase 2, convert_poster_type): admin-only, audited, refused
  while a subscription of the current poster type is still active; it never
  cancels, refunds or transfers subscriptions and never touches listings.
  Dealer -> Owner disables the live locality grants (history kept).
- user_current_role stays as the app MODE (UI/API compatibility). Switching
  to BUYER is always allowed; switching to the other poster type's mode is
  refused. A NULL poster type may enter OWNER/BROKER mode (to browse plans
  and buy one); the mode grants nothing by itself.

Nothing here deletes or rewrites listings, subscriptions, payments or history.
"""
from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from datetime import datetime, timezone

from ..models import (
    AuditLog,
    BrokerPostcodeAccessCurrent,
    BrokerPostcodeAccessHistory,
    Payment,
    Plan,
    RoleHistory,
    Subscription,
    User,
    UserCurrentRole,
)
from . import users_roles

POSTER_TYPES = ("OWNER", "BROKER")
POSTER_TYPE_CONFLICT = "POSTER_TYPE_CONFLICT"
POSTER_PLAN_REQUIRED = "POSTER_PLAN_REQUIRED"
_LABEL = {"OWNER": "Owner", "BROKER": "Dealer"}
# A Dealer free trial is never a posting plan (approved Phase 2 decision).
TRIAL_PLAN_CODES = ("RESTAMP_AGENT_TRIAL",)
TRIAL_PROVIDERS = ("FREE_TRIAL",)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def is_trial(db: Session, sub: Subscription, plan: Plan) -> bool:
    if plan.code in TRIAL_PLAN_CODES or sub.source == "TRIAL":
        return True
    payment = db.get(Payment, sub.payment_id) if sub.payment_id else None
    return payment is not None and payment.provider in TRIAL_PROVIDERS


def normalize_account_type(value: str | None) -> str | None:
    """Registration account type -> poster type. DEALER is an alias of BROKER."""
    if value is None:
        return None
    v = value.upper()
    if v == "DEALER":
        v = "BROKER"
    return v if v in POSTER_TYPES else None


def _conflict(current: str, requested: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": POSTER_TYPE_CONFLICT,
            "message": (
                f"This account is registered as {_LABEL[current]}. "
                f"{_LABEL[requested]} features are not available on it."
            ),
            "poster_type": current,
        },
    )


def get_poster_type(db: Session, user_id: int) -> str | None:
    user = db.get(User, user_id)
    return user.poster_type if user is not None else None


def ensure_compatible(db: Session, user_id: int, role: str) -> None:
    """Read-only check (e.g. before a checkout): 409 when `role` is the other
    poster type. BUYER (and non-poster roles) always pass."""
    if role not in POSTER_TYPES:
        return
    current = get_poster_type(db, user_id)
    if current is not None and current != role:
        raise _conflict(current, role)


def claim_poster_type(db: Session, user_id: int, role: str) -> None:
    """Record the poster type on the first Owner/Dealer activation.

    Locks the user row so two concurrent activations of different poster
    types cannot both claim it. Same type: no-op. Other type: 409.
    """
    if role not in POSTER_TYPES:
        return
    user = db.query(User).filter(User.id == user_id).with_for_update().first()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if user.poster_type is None:
        user.poster_type = role
        db.flush()
    elif user.poster_type != role:
        raise _conflict(user.poster_type, role)


def try_claim(db: Session, user_id: int, role: str) -> bool:
    """claim_poster_type without raising: False on an Owner/Dealer conflict.
    Used where raising would lose committed-worthy state (a verified payment)."""
    try:
        claim_poster_type(db, user_id, role)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_409_CONFLICT:
            return False
        raise
    return True


def _posting_plan(db: Session, user_id: int, poster_type: str):
    """(sub, plan, trial) of the current plan of `poster_type`, or None."""
    from . import subscriptions as sub_svc

    cur = sub_svc.current(db, user_id, poster_type)
    if cur is None:
        return None
    sub, plan = cur
    return sub, plan, is_trial(db, sub, plan)


def ensure_can_post(db: Session, user_id: int) -> None:
    """402 POSTER_PLAN_REQUIRED unless the caller may perform a restricted
    (paid) listing operation right now: their current mode is their poster
    type and a current, non-trial plan of that type exists. Read from the DB
    on every call (never from the token or the client)."""
    mode = users_roles.get_current_role(db, user_id)
    poster_type = get_poster_type(db, user_id)
    status_ = "NONE"
    if poster_type is not None and mode == poster_type:
        found = _posting_plan(db, user_id, poster_type)
        if found is not None and not found[2]:
            return
        status_ = "TRIAL" if found is not None else "INACTIVE"
    label = _LABEL.get(poster_type or mode or "", "Owner")
    raise HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail={
            "code": POSTER_PLAN_REQUIRED,
            "message": (
                f"An active paid {label} plan is required to post or change properties. "
                "Your existing properties, leads and visits stay available."
            ),
            "poster_type": poster_type,
            "poster_plan_status": status_,
        },
    )


def _has_active_poster_subscription(db: Session, user_id: int, poster_type: str) -> bool:
    """Any ACTIVE (incl. suspended or trial) unexpired subscription of the type."""
    now = _now()
    return (
        db.query(Subscription.id)
        .join(Plan, Plan.id == Subscription.plan_id)
        .filter(
            Subscription.user_id == user_id,
            Subscription.status == "ACTIVE",
            Plan.role == poster_type,
            (Subscription.end_date.is_(None)) | (Subscription.end_date > now),
        )
        .first()
        is not None
    )


def convert_poster_type(db: Session, admin_id: int, user_id: int, new_type: str, reason: str) -> dict:
    """Admin-only Owner <-> Dealer conversion (approved Phase 2 process).

    - 422 without a reason or for an unknown type; 404 unknown user.
    - 409 when the account already has that type, has no poster type, or
      still has an ACTIVE (unexpired, incl. trial/suspended) subscription of
      its current poster type. Nothing is cancelled, refunded or transferred.
    - Only users.poster_type changes; listings, media, verifications, leads,
      messages, visits, payments and subscription history are untouched.
    - Dealer -> Owner: live locality grants are removed from the current
      table and their history rows marked REVOKED (records kept, no credit).
    - A mode equal to the old type is reset to BUYER (role_history appended).
    - One audit_logs row: admin id, user id, old/new type, reason, time.
    """
    reason = (reason or "").strip()
    if not reason:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="A reason is required")
    if new_type not in POSTER_TYPES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown poster type")
    user = db.query(User).filter(User.id == user_id).with_for_update().first()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    old = user.poster_type
    if old is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="This account has no poster type to convert")
    if old == new_type:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=f"This account is already a {_LABEL[new_type]}")
    if _has_active_poster_subscription(db, user_id, old):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"The current {_LABEL[old]} subscription is still active. It must end before conversion.",
        )
    now = _now()
    revoked: list[int] = []
    if old == "BROKER":
        revoked = sorted(int(r[0]) for r in db.query(BrokerPostcodeAccessCurrent.postcode_id)
                         .filter(BrokerPostcodeAccessCurrent.broker_user_id == user_id).with_for_update().all())
        if revoked:
            db.query(BrokerPostcodeAccessCurrent).filter(
                BrokerPostcodeAccessCurrent.broker_user_id == user_id
            ).delete(synchronize_session=False)
        db.query(BrokerPostcodeAccessHistory).filter(
            BrokerPostcodeAccessHistory.broker_user_id == user_id,
            BrokerPostcodeAccessHistory.status == "ACTIVE",
        ).update({BrokerPostcodeAccessHistory.status: "REVOKED",
                  BrokerPostcodeAccessHistory.revoked_at: now,
                  BrokerPostcodeAccessHistory.revoked_by: admin_id}, synchronize_session=False)
    user.poster_type = new_type
    mode_row = db.get(UserCurrentRole, user_id)
    mode_reset = mode_row is not None and mode_row.role == old
    if mode_reset:
        mode_row.role = "BUYER"
        db.add(RoleHistory(user_id=user_id, role="BUYER"))
    db.add(AuditLog(
        user_id=admin_id, action="POSTER_TYPE_CONVERTED", entity=f"user:{user_id}",
        before_json={"user_id": user_id, "poster_type": old},
        after_json={"user_id": user_id, "poster_type": new_type, "reason": reason[:500],
                    "converted_at": now.isoformat(), "revoked_postcode_ids": revoked,
                    "mode_reset_to_buyer": mode_reset},
        created_at=now,
    ))
    db.flush()
    return {"user_id": user_id, "old_poster_type": old, "new_poster_type": new_type,
            "reason": reason[:500], "converted_at": now, "converted_by": admin_id,
            "revoked_locality_grants": len(revoked)}


def switch_mode(db: Session, user_id: int, mode: str) -> str:
    """Change the app mode (user_current_role) without granting anything.

    BUYER: always. OWNER/BROKER: refused (409) when the account holds the
    other poster type. Records role_history as before.
    """
    if mode in POSTER_TYPES:
        current = get_poster_type(db, user_id)
        if current is not None and current != mode:
            raise _conflict(current, mode)
    return users_roles.set_current_role(db, user_id, mode)


def capabilities(db: Session, user_id: int) -> dict:
    """The account's capabilities as the backend enforces them."""
    poster_type = get_poster_type(db, user_id)
    plan = None
    trial = False
    if poster_type is not None:
        found = _posting_plan(db, user_id, poster_type)
        if found is not None:
            sub, p, trial = found
            plan = {
                "subscription_id": sub.id,
                "plan_code": p.code,
                "plan_name": p.name,
                "source": sub.source,
                "end_date": sub.end_date,
                "is_trial": trial,
            }
    if poster_type is None:
        poster_status = "NONE"
    elif plan is None:
        poster_status = "INACTIVE"  # never had a plan, or expired/cancelled/suspended
    else:
        poster_status = "TRIAL" if trial else "ACTIVE"
    allowed = ["BUYER"] + ([poster_type] if poster_type else list(POSTER_TYPES))
    return {
        "buyer": True,
        "poster_type": poster_type,
        "owner": poster_type == "OWNER",
        "dealer": poster_type == "BROKER",
        "poster_plan_status": poster_status,
        "poster_plan": plan,
        # Restricted listing operations also need the mode to be the poster type.
        "can_post": poster_status == "ACTIVE",
        "active_mode": users_roles.get_current_role(db, user_id),
        "allowed_modes": allowed,
    }
