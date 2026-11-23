"""RESTAMP in-app notifications: generation (from business events) and the
recipient's own list / unread count / read / read-all / soft delete.

Generation (`notify_safely`, called from the existing business services right
after a confirmed state change, inside the caller's transaction):

- The recipient is resolved by the calling service from stored columns
  (enquiry/visit buyer_user_id/owner_user_id, listing owner), never from
  client input. The actor is never notified about their own action.
- Content comes from the fixed templates in `_TYPES`: public listing title,
  visit time in IST and static text only. Never phone numbers, names,
  message bodies, closing reasons or admin notes.
- Idempotent: UNIQUE (user_id, dedupe_key). The calling service builds the
  key from the event's identity (e.g. ``visit:{id}:CONFIRMED``), so a retried
  or repeated event finds the existing row and stores nothing. A concurrent
  duplicate that slips past the pre-check hits MySQL 1062 and is treated as
  "already notified".
- Failure isolation: every read and the insert run inside their own SAVEPOINT
  (`begin_nested`). Any error there rolls back only that savepoint and is
  logged (type name only, never contents); the business change still
  commits. The caller's pending business changes are flushed BEFORE the
  savepoint and outside the error handler, so a genuine business error is
  never swallowed here: it propagates exactly as it did before notifications
  existed.
- Transaction-level database failures are NOT isolated: a deadlock (1213),
  lock-wait timeout (1205) or a session that needs a rollback means the
  surrounding business transaction may already be gone, so the error is
  re-raised and the request fails (500) instead of reporting a success that
  was never committed. Only failures confined to the savepoint are swallowed.
- Optional coalescing (`coalesce_types`): no new row while the recipient
  still has an UNREAD, undeleted notification of one of those types for the
  same entity created within `coalesce_seconds`. Used for buyer follow-up
  messages (10 minutes) so an owner is not flooded; the full thread is one
  tap away in the lead. Concurrency contract (see _recent_unread_exists):
  callers must hold a row lock that every writer of those notification types
  for that entity also holds (the enquiry row); the check then reads the
  latest committed rows without taking any lock on `notifications`.

Reads/writes for the recipient are scoped by user_id alone: another user's
notification is a 404 (same as a missing one, so existence never leaks).
Deleted rows (`deleted_at`) never appear and cannot be marked read. GETs never
write; the routers commit. Navigation targets are derived at read time from
the fixed `type` (never stored, never a screen name or path).
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError, PendingRollbackError
from sqlalchemy.orm import Session

from ..models import Notification, PhysicalProperty, PropertyListing
from . import listings as listing_svc

log = logging.getLogger("restamp.notifications")

# MySQL ER_DUP_ENTRY: the UNIQUE (user_id, dedupe_key) constraint fired.
_MYSQL_ERR_DUP_ENTRY = 1062
# MySQL errors after which the WHOLE transaction may be gone (1213 deadlock
# always rolls back the transaction; 1205 lock-wait timeout does when
# innodb_rollback_on_timeout is on). Never swallowed: see notify_safely().
_MYSQL_TXN_FATAL = (1213, 1205)

# India has no daylight saving time, so a fixed offset is exact and avoids a
# dependency on the host's tz database.
_IST = timezone(timedelta(hours=5, minutes=30), "IST")

FOLLOW_UP_COALESCE_SECONDS = 600  # 10 minutes

_TITLE_MAX = 160
_BODY_MAX = 500
_LISTING_TITLE_MAX = 60

AUDIENCES = ("BUYER", "OWNER", "ACCOUNT")
CATEGORIES = ("PROPERTIES", "VISITS", "SYSTEM")
TARGET_KINDS = ("OWNER_LEAD", "BUYER_ENQUIRY", "OWNER_VISIT", "BUYER_VISIT", "OWNER_LISTING", "NONE")

ENTITY_ENQUIRY = "enquiry"
ENTITY_VISIT = "property_visit"
ENTITY_LISTING = "property_listing"
ENTITY_USER = "user"


@dataclass(frozen=True)
class _Spec:
    audience: str | None  # None: the caller passes it (visit events go to either party)
    category: str
    entity_type: str
    title: str
    body: str  # str.format fields: {listing}, {when}


_TYPES: dict[str, _Spec] = {
    "ENQUIRY_NEW": _Spec("OWNER", "PROPERTIES", ENTITY_ENQUIRY, "New enquiry",
                         "You have received a new property enquiry for {listing}."),
    "ENQUIRY_MESSAGE": _Spec("OWNER", "PROPERTIES", ENTITY_ENQUIRY, "New message on an enquiry",
                             "A buyer has sent a new message about {listing}."),
    "ENQUIRY_REPLY": _Spec("BUYER", "PROPERTIES", ENTITY_ENQUIRY, "Owner replied",
                           "The owner has replied to your enquiry about {listing}."),
    "LEAD_CONTACTED": _Spec("BUYER", "PROPERTIES", ENTITY_ENQUIRY, "Enquiry update",
                            "The owner has marked your enquiry about {listing} as contacted."),
    "LEAD_CLOSED": _Spec("BUYER", "PROPERTIES", ENTITY_ENQUIRY, "Enquiry closed",
                         "The owner has closed your enquiry about {listing}."),
    "VISIT_REQUESTED": _Spec("OWNER", "VISITS", ENTITY_VISIT, "Visit requested",
                             "A buyer has requested a visit to {listing} on {when}."),
    "VISIT_PROPOSED": _Spec("BUYER", "VISITS", ENTITY_VISIT, "Visit proposed",
                            "The owner has proposed a visit to {listing} on {when}."),
    "VISIT_CONFIRMED": _Spec(None, "VISITS", ENTITY_VISIT, "Visit confirmed",
                             "Your visit to {listing} on {when} has been confirmed."),
    "VISIT_DECLINED": _Spec(None, "VISITS", ENTITY_VISIT, "Visit declined",
                            "The visit to {listing} on {when} was declined."),
    # Phase 5: the poster rejects a pending visit (the buyer is told).
    "VISIT_REJECTED": _Spec(None, "VISITS", ENTITY_VISIT, "Visit not accepted",
                            "The visit to {listing} on {when} was not accepted."),
    "VISIT_CANCELLED": _Spec(None, "VISITS", ENTITY_VISIT, "Visit cancelled",
                             "The visit to {listing} on {when} was cancelled."),
    "VISIT_RESCHEDULED": _Spec(None, "VISITS", ENTITY_VISIT, "Visit rescheduled",
                               "The visit to {listing} has been rescheduled to {when}."),
    # Smart reminders (Phase 4). Created only by services/reminders.py scans
    # (never by a request); the email/push twins fan out like any other type.
    "VISIT_REMINDER": _Spec(None, "VISITS", ENTITY_VISIT, "Upcoming visit",
                            "Reminder: your visit to {listing} is scheduled for {when}."),
    "LEAD_REMINDER": _Spec("OWNER", "PROPERTIES", ENTITY_ENQUIRY, "Lead waiting for a reply",
                           "A buyer is waiting for a reply on their enquiry about {listing}."),
    "LISTING_VERIFIED": _Spec("OWNER", "PROPERTIES", ENTITY_LISTING, "Listing verified",
                              "Your property listing {listing} has been verified."),
    "LISTING_REJECTED": _Spec("OWNER", "PROPERTIES", ENTITY_LISTING, "Listing rejected",
                              "Your property listing {listing} was rejected. Review it and resubmit."),
    "ACCOUNT_REACTIVATED": _Spec("ACCOUNT", "SYSTEM", ENTITY_USER, "Account reactivated",
                                 "Your account has been reactivated."),
}

LEAD_CLOSED_VISITS_SUFFIX = " Scheduled visits for it were cancelled."


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def _utc_out(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=timezone.utc) if value is not None else None


def format_when(moment: datetime | None) -> str:
    """Naive-UTC DB time -> '14 Nov 2026, 5:30 PM IST' (no locale dependence)."""
    if moment is None:
        return "the scheduled time"
    local = moment.replace(tzinfo=timezone.utc).astimezone(_IST)
    hour = local.hour % 12 or 12
    return f"{local.day} {local.strftime('%b %Y')}, {hour}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'} IST"


def _listing_label(db: Session, listing_id: int | None) -> str:
    """The listing's public title, quoted and trimmed; a neutral fallback
    when there is none (never an id, never owner details)."""
    title = None
    if listing_id is not None:
        row = db.query(PropertyListing.title).filter(PropertyListing.id == listing_id).first()
        title = (row[0] or "").strip() if row is not None else None
    if not title:
        return "your property"
    if len(title) > _LISTING_TITLE_MAX:
        title = title[: _LISTING_TITLE_MAX - 1].rstrip() + "…"
    return f"“{title}”"


def _mysql_code(exc: Exception) -> int | None:
    orig = getattr(exc, "orig", None)
    args = getattr(orig, "args", None) or ()
    return args[0] if args and isinstance(args[0], int) else None


def _is_duplicate(exc: IntegrityError) -> bool:
    return _mysql_code(exc) == _MYSQL_ERR_DUP_ENTRY


def _fresh_read_engine():
    """The application engine, used for a short, separate read connection."""
    from ..db import engine

    return engine


def _recent_unread_exists(
    db: Session,
    recipient_id: int,
    entity_type: str,
    entity_id: int,
    types: tuple[str, ...],
    since: datetime,
) -> bool:
    """Is there an unread, undeleted notification of `types` for this entity
    created at or after `since`? Must see the LATEST committed rows.

    Why not a plain query on `db`: InnoDB REPEATABLE READ fixes the
    transaction's read view at its first consistent read. The follow-up path
    reads the listing and active enquiry BEFORE it takes the enquiry row lock,
    so a request that waited on that lock still reads a snapshot from before
    the previous holder committed its notification -> duplicate.

    Why not SELECT ... FOR UPDATE/FOR SHARE on `notifications`: when nothing
    matches (the usual case) InnoDB takes only a GAP lock; gap locks are
    mutually compatible, so two follow-ups on different enquiries of the same
    owner can hold the same gap and then both insert into it -> deadlock 1213.

    Design: serialization comes from the enquiry row lock the caller already
    holds (SELECT ... FOR UPDATE in enquiries._append_message; a newly
    inserted, uncommitted enquiry row carries the same implicit X lock). Every
    writer of ENQUIRY_NEW / ENQUIRY_MESSAGE for an enquiry holds that lock
    while inserting, and InnoDB keeps row locks until COMMIT/ROLLBACK. So by
    the time this runs, any earlier writer has finished. A NEW connection's
    first statement opens a NEW read view, which therefore contains that
    writer's committed row. It takes no locks at all (plain consistent read),
    so it adds no deadlock or lock-wait risk. The caller's own session is read
    too, for rows it has written but not yet committed (and for test sessions
    whose data is never committed)."""
    conditions = (
        Notification.user_id == recipient_id,
        Notification.entity_type == entity_type,
        Notification.entity_id == entity_id,
        Notification.type.in_(types),
        Notification.read_at.is_(None),
        Notification.deleted_at.is_(None),
        Notification.created_at >= since,
    )
    if db.query(Notification.id).filter(*conditions).first() is not None:
        return True
    with _fresh_read_engine().connect() as conn:  # closed (rolled back) on exit
        return conn.execute(select(Notification.id).where(*conditions).limit(1)).first() is not None


def _already_notified(db: Session, recipient_id: int, dedupe_key: str) -> bool:
    """Cheap pre-check; the UNIQUE constraint remains the real guarantee."""
    return (
        db.query(Notification.id)
        .filter(Notification.user_id == recipient_id, Notification.dedupe_key == dedupe_key)
        .first()
        is not None
    )


def notify_safely(
    db: Session,
    type_: str,
    *,
    recipient_id: int | None,
    actor_id: int | None,
    entity_id: int | None,
    dedupe_key: str,
    listing_id: int | None = None,
    audience: str | None = None,
    when: datetime | None = None,
    body_suffix: str = "",
    coalesce_types: tuple[str, ...] = (),
    coalesce_seconds: int = 0,
    push_payload: dict | None = None,
) -> Notification | None:
    """Store one notification for `recipient_id`, or nothing.

    Returns the new row, or None when skipped (no recipient, recipient is the
    actor, duplicate key, coalesced) or when storing failed (logged; the
    caller's business transaction is unaffected). Never commits.
    """
    if recipient_id is None or (actor_id is not None and int(recipient_id) == int(actor_id)):
        return None
    # Business changes first, OUTSIDE the error handler: a failing business
    # flush must surface to the caller, never be mistaken for a notification
    # failure (begin_nested() would otherwise flush it inside our try).
    db.flush()
    try:
        spec = _TYPES[type_]
        target_audience = audience or spec.audience
        if target_audience not in AUDIENCES:
            raise ValueError("notification audience is required")
        with db.begin_nested():
            if _already_notified(db, recipient_id, dedupe_key):
                return None
            now = _utcnow_naive()
            if coalesce_types and coalesce_seconds > 0 and entity_id is not None:
                if _recent_unread_exists(
                    db, recipient_id, spec.entity_type, entity_id, coalesce_types,
                    now - timedelta(seconds=coalesce_seconds),
                ):
                    return None
            body = spec.body.format(listing=_listing_label(db, listing_id), when=format_when(when))
            row = Notification(
                user_id=recipient_id,
                audience=target_audience,
                type=type_,
                category=spec.category,
                title=spec.title[:_TITLE_MAX],
                body=(body + body_suffix)[:_BODY_MAX],
                entity_type=spec.entity_type,
                entity_id=entity_id,
                property_listing_id=listing_id,
                dedupe_key=dedupe_key[:191],
                created_at=now,
            )
            db.add(row)
            db.flush()
        # Email twin (Phase 3): queued only for a notification that was just
        # created, so never-the-actor, dedupe and follow-up coalescing apply
        # to email exactly as they do in-app. It has its own savepoint and
        # isolation; a queuing failure never affects this notification.
        from . import email as email_svc  # local import: email depends on models only
        email_svc.enqueue_for_notification(db, row, _target(row)["kind"], reminder_facts=push_payload)
        # Push twin (Phase 4): same rules, own savepoint and isolation.
        # `push_payload` carries reminder eligibility facts for the
        # delivery-time recheck (see services/reminders.py).
        from . import push as push_svc  # local import: avoids an import cycle
        push_svc.enqueue_for_notification(db, row, _target(row)["kind"], extra_payload=push_payload)
        return row
    except PendingRollbackError:
        # The session's transaction is already unusable: the business work
        # cannot commit either, so this must surface (never a silent success).
        raise
    except OperationalError as exc:
        if _mysql_code(exc) in _MYSQL_TXN_FATAL:
            log.error("Notification %s aborted the transaction: %s", type_, type(exc).__name__)
            raise
        return _isolated_failure(db, type_, exc)
    except IntegrityError as exc:
        if _is_duplicate(exc):
            return _isolated_failure(db, type_, exc, duplicate=True)
        return _isolated_failure(db, type_, exc)
    except Exception as exc:  # isolation: a savepoint-confined failure never breaks the business operation
        return _isolated_failure(db, type_, exc)


def _isolated_failure(db: Session, type_: str, exc: Exception, duplicate: bool = False) -> None:
    """A failure confined to the notification savepoint (already rolled back
    by the context manager). If, despite that, the session can no longer
    continue its transaction, the business work cannot commit: re-raise
    rather than let the caller report success. Otherwise log (type name only,
    never contents) and carry on without the notification."""
    if not db.is_active:
        raise exc
    if not duplicate:  # a duplicate is the normal "already notified" outcome
        log.error("Notification %s not stored: %s", type_, type(exc).__name__)
    return None


def notify_listing_decision(db: Session, listing: PropertyListing, verification_status: str,
                            attempt_no: int, actor_id: int | None) -> Notification | None:
    """LISTING_VERIFIED / LISTING_REJECTED to the listing's owner after an
    admin decision or the automatic verification (never the owner's own
    self-serve verify/reject: those callers do not call this). The owner is
    resolved fail-closed: both ownership columns must agree, otherwise
    nobody is notified. One notification per verification attempt."""
    physical = db.get(PhysicalProperty, listing.physical_property_id)
    if physical is None or physical.user_id != listing.user_id:
        return None
    type_ = "LISTING_VERIFIED" if verification_status == "VERIFIED" else "LISTING_REJECTED"
    return notify_safely(
        db, type_,
        recipient_id=listing.user_id, actor_id=actor_id,
        entity_id=listing.id, listing_id=listing.id,
        dedupe_key=f"listing:{listing.id}:verification:{attempt_no}:{verification_status}",
    )


# --- recipient side: list / count / read / delete ---------------------------------------

def _target(row: Notification) -> dict:
    """Fixed navigation target from the stored type (fail closed to NONE)."""
    t = row.type
    if t in ("ENQUIRY_NEW", "ENQUIRY_MESSAGE", "LEAD_REMINDER"):
        return {"kind": "OWNER_LEAD", "id": row.entity_id}
    if t in ("ENQUIRY_REPLY", "LEAD_CONTACTED", "LEAD_CLOSED"):
        return {"kind": "BUYER_ENQUIRY", "id": row.entity_id}
    if t.startswith("VISIT_") and row.audience in ("OWNER", "BUYER"):
        return {"kind": "OWNER_VISIT" if row.audience == "OWNER" else "BUYER_VISIT", "id": row.entity_id}
    if t.startswith("LISTING_") and row.property_listing_id is not None:
        return {"kind": "OWNER_LISTING", "id": row.property_listing_id}
    return {"kind": "NONE", "id": None}


def _items(db: Session, rows: list[Notification]) -> list[dict]:
    listing_ids = sorted({r.property_listing_id for r in rows if r.property_listing_id is not None})
    live_ids = (
        [lid for (lid,) in db.query(PropertyListing.id).filter(
            PropertyListing.id.in_(listing_ids), PropertyListing.deleted_at.is_(None)).all()]
        if listing_ids else []
    )
    covers = listing_svc.fetch_cover_map(db, live_ids)  # one batched query, no N+1
    return [
        {
            "id": r.id,
            "type": r.type,
            "category": r.category,
            "audience": r.audience,
            "title": r.title,
            "body": r.body,
            "entity_type": r.entity_type,
            "entity_id": r.entity_id,
            "property_listing_id": r.property_listing_id,
            "cover_image_url": covers.get(r.property_listing_id),
            "target": _target(r),
            "is_read": r.read_at is not None,
            "read_at": _utc_out(r.read_at),
            "created_at": _utc_out(r.created_at),
        }
        for r in rows
    ]


AudienceFilter = str | Sequence[str] | None


def _audience_set(audience: AudienceFilter) -> tuple[str, ...] | None:
    """None/empty = every audience; one value or several = their union,
    de-duplicated (a repeated value can never duplicate rows or inflate a
    count: each row has exactly one audience and is matched against a set).
    Unknown values are a programming error here (the router validates them
    against schemas.NotificationAudience and answers 422 first)."""
    if audience is None:
        return None
    values = (audience,) if isinstance(audience, str) else tuple(audience)
    if not values:
        return None
    unknown = [a for a in values if a not in AUDIENCES]
    if unknown:
        raise ValueError("unknown notification audience")
    return tuple(sorted(set(values)))


def _visible(db: Session, user_id: int, audience: AudienceFilter = None):
    """The caller's own undeleted rows, optionally limited to an audience
    set. The single filter behind list, unread count and mark-all-read, so
    all three always use identical audience semantics."""
    q = db.query(Notification).filter(Notification.user_id == user_id, Notification.deleted_at.is_(None))
    audiences = _audience_set(audience)
    if audiences is not None:
        q = q.filter(Notification.audience.in_(audiences))
    return q


def unread_count(db: Session, user_id: int, audience: AudienceFilter = None) -> int:
    return int(
        _visible(db, user_id, audience)
        .filter(Notification.read_at.is_(None))
        .with_entities(func.count(Notification.id))
        .scalar()
        or 0
    )


def list_notifications(
    db: Session,
    user_id: int,
    page: int,
    page_size: int,
    audience: AudienceFilter = None,
    category: str | None = None,
    unread_only: bool = False,
) -> tuple[list[dict], int, int]:
    """Newest first. Returns (items, total, unread_count); unread_count is for
    the same audience filter, ignoring category/unread_only (badge value)."""
    q = _visible(db, user_id, audience)
    if category is not None:
        q = q.filter(Notification.category == category)
    if unread_only:
        q = q.filter(Notification.read_at.is_(None))
    total = q.with_entities(func.count(Notification.id)).scalar() or 0
    rows = q.order_by(Notification.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return _items(db, rows), int(total), unread_count(db, user_id, audience)


def _own(db: Session, user_id: int, notification_id: int) -> Notification:
    row = db.get(Notification, notification_id)
    if row is None or row.user_id != user_id or row.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return row


def mark_read(db: Session, user_id: int, notification_id: int) -> dict:
    """Idempotent: an already-read row keeps its first read_at. Flushes."""
    row = _own(db, user_id, notification_id)
    if row.read_at is None:
        row.read_at = _utcnow_naive()
        db.flush()
    return _items(db, [row])[0]


def mark_all_read(db: Session, user_id: int, audience: AudienceFilter = None) -> int:
    """Marks every unread, undeleted row of the caller (optionally limited to
    one audience or a set of audiences) read. Returns how many changed.
    Flushes."""
    updated = (
        _visible(db, user_id, audience)
        .filter(Notification.read_at.is_(None))
        .update({Notification.read_at: _utcnow_naive()}, synchronize_session=False)
    )
    db.flush()
    db.expire_all()  # bulk UPDATE bypasses the identity map
    return int(updated or 0)


def delete_notification(db: Session, user_id: int, notification_id: int) -> None:
    """Soft delete (keeps the dedupe key, so the event cannot re-create it).
    404 for another user's or a missing row; repeating a delete of the
    caller's own row is an idempotent no-op. Flushes."""
    row = db.get(Notification, notification_id)
    if row is None or row.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if row.deleted_at is None:
        row.deleted_at = _utcnow_naive()
        db.flush()
