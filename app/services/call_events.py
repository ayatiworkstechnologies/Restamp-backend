"""RESTAMP buyer call-button events (CONTRACT §B, decision D2).

Records that a buyer TAPPED a Call button for a listing — never that a call
was placed, answered or completed (the app cannot know). Stored in the
existing, previously unused activity_logs table (reviewed as suitable: generic
user-activity log per the design doc, user FK with CASCADE, no phone data):

    user_id = buyer, action = "CALL_BUTTON_TAP", entity_type = "property_listing",
    entity_id = listing id, details = NULL, created_at = app-set UTC

Rules: buyer role (router), listing must be publicly visible with consistent
dual ownership (else 404, no probing), an owner can never record a tap on their
OWN listing — even after switching to buyer mode (422, nothing stored; same
rule as self-visits) — so owners cannot inflate their own call counts, a
repeat tap on the same listing within
_DEDUP_SECONDS returns the existing event instead of a new row (retries /
double taps). Owners only ever see per-listing counts (owner_listings), never
which buyer tapped.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import ActivityLog, PhysicalProperty, PropertyListing
from . import listings as listing_svc

CALL_ACTION = "CALL_BUTTON_TAP"
ENTITY_LISTING = "property_listing"
_DEDUP_SECONDS = 60


def _utcnow_naive_seconds() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def _out(event: ActivityLog) -> dict:
    return {
        "id": event.id,
        "property_listing_id": event.entity_id,
        "created_at": event.created_at.replace(tzinfo=timezone.utc),
    }


def record_call_tap(db: Session, buyer_id: int, listing_id: int) -> tuple[dict, bool]:
    """Record one call-button tap. Returns (event, created). Flushes; the
    caller commits."""
    listing = listing_svc._visible(db.query(PropertyListing)).filter(PropertyListing.id == listing_id).first()
    physical = db.get(PhysicalProperty, listing.physical_property_id) if listing is not None else None
    if listing is None or physical is None or physical.user_id != listing.user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if buyer_id == listing.user_id:
        # Ownership is decided by the stored listing owner (both ownership
        # columns already agree above), never by the caller's current role.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Cannot record a call on your own listing",
        )
    now = _utcnow_naive_seconds()
    recent = (
        db.query(ActivityLog)
        .filter(
            ActivityLog.user_id == buyer_id,
            ActivityLog.action == CALL_ACTION,
            ActivityLog.entity_type == ENTITY_LISTING,
            ActivityLog.entity_id == listing_id,
            ActivityLog.created_at >= now - timedelta(seconds=_DEDUP_SECONDS),
        )
        .order_by(ActivityLog.id.desc())
        .first()
    )
    if recent is not None:
        return _out(recent), False
    # `details` is left unset on purpose: assigning None to a JSON column
    # stores the JSON literal null, whereas unset keeps SQL NULL.
    event = ActivityLog(
        user_id=buyer_id, action=CALL_ACTION, entity_type=ENTITY_LISTING,
        entity_id=listing_id, created_at=now,
    )
    db.add(event)
    db.flush()
    return _out(event), True


def list_call_events(db: Session, buyer_id: int, page: int, page_size: int) -> tuple[list[dict], int]:
    """The caller's own call-button taps, newest first. Each item carries the
    listing's public card, or null when it is no longer publicly visible."""
    base = db.query(ActivityLog).filter(
        ActivityLog.user_id == buyer_id,
        ActivityLog.action == CALL_ACTION,
        ActivityLog.entity_type == ENTITY_LISTING,
    )
    total = base.with_entities(func.count(ActivityLog.id)).scalar() or 0
    events = base.order_by(ActivityLog.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    ids = sorted({int(e.entity_id) for e in events if e.entity_id is not None})
    cards: dict[int, dict] = {}
    if ids:
        rows = (
            listing_svc._visible(listing_svc._location_joins(db.query(*listing_svc._CARD_COLUMNS)))
            .filter(PropertyListing.id.in_(ids))
            .all()
        )
        found = listing_svc.attach_covers(
            [listing_svc._card_row(r) for r in rows], listing_svc.fetch_cover_map(db, ids)
        )
        cards = {c["listing_id"]: c for c in found}
    return [{**_out(e), "listing": cards.get(int(e.entity_id))} for e in events], total


def call_counts(db: Session, listing_ids: list[int]) -> dict[int, int]:
    """listing_id -> number of call-button taps (all buyers, all time).
    Counts only; uses ix_activity_logs_action_entity."""
    if not listing_ids:
        return {}
    rows = (
        db.query(ActivityLog.entity_id, func.count(ActivityLog.id))
        .filter(
            ActivityLog.action == CALL_ACTION,
            ActivityLog.entity_type == ENTITY_LISTING,
            ActivityLog.entity_id.in_(listing_ids),
        )
        .group_by(ActivityLog.entity_id)
        .all()
    )
    return {int(lid): int(n) for lid, n in rows}
