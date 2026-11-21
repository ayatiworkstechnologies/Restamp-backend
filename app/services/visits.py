"""RESTAMP property visits — buyer/owner scheduled viewings.

Lifecycle: strict Phase 5 (owner-approved). Every new visit — a buyer
request (initiator BUYER) or an owner/dealer proposal (initiator OWNER) —
starts PENDING.

Actor rules (enforced here, never in the frontend):
- PENDING -> CONFIRMED / REJECTED: the listing's poster (owner or dealer)
  only, for every pending visit, including the poster's own proposal.
  PENDING -> CANCELLED: either party (a buyer withdraws a request or turns
  down a proposal by cancelling it).
- CONFIRMED -> COMPLETED: poster only; CONFIRMED -> CANCELLED: either.
- REJECTED / DECLINED / CANCELLED / COMPLETED are terminal.
- No time or note edits on any visit: the proposer can never edit a
  submitted proposal, and confirmed/rejected visits are immutable. A new
  time is a new proposal (reject or cancel, then create another).
- Same-state repeats are idempotent no-ops (no write, no notification).
  Anything else raises ValueError (-> 422).
- Legacy rows created before Phase 5 (REQUESTED / PROPOSED) follow the same
  rules as PENDING; DECLINED stays a valid terminal state for them.

Creation rules: the listing must be VERIFIED + AVAILABLE with consistent
dual ownership columns; scheduled_at must be in the future; buyers cannot
schedule on their own listings; owners may propose only to buyers holding
an existing enquiry or contact-reveal relationship on that listing (no
arbitrary user IDs). An owner proposing on their OWN listing that is not
live (closed, removed, unverified) gets a 422 naming that reason — leads
outlive their listing, and a bare 404 there read as "lead not found". A
listing the caller does not own stays 404 (no existence leak). Responses carry no phones. Names (D6): the buyer's
chosen name and the owner's public name, null when there is none — never a
phone-number default. rescheduled_at (D3) is cleared by every status change
(time edits no longer exist, so it is only ever set on legacy rows).

Timestamps: scheduled_at/created_at/updated_at are stored as naive UTC
(whole seconds) and returned as explicit UTC ("...Z"), so a client can
never read them as local time.

Closed leads (D5): a visit belongs to the lead (enquiry) of the same
listing + buyer + owner; the newest such enquiry is the current lead.
Closing a lead cancels its active visits (PENDING/REQUESTED/PROPOSED/CONFIRMED ->
CANCELLED) in the same transaction (close_visits_for_lead). While that lead
stays closed, neither party may create or change a visit for the pair: 422
LEAD_CLOSED_DETAIL, after the usual 404 scope checks (no existence leak).
A buyer's new enquiry opens a fresh lead and lifts the block; visits with
no enquiry behind them (contact-reveal only) are not tied to any lead.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from ..models import ActivityLog, ContactRevealAudit, Enquiry, PhysicalProperty, PropertyListing
from ..models.visits import PropertyVisit
from . import notifications as notification_svc

LEAD_CLOSED_DETAIL = "Lead is closed"

# Who requested a visit (BUYER or BROKER = agent), so owners can tell agent
# visits apart. property_visits has no role column; like call taps
# (call_events.py) the role is kept in activity_logs, one row per buyer-made
# visit, keyed by the visit id. Visits from before this row existed, and
# owner proposals, have no requester role (null), never a guessed one.
VISIT_REQUEST_ACTION = "VISIT_REQUESTED"
ENTITY_VISIT = "property_visit"
# Open visits (a lead close cancels these). PENDING is the Phase 5 initial
# state; REQUESTED/PROPOSED remain for rows created before it.
_ACTIVE_STATUSES = ("PENDING", "REQUESTED", "PROPOSED", "CONFIRMED")
# Awaiting the poster's decision (one per buyer+listing+initiator at a time).
_PENDING_STATUSES = ("PENDING", "REQUESTED", "PROPOSED")


def _lead_closed(
    db: Session, listing_id: int, buyer_id: int, owner_id: int, for_update: bool = False
) -> bool:
    """True when the newest enquiry for this listing + buyer + owner is CLOSED.

    for_update locks that enquiry row, serializing visit creation against a
    concurrent close (which holds the same lock while it cancels visits), so
    a closed lead can never be left with a freshly created active visit.
    """
    q = (
        db.query(Enquiry)
        .filter(
            Enquiry.property_listing_id == listing_id,
            Enquiry.buyer_user_id == buyer_id,
            Enquiry.owner_user_id == owner_id,
        )
        .order_by(Enquiry.id.desc())
    )
    if for_update:
        q = q.with_for_update()
    latest = q.first()
    return latest is not None and latest.status == "CLOSED"


def close_visits_for_lead(db: Session, enquiry: Enquiry) -> list[tuple[int, str]]:
    """Cancel every active visit of the lead's listing + buyer + owner.

    Called by the lead close with the enquiry row already locked, inside the
    same transaction: either the lead closes AND its visits are cancelled, or
    neither happens. Rows are locked first so a concurrent visit edit either
    finishes before (and is then cancelled here) or runs after and is refused
    by the closed-lead check. Terminal visits are left as they are (history).
    Returns [(visit id, previous status)] for the caller's audit row.
    """
    rows = (
        db.query(PropertyVisit)
        .filter(
            PropertyVisit.property_listing_id == enquiry.property_listing_id,
            PropertyVisit.buyer_user_id == enquiry.buyer_user_id,
            PropertyVisit.owner_user_id == enquiry.owner_user_id,
            PropertyVisit.status.in_(_ACTIVE_STATUSES),
        )
        .order_by(PropertyVisit.id)
        .with_for_update()
        .all()
    )
    now = _utcnow_naive()
    cancelled = []
    for visit in rows:
        cancelled.append((int(visit.id), visit.status))
        visit.status = "CANCELLED"
        visit.rescheduled_at = None  # a status change ends the "Rescheduled" state
        visit.updated_at = now
    db.flush()
    return cancelled


def _utcnow_naive() -> datetime:
    """Naive UTC, whole seconds: DATETIME keeps no fractions (MySQL would
    round them), so the value returned is exactly the value stored."""
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def _utc_out(value: datetime | None) -> datetime | None:
    """Stored naive UTC -> explicit UTC for the API (serialized with "Z")."""
    return value.replace(tzinfo=timezone.utc) if value is not None else None


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
    """Fetch a publicly visible listing and lock its row for the duration of
    the transaction. The row lock serializes competing visit-creation and
    reschedule requests for the same listing, preventing duplicate active
    visits under concurrency.
    """
    listing = (
        db.query(PropertyListing)
        .filter(
            PropertyListing.id == listing_id,
            PropertyListing.verification_status == "VERIFIED",
            PropertyListing.listing_status == "AVAILABLE",
            PropertyListing.deleted_at.is_(None),
        )
        .with_for_update()
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


def _snapshots(db: Session, visits: list[PropertyVisit]) -> list[dict]:
    """VisitOut dicts for a page of visits: names, listing details and
    locations in batched reads (no per-row queries)."""
    from .enquiries import _listing_info, safe_display_names

    info = _listing_info(db, [v.property_listing_id for v in visits])
    names = safe_display_names(db, [u for v in visits for u in (v.buyer_user_id, v.owner_user_id)])
    roles = _requester_roles(db, [v.id for v in visits])
    out = []
    for visit in visits:
        li = info[int(visit.property_listing_id)]
        listing = li["listing"]
        out.append({
            "id": visit.id,
            "property_listing_id": visit.property_listing_id,
            "buyer_user_id": visit.buyer_user_id,
            "owner_user_id": visit.owner_user_id,
            "buyer_display_name": names.get(int(visit.buyer_user_id)),
            "owner_display_name": names.get(int(visit.owner_user_id)),
            "scheduled_at": _utc_out(visit.scheduled_at),
            "status": visit.status,
            "initiator": visit.initiator,
            "requester_role": roles.get(int(visit.id)),
            "note": visit.note,
            "listing": {
                "title": listing.title,
                "price_paise": listing.price_paise,
                "price_period": listing.price_period,
                "property_type": listing.property_type,
                "locality": li["locality"],
                "city": li["city"],
            },
            "created_at": _utc_out(visit.created_at),
            "updated_at": _utc_out(visit.updated_at),
            "rescheduled_at": _utc_out(visit.rescheduled_at),
        })
    return out


def _requester_roles(db: Session, visit_ids: list[int]) -> dict[int, str]:
    """visit id -> requester role (BUYER/BROKER) from VISIT_REQUESTED rows."""
    if not visit_ids:
        return {}
    rows = (
        db.query(ActivityLog.entity_id, ActivityLog.details)
        .filter(
            ActivityLog.action == VISIT_REQUEST_ACTION,
            ActivityLog.entity_type == ENTITY_VISIT,
            ActivityLog.entity_id.in_(visit_ids),
        )
        .all()
    )
    out: dict[int, str] = {}
    for vid, details in rows:
        role = (details or {}).get("source_role") if isinstance(details, dict) else None
        if role in ("BUYER", "BROKER"):
            out[int(vid)] = role
    return out


def _snapshot(db: Session, visit: PropertyVisit) -> dict:
    return _snapshots(db, [visit])[0]


def create_buyer_visit(
    db: Session,
    buyer_id: int,
    listing_id: int,
    scheduled_at: datetime,
    note: str | None,
    *,
    source_role: str,
) -> dict:
    """Buyer schedules a visit (lands REQUESTED). Returns the visit body.
    `source_role` is the requester's current role (BUYER or BROKER); it is
    recorded for the owner (required: never defaulted, so an agent's visit
    cannot silently read as a buyer's)."""
    listing = _visible_listing(db, listing_id)
    owner_id = _consistent_owner_id(db, listing)
    if buyer_id == owner_id:
        raise ValueError("cannot schedule a visit on your own listing")
    if _lead_closed(db, listing_id, buyer_id, owner_id, for_update=True):
        raise ValueError(LEAD_CLOSED_DETAIL)
    moment = _normalize_scheduled_at(scheduled_at)
    if _has_active_visit_for_buyer_listing(db, listing_id, buyer_id, initiator="BUYER"):
        raise ValueError("A visit is already active for this buyer on this listing")
    visit = PropertyVisit(
        property_listing_id=listing_id,
        buyer_user_id=buyer_id,
        owner_user_id=owner_id,
        scheduled_at=moment,
        status="PENDING",
        initiator="BUYER",
        note=note,
        created_at=_utcnow_naive(),
        updated_at=_utcnow_naive(),
    )
    db.add(visit)
    db.flush()
    db.add(ActivityLog(
        user_id=buyer_id, action=VISIT_REQUEST_ACTION, entity_type=ENTITY_VISIT,
        entity_id=visit.id, created_at=_utcnow_naive(),
        details={"source_role": source_role, "listing_id": int(listing_id)},
    ))
    db.flush()
    notification_svc.notify_safely(
        db, "VISIT_REQUESTED",
        recipient_id=owner_id, actor_id=buyer_id, entity_id=visit.id,
        listing_id=listing_id, when=visit.scheduled_at,
        dedupe_key=f"visit:{visit.id}:REQUESTED",
    )
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
            PropertyVisit.status.in_(_PENDING_STATUSES),
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

    # Ownership first (404 for anything not the caller's), then liveness: the
    # owner may learn their own listing's state, nobody else's.
    candidate = db.get(PropertyListing, listing_id)
    if candidate is None or _consistent_owner_id(db, candidate) != owner_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if not (
        candidate.verification_status == "VERIFIED"
        and candidate.listing_status == "AVAILABLE"
        and candidate.deleted_at is None
    ):
        raise ValueError("Visits can only be scheduled on a live listing")
    listing = _visible_listing(db, listing_id)  # row lock; 404 if it changed meanwhile
    if db.get(User, buyer_user_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if buyer_user_id == owner_id:
        raise ValueError("cannot propose a visit to yourself")
    if not _buyer_has_relationship(db, listing_id, buyer_user_id):
        raise ValueError("buyer has no enquiry or contact relationship on this listing")
    if _lead_closed(db, listing_id, buyer_user_id, owner_id, for_update=True):
        raise ValueError(LEAD_CLOSED_DETAIL)
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
        status="PENDING",
        initiator="OWNER",
        note=note,
        created_at=_utcnow_naive(),
        updated_at=_utcnow_naive(),
    )
    db.add(visit)
    db.flush()
    notification_svc.notify_safely(
        db, "VISIT_PROPOSED",
        recipient_id=buyer_user_id, actor_id=owner_id, entity_id=visit.id,
        listing_id=listing_id, when=visit.scheduled_at,
        dedupe_key=f"visit:{visit.id}:PROPOSED",
    )
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
    return _snapshots(db, rows), total


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
    return _snapshots(db, rows), total


def get_owner_visit(db: Session, owner_id: int, visit_id: int) -> dict:
    visit = db.get(PropertyVisit, visit_id)
    if visit is None or visit.owner_user_id != owner_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return _snapshot(db, visit)


# Allowed (status, actor) moves. "either" = the visit's buyer or owner;
# otherwise the named party only. Strict Phase 5 (see module docstring):
# the poster alone confirms/rejects a pending visit; either party may cancel
# an open one; there are no back edges, so each status is reached at most
# once per visit (the notification dedupe keys rely on that).
_ALLOWED = {
    ("PENDING", "CONFIRMED"): ("owner",),
    ("PENDING", "REJECTED"): ("owner",),
    ("PENDING", "CANCELLED"): ("either",),
    # Legacy rows (created before Phase 5) follow the same authority.
    ("REQUESTED", "CONFIRMED"): ("owner",),
    ("REQUESTED", "REJECTED"): ("owner",),
    ("REQUESTED", "DECLINED"): ("owner",),
    ("REQUESTED", "CANCELLED"): ("either",),
    ("PROPOSED", "CONFIRMED"): ("owner",),
    ("PROPOSED", "REJECTED"): ("owner",),
    ("PROPOSED", "DECLINED"): ("owner",),
    ("PROPOSED", "CANCELLED"): ("either",),
    # Confirmed: schedule immutable; it can only end.
    ("CONFIRMED", "CONFIRMED"): ("either",),
    ("CONFIRMED", "CANCELLED"): ("either",),
    ("CONFIRMED", "COMPLETED"): ("owner",),
    # Terminal states: same-state repeats only (idempotent no-ops).
    ("REJECTED", "REJECTED"): ("either",),
    ("DECLINED", "DECLINED"): ("either",),
    ("CANCELLED", "CANCELLED"): ("either",),
    ("COMPLETED", "COMPLETED"): ("either",),
}

_NOTIFIED_STATUSES = ("CONFIRMED", "REJECTED", "DECLINED", "CANCELLED")


def _is_proposer(visit: PropertyVisit, actor: str) -> bool:
    """The party that submitted the visit: the buyer for a buyer request,
    the poster for a poster proposal (from the stored initiator, never the
    client)."""
    return (actor == "buyer" and visit.initiator == "BUYER") or (
        actor == "owner" and visit.initiator == "OWNER"
    )


def transition_visit(
    db: Session,
    actor: str,
    actor_id: int,
    visit_id: int,
    scheduled_at: datetime | None = None,
    to_status: str | None = None,
    note: str | None = None,
) -> dict:
    """Apply a status transition with actor enforcement (strict Phase 5).

    Scope: buyers see only buyer_user_id == self, owners only
    owner_user_id == self (else 404, no existence leak). The visit row is
    locked (SELECT ... FOR UPDATE) so concurrent transitions serialize: the
    second one sees the first one's result and is judged against it.
    Time/note edits are always refused (422): the proposer may not edit a
    submitted proposal and nobody may edit a confirmed or rejected one.
    Same-state repeats change nothing (no write, no notification). Invalid
    moves raise ValueError (-> 422). Flushes; the caller commits.
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
    # After the scope checks (no leak): a closed lead's visits are read-only
    # for both parties, including same-state repeats from a stale screen.
    if _lead_closed(db, visit.property_listing_id, visit.buyer_user_id, visit.owner_user_id):
        raise ValueError(LEAD_CLOSED_DETAIL)

    original_status = visit.status
    if scheduled_at is not None or note is not None:
        if _is_proposer(visit, actor) and original_status in _PENDING_STATUSES:
            raise ValueError("The proposer cannot edit a submitted visit proposal")
        raise ValueError(f"A {original_status} visit cannot be rescheduled or edited")
    if to_status is None:  # pragma: no cover - the request schema requires a field
        raise ValueError("status is required")

    allowed = _ALLOWED.get((original_status, to_status))
    if allowed is None:
        raise ValueError(f"Cannot move visit from {original_status} to {to_status}")
    if "either" not in allowed and actor not in allowed:
        raise ValueError(f"Only the {allowed[0]} may move visit from {original_status} to {to_status}")
    if to_status == original_status:
        return _snapshot(db, visit)  # idempotent repeat: nothing written

    visit.status = to_status
    visit.rescheduled_at = None  # a status change ends the "Rescheduled" display state
    visit.updated_at = _utcnow_naive()  # created_at is never touched
    db.flush()
    if to_status in _NOTIFIED_STATUSES:
        _notify_other_party(db, visit, actor, actor_id)
    db.refresh(visit)
    return _snapshot(db, visit)


def _notify_other_party(db: Session, visit: PropertyVisit, actor: str, actor_id: int) -> None:
    """Tell the OTHER party about a real status change (never the actor).

    Each status is reachable at most once per visit (_ALLOWED has no back
    edges), so ``visit:{id}:{status}`` dedupes retries. COMPLETED is silent.
    Lead-close cancellations never come through here (close_visits_for_lead),
    so a close sends one lead notification only. When the poster confirms or
    rejects their own proposal, the buyer is the one told.
    """
    if actor == "buyer":
        recipient_id, audience = visit.owner_user_id, "OWNER"
    else:
        recipient_id, audience = visit.buyer_user_id, "BUYER"
    notification_svc.notify_safely(
        db, f"VISIT_{visit.status}", audience=audience,
        recipient_id=recipient_id, actor_id=actor_id, entity_id=visit.id,
        listing_id=visit.property_listing_id, when=visit.scheduled_at,
        dedupe_key=f"visit:{visit.id}:{visit.status}",
    )
