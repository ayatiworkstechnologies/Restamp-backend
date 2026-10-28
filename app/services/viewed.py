"""RESTAMP Buyer viewed-property service.

Tracks which properties a buyer has actually opened and viewed (detail
screen). Each row is uniquely keyed by (buyer_user_id, property_listing_id);
repeat views update viewed_at instead of duplicating rows. Per-buyer
retention is capped at the 20 most recent distinct properties.

Ordering is deterministic: viewed_at is a whole-second DATETIME, so views
within the same second tie; every ordering here breaks ties by the row id
(newest row first). The row just recorded is never trimmed.

viewed_at is UTC, stamped by the app (the MySQL server clock is not UTC on
every environment) and returned as explicit UTC — the same convention as
enquiries.created_at.

Listing data is the public ListingCard of listings that are still visible
to buyers (same rule as saved properties); history rows whose listing lost
visibility are omitted, never leaked.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import PropertyListing, ViewedProperty
from . import listings as listing_svc

_RETENTION_LIMIT = 20


def _utcnow_naive_seconds() -> datetime:
    """Naive UTC, whole seconds (DATETIME keeps no fractions, so the value
    returned when recording equals every later read)."""
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def _viewed_at_out(value: datetime | None) -> datetime | None:
    """Stored naive UTC -> explicit UTC for the API ("...Z")."""
    return value.replace(tzinfo=timezone.utc) if value is not None else None


def _newest_first():
    return (ViewedProperty.viewed_at.desc(), ViewedProperty.id.desc())


def _visible_viewed_query(db: Session, buyer_user_id: int):
    """Card columns + (viewed id, viewed_at) for this buyer's still-visible listings."""
    return listing_svc._visible(
        listing_svc._location_joins(
            db.query(*listing_svc._CARD_COLUMNS, ViewedProperty.id, ViewedProperty.viewed_at).join(
                ViewedProperty, ViewedProperty.property_listing_id == PropertyListing.id
            )
        )
    ).filter(ViewedProperty.buyer_user_id == buyer_user_id)


def _items(db: Session, rows, buyer_user_id: int) -> list[dict]:
    n = len(listing_svc._CARD_COLUMNS)
    items = []
    for row in rows:
        values = tuple(row)
        card = listing_svc._card_row(values[:n])
        viewed_id, viewed_at = values[n], values[n + 1]
        items.append({
            "id": viewed_id,
            "property_listing_id": card["listing_id"],
            "buyer_user_id": buyer_user_id,
            "viewed_at": _viewed_at_out(viewed_at),
            "listing": card,
        })
    covers = listing_svc.fetch_cover_map(db, [i["property_listing_id"] for i in items])
    listing_svc.attach_covers([i["listing"] for i in items], covers)
    return items


def record_view(db: Session, buyer_user_id: int, property_listing_id: int) -> dict:
    """Record that a buyer viewed a property detail; returns the item dict.

    Idempotent: if the buyer already viewed this listing, only the timestamp
    is refreshed. No duplicate row is created (a concurrent duplicate insert
    falls back to refreshing the winner's row).

    After inserting/updating, enforces the 20-per-buyer retention rule by
    deleting this buyer's oldest rows beyond the limit — never the row just
    recorded. The caller must already have checked the listing is visible.
    Commits are left to the caller.
    """
    now = _utcnow_naive_seconds()

    def _existing():
        return (
            db.query(ViewedProperty)
            .filter(
                ViewedProperty.buyer_user_id == buyer_user_id,
                ViewedProperty.property_listing_id == property_listing_id,
            )
            .first()
        )

    row = _existing()
    if row is None:
        try:
            with db.begin_nested():
                row = ViewedProperty(
                    buyer_user_id=buyer_user_id,
                    property_listing_id=property_listing_id,
                    viewed_at=now,
                )
                db.add(row)
                db.flush()
        except IntegrityError:
            row = _existing()  # lost a concurrent race: refresh the winner's row
            row.viewed_at = now
            db.flush()
    else:
        row.viewed_at = now
        db.flush()

    _enforce_retention(db, buyer_user_id, keep_id=row.id)

    recorded = _visible_viewed_query(db, buyer_user_id).filter(ViewedProperty.id == row.id).all()
    return _items(db, recorded, buyer_user_id)[0]


def _enforce_retention(db: Session, buyer_user_id: int, keep_id: int) -> None:
    """Keep at most _RETENTION_LIMIT rows for this buyer: the row just
    recorded (keep_id) plus the newest others, ties broken by id.

    Two statements (select ids, then delete by id) instead of a LIMIT/OFFSET
    subquery, which MySQL does not support inside IN (...). Runs within the
    caller's transaction and only touches this buyer's rows.
    """
    stale_ids = [
        r[0]
        for r in db.query(ViewedProperty.id)
        .filter(ViewedProperty.buyer_user_id == buyer_user_id, ViewedProperty.id != keep_id)
        .order_by(*_newest_first())
        .offset(_RETENTION_LIMIT - 1)
        .all()
    ]
    if stale_ids:
        db.query(ViewedProperty).filter(
            ViewedProperty.buyer_user_id == buyer_user_id,
            ViewedProperty.id.in_(stale_ids),
        ).delete(synchronize_session=False)


def list_viewed(
    db: Session, buyer_user_id: int, page: int = 1, page_size: int = 20
) -> tuple[list[dict], int]:
    """List a buyer's still-visible viewed properties, most recent first
    (ties by newest row). Returns (items, total)."""
    q = _visible_viewed_query(db, buyer_user_id)
    total = q.with_entities(func.count(ViewedProperty.id)).scalar() or 0
    rows = q.order_by(*_newest_first()).offset((page - 1) * page_size).limit(page_size).all()
    return _items(db, rows, buyer_user_id), total
