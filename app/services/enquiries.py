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
of the four outcomes (D5), is terminal, writes one audit_logs row and never
touches the listing. Private lead notes (D4) are owner-only and are never
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
    Enquiry,
    EnquiryMessage,
    LeadNote,
    PhysicalProperty,
    PropertyListing,
)
from . import listings as listing_svc

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


def create_enquiry(
    db: Session, buyer_id: int, listing_id: int, message: str | None
) -> tuple[dict, bool]:
    """Create (NEW) or reuse the active enquiry; store the message. Returns
    (body, created). A reused enquiry gets the message appended (D1)."""
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

    def _active() -> Enquiry | None:
        row = (
            db.query(ActiveEnquiry)
            .filter(
                ActiveEnquiry.buyer_user_id == buyer_id,
                ActiveEnquiry.property_listing_id == listing_id,
            )
            .first()
        )
        return db.get(Enquiry, row.enquiry_id) if row is not None else None

    existing = _active()
    if existing is not None:
        if body:
            _append_message(db, existing, buyer_id, body)
        return _buyer_items(db, [existing], with_messages=True)[0], False

    now = _utcnow_naive_seconds()
    enquiry = Enquiry(
        property_listing_id=listing_id,
        buyer_user_id=buyer_id,
        owner_user_id=physical.user_id,
        message=body,
        status="NEW",
        created_at=now,
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
        existing = _active()
        if existing is None:  # pragma: no cover - defensive; constraint guarantees a row
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Enquiry already active")
        if body:
            _append_message(db, existing, buyer_id, body)
        return _buyer_items(db, [existing], with_messages=True)[0], False
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
# CLOSED is terminal (D5): repeating the same close is a no-op, a different
# outcome is a 422. Closing frees the buyer's active-enquiry row so a new
# enquiry creates a fresh lead. The listing is never touched.
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
    reason. CLOSED removes the active row and writes one audit_logs row.
    Never touches the property listing. Flushes; the caller commits."""
    enquiry = _owned_lead(db, owner_id, lead_id, for_update=True)
    previous_status, previous_reason = enquiry.status, enquiry.closing_reason
    if to_status not in _ALLOWED_OWNER_TRANSITIONS.get(previous_status, set()):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid request")
    if previous_status == "CLOSED":
        if closing_reason != previous_reason:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Lead is already closed")
        return _owner_items(db, [enquiry], with_messages=False)[0]  # idempotent repeat
    enquiry.status = to_status
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
    db.flush()
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
    row, created = _append_message(db, enquiry, owner_id, body)
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
