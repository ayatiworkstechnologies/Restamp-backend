"""RESTAMP Buyer V1 — buyer enquiries (Phase 6B-5).

Canonical states NEW/CONTACTED/CLOSED; buyers can only CREATE (forced NEW).
Owner-side transitions belong to future Owner APIs. One ACTIVE enquiry per
buyer+listing is enforced by active_enquiries' UNIQUE plus an IntegrityError
fallback for true races. No snapshots, no display tables, no chat.
"""
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
        )
        .first()
    )
    if listing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return listing


def _snapshot(db: Session, enquiry: Enquiry) -> dict:
    listing = db.get(PropertyListing, enquiry.property_listing_id)
    return {
        "id": enquiry.id,
        "property_listing_id": enquiry.property_listing_id,
        "message": enquiry.message,
        "status": enquiry.status,
        "closing_reason": enquiry.closing_reason,
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
    owner_id = db.get(PhysicalProperty, listing.physical_property_id).user_id
    enquiry = Enquiry(
        property_listing_id=listing_id,
        buyer_user_id=buyer_id,
        owner_user_id=owner_id,
        message=message,
        status="NEW",
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
            "listing": {"title": title, "price_paise": price_paise, "price_period": price_period},
        }
        for e, title, price_paise, price_period in rows
    ], total


def get_enquiry(db: Session, buyer_id: int, enquiry_id: int) -> dict:
    enquiry = db.get(Enquiry, enquiry_id)
    if enquiry is None or enquiry.buyer_user_id != buyer_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return _snapshot(db, enquiry)
