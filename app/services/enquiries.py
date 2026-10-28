"""RESTAMP Buyer V1 — buyer enquiries (Phase 6B-5).

Canonical states NEW/CONTACTED/CLOSED; buyers can only CREATE (forced NEW).
Owner-side transitions belong to future Owner APIs. One ACTIVE enquiry per
buyer+listing is enforced by active_enquiries' UNIQUE plus an IntegrityError
fallback for true races. No snapshots, no display tables, no chat.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import ActiveEnquiry, Enquiry, PhysicalProperty, PropertyListing


def _visible_listing(db: Session, listing_id: int) -> PropertyListing:
    listing = (
        db.query(PropertyListing)
        .filter(
            PropertyListing.id == listing_id,
            PropertyListing.verification_status == "VERIFIED",
            PropertyListing.listing_status == "AVAILABLE",
            PropertyListing.deleted_at.is_(None),
        )
        .first()
    )
    if listing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return listing


def _utcnow_naive_seconds() -> datetime:
    """Naive UTC, whole seconds: enquiries.created_at is a MySQL DATETIME
    (no fractional part), so the value returned at creation is exactly the
    value every later read returns."""
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def _created_at_out(value: datetime | None) -> datetime | None:
    """Stored naive UTC -> explicit UTC for the API (serialized with "Z", so
    clients never mistake it for local time). None stays None: rows created
    before the column existed have no trustworthy creation time."""
    return value.replace(tzinfo=timezone.utc) if value is not None else None


def _snapshot(db: Session, enquiry: Enquiry) -> dict:
    listing = db.get(PropertyListing, enquiry.property_listing_id)
    return {
        "id": enquiry.id,
        "property_listing_id": enquiry.property_listing_id,
        "message": enquiry.message,
        "status": enquiry.status,
        "closing_reason": enquiry.closing_reason,
        "created_at": _created_at_out(enquiry.created_at),
        "listing": {
            "title": listing.title,
            "price_paise": listing.price_paise,
            "price_period": listing.price_period,
        },
    }


def create_enquiry(
    db: Session, buyer_id: int, listing_id: int, message: str | None
) -> tuple[dict, bool]:
    """Create (NEW) or return the existing active enquiry. Returns (body, created)."""
    listing = _visible_listing(db, listing_id)
    # Consistency gate first (fail-closed before any read of enquiry state):
    # both ownership columns must agree, or the row matches nobody.
    physical = db.get(PhysicalProperty, listing.physical_property_id)
    if physical is None or physical.user_id != listing.user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    existing = (
        db.query(ActiveEnquiry)
        .filter(
            ActiveEnquiry.buyer_user_id == buyer_id,
            ActiveEnquiry.property_listing_id == listing_id,
        )
        .first()
    )
    if existing is not None:
        return _snapshot(db, db.get(Enquiry, existing.enquiry_id)), False
    owner_id = physical.user_id
    enquiry = Enquiry(
        property_listing_id=listing_id,
        buyer_user_id=buyer_id,
        owner_user_id=owner_id,
        message=message,
        status="NEW",
        created_at=_utcnow_naive_seconds(),
    )
    db.add(enquiry)
    try:
        # Savepoint: a concurrent duplicate rolls back only this INSERT pair,
        # never the caller's wider transaction.
        with db.begin_nested():
            db.flush()
            db.add(
                ActiveEnquiry(
                    enquiry_id=enquiry.id,
                    buyer_user_id=buyer_id,
                    property_listing_id=listing_id,
                )
            )
            db.flush()
    except IntegrityError:
        # Lost a concurrent race: the winner's row now exists — return it.
        existing = (
            db.query(ActiveEnquiry)
            .filter(
                ActiveEnquiry.buyer_user_id == buyer_id,
                ActiveEnquiry.property_listing_id == listing_id,
            )
            .first()
        )
        if existing is None:  # pragma: no cover - defensive; constraint guarantees a row
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Enquiry already active"
            )
        return _snapshot(db, db.get(Enquiry, existing.enquiry_id)), False
    return _snapshot(db, enquiry), True


def list_enquiries(
    db: Session, buyer_id: int, page: int, page_size: int, stat: str | None = None
) -> tuple[list[dict], int]:
    from sqlalchemy import func as _func

    base = (
        db.query(
            Enquiry,
            PropertyListing.title,
            PropertyListing.price_paise,
            PropertyListing.price_period,
        )
        .join(PropertyListing, PropertyListing.id == Enquiry.property_listing_id)
        .filter(Enquiry.buyer_user_id == buyer_id)
    )
    if stat is not None:
        base = base.filter(Enquiry.status == stat)
    total = base.with_entities(_func.count(Enquiry.id)).scalar() or 0
    rows = (
        base.order_by(Enquiry.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    )
    return [
        {
            "id": e.id,
            "property_listing_id": e.property_listing_id,
            "message": e.message,
            "status": e.status,
            "closing_reason": e.closing_reason,
            "created_at": _created_at_out(e.created_at),
            "listing": {"title": title, "price_paise": price_paise, "price_period": price_period},
        }
        for e, title, price_paise, price_period in rows
    ], total


def get_enquiry(db: Session, buyer_id: int, enquiry_id: int) -> dict:
    enquiry = db.get(Enquiry, enquiry_id)
    if enquiry is None or enquiry.buyer_user_id != buyer_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return _snapshot(db, enquiry)


# Owner-side lifecycle (P4): NEW -> CONTACTED -> CLOSED, plus NEW -> CLOSED
# directly. Same-status writes are idempotent no-ops. CLOSED is terminal
# (reopening is not supported); anything else is a 422. Closing frees the
# buyer's active-enquiry row so re-enquiry creates a fresh row, mirroring
# the buyer-tested close semantics. The listing is never touched.
_ALLOWED_OWNER_TRANSITIONS = {
    "NEW": {"NEW", "CONTACTED", "CLOSED"},
    "CONTACTED": {"CONTACTED", "NEW", "CLOSED"},
    "CLOSED": {"CLOSED"},
}


def _buyer_display_names(db: Session, buyer_ids: list[int]) -> dict[int, str | None]:
    """buyer_user_id -> owner-safe display name (None when there is none).

    Phone-registered accounts start with display_name = their phone number,
    so a name is only returned when it passes the same phone-leak rule the
    public owner name uses (listings.safe_display_name). Two batched reads
    per page; no other user columns are selected.
    """
    from ..models import AuthIdentity, User
    from .listings import safe_display_name

    ids = sorted({int(b) for b in buyer_ids})
    if not ids:
        return {}
    phones: dict[int, set[str]] = {}
    for uid, ident in (
        db.query(AuthIdentity.user_id, AuthIdentity.provider_identifier)
        .filter(AuthIdentity.user_id.in_(ids), AuthIdentity.provider == "phone")
        .all()
    ):
        phones.setdefault(int(uid), set()).add(ident)
    return {
        int(uid): safe_display_name(name, phones.get(int(uid), set()))
        for uid, name in db.query(User.id, User.display_name).filter(User.id.in_(ids)).all()
    }


def _owner_snapshot(db: Session, enquiry: Enquiry) -> dict:
    listing = db.get(PropertyListing, enquiry.property_listing_id)
    names = _buyer_display_names(db, [enquiry.buyer_user_id])
    return {
        "id": enquiry.id,
        "property_listing_id": enquiry.property_listing_id,
        "buyer_user_id": enquiry.buyer_user_id,
        "buyer_display_name": names.get(int(enquiry.buyer_user_id)),
        "listing_removed": listing.deleted_at is not None,
        "message": enquiry.message,
        "status": enquiry.status,
        "closing_reason": enquiry.closing_reason,
        "created_at": _created_at_out(enquiry.created_at),
        "listing": {
            "title": listing.title,
            "price_paise": listing.price_paise,
            "price_period": listing.price_period,
            "property_type": listing.property_type,
        },
    }


def transition_owner_lead(
    db: Session, owner_id: int, lead_id: int, to_status: str, closing_reason: str | None
) -> dict:
    """Persist an owner-side status change. Raises 404 for foreign/missing
    leads, 422 for unsupported transitions. CLOSED removes the active row
    (buyer may re-enquire); other transitions keep it. Never touches the
    property listing."""
    enquiry = db.get(Enquiry, lead_id)
    if enquiry is None or enquiry.owner_user_id != owner_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if to_status not in _ALLOWED_OWNER_TRANSITIONS.get(enquiry.status, set()):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid request")
    enquiry.status = to_status
    if closing_reason is not None:
        enquiry.closing_reason = closing_reason
    db.flush()
    if to_status == "CLOSED":
        db.query(ActiveEnquiry).filter(ActiveEnquiry.enquiry_id == enquiry.id).delete()
        db.flush()
    return _owner_snapshot(db, enquiry)


def list_owner_leads(
    db: Session,
    owner_id: int,
    page: int,
    page_size: int,
    property_listing_id: int | None = None,
    stat: str | None = None,
) -> tuple[list[dict], int]:
    """List buyer enquiries belonging to the authenticated owner (P3 leads MVP).

    Ownership authority is the enquiry's stored owner_user_id (written from
    physical_properties.user_id at creation, the same authority contact
    reveal uses). A property filter is validated against the caller's owned
    physical properties — a foreign id raises 404, never an empty list, so
    callers cannot probe other owners' listings. No phone numbers or avatars
    are selected; the buyer's display name is included only when it is a
    chosen name (never the phone-number default — see _buyer_display_names).
    """
    from sqlalchemy import func as _func

    if property_listing_id is not None:
        # Dual-authority check (consistent with owner listing reads): the
        # filter passes only when BOTH ownership columns equal the caller.
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

    # Historical leads stay visible: there is deliberately no deleted_at or
    # listing-status filter here. A soft-removed listing keeps its row, so
    # its real title/price are still returned, flagged via listing_removed.
    base = (
        db.query(
            Enquiry,
            PropertyListing.title,
            PropertyListing.price_paise,
            PropertyListing.price_period,
            PropertyListing.deleted_at,
            PropertyListing.property_type,
        )
        .join(PropertyListing, PropertyListing.id == Enquiry.property_listing_id)
        .filter(Enquiry.owner_user_id == owner_id)
    )
    if property_listing_id is not None:
        base = base.filter(Enquiry.property_listing_id == property_listing_id)
    if stat is not None:
        base = base.filter(Enquiry.status == stat)
    total = base.with_entities(_func.count(Enquiry.id)).scalar() or 0
    rows = (
        base.order_by(Enquiry.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    )
    names = _buyer_display_names(db, [e.buyer_user_id for e, *_ in rows])
    return [
        {
            "id": e.id,
            "property_listing_id": e.property_listing_id,
            "buyer_user_id": e.buyer_user_id,
            "buyer_display_name": names.get(int(e.buyer_user_id)),
            "listing_removed": deleted_at is not None,
            "message": e.message,
            "status": e.status,
            "closing_reason": e.closing_reason,
            "created_at": _created_at_out(e.created_at),
            "listing": {
                "title": title,
                "price_paise": price_paise,
                "price_period": price_period,
                "property_type": property_type,
            },
        }
        for e, title, price_paise, price_period, deleted_at, property_type in rows
    ], total
