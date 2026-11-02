"""RESTAMP admin services: listings, users, enquiries, dashboard, analytics,
audit log and admin staff.

Authorization is done by the router (deps.require_admin_permission); every
function here assumes an authorized admin and receives that admin's id as
the actor. Invariants:

- Reuse, don't fork: verification uses the owner lifecycle table
  (owner_listings._VERIFICATION_TRANSITIONS) and writes the same
  `verifications` rows (actor "admin"); listing detail is the owner card
  (owner_listings.get_owner_listing); enquiry threads are enquiries._threads.
  Buyer visibility (_visible) and the owner lifecycle are unchanged.
- Admin decisions only move PENDING listings (PENDING -> VERIFIED |
  REJECTED), exactly like the owner/auto paths; same-state repeats are
  idempotent and write nothing. Removed listings cannot be decided.
- Every state-changing admin action writes one audit_logs row (actor =
  admin user id, action, entity, before/after JSON with the target id and
  the reason when one applies; app-set UTC created_at). No phone numbers or
  tokens are ever written to the audit log.
- Lists never show phone numbers as names: names go through
  enquiries.safe_display_names (the existing phone-leak rule). A user's own
  phone is shown only on the user detail (users.view).
- Lead notes (owner-private) are never returned; enquiry statuses stay owned
  by the owner workflow (admins read enquiries, they do not change them).
- Metrics are real counts; anything without a backend source is listed in
  ``unavailable`` instead of being estimated.
- Safety rails: an admin cannot change their own account status or admin
  access, and at least one full-access admin (SUPER_ADMIN or legacy ADMIN)
  must always remain. Changing another admin's account status needs
  settings.manage_team.
Services flush; routers commit only on success.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..models import (
    AdminAccount,
    AuditLog,
    AuthIdentity,
    BrokerPostcodeAccessCurrent,
    City,
    District,
    Enquiry,
    Locality,
    PhysicalProperty,
    Postcode,
    PropertyListing,
    PropertyVisit,
    Taluk,
    User,
    UserCurrentRole,
    Verification,
    Village,
)
from . import admin_rbac, users_roles
from . import enquiries as enquiry_svc
from . import listings as listing_svc
from . import owner_listings as owner_svc

_ADMIN_RULESET = "admin-review-v1"  # verifications.ruleset_version for admin decisions
UNAVAILABLE_DASHBOARD = ("pending_kyc",)
UNAVAILABLE_ANALYTICS = ("listings_added_per_month", "approval_sla")


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def _utc(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=timezone.utc) if value is not None else None


def _not_found():
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


def _unprocessable(detail: str):
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


def record_audit(db: Session, admin_id: int, action: str, entity: str, before: dict | None, after: dict | None) -> None:
    db.add(AuditLog(
        user_id=admin_id, action=action, entity=entity,
        before_json=before, after_json=after, created_at=_utcnow_naive(),
    ))


def _like(term: str) -> str:
    return f"%{listing_svc.escape_like_term(term)}%"


# --- admin identity -----------------------------------------------------------------

def admin_profile(db: Session, admin_id: int) -> dict:
    user = users_roles.get_user(db, admin_id)
    role = users_roles.get_admin_role(db, admin_id)
    return {
        "user_id": user.id,
        "display_name": user.display_name,
        "admin_role": role,
        "role_label": admin_rbac.ROLE_LABELS.get(role, role),
        "permissions": sorted(admin_rbac.permissions_for(role)),
    }


def roles_matrix() -> list[dict]:
    return [
        {
            "code": code,
            "label": admin_rbac.ROLE_LABELS[code],
            "assignable": code in admin_rbac.ASSIGNABLE_ROLES,
            "permissions": sorted(admin_rbac.ROLE_PERMISSIONS[code]),
        }
        for code in (*admin_rbac.ASSIGNABLE_ROLES, "ADMIN")
    ]


# --- listings ---------------------------------------------------------------------------

def list_listings(
    db: Session,
    page: int,
    page_size: int,
    verification_status: str | None = None,
    listing_status: str | None = None,
    property_type: str | None = None,
    owner_user_id: int | None = None,
    q: str | None = None,
    include_removed: bool = False,
    sort: str = "newest",
) -> tuple[list[dict], int]:
    """Every listing regardless of buyer visibility (admins review all of
    them). Removed (soft-deleted) listings only with include_removed."""
    base = listing_svc._location_joins(db.query(
        PropertyListing.id, PropertyListing.title, PropertyListing.property_type,
        PropertyListing.transaction_type, PropertyListing.price_paise, PropertyListing.price_period,
        PropertyListing.verification_status, PropertyListing.listing_status,
        PropertyListing.submitted_at, PropertyListing.deleted_at, PropertyListing.user_id,
        Locality.name.label("locality"), City.name.label("city"), Postcode.pincode,
    ))
    if not include_removed:
        base = base.filter(PropertyListing.deleted_at.is_(None))
    if verification_status is not None:
        base = base.filter(PropertyListing.verification_status == verification_status)
    if listing_status is not None:
        base = base.filter(PropertyListing.listing_status == listing_status)
    if property_type is not None:
        base = base.filter(PropertyListing.property_type == property_type)
    if owner_user_id is not None:
        base = base.filter(PropertyListing.user_id == owner_user_id)
    if q:
        conds = []
        for term in q.split()[:5]:
            like = _like(term)
            conds.append(or_(
                PropertyListing.title.ilike(like, escape="\\"),
                Locality.name.ilike(like, escape="\\"),
                City.name.ilike(like, escape="\\"),
                Postcode.pincode == term,
                PropertyListing.id == (int(term) if term.isdigit() else -1),
            ))
        base = base.filter(*conds)
    total = base.with_entities(func.count(func.distinct(PropertyListing.id))).scalar() or 0
    if sort == "submitted_oldest":  # the approval queue: longest-waiting first
        base = base.order_by(PropertyListing.submitted_at.is_(None), PropertyListing.submitted_at.asc(),
                             PropertyListing.id.asc())
    else:
        base = base.order_by(PropertyListing.id.desc())
    rows = base.offset((page - 1) * page_size).limit(page_size).all()
    names = enquiry_svc.safe_display_names(db, [r.user_id for r in rows])
    covers = listing_svc.fetch_cover_map(db, [int(r.id) for r in rows])
    return [
        {
            "listing_id": int(r.id),
            "title": r.title,
            "property_type": r.property_type,
            "transaction_type": r.transaction_type,
            "price_paise": r.price_paise,
            "price_period": r.price_period,
            "price_display": listing_svc.format_price(r.price_paise, r.price_period),
            "verification_status": r.verification_status,
            "listing_status": r.listing_status,
            "removed": r.deleted_at is not None,
            "submitted_at": _utc(r.submitted_at),
            "owner_user_id": int(r.user_id),
            "owner_name": names.get(int(r.user_id)),
            "locality": r.locality,
            "city": r.city,
            "pincode": r.pincode,
            "cover_image_url": covers.get(int(r.id)),
        }
        for r in rows
    ], total


def _verification_history(db: Session, listing_id: int) -> list[dict]:
    rows = (
        db.query(Verification)
        .filter(Verification.property_listing_id == listing_id)
        .order_by(Verification.attempt_no, Verification.id)
        .all()
    )
    out = []
    for v in rows:
        checks = v.checks_json if isinstance(v.checks_json, dict) else {}
        out.append({
            "attempt_no": v.attempt_no,
            "action": checks.get("action"),
            "actor": checks.get("actor"),
            "result": v.result,
            "reason": checks.get("reason"),
            "category": checks.get("category"),
            "triggered_at": _utc(v.triggered_at),
            "triggered_by_user_id": v.triggered_by,
        })
    return out


def get_listing(db: Session, listing_id: int) -> dict:
    listing = db.get(PropertyListing, listing_id)
    if listing is None:
        raise _not_found()
    physical = db.get(PhysicalProperty, listing.physical_property_id)
    card = None
    if physical is not None and physical.user_id == listing.user_id:
        card = owner_svc.get_owner_listing(db, int(listing.user_id), listing_id)
    if card is None:
        # Ownership columns disagree: the owner card cannot be built safely.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Listing record is inconsistent")
    owner = db.get(User, listing.user_id)
    return {
        "listing": card,
        "owner": {
            "user_id": int(listing.user_id),
            "display_name": enquiry_svc.safe_display_names(db, [listing.user_id]).get(int(listing.user_id)),
            "status": owner.status if owner is not None else None,
        },
        "verification_history": _verification_history(db, listing_id),
    }


def decide_listing(
    db: Session,
    admin_id: int,
    listing_id: int,
    to_status: str,
    reason: str | None = None,
    category: str | None = None,
    notes: str | None = None,
) -> None:
    """Admin verify (to_status VERIFIED) or reject (REJECTED) of a PENDING
    listing. Same-state repeats are idempotent (no rows written). Writes a
    `verifications` row (actor admin) and an audit_logs row. Flushes."""
    if to_status not in ("VERIFIED", "REJECTED"):
        raise ValueError("to_status must be VERIFIED or REJECTED")
    listing = db.query(PropertyListing).filter(PropertyListing.id == listing_id).with_for_update().first()
    if listing is None:
        raise _not_found()
    if listing.deleted_at is not None:
        raise _unprocessable("Listing has been removed")
    current = listing.verification_status
    if current == to_status:
        return  # idempotent repeat
    if current != "PENDING" or to_status not in owner_svc._VERIFICATION_TRANSITIONS.get(current, set()):
        raise _unprocessable("Only pending listings can be verified or rejected")
    listing.verification_status = to_status
    action = "verified" if to_status == "VERIFIED" else "rejected"
    checks: dict = {"action": action, "actor": "admin"}
    if reason:
        checks["reason"] = reason
    if category:
        checks["category"] = category
    if notes:
        checks["notes"] = notes
    last_attempt = (
        db.query(func.max(Verification.attempt_no))
        .filter(Verification.property_listing_id == listing_id)
        .scalar()
    )
    now = _utcnow_naive()
    db.add(Verification(
        property_listing_id=listing_id,
        attempt_no=int(last_attempt or 0) + 1,
        ruleset_version=_ADMIN_RULESET,
        checks_json=checks,
        result="PASS" if to_status == "VERIFIED" else "FAIL",
        triggered_at=now,
        triggered_by=admin_id,
    ))
    after = {"listing_id": listing_id, "verification_status": to_status}
    for key, value in (("reason", reason), ("category", category), ("notes", notes)):
        if value:
            after[key] = value
    record_audit(
        db, admin_id, f"ADMIN_LISTING_{to_status}", "property_listing",
        {"listing_id": listing_id, "verification_status": current}, after,
    )
    db.flush()


# --- users -----------------------------------------------------------------------------

def _phone_of(db: Session, user_id: int) -> str | None:
    row = (
        db.query(AuthIdentity.provider_identifier)
        .filter(AuthIdentity.user_id == user_id, AuthIdentity.provider == "phone")
        .first()
    )
    return row[0] if row else None


def list_users(
    db: Session,
    page: int,
    page_size: int,
    role: str | None = None,
    account_status: str | None = None,
    admins_only: bool | None = None,
    q: str | None = None,
) -> tuple[list[dict], int]:
    base = (
        db.query(User.id, User.display_name, User.status, User.created_at,
                 UserCurrentRole.role, AdminAccount.admin_role)
        .outerjoin(UserCurrentRole, UserCurrentRole.user_id == User.id)
        .outerjoin(AdminAccount, AdminAccount.user_id == User.id)
    )
    if role is not None:
        base = base.filter(UserCurrentRole.role == role)
    if account_status is not None:
        base = base.filter(User.status == account_status)
    if admins_only is True:
        base = base.filter(AdminAccount.user_id.is_not(None))
    elif admins_only is False:
        base = base.filter(AdminAccount.user_id.is_(None))
    if q:
        term = q.strip()
        digits = re.sub(r"\D", "", term)
        conds = [User.display_name.ilike(_like(term), escape="\\")]
        if digits:
            phone_ids = db.query(AuthIdentity.user_id).filter(
                AuthIdentity.provider == "phone",
                AuthIdentity.provider_identifier.like(f"%{digits}%"),
            )
            conds.append(User.id.in_(phone_ids))
        if term.isdigit():
            conds.append(User.id == int(term))
        base = base.filter(or_(*conds))
    total = base.with_entities(func.count(User.id)).scalar() or 0
    rows = base.order_by(User.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    names = enquiry_svc.safe_display_names(db, [r.id for r in rows])
    phones = dict(
        db.query(AuthIdentity.user_id, AuthIdentity.provider_identifier)
        .filter(AuthIdentity.user_id.in_([r.id for r in rows] or [-1]), AuthIdentity.provider == "phone")
        .all()
    )
    return [
        {
            "user_id": int(r.id),
            "display_name": names.get(int(r.id)),
            "status": r.status,
            "current_role": r.role,
            "admin_role": r.admin_role,
            # Lists show only the last four digits; the full number is on the
            # user detail (users.view) for support.
            "phone_last4": (phones.get(r.id) or "")[-4:] or None,
            "joined_at": r.created_at,
        }
        for r in rows
    ], total


def get_user(db: Session, user_id: int) -> dict:
    user = db.get(User, user_id)
    if user is None:
        raise _not_found()
    providers = sorted({p for (p,) in db.query(AuthIdentity.provider).filter(AuthIdentity.user_id == user_id).all()})
    now = _utcnow_naive()

    def count(q):
        return q.scalar() or 0

    return {
        "user_id": user.id,
        "display_name": user.display_name,
        "status": user.status,
        "current_role": users_roles.get_current_role(db, user_id),
        "admin_role": users_roles.get_admin_role(db, user_id),
        "phone": _phone_of(db, user_id),
        "providers": providers,
        "joined_at": user.created_at,
        "activity": {
            "listings_owned": count(db.query(func.count(PropertyListing.id)).filter(
                PropertyListing.user_id == user_id, PropertyListing.deleted_at.is_(None))),
            "enquiries_sent": count(db.query(func.count(Enquiry.id)).filter(Enquiry.buyer_user_id == user_id)),
            "enquiries_received": count(db.query(func.count(Enquiry.id)).filter(Enquiry.owner_user_id == user_id)),
            "visits_requested": count(db.query(func.count(PropertyVisit.id)).filter(
                PropertyVisit.buyer_user_id == user_id)),
            "broker_covered_locations": count(db.query(func.count(BrokerPostcodeAccessCurrent.postcode_id)).filter(
                BrokerPostcodeAccessCurrent.broker_user_id == user_id,
                or_(BrokerPostcodeAccessCurrent.expires_at.is_(None), BrokerPostcodeAccessCurrent.expires_at > now))),
        },
    }


def set_user_status(db: Session, admin_id: int, user_id: int, new_status: str, reason: str | None) -> None:
    """Suspend (new_status 'suspended') or reactivate ('active') an account.
    Idempotent on same state. Flushes."""
    if new_status not in ("active", "suspended"):
        raise ValueError("new_status must be active or suspended")
    if user_id == admin_id:
        raise _unprocessable("You cannot change your own account status")
    user = db.query(User).filter(User.id == user_id).with_for_update().first()
    if user is None:
        raise _not_found()
    target_admin_role = users_roles.get_admin_role(db, user_id)
    if target_admin_role is not None and not admin_rbac.has_permission(
        users_roles.get_admin_role(db, admin_id), "settings.manage_team"
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="settings.manage_team permission required")
    if user.status == "closed":
        raise _unprocessable("Closed accounts cannot be changed")
    if user.status == new_status:
        return
    if new_status == "suspended" and target_admin_role in admin_rbac.FULL_ACCESS_ROLES:
        _ensure_other_full_access_admin(db, user_id)
    previous = user.status
    user.status = new_status
    after = {"user_id": user_id, "status": new_status}
    if reason:
        after["reason"] = reason
    record_audit(
        db, admin_id, "ADMIN_USER_SUSPENDED" if new_status == "suspended" else "ADMIN_USER_REACTIVATED",
        "user", {"user_id": user_id, "status": previous}, after,
    )
    db.flush()


# --- enquiries (read-only) ------------------------------------------------------------

def _enquiry_items(db: Session, rows: list[Enquiry], with_messages: bool) -> list[dict]:
    threads = enquiry_svc._threads(db, rows)
    names = enquiry_svc.safe_display_names(
        db, [e.buyer_user_id for e in rows] + [e.owner_user_id for e in rows])
    titles = dict(
        db.query(PropertyListing.id, PropertyListing.title)
        .filter(PropertyListing.id.in_([e.property_listing_id for e in rows] or [-1]))
        .all()
    )
    items = []
    for e in rows:
        thread = threads[int(e.id)]
        item = {
            "id": int(e.id),
            "status": e.status,
            "closing_reason": e.closing_reason,
            "created_at": _utc(e.created_at),
            "property_listing_id": int(e.property_listing_id),
            "listing_title": titles.get(e.property_listing_id),
            "buyer_user_id": int(e.buyer_user_id),
            "buyer_name": names.get(int(e.buyer_user_id)),
            "owner_user_id": int(e.owner_user_id),
            "owner_name": names.get(int(e.owner_user_id)),
            **enquiry_svc._thread_summary(thread),
        }
        if with_messages:
            item["messages"] = thread
        items.append(item)
    return items


def list_enquiries(
    db: Session,
    page: int,
    page_size: int,
    enquiry_status: str | None = None,
    property_listing_id: int | None = None,
    buyer_user_id: int | None = None,
    owner_user_id: int | None = None,
) -> tuple[list[dict], int]:
    base = db.query(Enquiry)
    if enquiry_status is not None:
        base = base.filter(Enquiry.status == enquiry_status)
    if property_listing_id is not None:
        base = base.filter(Enquiry.property_listing_id == property_listing_id)
    if buyer_user_id is not None:
        base = base.filter(Enquiry.buyer_user_id == buyer_user_id)
    if owner_user_id is not None:
        base = base.filter(Enquiry.owner_user_id == owner_user_id)
    total = base.with_entities(func.count(Enquiry.id)).scalar() or 0
    rows = base.order_by(Enquiry.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return _enquiry_items(db, rows, with_messages=False), total


def get_enquiry(db: Session, enquiry_id: int) -> dict:
    enquiry = db.get(Enquiry, enquiry_id)
    if enquiry is None:
        raise _not_found()
    return _enquiry_items(db, [enquiry], with_messages=True)[0]


# --- dashboard and analytics ---------------------------------------------------------

def dashboard(db: Session) -> dict:
    """Marketplace counts, each defined in schemas.AdminDashboardOut."""
    live_listing = PropertyListing.deleted_at.is_(None)

    def listings(*conds):
        return db.query(func.count(PropertyListing.id)).filter(live_listing, *conds).scalar() or 0

    role_counts = dict(db.query(UserCurrentRole.role, func.count(UserCurrentRole.user_id))
                       .group_by(UserCurrentRole.role).all())
    return {
        "listings_total": listings(),
        "listings_live": listings(PropertyListing.verification_status == "VERIFIED",
                                  PropertyListing.listing_status == "AVAILABLE"),
        "listings_pending": listings(PropertyListing.verification_status == "PENDING"),
        "listings_rejected": listings(PropertyListing.verification_status == "REJECTED"),
        "enquiries_total": db.query(func.count(Enquiry.id)).scalar() or 0,
        "enquiries_open": db.query(func.count(Enquiry.id)).filter(Enquiry.status != "CLOSED").scalar() or 0,
        "users_total": db.query(func.count(User.id)).scalar() or 0,
        "users_suspended": db.query(func.count(User.id)).filter(User.status == "suspended").scalar() or 0,
        "buyers": int(role_counts.get("BUYER", 0)),
        "owners": int(role_counts.get("OWNER", 0)),
        "brokers": int(role_counts.get("BROKER", 0)),
        "admins": db.query(func.count(AdminAccount.user_id)).scalar() or 0,
        "unavailable": list(UNAVAILABLE_DASHBOARD),
    }


def _month_key(value: datetime) -> str:
    return f"{value.year:04d}-{value.month:02d}"


def analytics(db: Session, months: int) -> dict:
    """Verification decisions per UTC month (last `months` months including
    the current one), listings per property type, and the localities with
    the most enquiries (all time)."""
    now = _utcnow_naive()
    keys: list[str] = []
    year, month = now.year, now.month
    for _ in range(months):
        keys.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    keys.reverse()
    start = datetime(int(keys[0][:4]), int(keys[0][5:]), 1)
    decided: dict[str, Counter] = defaultdict(Counter)
    for triggered_at, checks in (
        db.query(Verification.triggered_at, Verification.checks_json)
        .filter(Verification.triggered_at >= start)
        .all()
    ):
        action = checks.get("action") if isinstance(checks, dict) else None
        if action in ("verified", "rejected") and triggered_at is not None:
            decided[_month_key(triggered_at)][action] += 1
    by_type = (
        db.query(PropertyListing.property_type, func.count(PropertyListing.id))
        .filter(PropertyListing.deleted_at.is_(None))
        .group_by(PropertyListing.property_type)
        .order_by(func.count(PropertyListing.id).desc(), PropertyListing.property_type)
        .all()
    )
    localities = (
        db.query(Locality.name, City.name, func.count(Enquiry.id))
        .select_from(Enquiry)
        .join(PropertyListing, PropertyListing.id == Enquiry.property_listing_id)
        .join(PhysicalProperty, PhysicalProperty.id == PropertyListing.physical_property_id)
        .join(Postcode, Postcode.id == PhysicalProperty.postcode_id)
        .join(Locality, Locality.id == Postcode.locality_id)
        .join(Village, Village.id == Locality.village_id)
        .join(Taluk, Taluk.id == Village.taluk_id)
        .join(District, District.id == Taluk.district_id)
        .join(City, City.id == District.city_id)
        .group_by(Locality.name, City.name)
        .order_by(func.count(Enquiry.id).desc(), Locality.name)
        .limit(10)
        .all()
    )
    return {
        "months": [
            {"month": k, "verified": decided[k]["verified"], "rejected": decided[k]["rejected"]}
            for k in keys
        ],
        "listings_by_type": [{"property_type": t, "count": int(n)} for t, n in by_type],
        "top_enquiry_localities": [
            {"locality": loc, "city": city, "enquiries": int(n)} for loc, city, n in localities
        ],
        "unavailable": list(UNAVAILABLE_ANALYTICS),
    }


# --- audit log --------------------------------------------------------------------------

def list_audit_logs(
    db: Session,
    page: int,
    page_size: int,
    action: str | None = None,
    entity: str | None = None,
    actor_user_id: int | None = None,
) -> tuple[list[dict], int]:
    base = db.query(AuditLog)
    if action is not None:
        base = base.filter(AuditLog.action == action)
    if entity is not None:
        base = base.filter(AuditLog.entity == entity)
    if actor_user_id is not None:
        base = base.filter(AuditLog.user_id == actor_user_id)
    total = base.with_entities(func.count(AuditLog.id)).scalar() or 0
    rows = base.order_by(AuditLog.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    names = enquiry_svc.safe_display_names(db, [r.user_id for r in rows])
    return [
        {
            "id": int(r.id),
            "actor_user_id": int(r.user_id),
            "actor_name": names.get(int(r.user_id)),
            "action": r.action,
            "entity": r.entity,
            "before": r.before_json,
            "after": r.after_json,
            "created_at": _utc(r.created_at),
        }
        for r in rows
    ], total


# --- admin staff --------------------------------------------------------------------

def _full_access_admin_count(db: Session, excluding_user_id: int | None = None) -> int:
    q = (
        db.query(func.count(AdminAccount.user_id))
        .join(User, User.id == AdminAccount.user_id)
        .filter(AdminAccount.admin_role.in_(admin_rbac.FULL_ACCESS_ROLES), User.status == "active")
    )
    if excluding_user_id is not None:
        q = q.filter(AdminAccount.user_id != excluding_user_id)
    return q.scalar() or 0


def _ensure_other_full_access_admin(db: Session, user_id: int) -> None:
    if _full_access_admin_count(db, excluding_user_id=user_id) < 1:
        raise _unprocessable("At least one active Super Admin must remain")


def list_staff(db: Session) -> list[dict]:
    rows = (
        db.query(AdminAccount.user_id, AdminAccount.admin_role, User.display_name, User.status)
        .join(User, User.id == AdminAccount.user_id)
        .order_by(AdminAccount.user_id)
        .all()
    )
    names = enquiry_svc.safe_display_names(db, [r.user_id for r in rows])
    return [
        {
            "user_id": int(r.user_id),
            "display_name": names.get(int(r.user_id)),
            "admin_role": r.admin_role,
            "role_label": admin_rbac.ROLE_LABELS.get(r.admin_role, r.admin_role),
            "status": r.status,
        }
        for r in rows
    ]


def _staff_row(db: Session, user_id: int) -> dict:
    return next(s for s in list_staff(db) if s["user_id"] == user_id)


def grant_staff(db: Session, admin_id: int, user_id: int, admin_role: str) -> dict:
    if admin_role not in admin_rbac.ASSIGNABLE_ROLES:
        raise _unprocessable("Unknown admin role")
    user = db.get(User, user_id)
    if user is None:
        raise _not_found()
    if user.status != "active":
        raise _unprocessable("User account is not active")
    if users_roles.is_admin(db, user_id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already admin")
    users_roles.grant_admin(db, user_id, admin_role)
    record_audit(db, admin_id, "ADMIN_STAFF_GRANTED", "admin_account", None,
                 {"user_id": user_id, "admin_role": admin_role})
    db.flush()
    return _staff_row(db, user_id)


def change_staff_role(db: Session, admin_id: int, user_id: int, admin_role: str) -> dict:
    if admin_role not in admin_rbac.ASSIGNABLE_ROLES:
        raise _unprocessable("Unknown admin role")
    if user_id == admin_id:
        raise _unprocessable("You cannot change your own admin access")
    link = db.query(AdminAccount).filter(AdminAccount.user_id == user_id).with_for_update().first()
    if link is None:
        raise _not_found()
    if link.admin_role == admin_role:
        return _staff_row(db, user_id)
    if link.admin_role in admin_rbac.FULL_ACCESS_ROLES and admin_role not in admin_rbac.FULL_ACCESS_ROLES:
        _ensure_other_full_access_admin(db, user_id)
    previous = link.admin_role
    link.admin_role = admin_role
    record_audit(db, admin_id, "ADMIN_STAFF_ROLE_CHANGED", "admin_account",
                 {"user_id": user_id, "admin_role": previous}, {"user_id": user_id, "admin_role": admin_role})
    db.flush()
    return _staff_row(db, user_id)


def revoke_staff(db: Session, admin_id: int, user_id: int) -> None:
    if user_id == admin_id:
        raise _unprocessable("You cannot change your own admin access")
    link = db.query(AdminAccount).filter(AdminAccount.user_id == user_id).with_for_update().first()
    if link is None:
        raise _not_found()
    if link.admin_role in admin_rbac.FULL_ACCESS_ROLES:
        _ensure_other_full_access_admin(db, user_id)
    previous = link.admin_role
    db.delete(link)
    record_audit(db, admin_id, "ADMIN_STAFF_REVOKED", "admin_account",
                 {"user_id": user_id, "admin_role": previous}, None)
    db.flush()
