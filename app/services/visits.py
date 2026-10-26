"""RESTAMP property visits — buyer/owner scheduled viewings.

States: REQUESTED (buyer-initiated, awaiting owner), PROPOSED (owner-
initiated, awaiting buyer), CONFIRMED, DECLINED, CANCELLED, COMPLETED.

Actor rules (enforced here, never in the frontend):
- REQUESTED -> CONFIRMED/DECLINED: owner only; -> CANCELLED: either;
  time edits: buyer only.
- PROPOSED -> CONFIRMED/DECLINED: buyer only; -> CANCELLED: either;
  time edits: owner only.
- CONFIRMED -> COMPLETED: owner only; -> CANCELLED or time edit: either.
- DECLINED/CANCELLED/COMPLETED are terminal; same-state repeats are
  idempotent no-ops. Anything else raises ValueError (-> 422).

Creation rules: the listing must be VERIFIED + AVAILABLE with consistent
dual ownership columns; scheduled_at must be in the future; buyers cannot
schedule on their own listings; owners may propose only to buyers holding
an existing enquiry or contact-reveal relationship on that listing (no
arbitrary user IDs). Responses carry no phones and no display names.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from ..models import ContactRevealAudit, Enquiry, PhysicalProperty, PropertyListing
from ..models.visits import PropertyVisit


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _normalize_scheduled_at(value: datetime) -> datetime:
    """Normalize to naive UTC and require the future; else ValueError."""
    if value is None or not isinstance(value, datetime):
        raise ValueError("scheduled_at must be a date and time")
    moment = value
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    if moment <= _utcnow_naive():
        raise ValueError("scheduled_at must be in the future")
    return moment


def _visible_listing(db: Session, listing_id: int) -> PropertyListing:
    listing = (
        db.query(PropertyListing)
        .filter(
            PropertyListing.id == listing_id,
            PropertyListing.verification_status == "VERIFIED",
            PropertyListing.listing_status == "AVAILABLE",
        )
        .first()
    )
    if listing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return listing


def _consistent_owner_id(db: Session, listing: PropertyListing) -> int:
    """Fail-closed dual-authority check; returns the owner id or 404s."""
    physical = db.get(PhysicalProperty, listing.physical_property_id)
    if physical is None or physical.user_id != listing.user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return physical.user_id


def _snapshot(db: Session, visit: PropertyVisit) -> dict:
    listing = db.get(PropertyListing, visit.property_listing_id)
    return {
        "id": visit.id,
        "property_listing_id": visit.property_listing_id,
        "buyer_user_id": visit.buyer_user_id,
        "owner_user_id": visit.owner_user_id,
        "scheduled_at": visit.scheduled_at,
        "status": visit.status,
        "initiator": visit.initiator,
        "note": visit.note,
        "listing": {
            "title": listing.title,
            "price_paise": listing.price_paise,
            "price_period": listing.price_period,
        },
        "created_at": visit.created_at,
        "updated_at": visit.updated_at,
    }


def create_buyer_visit(
    db: Session,
    buyer_id: int,
    listing_id: int,
    scheduled_at: datetime,
    note: str | None,
) -> dict:
    """Buyer schedules a visit (lands REQUESTED). Returns the visit body."""
    listing = _visible_listing(db, listing_id)
    owner_id = _consistent_owner_id(db, listing)
    if buyer_id == owner_id:
        raise ValueError("cannot schedule a visit on your own listing")
    moment = _normalize_scheduled_at(scheduled_at)
    if _has_active_visit_for_buyer_listing(db, listing_id, buyer_id, initiator="BUYER"):
        raise ValueError("A visit is already active for this buyer on this listing")
    visit = PropertyVisit(
        property_listing_id=listing_id,
        buyer_user_id=buyer_id,
        owner_user_id=owner_id,
        scheduled_at=moment,
        status="REQUESTED",
        initiator="BUYER",
        note=note,
        created_at=_utcnow_naive(),
        updated_at=_utcnow_naive(),
    )
    db.add(visit)
    db.flush()
    db.refresh(visit)
    return _snapshot(db, visit)


def _buyer_has_relationship(db: Session, listing_id: int, buyer_id: int) -> bool:
    """True when the buyer holds an enquiry or contact-reveal relationship
    on the listing (either direction of past contact counts)."""
    if (
        db.query(Enquiry.id)
        .filter(
            Enquiry.property_listing_id == listing_id,
            Enquiry.buyer_user_id == buyer_id,
        )
        .first()
        is not None
    ):
        return True
    return (
        db.query(ContactRevealAudit)
        .filter(
            ContactRevealAudit.property_listing_id == listing_id,
            ContactRevealAudit.user_id == buyer_id,
        )
        .first()
        is not None
    )


def _has_active_visit_for_buyer_listing(
    db: Session,
    listing_id: int,
    buyer_id: int,
    initiator: str | None = None,
    exclude_visit_id: int | None = None,
) -> bool:
    """True when a visit already exists for the same listing + buyer pair in
    a pending state (PROPOSED or REQUESTED), excluding an optional visit
    being rescheduled. When `initiator` is given, only visits started by the
    same initiator are considered, so an owner-proposed visit does not block
    a buyer-requested visit (and vice versa) for the same buyer+listing.

    Narrowest conflict rule: one pending visit per buyer per listing per
    initiator at a time. CONFIRMED visits are scheduled and do not block a
    new proposal; CANCELLED, DECLINED, and COMPLETED visits are terminal
    and allow legitimate repeat visits.
    """
    q = (
        db.query(PropertyVisit.id)
        .filter(
            PropertyVisit.property_listing_id == listing_id,
            PropertyVisit.buyer_user_id == buyer_id,
            PropertyVisit.status.in_(("PROPOSED", "REQUESTED")),
        )
    )
    if initiator is not None:
        q = q.filter(PropertyVisit.initiator == initiator)
    if exclude_visit_id is not None:
        q = q.filter(PropertyVisit.id != exclude_visit_id)
    return q.first() is not None


def create_owner_visit(
    db: Session,
    owner_id: int,
    listing_id: int,
    buyer_user_id: int,
    scheduled_at: datetime,
    note: str | None,
) -> dict:
    """Owner proposes a visit to a related buyer (lands PROPOSED)."""
    from ..models import User

    listing = _visible_listing(db, listing_id)
    if _consistent_owner_id(db, listing) != owner_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if db.get(User, buyer_user_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if buyer_user_id == owner_id:
        raise ValueError("cannot propose a visit to yourself")
    if not _buyer_has_relationship(db, listing_id, buyer_user_id):
        raise ValueError("buyer has no enquiry or contact relationship on this listing")
    moment = _normalize_scheduled_at(scheduled_at)
    if _has_active_visit_for_buyer_listing(
        db, listing_id, buyer_user_id, initiator="OWNER"
    ):
        raise ValueError("A visit is already active for this buyer on this listing")
    visit = PropertyVisit(
        property_listing_id=listing_id,
        buyer_user_id=buyer_user_id,
        owner_user_id=owner_id,
        scheduled_at=moment,
        status="PROPOSED",
        initiator="OWNER",
        note=note,
        created_at=_utcnow_naive(),
        updated_at=_utcnow_naive(),
    )
    db.add(visit)
    db.flush()
    db.refresh(visit)
    return _snapshot(db, visit)


def list_buyer_visits(
    db: Session, buyer_id: int, page: int, page_size: int, stat: str | None = None
) -> tuple[list[dict], int]:
    from sqlalchemy import func as _func

    base = db.query(PropertyVisit).filter(PropertyVisit.buyer_user_id == buyer_id)
    if stat is not None:
        base = base.filter(PropertyVisit.status == stat)
    total = base.with_entities(_func.count(PropertyVisit.id)).scalar() or 0
    rows = (
        base.order_by(PropertyVisit.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return [_snapshot(db, v) for v in rows], total


def get_buyer_visit(db: Session, buyer_id: int, visit_id: int) -> dict:
    visit = db.get(PropertyVisit, visit_id)
    if visit is None or visit.buyer_user_id != buyer_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return _snapshot(db, visit)


def list_owner_visits(
    db: Session,
    owner_id: int,
    page: int,
    page_size: int,
    property_listing_id: int | None = None,
    stat: str | None = None,
) -> tuple[list[dict], int]:
    from sqlalchemy import func as _func

    if property_listing_id is not None:
        owned = (
            db.query(PropertyListing.id)
            .join(PhysicalProperty, PhysicalProperty.id == PropertyListing.physical_property_id)
            .filter(
                PropertyListing.id == property_listing_id,
                PropertyListing.user_id == owner_id,
                PhysicalProperty.user_id == owner_id,
            )
            .first()
        )
        if owned is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    base = db.query(PropertyVisit).filter(PropertyVisit.owner_user_id == owner_id)
    if property_listing_id is not None:
        base = base.filter(PropertyVisit.property_listing_id == property_listing_id)
    if stat is not None:
        base = base.filter(PropertyVisit.status == stat)
    total = base.with_entities(_func.count(PropertyVisit.id)).scalar() or 0
    rows = (
        base.order_by(PropertyVisit.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return [_snapshot(db, v) for v in rows], total


def get_owner_visit(db: Session, owner_id: int, visit_id: int) -> dict:
    visit = db.get(PropertyVisit, visit_id)
    if visit is None or visit.owner_user_id != owner_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return _snapshot(db, visit)


# Allowed (status, actor) moves; time edits are gated separately below.
# "either" = the visit's buyer or owner; otherwise the named party only.
_ALLOWED = {
    ("REQUESTED", "REQUESTED"): ("buyer",),
    ("REQUESTED", "CONFIRMED"): ("owner",),
    ("REQUESTED", "DECLINED"): ("owner",),
    ("REQUESTED", "CANCELLED"): ("either",),
    ("PROPOSED", "PROPOSED"): ("owner",),
    ("PROPOSED", "CONFIRMED"): ("buyer",),
    ("PROPOSED", "DECLINED"): ("buyer",),
    ("PROPOSED", "CANCELLED"): ("either",),
    ("CONFIRMED", "CONFIRMED"): ("either",),
    ("CONFIRMED", "CANCELLED"): ("either",),
    ("CONFIRMED", "COMPLETED"): ("owner",),
    ("DECLINED", "DECLINED"): ("either",),
    ("CANCELLED", "CANCELLED"): ("either",),
    ("COMPLETED", "COMPLETED"): ("either",),
}

# Who may move the clock in each status (time-only edits keep status).
_TIME_EDITORS = {
    "REQUESTED": ("buyer",),
    "PROPOSED": ("owner",),
    "CONFIRMED": ("either",),
}


def transition_visit(
    db: Session,
    actor: str,
    actor_id: int,
    visit_id: int,
    scheduled_at: datetime | None = None,
    to_status: str | None = None,
) -> dict:
    """Apply a reschedule and/or status transition with actor enforcement.

    Scope: buyers see only buyer_user_id == self, owners only
    owner_user_id == self (else 404). Invalid moves raise ValueError
    (-> 422). Same-state repeats are idempotent (time updated when
    supplied and permitted). Flushes; the caller commits.
    """
    if actor not in ("buyer", "owner"):
        raise ValueError("unknown actor")
    visit = db.get(PropertyVisit, visit_id, with_for_update=True)
    if visit is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if actor == "buyer" and visit.buyer_user_id != actor_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if actor == "owner" and visit.owner_user_id != actor_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    original_status = visit.status
    next_moment = None

    if to_status is not None:
        allowed = _ALLOWED.get((original_status, to_status))
        if allowed is None:
            raise ValueError(
                f"Cannot move visit from {original_status} to {to_status}"
            )
        if "either" not in allowed and actor not in allowed:
            raise ValueError(
                f"Only {allowed[0]} may move visit from {original_status} to {to_status}"
            )

    if scheduled_at is not None:
        # Time-edit authority is defined by the visit's current state, not the
        # state being requested in the same request. Checking after applying
        # to_status lets a confirming party smuggle in a time change.
        editors = _TIME_EDITORS.get(original_status)
        if editors is None:
            raise ValueError(f"Cannot reschedule a {original_status} visit")
        if "either" not in editors and actor not in editors:
            raise ValueError(
                f"Only {editors[0]} may reschedule a {original_status} visit"
            )
        next_moment = _normalize_scheduled_at(scheduled_at)
        if _has_active_visit_for_buyer_listing(
            db,
            visit.property_listing_id,
            visit.buyer_user_id,
            initiator=visit.initiator,
            exclude_visit_id=visit.id,
        ):
            raise ValueError(
                "A conflicting visit already exists for this buyer on this listing"
            )

    if to_status is not None:
        visit.status = to_status
    if next_moment is not None:
        visit.scheduled_at = next_moment

    visit.updated_at = _utcnow_naive()
    db.flush()
    db.refresh(visit)
    return _snapshot(db, visit)
