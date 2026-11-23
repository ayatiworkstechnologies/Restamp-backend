"""RESTAMP buyer enquiries, enquiry messages, owner leads and private lead notes.

Canonical states NEW/CONTACTED/CLOSED; buyers can only CREATE (forced NEW).
One ACTIVE enquiry per buyer+listing is enforced by active_enquiries' UNIQUE
plus an IntegrityError fallback for true races — that enquiry is the owner's
lead. Every DISTINCT buyer message is stored in enquiry_messages (D1): a
repeat send to the same listing appends to the active enquiry instead of
creating a second lead; an identical message from the same buyer within
_DOUBLE_TAP_SECONDS is treated as an accidental double tap and not stored
twice. The opening message of an enquiry created before enquiry_messages
existed lives only in enquiries.message and is presented as the first message
with id/created_at = null (never a fabricated time).

Owner side: leads are scoped by the stored owner_user_id; closing requires one
of the four outcomes (D5), is terminal (any later status write is a 422),
writes one audit_logs row, cancels the lead's active visits in the same
transaction (one more audit row when there were any) and never touches the
listing. The buyer's phone is never part of a lead response; the owner gets
it only through reveal_buyer_contact (audited). Private lead notes (D4) are owner-only and are never
included in any buyer, visit or public response.

Two-way threads: the listing owner may reply on an open lead
(reply_as_owner). Replies live in the same enquiry_messages table; a
message's sender role is DERIVED from sender_user_id against the enquiry's
stored buyer_user_id/owner_user_id (never sent by the client), which is
unambiguous because self-enquiries are refused. Each sender may store at
most _MESSAGE_RATE_LIMIT messages per thread per _MESSAGE_RATE_WINDOW_SECONDS;
the count is read from the database under the enquiry row lock, so it holds
across workers and processes (unlike the in-memory OTP throttle).

All new timestamps are app-set naive UTC (whole seconds, MySQL DATETIME) and
returned as explicit UTC.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import (
    ActiveEnquiry,
    AuditLog,
    AuthIdentity,
    Enquiry,
    EnquiryMessage,
    LeadNote,
    PhysicalProperty,
    PropertyListing,
)
from . import listings as listing_svc
from . import contact_guard
from . import notifications as notification_svc
from . import visits as visit_svc

_DOUBLE_TAP_SECONDS = 60
_MESSAGE_RATE_LIMIT = 30
_MESSAGE_RATE_WINDOW_SECONDS = 3600
CLOSING_OUTCOMES = ("Rented", "Sold", "Leased", "Not Converted")


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
    """Naive UTC, whole seconds: the DATETIME columns keep no fractions, so
    the value returned at creation is exactly the value every later read
    returns."""
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def _created_at_out(value: datetime | None) -> datetime | None:
    """Stored naive UTC -> explicit UTC for the API (serialized with "Z", so
    clients never mistake it for local time). None stays None: rows created
    before a timestamp existed have no trustworthy time."""
    return value.replace(tzinfo=timezone.utc) if value is not None else None


def safe_display_names(db: Session, user_ids: list[int]) -> dict[int, str | None]:
    """user_id -> display name safe to show another party (None when none).

    Phone-registered accounts start with display_name = their phone number,
    so a name is only returned when it passes the phone-leak rule of
    listings.safe_display_name (also used for the public owner name). Two
    batched reads; no other user columns are selected.
    """
    from ..models import AuthIdentity, User

    ids = sorted({int(u) for u in user_ids})
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
        int(uid): listing_svc.safe_display_name(name, phones.get(int(uid), set()))
        for uid, name in db.query(User.id, User.display_name).filter(User.id.in_(ids)).all()
    }


# Kept for existing call sites: buyer names on leads use the same rule.
_buyer_display_names = safe_display_names


# --- batched read helpers --------------------------------------------------------

def _listing_info(db: Session, listing_ids: list[int]) -> dict[int, dict]:
    """listing_id -> {listing, locality, city, cover} in three batched reads.
    Listings are never hard-deleted, so every enquiry's listing row exists;
    locality/city are None if the location chain is incomplete."""
    from ..models.locations import City, Locality

    ids = sorted({int(i) for i in listing_ids})
    if not ids:
        return {}
    listings = {
        int(l.id): l for l in db.query(PropertyListing).filter(PropertyListing.id.in_(ids)).all()
    }
    places = {
        int(lid): (loc, city)
        for lid, loc, city in listing_svc._location_joins(
            db.query(PropertyListing.id, Locality.name, City.name)
        ).filter(PropertyListing.id.in_(ids)).all()
    }
    covers = listing_svc.fetch_cover_map(db, ids)
    return {
        lid: {
            "listing": listing,
            "locality": places.get(lid, (None, None))[0],
            "city": places.get(lid, (None, None))[1],
            "cover": covers.get(lid),
        }
        for lid, listing in listings.items()
    }


def _sender_role(enquiry: Enquiry, sender_user_id: int) -> str:
    """OWNER only for the enquiry's stored owner (who is never its buyer);
    everything else on a thread was sent by the buyer."""
    sender = int(sender_user_id)
    if sender == int(enquiry.owner_user_id) and sender != int(enquiry.buyer_user_id):
        return "OWNER"
    return "BUYER"


def _message_out(enquiry: Enquiry, row: EnquiryMessage) -> dict:
    return {
        "id": row.id, "body": row.body, "sender": _sender_role(enquiry, row.sender_user_id),
        "created_at": _created_at_out(row.created_at),
    }


def _threads(db: Session, enquiries: list[Enquiry]) -> dict[int, list[dict]]:
    """enquiry_id -> messages [{id, body, sender, created_at}] in stable
    chronological (insertion id) order, both parties interleaved (one read)."""
    ids = [e.id for e in enquiries]
    rows_by: dict[int, list[EnquiryMessage]] = defaultdict(list)
    if ids:
        for row in (
            db.query(EnquiryMessage)
            .filter(EnquiryMessage.enquiry_id.in_(ids))
            .order_by(EnquiryMessage.enquiry_id, EnquiryMessage.id)
            .all()
        ):
            rows_by[int(row.enquiry_id)].append(row)
    out: dict[int, list[dict]] = {}
    for e in enquiries:
        rows = rows_by.get(int(e.id), [])
        thread: list[dict] = []
        opening = (e.message or "").strip()
        first = rows[0] if rows else None
        opening_stored = (
            first is not None
            and first.sender_user_id == e.buyer_user_id
            and e.created_at is not None
            and first.created_at == e.created_at
            and first.body == e.message
        )
        if opening and not opening_stored:
            # Opening message of an enquiry created before enquiry_messages:
            # only in enquiries.message; its time is the enquiry's (or null).
            thread.append({
                "id": None, "body": e.message, "sender": "BUYER",
                "created_at": _created_at_out(e.created_at),
            })
        thread.extend(_message_out(e, r) for r in rows)
        out[int(e.id)] = thread
    return out


def _thread_summary(thread: list[dict]) -> dict:
    last = thread[-1] if thread else None
    return {
        "message_count": len(thread),
        "last_message": last["body"] if last else None,
        "last_message_at": last["created_at"] if last else None,
    }


def _buyer_items(db: Session, enquiries: list[Enquiry], with_messages: bool) -> list[dict]:
    info = _listing_info(db, [e.property_listing_id for e in enquiries])
    threads = _threads(db, enquiries)
    items = []
    for e in enquiries:
        li = info[int(e.property_listing_id)]
        listing = li["listing"]
        thread = threads[int(e.id)]
        items.append({
            "id": e.id,
            "property_listing_id": e.property_listing_id,
            "message": e.message,
            "status": e.status,
            "closing_reason": e.closing_reason,
            "created_at": _created_at_out(e.created_at),
            **_thread_summary(thread),
            "messages": thread if with_messages else None,
            "listing": {
                "title": listing.title,
                "price_paise": listing.price_paise,
                "price_period": listing.price_period,
                "price_display": listing_svc.format_price(listing.price_paise, listing.price_period),
                "property_type": listing.property_type,
                "locality": li["locality"],
                "city": li["city"],
                "cover_image_url": li["cover"],
            },
        })
    return items


def _owner_items(db: Session, enquiries: list[Enquiry], with_messages: bool) -> list[dict]:
    info = _listing_info(db, [e.property_listing_id for e in enquiries])
    threads = _threads(db, enquiries)
    names = safe_display_names(db, [e.buyer_user_id for e in enquiries])
    items = []
    for e in enquiries:
        li = info[int(e.property_listing_id)]
        listing = li["listing"]
        thread = threads[int(e.id)]
        item = {
            "id": e.id,
            "property_listing_id": e.property_listing_id,
            "buyer_user_id": e.buyer_user_id,
            "buyer_display_name": names.get(int(e.buyer_user_id)),
            # NULL = created before source_role existed, when only BUYER-mode
            # accounts could enquire (alembic l2m3n4o5p6q7): a fact, not a guess.
            "source_role": e.source_role or "BUYER",
            "listing_removed": listing.deleted_at is not None,
            "message": e.message,
            "status": e.status,
            "closing_reason": e.closing_reason,
            "created_at": _created_at_out(e.created_at),
            **_thread_summary(thread),
            "listing": {
                "title": listing.title,
                "price_paise": listing.price_paise,
                "price_period": listing.price_period,
                "property_type": listing.property_type,
                "locality": li["locality"],
                "city": li["city"],
            },
        }
        if with_messages:
            item["messages"] = thread
        items.append(item)
    return items


# --- buyer: create / append / read ------------------------------------------------

def _clean_message(message: str | None) -> str | None:
    cleaned = (message or "").strip()
    return cleaned or None


def _append_message(
    db: Session, enquiry: Enquiry, sender_id: int, body: str
) -> tuple[EnquiryMessage | None, bool]:
    """Append a message from the enquiry's buyer or owner unless it repeats
    the same sender's latest message within the double-tap window.

    Returns (row, stored): the new row, or the repeated one (None only for a
    repeat of a pre-enquiry_messages opening message). Raises 429 once the
    sender has stored _MESSAGE_RATE_LIMIT messages on this thread within the
    window. Serialized per thread by locking the enquiry row, so the
    database count is reliable across workers. Flushes."""
    db.query(Enquiry.id).filter(Enquiry.id == enquiry.id).with_for_update().one()
    now = _utcnow_naive_seconds()
    window = timedelta(seconds=_DOUBLE_TAP_SECONDS)
    last = (
        db.query(EnquiryMessage)
        .filter(EnquiryMessage.enquiry_id == enquiry.id)
        .order_by(EnquiryMessage.id.desc())
        .first()
    )
    if last is not None:
        if last.sender_user_id == sender_id and last.body == body and now - last.created_at < window:
            return last, False
    elif (sender_id == enquiry.buyer_user_id and enquiry.message == body
          and enquiry.created_at is not None and now - enquiry.created_at < window):
        # Opening message (pre-enquiry_messages) just sent by the buyer. The
        # sender check matters: an owner reply that happens to repeat the
        # buyer's words is a different message and must be stored.
        return None, False
    recent = (
        db.query(func.count(EnquiryMessage.id))
        .filter(
            EnquiryMessage.enquiry_id == enquiry.id,
            EnquiryMessage.sender_user_id == sender_id,
            EnquiryMessage.created_at >= now - timedelta(seconds=_MESSAGE_RATE_WINDOW_SECONDS),
        )
        .scalar()
        or 0
    )
    if recent >= _MESSAGE_RATE_LIMIT:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many messages")
    row = EnquiryMessage(enquiry_id=enquiry.id, sender_user_id=sender_id, body=body, created_at=now)
    db.add(row)
    db.flush()
    return row, True


def _notify_follow_up(db: Session, enquiry: Enquiry, row: EnquiryMessage | None, stored: bool) -> None:
    """Buyer follow-up on an existing enquiry -> the enquiry's owner, coalesced:
    no new notification while the owner still has an unread new-enquiry or
    follow-up notification for this enquiry from the last 10 minutes. Only a
    newly STORED message counts (a double tap stores nothing).

    Concurrency: callers hold the enquiry row lock (SELECT ... FOR UPDATE in
    _append_message, held to COMMIT), and every ENQUIRY_NEW / ENQUIRY_MESSAGE
    writer for this enquiry holds it too (the creator holds the implicit lock
    on its newly inserted enquiry row). Concurrent follow-ups — including the
    create_enquiry IntegrityError race branch, whose loser appends to the
    winner's enquiry — therefore run one after another, and the coalescing
    check reads the latest committed notifications through a fresh read view
    (notifications._recent_unread_exists), not this transaction's older
    REPEATABLE READ snapshot. Proven by tests/test_notifications_concurrency.py
    against real MySQL with two connections."""
    if not stored or row is None:
        return
    notification_svc.notify_safely(
        db, "ENQUIRY_MESSAGE",
        recipient_id=enquiry.owner_user_id, actor_id=row.sender_user_id,
        entity_id=enquiry.id, listing_id=enquiry.property_listing_id,
        dedupe_key=f"enquiry:{enquiry.id}:message:{row.id}",
        coalesce_types=("ENQUIRY_NEW", "ENQUIRY_MESSAGE"),
        coalesce_seconds=notification_svc.FOLLOW_UP_COALESCE_SECONDS,
    )


def create_enquiry(
    db: Session, buyer_id: int, listing_id: int, message: str | None, *, source_role: str
) -> tuple[dict, bool]:
    """Create (NEW) or reuse the active enquiry; store the message. Returns
    (body, created). A reused enquiry gets the message appended (D1).
    `source_role` is the sender's current role (BUYER or BROKER = agent),
    stored on a NEW enquiry so its owner can tell buyer and agent leads apart;
    a reused enquiry keeps the role it was created with. Required (no
    default) so no caller can silently label an agent's lead as a buyer's."""
    listing = _visible_listing(db, listing_id)
    # Consistency gate first (fail-closed before any read of enquiry state):
    # both ownership columns must agree, or the row matches nobody.
    physical = db.get(PhysicalProperty, listing.physical_property_id)
    if physical is None or physical.user_id != listing.user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if physical.user_id == buyer_id:
        # Same rule as visits and call taps: an owner switched to BUYER mode
        # must not create a lead on (or inflate the counts of) their own
        # listing. Checked before any read or write of enquiry state.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Cannot enquire on your own listing",
        )
    body = _clean_message(message)

    def _active(current: bool = False) -> Enquiry | None:
        # current=True (race branch only): a locking read returns the latest
        # COMMITTED rows. The concurrent winner's active row and enquiry were
        # committed after this transaction's REPEATABLE READ snapshot, so a
        # plain read cannot see them (that made the loser fail with 409
        # instead of appending). Both are equality lookups on rows the
        # duplicate-key error proves exist (unique key / primary key), so
        # InnoDB takes record locks only, no gap locks.
        q = db.query(ActiveEnquiry).filter(
            ActiveEnquiry.buyer_user_id == buyer_id,
            ActiveEnquiry.property_listing_id == listing_id,
        )
        row = (q.with_for_update() if current else q).first()
        if row is None:
            return None
        if current:
            return db.get(Enquiry, row.enquiry_id, with_for_update=True, populate_existing=True)
        return db.get(Enquiry, row.enquiry_id)

    existing = _active()
    if existing is not None:
        if body:
            row, stored = _append_message(db, existing, buyer_id, body)
            _notify_follow_up(db, existing, row, stored)
        return _buyer_items(db, [existing], with_messages=True)[0], False

    now = _utcnow_naive_seconds()
    enquiry = Enquiry(
        property_listing_id=listing_id,
        buyer_user_id=buyer_id,
        owner_user_id=physical.user_id,
        message=body,
        status="NEW",
        created_at=now,
        source_role=source_role,
    )
    try:
        # Savepoint: a concurrent duplicate rolls back only this insert set,
        # never the caller's wider transaction.
        with db.begin_nested():
            db.add(enquiry)
            db.flush()
            db.add(ActiveEnquiry(enquiry_id=enquiry.id, buyer_user_id=buyer_id, property_listing_id=listing_id))
            if body:
                # Same timestamp as the enquiry: marks it as the opening message.
                db.add(EnquiryMessage(enquiry_id=enquiry.id, sender_user_id=buyer_id, body=body, created_at=now))
            db.flush()
    except IntegrityError:
        # Lost a concurrent race: the winner's enquiry now exists — append to it.
        existing = _active(current=True)
        if existing is None:  # pragma: no cover - defensive; constraint guarantees a row
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Enquiry already active")
        if body:
            row, stored = _append_message(db, existing, buyer_id, body)
            _notify_follow_up(db, existing, row, stored)
        return _buyer_items(db, [existing], with_messages=True)[0], False
    # A genuinely new lead (not a reused active enquiry): tell the owner. One
    # key per enquiry, so a retry can never notify twice.
    notification_svc.notify_safely(
        db, "ENQUIRY_NEW",
        recipient_id=enquiry.owner_user_id, actor_id=buyer_id,
        entity_id=enquiry.id, listing_id=listing_id,
        dedupe_key=f"enquiry:{enquiry.id}:new",
    )
    return _buyer_items(db, [enquiry], with_messages=True)[0], True


def list_enquiries(
    db: Session, buyer_id: int, page: int, page_size: int, stat: str | None = None
) -> tuple[list[dict], int]:
    """Own enquiries, newest activity first (latest message, else creation;
    enquiries with no known time last, newest id first)."""
    base = db.query(Enquiry).filter(Enquiry.buyer_user_id == buyer_id)
    if stat is not None:
        base = base.filter(Enquiry.status == stat)
    total = base.with_entities(func.count(Enquiry.id)).scalar() or 0
    last_msg = (
        db.query(EnquiryMessage.enquiry_id.label("enquiry_id"), func.max(EnquiryMessage.created_at).label("last_at"))
        .group_by(EnquiryMessage.enquiry_id)
        .subquery()
    )
    activity = func.coalesce(last_msg.c.last_at, Enquiry.created_at)
    rows = (
        base.outerjoin(last_msg, last_msg.c.enquiry_id == Enquiry.id)
        .order_by(activity.is_(None), activity.desc(), Enquiry.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return _buyer_items(db, rows, with_messages=False), total


def get_enquiry(db: Session, buyer_id: int, enquiry_id: int) -> dict:
    enquiry = db.get(Enquiry, enquiry_id)
    if enquiry is None or enquiry.buyer_user_id != buyer_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return _buyer_items(db, [enquiry], with_messages=True)[0]


# --- owner: leads ----------------------------------------------------------------

# Owner-side lifecycle (P4): NEW -> CONTACTED -> CLOSED, plus NEW -> CLOSED
# directly, CONTACTED -> NEW. Same-status writes are idempotent no-ops.
# CLOSED is terminal (D5): every later write — even repeating the same close
# from a stale screen — is a 422, so a closed lead is never actionable again.
# Closing frees the buyer's active-enquiry row so a new enquiry creates a
# fresh lead, and cancels the lead's active visits atomically. The listing is
# never touched.
_ALLOWED_OWNER_TRANSITIONS = {
    "NEW": {"NEW", "CONTACTED", "CLOSED"},
    "CONTACTED": {"CONTACTED", "NEW", "CLOSED"},
    "CLOSED": {"CLOSED"},
}


def _owned_lead(db: Session, owner_id: int, lead_id: int, for_update: bool = False) -> Enquiry:
    enquiry = db.get(Enquiry, lead_id, with_for_update=True if for_update else None)
    if enquiry is None or enquiry.owner_user_id != owner_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return enquiry


def transition_owner_lead(
    db: Session, owner_id: int, lead_id: int, to_status: str, closing_reason: str | None
) -> dict:
    """Persist an owner-side status change. 404 for foreign/missing leads,
    422 for unsupported transitions. The request schema guarantees that
    CLOSED comes with one of CLOSING_OUTCOMES and other statuses without a
    reason. CLOSED removes the active row, cancels the lead's active visits
    and writes the audit_logs rows. Any write on an already CLOSED lead is a
    422 "Lead is already closed". Never touches the property listing.
    Flushes; the caller commits (one transaction: lead, visits and audit
    rows land together or not at all)."""
    enquiry = _owned_lead(db, owner_id, lead_id, for_update=True)
    previous_status, previous_reason = enquiry.status, enquiry.closing_reason
    if previous_status == "CLOSED":
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Lead is already closed")
    if to_status not in _ALLOWED_OWNER_TRANSITIONS.get(previous_status, set()):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid request")
    enquiry.status = to_status
    cancelled: list = []
    if to_status == "CLOSED":
        enquiry.closing_reason = closing_reason
        db.query(ActiveEnquiry).filter(ActiveEnquiry.enquiry_id == enquiry.id).delete()
        db.add(AuditLog(
            user_id=owner_id,
            action="LEAD_CLOSED",
            entity="enquiry",
            before_json={"enquiry_id": enquiry.id, "status": previous_status, "closing_reason": previous_reason},
            after_json={"enquiry_id": enquiry.id, "status": "CLOSED", "closing_reason": closing_reason},
            created_at=_utcnow_naive_seconds(),
        ))
        cancelled = visit_svc.close_visits_for_lead(db, enquiry)
        if cancelled:
            # Visit history stays (rows are updated, never deleted); this row
            # records which visits the close ended and what they were before.
            db.add(AuditLog(
                user_id=owner_id,
                action="LEAD_VISITS_CANCELLED",
                entity="property_visit",
                before_json={"enquiry_id": enquiry.id,
                             "visits": [{"id": vid, "status": st} for vid, st in cancelled]},
                after_json={"enquiry_id": enquiry.id,
                            "visits": [{"id": vid, "status": "CANCELLED"} for vid, _ in cancelled]},
                created_at=_utcnow_naive_seconds(),
            ))
    db.flush()
    # Buyer (enquiry sender) hears about real changes only: CONTACTED once per
    # enquiry (a NEW<->CONTACTED flip-flop cannot notify twice: same key) and
    # CLOSED once (terminal). A move back to NEW and same-state repeats are
    # silent. A close that cancelled visits sends this ONE notification
    # (with a suffix), never one per cancelled visit. The closing reason is
    # the owner's private outcome and is not shown.
    if to_status != previous_status and to_status in ("CONTACTED", "CLOSED"):
        notification_svc.notify_safely(
            db, "LEAD_CONTACTED" if to_status == "CONTACTED" else "LEAD_CLOSED",
            recipient_id=enquiry.buyer_user_id, actor_id=owner_id,
            entity_id=enquiry.id, listing_id=enquiry.property_listing_id,
            dedupe_key=f"enquiry:{enquiry.id}:status:{to_status}",
            body_suffix=notification_svc.LEAD_CLOSED_VISITS_SUFFIX if cancelled else "",
        )
    return _owner_items(db, [enquiry], with_messages=False)[0]


def list_owner_leads(
    db: Session,
    owner_id: int,
    page: int,
    page_size: int,
    property_listing_id: int | None = None,
    stat: str | None = None,
) -> tuple[list[dict], int]:
    """List buyer enquiries belonging to the authenticated owner (newest first).

    Ownership authority is the enquiry's stored owner_user_id (written from
    physical_properties.user_id at creation, the same authority contact
    reveal uses). A property filter is validated against the caller's owned
    physical properties — a foreign id raises 404, never an empty list, so
    callers cannot probe other owners' listings. Historical leads stay
    visible (no deleted_at/listing-status filter; flagged via
    listing_removed). No phone numbers; buyer names only when chosen.
    """
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

    base = db.query(Enquiry).filter(Enquiry.owner_user_id == owner_id)
    if property_listing_id is not None:
        base = base.filter(Enquiry.property_listing_id == property_listing_id)
    if stat is not None:
        base = base.filter(Enquiry.status == stat)
    total = base.with_entities(func.count(Enquiry.id)).scalar() or 0
    rows = base.order_by(Enquiry.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return _owner_items(db, rows, with_messages=False), total


def get_owner_lead(db: Session, owner_id: int, lead_id: int) -> dict:
    """One lead with its full two-way thread; 404 unless the caller's."""
    return _owner_items(db, [_owned_lead(db, owner_id, lead_id)], with_messages=True)[0]


def reveal_buyer_contact(db: Session, owner_id: int, lead_id: int) -> str:
    """The buyer's verified phone for the caller's own lead, audited.

    Authority is the lead itself: the enquiry's stored owner_user_id AND both
    listing ownership columns must equal the caller (same gate as owner
    replies), else 404 — another owner can never reach a buyer's number by
    guessing lead ids. Closed leads still answer (read-only reference to a
    buyer who contacted the owner); only writes are blocked on them. 404
    "Contact not available" when the buyer has no phone identity (e.g. a
    Google-only account). Writes one audit_logs row naming the lead and
    buyer — never the number. Flushes; the caller commits.
    """
    enquiry = _owned_lead(db, owner_id, lead_id)
    listing = db.get(PropertyListing, enquiry.property_listing_id)
    physical = db.get(PhysicalProperty, listing.physical_property_id) if listing is not None else None
    if listing is None or physical is None or listing.user_id != owner_id or physical.user_id != owner_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if enquiry.buyer_user_id == owner_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    phone_row = (
        db.query(AuthIdentity)
        .filter(AuthIdentity.user_id == enquiry.buyer_user_id, AuthIdentity.provider == "phone")
        .first()
    )
    if phone_row is None or not phone_row.provider_identifier:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Contact not available")
    db.add(AuditLog(
        user_id=owner_id,
        action="LEAD_CONTACT_REVEALED",
        entity="enquiry",
        before_json=None,
        after_json={"enquiry_id": enquiry.id, "buyer_user_id": enquiry.buyer_user_id},
        created_at=_utcnow_naive_seconds(),
    ))
    db.flush()
    return phone_row.provider_identifier


def reply_as_owner(db: Session, owner_id: int, lead_id: int, body: str) -> tuple[dict, bool]:
    """Store an owner reply on the caller's open lead. Returns (message,
    created); an identical double tap within the window returns the stored
    reply with created=False. The schema guarantees a trimmed 1-2000 char
    body. Flushes; the caller commits.

    404 unless the lead is the caller's AND the listing's two ownership
    columns still both equal the caller (fail closed, same as owner listing
    writes); 422 on a closed lead (terminal) or a legacy self-enquiry; 429
    from the per-thread rate limit."""
    enquiry = _owned_lead(db, owner_id, lead_id, for_update=True)
    listing = db.get(PropertyListing, enquiry.property_listing_id)
    physical = db.get(PhysicalProperty, listing.physical_property_id) if listing is not None else None
    if listing is None or physical is None or listing.user_id != owner_id or physical.user_id != owner_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if enquiry.buyer_user_id == owner_id:
        # Only possible for rows created before self-enquiries were refused.
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="Cannot reply to your own enquiry")
    if enquiry.status == "CLOSED":
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Lead is closed")
    # M9: no contact details to a buyer who has not unlocked THIS listing.
    # Read-only check on server-side ids, before anything is stored or
    # notified (refusal = 422, no message row, notification or audit row).
    contact_guard.ensure_allowed(db, enquiry.buyer_user_id, enquiry.property_listing_id, body)
    row, created = _append_message(db, enquiry, owner_id, body)
    if created:
        # One notification per stored reply (a double tap stores nothing).
        notification_svc.notify_safely(
            db, "ENQUIRY_REPLY",
            recipient_id=enquiry.buyer_user_id, actor_id=owner_id,
            entity_id=enquiry.id, listing_id=enquiry.property_listing_id,
            dedupe_key=f"enquiry:{enquiry.id}:message:{row.id}",
        )
    return _message_out(enquiry, row), created


# --- owner: private lead notes (D4) ----------------------------------------------

def _note_out(note: LeadNote) -> dict:
    return {"id": note.id, "body": note.body, "created_at": _created_at_out(note.created_at)}


def list_lead_notes(db: Session, owner_id: int, lead_id: int) -> tuple[list[dict], int]:
    """Oldest -> newest notes on the caller's lead; 404 unless the caller's."""
    _owned_lead(db, owner_id, lead_id)
    notes = (
        db.query(LeadNote)
        .filter(LeadNote.enquiry_id == lead_id, LeadNote.owner_user_id == owner_id)
        .order_by(LeadNote.id)
        .all()
    )
    return [_note_out(n) for n in notes], len(notes)


def add_lead_note(db: Session, owner_id: int, lead_id: int, body: str) -> dict:
    """Append a private note (append-only; allowed on CLOSED leads). The
    schema guarantees a trimmed 1-2000 char body. Flushes; caller commits."""
    _owned_lead(db, owner_id, lead_id)
    note = LeadNote(enquiry_id=lead_id, owner_user_id=owner_id, body=body, created_at=_utcnow_naive_seconds())
    db.add(note)
    db.flush()
    return _note_out(note)
