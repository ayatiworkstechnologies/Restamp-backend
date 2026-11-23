"""RESTAMP Phase 3 — buyer property-contact unlocks and contact credits.

Finalized rules (approved; not reinterpreted here):
- Every account has exactly FREE_LIFETIME = 3 free property-contact unlocks
  over its whole lifetime. They never reset or renew (no 30-day window, not
  touched by subscriptions, mode switches, logout or device).
- An unlock is PERMANENT for (user, listing): reopening, refreshing, another
  device, another app mode -> no further charge (UNIQUE(user, listing)).
- After the free unlocks, one purchased credit unlocks one more property.
  Credits come in packs of PACK_CREDITS = 25, never expire and are
  independent of Owner/Dealer subscriptions. The pack PRICE is NOT decided:
  it is the admin-managed `plans` row PACK_CODE (role NULL = not a
  subscription), INACTIVE and unpurchasable until an admin prices it.
- Dealer locality access (agent mode, services/agent_plans) is a SEPARATE
  entitlement: the buyer router never calls this module for an agent-mode
  reveal inside the dealer's areas, so it neither consumes nor grants
  buyer unlocks/credits. The same account in BUYER mode uses this module.

Existing PLUS / PRO / UNLIMITED subscribers (backwards compatibility): their
purchased plan is honoured until it ends, exactly as before (same quota,
counted the same way: distinct reveals inside the subscription window,
UNLIMITED = no cap). While it has room, a NEW unlock uses the plan (source
PLAN) and consumes neither free unlocks nor credits; the unlock is permanent.
Nothing is converted into credits and nothing is removed.

Order for a NEW property: already unlocked (no charge) -> active legacy
plan with room -> free lifetime unlock -> purchased credit -> 402.

Concurrency: every unlock and every purchase first locks the user's
contact_credit_accounts row (created on demand with an upsert), so requests
of one user are serialised: two parallel requests can never spend the same
free unlock or credit, never both create an unlock for the same property,
and the balance can never go negative (CHECK constraint as a backstop).
Every new unlock writes exactly one ledger row (UNIQUE unlock_id); every
credited payment exactly one PURCHASE row (UNIQUE payment_id). Callers own
the transaction (routers commit only on success), so a failure rolls the
unlock, the deduction and the ledger row back together.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..models import (
    AuditLog,
    ContactCreditAccount,
    ContactCreditLedger,
    Payment,
    Plan,
    PlanVersion,
    PropertyContactUnlock,
)

FREE_LIFETIME = 3
PACK_CODE = "RESTAMP_CONTACT_CREDITS_25"
PACK_CREDITS = 25
# Kept for client compatibility: the 402 the apps already handle.
LEGACY_402_CODE = "BUYER_PLAN_REQUIRED"
CREDITS_REQUIRED = "CONTACT_CREDITS_REQUIRED"

ALREADY_UNLOCKED = "ALREADY_UNLOCKED"
UNLOCKED_FREE = "UNLOCKED_FREE"
UNLOCKED_CREDIT = "UNLOCKED_CREDIT"
UNLOCKED_PLAN = "UNLOCKED_PLAN"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


# --- pack -----------------------------------------------------------------------------


def pack_row(db: Session) -> Plan | None:
    return db.query(Plan).filter(Plan.code == PACK_CODE, Plan.role.is_(None)).first()


def pack_info(db: Session) -> dict:
    row = pack_row(db)
    priced = row is not None and row.status == "ACTIVE" and (row.price_paise or 0) > 0
    return {
        "code": PACK_CODE,
        "credits": PACK_CREDITS,
        "price_paise": row.price_paise if priced else None,
        "currency": "INR",
        "available": priced,
    }


def set_pack_price(db: Session, admin_id: int, price_paise: int, active: bool, note: str | None) -> dict:
    """Admin sets the (undecided) commercial price. Versioned + audited like
    other plan edits; the credit count (25) is a business constant, not
    editable here."""
    if not isinstance(price_paise, int) or price_paise < 100:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="price_paise must be at least 100")
    row = db.query(Plan).filter(Plan.code == PACK_CODE, Plan.role.is_(None)).with_for_update().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    before = {"price_paise": row.price_paise, "status": row.status}
    row.price_paise = price_paise
    row.status = "ACTIVE" if active else "INACTIVE"
    row.version = (row.version or 1) + 1
    row.updated_at = _now()
    row.updated_by_user_id = admin_id
    db.add(PlanVersion(plan_id=row.id, version=row.version, change_note=(note or "")[:500] or None,
                       changed_by_user_id=admin_id,
                       snapshot={"code": row.code, "price_paise": row.price_paise, "status": row.status,
                                 "credits": PACK_CREDITS}))
    db.add(AuditLog(user_id=admin_id, action="CONTACT_CREDIT_PACK_UPDATED", entity=f"plan:{row.id}",
                    before_json=before, after_json={"price_paise": row.price_paise, "status": row.status,
                                                    "version": row.version}, created_at=_now()))
    db.flush()
    return pack_info(db)


# --- accounts ----------------------------------------------------------------------------


def _lock_account(db: Session, user_id: int) -> ContactCreditAccount:
    """Create the account row if missing (race-safe upsert) and lock it."""
    t = _now()
    db.flush()
    db.execute(
        text("INSERT INTO contact_credit_accounts (user_id, free_unlocks_used, credit_balance, created_at, updated_at)"
             " VALUES (:u, 0, 0, :t, :t) ON DUPLICATE KEY UPDATE user_id = user_id"),
        {"u": user_id, "t": t},
    )
    return (db.query(ContactCreditAccount).filter(ContactCreditAccount.user_id == user_id)
            .with_for_update().populate_existing().one())


def _legacy_plan_room(db: Session, user_id: int):
    """(subscription, plan) when the user has an ACTIVE paid legacy buyer plan
    (PLUS/PRO/UNLIMITED or admin-created) with quota left in its window."""
    from . import buyer_plans  # existing quota logic, unchanged

    active = buyer_plans._active_paid(db, user_id)
    if active is None:
        return None
    sub, plan = active
    if plan.lead_limit is None:
        return sub, plan
    used = len(buyer_plans._revealed_ids(db, user_id, sub.start_date))
    return (sub, plan) if used < plan.lead_limit else None


def summary(db: Session, user_id: int) -> dict:
    """Read-only entitlement summary (GETs never write: no account row is
    created here)."""
    from . import buyer_plans

    acct = db.get(ContactCreditAccount, user_id)
    used = acct.free_unlocks_used if acct else 0
    unlocked = db.query(PropertyContactUnlock).filter(PropertyContactUnlock.user_id == user_id).count()
    legacy = None
    active = buyer_plans._active_paid(db, user_id)
    if active is not None:
        sub, plan = active
        legacy_used = len(buyer_plans._revealed_ids(db, user_id, sub.start_date))
        legacy = {"plan_code": plan.code, "plan_name": plan.name, "lead_limit": plan.lead_limit,
                  "leads_used": legacy_used,
                  "leads_remaining": None if plan.lead_limit is None else max(plan.lead_limit - legacy_used, 0),
                  "valid_until": sub.end_date}
    return {
        "free_unlocks_total": FREE_LIFETIME,
        "free_unlocks_used": used,
        "free_unlocks_remaining": max(FREE_LIFETIME - used, 0),
        "credit_balance": acct.credit_balance if acct else 0,
        "unlocked_properties": unlocked,
        "legacy_plan": legacy,
        "pack": pack_info(db),
    }


# --- unlock -------------------------------------------------------------------------------


def get_unlock(db: Session, user_id: int, listing_id: int) -> PropertyContactUnlock | None:
    return (db.query(PropertyContactUnlock)
            .filter(PropertyContactUnlock.user_id == user_id, PropertyContactUnlock.property_listing_id == listing_id)
            .first())


def unlock(db: Session, user_id: int, listing_id: int, phone_last4: str | None = None) -> dict:
    """Unlock `listing_id` for `user_id` (identity from the token, never the
    client). The caller has already proved the listing is visible and
    contactable and is not the caller's own. Returns the outcome; raises 402
    (nothing written) when no entitlement is left."""
    acct = _lock_account(db, user_id)
    existing = get_unlock(db, user_id, listing_id)
    if existing is not None:
        return _outcome(ALREADY_UNLOCKED, existing, acct)

    t = _now()
    room = _legacy_plan_room(db, user_id)
    if room is not None:
        source, entry, credits_delta, free_delta, sub_id, label = "PLAN", "PLAN_UNLOCK", 0, 0, room[0].id, UNLOCKED_PLAN
    elif acct.free_unlocks_used < FREE_LIFETIME:
        source, entry, credits_delta, free_delta, sub_id, label = "FREE", "FREE_UNLOCK", 0, 1, None, UNLOCKED_FREE
    elif acct.credit_balance > 0:
        source, entry, credits_delta, free_delta, sub_id, label = "CREDIT", "CREDIT_UNLOCK", -1, 0, None, UNLOCKED_CREDIT
    else:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "code": LEGACY_402_CODE,
                "reason": CREDITS_REQUIRED,
                "message": ("You've used your 3 free contact unlocks. Buy contact credits to unlock more owners; "
                            "properties you already unlocked stay unlocked."),
                "free_unlocks_total": FREE_LIFETIME,
                "free_unlocks_used": acct.free_unlocks_used,
                "credit_balance": acct.credit_balance,
                "pack": pack_info(db),
                "phone_last4": phone_last4,
            },
        )
    row = PropertyContactUnlock(user_id=user_id, property_listing_id=listing_id, source=source,
                                subscription_id=sub_id, unlocked_at=t)
    db.add(row)
    db.flush()  # UNIQUE(user, listing) backstop; the account lock makes a clash impossible
    acct.free_unlocks_used += free_delta
    acct.credit_balance += credits_delta
    acct.updated_at = t
    db.add(ContactCreditLedger(user_id=user_id, entry_type=entry, credits_delta=credits_delta, free_delta=free_delta,
                               balance_after=acct.credit_balance, unlock_id=row.id,
                               reference=f"listing:{listing_id}", created_at=t))
    db.flush()
    return _outcome(label, row, acct)


def _outcome(label: str, row: PropertyContactUnlock, acct: ContactCreditAccount) -> dict:
    return {
        "unlock_status": label,
        "unlock_source": row.source,
        "unlocked_at": row.unlocked_at,
        "free_unlocks_remaining": max(FREE_LIFETIME - acct.free_unlocks_used, 0),
        "credit_balance": acct.credit_balance,
    }


# --- purchase -------------------------------------------------------------------------------


def grant_for_payment(db: Session, payment: Payment) -> dict:
    """Credit PACK_CREDITS for a VERIFIED payment (caller holds the payment row
    FOR UPDATE after signature + amount verification). Idempotent: a payment
    that already has its PURCHASE ledger row adds nothing (UNIQUE payment_id
    is the database backstop)."""
    acct = _lock_account(db, payment.user_id)
    out = {"transaction_id": payment.provider_payment_id, "amount_paise": payment.amount_paise,
           "plan_code": PACK_CODE, "credits_added": 0}
    done = db.query(ContactCreditLedger.id).filter(ContactCreditLedger.payment_id == payment.id).first()
    if done is not None:
        return {**out, "activated": False, "credit_balance": acct.credit_balance}
    t = _now()
    acct.credit_balance += PACK_CREDITS
    acct.updated_at = t
    db.add(ContactCreditLedger(user_id=payment.user_id, entry_type="PURCHASE", credits_delta=PACK_CREDITS,
                               free_delta=0, balance_after=acct.credit_balance, payment_id=payment.id,
                               reference=f"payment:{payment.id}", created_at=t))
    db.flush()
    return {**out, "activated": True, "credits_added": PACK_CREDITS, "credit_balance": acct.credit_balance}


# --- history ---------------------------------------------------------------------------------


def list_unlocks(db: Session, user_id: int, page: int, page_size: int):
    q = db.query(PropertyContactUnlock).filter(PropertyContactUnlock.user_id == user_id)
    total = q.count()
    rows = (q.order_by(PropertyContactUnlock.unlocked_at.desc(), PropertyContactUnlock.id.desc())
            .offset((page - 1) * page_size).limit(page_size).all())
    return [{"listing_id": r.property_listing_id, "source": r.source, "unlocked_at": r.unlocked_at} for r in rows], total


def list_ledger(db: Session, user_id: int, page: int, page_size: int):
    q = db.query(ContactCreditLedger).filter(ContactCreditLedger.user_id == user_id)
    total = q.count()
    rows = q.order_by(ContactCreditLedger.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    unlock_ids = [r.unlock_id for r in rows if r.unlock_id]
    listing_of = dict(db.query(PropertyContactUnlock.id, PropertyContactUnlock.property_listing_id)
                      .filter(PropertyContactUnlock.id.in_(unlock_ids)).all()) if unlock_ids else {}
    return [{"id": r.id, "entry_type": r.entry_type, "credits_delta": r.credits_delta, "free_delta": r.free_delta,
             "balance_after": r.balance_after, "payment_id": r.payment_id,
             "listing_id": listing_of.get(r.unlock_id), "created_at": r.created_at} for r in rows], total
