"""RESTAMP smart reminders (notification system Phase 4).

Reminders are ordinary in-app notifications created through
notifications.notify_safely, so every Phase 1 rule applies unchanged (never
the actor, one per dedupe key, savepoint isolation) and BOTH channel twins —
email (Phase 3) and push (Phase 4) — fan out automatically. A reminder row is
created by a background scan (worker.py, every 60 s), never by a request.

VISIT REMINDERS
- CONFIRMED visits only. Recipients are the visit's buyer AND owner; there is
  no actor (a system reminder), so both parties receive it.
- Two fixed intervals: T-24h and T-1h before the scheduled time. The 24h
  reminder fires while 1h < remaining <= 24h; the 1h reminder while
  0 < remaining <= 1h. A visit that crosses a whole window while the worker
  is down simply misses that reminder (the narrower one still fires).
- Dedupe key: visit:{id}:reminder:{interval}:{scheduled_at isoformat} — the
  scheduled time is part of the key, so a RESCHEDULED visit legitimately gets
  a fresh reminder for the new time; the stale row for the old time is
  dropped at delivery by the recheck below.
- Delivery-time recheck (push._prepare): the visit must still exist, be
  CONFIRMED, still have exactly the payload's scheduled time, and that time
  must still be in the future. Otherwise the twin is dropped ("superseded" /
  "gone" / "past").

LEAD REMINDERS
- Owner-only nudges for enquiries that sat unanswered: status NEW and no
  owner reply (no enquiry message authored by the owner). The buyer is never
  reminded; the actor is nobody, so the owner's own message cannot suppress
  it (there is no actor — the enquiry simply qualifies or not).
- Fixed offsets from creation: 2h, 24h, 72h. Each fires once the enquiry's
  age reaches the offset; nothing new is queued after
  LEAD_MAX_AGE_HOURS + LEAD_GRACE_HOURS (78h).
- Dedupe key: enquiry:{id}:lead_reminder:{offset_h} — one reminder per offset.
- Delivery-time recheck: the enquiry must still exist, still be NEW and still
  have no owner reply; otherwise the twin is dropped ("superseded" / "gone").
  An enquiry older than the max age is dropped as "stale".

The scan runs in the worker loop with its own session; notify_safely isolates
every insert, so one bad row never stops the scan.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ..models import Enquiry, EnquiryMessage, PropertyVisit, User
from .notifications import notify_safely

log = logging.getLogger("restamp.reminders")

# Visit reminder intervals (hours before the scheduled time). The 24h
# reminder's lower bound is the 1h mark, so the two windows never overlap.
VISIT_REMINDER_HOURS = (24, 1)
# Lead reminder offsets (hours after the enquiry was created).
LEAD_REMINDER_OFFSETS_HOURS = (2, 24, 72)
LEAD_MAX_AGE_HOURS = 72
LEAD_GRACE_HOURS = 6  # a scan overdue by up to this much still fires the last offset
SCAN_BATCH_VISITS = 200
SCAN_BATCH_ENQUIRIES = 200


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def _iso(moment: datetime) -> str:
    """Stable string form for the payload (the DB stores whole seconds)."""
    return moment.replace(microsecond=0).isoformat()


# --- delivery-time rechecks (called from services/push.py) ---------------------------

def visit_reminder_drop_reason(db: Session, payload: dict, now: datetime) -> str | None:
    """Why the queued reminder must NOT be delivered now, or None."""
    visit = db.get(PropertyVisit, int(payload.get("visit_id") or 0))
    if visit is None:
        return "gone"
    if visit.status != "CONFIRMED":
        return "superseded"
    scheduled_at = visit.scheduled_at
    stored = str(payload.get("scheduled_at") or "")
    if scheduled_at is None or _iso(scheduled_at) != stored:
        return "superseded"  # rescheduled (or the payload lost its time)
    if scheduled_at <= now:
        return "past"
    # Both parties must still be active accounts (a suspended or closed
    # counterpart makes the visit moot; the recipient check is repeated by
    # each channel for its own rules).
    if not _active(db, visit.buyer_user_id) or not _active(db, visit.owner_user_id):
        return "account_inactive"
    return None


def lead_reminder_drop_reason(db: Session, payload: dict, now: datetime) -> str | None:
    """Why the queued reminder must NOT be delivered now, or None."""
    enquiry = db.get(Enquiry, int(payload.get("enquiry_id") or 0))
    if enquiry is None:
        return "gone"
    if enquiry.status != "NEW":
        return "superseded"  # contacted / closed
    if _owner_replied(db, enquiry):
        return "superseded"
    if enquiry.created_at is None:
        return "stale"
    if now - enquiry.created_at > timedelta(hours=LEAD_MAX_AGE_HOURS + LEAD_GRACE_HOURS):
        return "stale"
    if not _active(db, enquiry.owner_user_id):
        return "account_inactive"
    buyer = db.get(User, int(enquiry.buyer_user_id)) if enquiry.buyer_user_id is not None else None
    if buyer is None or buyer.status == "closed":
        return "account_inactive"
    return None


def _active(db: Session, user_id) -> bool:
    if user_id is None:
        return False
    user = db.get(User, int(user_id))
    return user is not None and user.status == "active"


REMINDER_TEMPLATES = ("VISIT_REMINDER", "LEAD_REMINDER")


def reminder_drop_reason(db: Session, template: str, facts: dict, now: datetime) -> str | None:
    """THE delivery-time eligibility check for reminders, shared by the email
    and push workers (one source of truth). Non-reminder templates -> None.
    A reminder whose facts are missing is dropped (`stale`, fail closed)."""
    if template not in REMINDER_TEMPLATES:
        return None
    facts = facts or {}
    if template == "VISIT_REMINDER":
        if not facts.get("visit_id") or not facts.get("scheduled_at"):
            return "stale"
        return visit_reminder_drop_reason(db, facts, now)
    if not facts.get("enquiry_id"):
        return "stale"
    return lead_reminder_drop_reason(db, facts, now)


def _owner_replied(db: Session, enquiry: Enquiry) -> bool:
    return (
        db.query(EnquiryMessage.id)
        .filter(EnquiryMessage.enquiry_id == enquiry.id,
                EnquiryMessage.sender_user_id == enquiry.owner_user_id)
        .first()
        is not None
    )


# --- scans (worker; own session per pass) --------------------------------------------

def run_visit_scan(db: Session, now: datetime) -> int:
    """Queue reminder notifications for due CONFIRMED visits. Returns how many
    NEW in-app rows were created (their email/push twins ride along)."""
    rows = (
        db.query(PropertyVisit)
        .filter(
            PropertyVisit.status == "CONFIRMED",
            PropertyVisit.scheduled_at.is_not(None),
            PropertyVisit.scheduled_at > now,  # a past visit is never reminded
            PropertyVisit.scheduled_at <= now + timedelta(hours=VISIT_REMINDER_HOURS[0]),
        )
        .order_by(PropertyVisit.id)
        .limit(SCAN_BATCH_VISITS)
        .all()
    )
    queued = 0
    for visit in rows:
        remaining = visit.scheduled_at - now
        for hours in VISIT_REMINDER_HOURS:
            lower = timedelta(hours=VISIT_REMINDER_HOURS[-1]) if hours == VISIT_REMINDER_HOURS[0] else timedelta(0)
            if not (lower < remaining <= timedelta(hours=hours)):
                continue
            label = f"{hours}h"
            scheduled_iso = _iso(visit.scheduled_at)
            key = f"visit:{int(visit.id)}:reminder:{label}:{scheduled_iso}"
            payload = {"visit_id": int(visit.id), "scheduled_at": scheduled_iso, "interval": label}
            for recipient, audience in ((visit.buyer_user_id, "BUYER"), (visit.owner_user_id, "OWNER")):
                row = notify_safely(
                    db, "VISIT_REMINDER",
                    recipient_id=recipient, actor_id=None,
                    entity_id=int(visit.id), listing_id=visit.property_listing_id,
                    audience=audience, dedupe_key=key, when=visit.scheduled_at,
                    push_payload=payload,
                )
                if row is not None:
                    queued += 1
    return queued


def run_lead_scan(db: Session, now: datetime) -> int:
    """Queue owner reminders for enquiries that sat unanswered. Returns how
    many NEW in-app rows were created."""
    max_age = timedelta(hours=LEAD_MAX_AGE_HOURS + LEAD_GRACE_HOURS)
    rows = (
        db.query(Enquiry)
        .filter(
            Enquiry.status == "NEW",
            Enquiry.created_at.is_not(None),
            Enquiry.created_at >= now - max_age,
        )
        .order_by(Enquiry.id)
        .limit(SCAN_BATCH_ENQUIRIES)
        .all()
    )
    queued = 0
    for enquiry in rows:
        age = now - enquiry.created_at
        if age <= timedelta(0):
            continue
        if _owner_replied(db, enquiry):  # cheap pre-filter; the delivery recheck repeats it
            continue
        for offset in LEAD_REMINDER_OFFSETS_HOURS:
            if age < timedelta(hours=offset):
                continue
            key = f"enquiry:{int(enquiry.id)}:lead_reminder:{offset}"
            payload = {"enquiry_id": int(enquiry.id), "offset_hours": int(offset)}
            row = notify_safely(
                db, "LEAD_REMINDER",
                recipient_id=enquiry.owner_user_id, actor_id=None,
                entity_id=int(enquiry.id), listing_id=enquiry.property_listing_id,
                audience="OWNER", dedupe_key=key, push_payload=payload,
            )
            if row is not None:
                queued += 1
    return queued


def run_scan(session_factory=None) -> dict:
    """One worker pass: both scans, each in its own transaction so a failure
    in one never blocks the other. Does not touch any provider (twins are
    queued inside notify_safely and delivered by the email/push workers)."""
    if session_factory is None:
        from ..db import SessionLocal as session_factory  # lazy: tests swap the factory
    counts = {"visit_reminders": 0, "lead_reminders": 0}
    db: Session = session_factory()
    try:
        now = _utcnow_naive()
        try:
            counts["visit_reminders"] = run_visit_scan(db, now)
            db.commit()
        except Exception as exc:
            db.rollback()
            log.error("Visit reminder scan failed: %s", type(exc).__name__)
        try:
            counts["lead_reminders"] = run_lead_scan(db, now)
            db.commit()
        except Exception as exc:
            db.rollback()
            log.error("Lead reminder scan failed: %s", type(exc).__name__)
        return counts
    finally:
        db.close()
