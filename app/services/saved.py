"""RESTAMP Buyer V1 — saved/wishlist (Phase 6B-6).

Uses saved_properties exactly: composite PK makes duplicates impossible;
IntegrityError fallback keeps concurrent double-saves safe and idempotent.
Listings that lose visibility are excluded from list results (never leaked).
"""
from fastapi import HTTPException, status
from sqlalchemy import func as _func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import PropertyListing, SavedProperty
from . import listings as listing_svc


def _visible_saved_query(db: Session, buyer_id: int):
    return (
        listing_svc._visible(
            listing_svc._location_joins(
                db.query(*listing_svc._CARD_COLUMNS).join(
                    SavedProperty,
                    SavedProperty.property_listing_id == PropertyListing.id,
                )
            )
        ).filter(SavedProperty.buyer_user_id == buyer_id)
    )


def save_listing(db: Session, buyer_id: int, listing_id: int) -> tuple[dict, bool]:
    """Save; (card, created). Invisible listings 404. Duplicates return existing."""
    row = (
        listing_svc._visible(listing_svc._location_joins(db.query(*listing_svc._CARD_COLUMNS)))
        .filter(PropertyListing.id == listing_id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    try:
        # Savepoint: a concurrent duplicate rolls back only this INSERT,
        # never the caller's wider transaction (e.g. test seeds, prior reads).
        with db.begin_nested():
            db.add(SavedProperty(buyer_user_id=buyer_id, property_listing_id=listing_id))
            db.flush()
        created = True
    except IntegrityError:
        created = False  # concurrent duplicate won elsewhere; fall through to existing
    card = listing_svc._card_row(row)
    covers = listing_svc.fetch_cover_map(db, [listing_id])
    card["cover_image_url"] = covers.get(listing_id)
    return card, created


def list_saved(db: Session, buyer_id: int, page: int, page_size: int) -> tuple[list[dict], int]:
    q = _visible_saved_query(db, buyer_id)
    total = q.with_entities(_func.count(_func.distinct(PropertyListing.id))).scalar() or 0
    rows = (
        q.order_by(SavedProperty.saved_at.desc(), SavedProperty.property_listing_id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    cards = [listing_svc._card_row(r) for r in rows]
    return (
        listing_svc.attach_covers(cards, listing_svc.fetch_cover_map(db, [c["listing_id"] for c in cards])),
        total,
    )


def unsave_listing(db: Session, buyer_id: int, listing_id: int) -> bool:
    """Delete own save; False when nothing was saved (idempotent)."""
    row = (
        db.query(SavedProperty)
        .filter(
            SavedProperty.buyer_user_id == buyer_id,
            SavedProperty.property_listing_id == listing_id,
        )
        .first()
    )
    if row is None:
        return False
    db.delete(row)
    db.flush()
    return True
