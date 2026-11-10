"""RESTAMP Buyer V1 — discovery/detail/similar + contact + enquiries + saved (6B-1..6),
visits, viewed history and call-button events."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from .. import schemas
from ..db import get_db
from ..deps import require_buyer, require_buyer_or_agent
from ..services import listings as listing_svc
from ..services import call_events as call_svc
from ..services import buyer_plans as plan_svc
from ..services import contact as contact_svc
from ..services import enquiries as enquiry_svc
from ..services import saved as saved_svc
from ..services import visits as visit_svc
from ..services import viewed as viewed_svc
from ..services import users_roles
from ..services import agent_plans as agent_plan_svc

router = APIRouter(prefix="/buyer", tags=["buyer"])


def _agent_area_gate(db: Session, user_id: int, listing_id: int) -> str | None:
    """Agents (BROKER) may act only on listings inside their plan's areas
    (free trial: one area) -> 402 AGENT_AREA_NOT_IN_PLAN otherwise. Buyers
    are never area-restricted. Returns the caller's current role."""
    role = users_roles.get_current_role(db, user_id)
    if role == "BROKER":
        agent_plan_svc.ensure_listing_in_plan(db, user_id, listing_id)
    return role

Deal = Literal["BUY", "RESALE", "RENT", "LEASE"]
PropType = Literal["APARTMENT", "VILLA", "HOUSE", "COMMERCIAL", "PLOT", "AGRICULTURAL_LAND"]
ConstructionStatus = Literal["READY_TO_MOVE", "UNDER_CONSTRUCTION", "NEW_LAUNCH"]
Sort = Literal["relevance", "price_asc", "price_desc", "area_asc", "area_desc"]


@router.get("/listings", response_model=schemas.ListingsResponse)
def list_listings(
    db: Session = Depends(get_db),
    deal: Deal | None = Query(default=None),
    property_type: PropType | None = Query(default=None),
    locality: str | None = Query(default=None, max_length=255),
    city: str | None = Query(default=None, max_length=255),
    bedrooms: int | None = Query(default=None, ge=0, le=20),
    bedrooms_min: int | None = Query(default=None, ge=0, le=20),
    min_price_paise: int | None = Query(default=None, ge=0),
    max_price_paise: int | None = Query(default=None, ge=0),
    q: str | None = Query(default=None, max_length=255),
    construction_status: ConstructionStatus | None = Query(default=None),
    sort: Sort = Query(default="relevance"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    count_only: bool = Query(default=False),
):
    if min_price_paise is not None and max_price_paise is not None and min_price_paise > max_price_paise:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid request")
    cards, total = listing_svc.search_listings(
        db,
        page=page,
        page_size=0 if count_only else page_size,
        deal=deal,
        property_type=property_type,
        locality=locality,
        city=city,
        bedrooms=bedrooms,
        bedrooms_min=bedrooms_min,
        min_price_paise=min_price_paise,
        max_price_paise=max_price_paise,
        query=q,
        construction_status=construction_status,
        sort=sort,
    )
    if count_only:
        return schemas.ListingsResponse(items=[], page=page, page_size=page_size, total=total)
    covers = listing_svc.fetch_cover_map(db, [c["listing_id"] for c in cards])
    return schemas.ListingsResponse(
        items=listing_svc.attach_covers(cards, covers),
        page=page,
        page_size=page_size,
        total=total,
    )


@router.get("/listings/{listing_id}", response_model=schemas.ListingDetail)
def get_listing(listing_id: int, db: Session = Depends(get_db)):
    detail = listing_svc.get_listing_detail(db, listing_id)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    covers = listing_svc.fetch_cover_map(db, [listing_id])
    detail["cover_image_url"] = covers.get(listing_id)
    return detail


@router.get("/listings/{listing_id}/similar", response_model=list[schemas.ListingCard])
def get_similar(
    listing_id: int,
    db: Session = Depends(get_db),
    limit: int = Query(default=5, ge=1, le=5),
):
    cards = listing_svc.find_similar(db, listing_id, limit=limit)
    if cards is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return cards


@router.post("/listings/{listing_id}/contact", response_model=schemas.ListingContactRevealOut)
def reveal_listing_contact(
    listing_id: int,
    db: Session = Depends(get_db),
    buyer_id: int = Depends(require_buyer_or_agent),
):
    # Buyer-plan lead quota: a NEW property beyond the plan's limit is
    # refused with 402 BUYER_PLAN_REQUIRED before anything is revealed.
    # The 402 carries only the last 4 digits of the owner's number (teaser);
    # an invisible/uncontactable listing still answers 404 first.
    owner_id, owner_phone = contact_svc.find_owner(db, listing_id)
    role = _agent_area_gate(db, buyer_id, listing_id)
    # The owner's public-safe name (same rule as the public listing detail:
    # never phone-based) so the contact sheet names the real owner.
    owner_name = listing_svc._safe_owner_name(db, owner_id)
    if owner_id == buyer_id:
        # Owner (switched to buyer mode) viewing their OWN listing: it's their
        # own number — no lead, no audit, no quota (create_enquiry would 422).
        return schemas.ListingContactRevealOut(phone=owner_phone, owner_name=owner_name, own_listing=True)
    if role != "BROKER":
        # Buyers: 3 free owner numbers, then a buyer plan. Agents have no
        # reveal limit; their limit is the areas in their plan (gate above).
        plan_svc.ensure_can_reveal(db, buyer_id, listing_id, phone_last4=owner_phone[-4:])
    phone, _audit = contact_svc.reveal_contact(db, buyer_id, listing_id)
    # P3 leads MVP: a successful reveal guarantees exactly one ACTIVE lead
    # (NEW enquiry) for this buyer+listing. Reuses the existing row on
    # repeats (idempotent 200 path), so unique lead counts never inflate
    # while every reveal still keeps its own audit record above.
    # The lead keeps the revealer's role, so an agent's reveal is an Agent lead.
    enquiry_svc.create_enquiry(db, buyer_id, listing_id, None, source_role=role or "BUYER")
    db.commit()
    return schemas.ListingContactRevealOut(phone=phone, owner_name=owner_name)


@router.get("/plans")
def list_buyer_plans(db: Session = Depends(get_db)):
    """Public catalogue of RESTAMP buyer plans (lead quotas, price, validity)."""
    return {"items": plan_svc.catalogue(db)}


@router.get("/plan")
def get_buyer_plan(
    db: Session = Depends(get_db),
    buyer_id: int = Depends(require_buyer),
):
    """The buyer's current plan and lead usage in its window."""
    return plan_svc.status_for(db, buyer_id)


@router.post("/plans/{code}/subscribe")
def subscribe_buyer_plan(
    code: str,
    db: Session = Depends(get_db),
    buyer_id: int = Depends(require_buyer),
):
    """Activate a paid buyer plan. PAYMENT IS SIMULATED — no money is charged."""
    result = plan_svc.subscribe_simulated(db, buyer_id, code)
    db.commit()
    return result


@router.post("/enquiries", response_model=schemas.EnquiryOut, status_code=201)
def create_buyer_enquiry(
    body: schemas.EnquiryCreate,
    db: Session = Depends(get_db),
    buyer_id: int = Depends(require_buyer_or_agent),
):
    if body.property_listing_id <= 0:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid request")
    # The sender's role (BUYER or BROKER) is recorded on a new lead so the
    # owner can see whether a buyer or an agent sent it.
    role = _agent_area_gate(db, buyer_id, body.property_listing_id)
    enquiry, created = enquiry_svc.create_enquiry(
        db, buyer_id, body.property_listing_id, body.message, source_role=role or "BUYER"
    )
    db.commit()
    if not created:
        # Existing active enquiry: any new distinct message was appended to
        # its thread (D1); 200 instead of 201 because no new lead was made.
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            # mode="json": created_at is a datetime; serialize it exactly as
            # the 201 path's response_model does (explicit UTC string).
            content=schemas.EnquiryOut(**enquiry).model_dump(mode="json"),
        )
    return enquiry


@router.get("/enquiries", response_model=schemas.EnquiriesResponse)
def list_buyer_enquiries(
    db: Session = Depends(get_db),
    buyer_id: int = Depends(require_buyer_or_agent),
    status: Literal["NEW", "CONTACTED", "CLOSED"] | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    items, total = enquiry_svc.list_enquiries(db, buyer_id, page, page_size, status)
    return schemas.EnquiriesResponse(items=items, page=page, page_size=page_size, total=total)


@router.get("/enquiries/{enquiry_id}", response_model=schemas.EnquiryOut)
def get_buyer_enquiry(
    enquiry_id: int,
    db: Session = Depends(get_db),
    buyer_id: int = Depends(require_buyer_or_agent),
):
    return enquiry_svc.get_enquiry(db, buyer_id, enquiry_id)


@router.post("/saved/{listing_id}", response_model=schemas.ListingCard, status_code=201)
def save_buyer_listing(
    listing_id: int,
    db: Session = Depends(get_db),
    buyer_id: int = Depends(require_buyer_or_agent),
):
    _agent_area_gate(db, buyer_id, listing_id)
    card, created = saved_svc.save_listing(db, buyer_id, listing_id)
    db.commit()
    if not created:
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=schemas.ListingCard(**card).model_dump(),
        )
    return card


@router.get("/saved", response_model=schemas.ListingsResponse)
def list_buyer_saved(
    db: Session = Depends(get_db),
    buyer_id: int = Depends(require_buyer_or_agent),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    items, total = saved_svc.list_saved(db, buyer_id, page, page_size)
    return schemas.ListingsResponse(items=items, page=page, page_size=page_size, total=total)


@router.delete("/saved/{listing_id}", status_code=204)
def unsave_buyer_listing(
    listing_id: int,
    db: Session = Depends(get_db),
    buyer_id: int = Depends(require_buyer_or_agent),
):
    if saved_svc.unsave_listing(db, buyer_id, listing_id):
        db.commit()
    return None


@router.post("/visits", response_model=schemas.VisitOut, status_code=201)
def schedule_buyer_visit(
    body: schemas.VisitCreate,
    db: Session = Depends(get_db),
    buyer_id: int = Depends(require_buyer_or_agent),
):
    """Schedule a visit on a verified, available listing (lands REQUESTED).

    The scheduled time must be in the future. Returns 404 for invisible
    listings or diverged ownership, 422 for past times, self-visits, or
    when the owner has closed the buyer's lead on it ("Lead is closed" —
    a new enquiry opens a fresh lead).
    """
    if body.property_listing_id <= 0:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid request")
    role = _agent_area_gate(db, buyer_id, body.property_listing_id)
    try:
        # The requester's role lets owners tell agent visits apart.
        visit = visit_svc.create_buyer_visit(
            db, buyer_id, body.property_listing_id, body.scheduled_at, body.note,
            source_role=role or "BUYER",
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    db.commit()
    return visit


@router.get("/visits", response_model=schemas.VisitsResponse)
def list_buyer_visits(
    db: Session = Depends(get_db),
    buyer_id: int = Depends(require_buyer_or_agent),
    status: Literal["REQUESTED", "PROPOSED", "CONFIRMED", "DECLINED", "CANCELLED", "COMPLETED"] | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """List the caller's own visits, newest first."""
    items, total = visit_svc.list_buyer_visits(db, buyer_id, page, page_size, status)
    return schemas.VisitsResponse(items=items, page=page, page_size=page_size, total=total)


@router.get("/visits/{visit_id}", response_model=schemas.VisitOut)
def get_buyer_visit(
    visit_id: int,
    db: Session = Depends(get_db),
    buyer_id: int = Depends(require_buyer_or_agent),
):
    """One own visit; 404 when missing or owned by someone else."""
    return visit_svc.get_buyer_visit(db, buyer_id, visit_id)


@router.patch("/visits/{visit_id}", response_model=schemas.VisitOut)
def update_buyer_visit(
    visit_id: int,
    body: schemas.VisitUpdate,
    db: Session = Depends(get_db),
    buyer_id: int = Depends(require_buyer_or_agent),
):
    """Reschedule and/or confirm/decline/cancel own visit per the state
    rules (buyers confirm/decline owner-proposed visits, reschedule
    requested/confirmed times, cancel open visits). 404 when not owned,
    422 for invalid moves, past times, or a visit whose lead the owner has
    closed ("Lead is closed")."""
    sp = db.begin_nested()
    try:
        visit = visit_svc.transition_visit(
            db, "buyer", buyer_id, visit_id, body.scheduled_at, body.status, body.note
        )
    except HTTPException as e:
        sp.rollback()
        raise e
    except ValueError as e:
        sp.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    sp.commit()
    db.commit()
    return visit


@router.post("/viewed/{listing_id}", response_model=schemas.ViewedPropertyOut, status_code=201)
def record_viewed(
    listing_id: int,
    db: Session = Depends(get_db),
    buyer_id: int = Depends(require_buyer_or_agent),
):
    """Record that the caller viewed a property listing detail.

    Idempotent: repeats update viewed_at instead of duplicating rows.
    Enforces the 20-per-buyer retention rule. 404 if the listing does not
    exist or is not visible to buyers.
    """
    from ..models import PropertyListing, PhysicalProperty

    visible = (
        db.query(PropertyListing)
        .join(PhysicalProperty, PhysicalProperty.id == PropertyListing.physical_property_id)
        .filter(
            PropertyListing.id == listing_id,
            PropertyListing.verification_status == "VERIFIED",
            PropertyListing.listing_status == "AVAILABLE",
            PropertyListing.deleted_at.is_(None),
            PhysicalProperty.user_id == PropertyListing.user_id,
        )
        .first()
    )
    if visible is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    _agent_area_gate(db, buyer_id, listing_id)

    vp = viewed_svc.record_view(db, buyer_id, listing_id)
    db.commit()
    return vp


@router.get("/viewed", response_model=schemas.ViewedPropertiesResponse)
def list_buyer_viewed(
    db: Session = Depends(get_db),
    buyer_id: int = Depends(require_buyer_or_agent),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """List the caller's most recent viewed properties (max 20 per buyer)."""
    items, total = viewed_svc.list_viewed(db, buyer_id, page, page_size)
    return schemas.ViewedPropertiesResponse(items=items, page=page, page_size=page_size, total=total)


# --- Call-button events (CONTRACT §B) -------------------------------------------


@router.post("/listings/{listing_id}/call-events", response_model=schemas.CallEventOut, status_code=201)
def record_call_event(
    listing_id: int,
    db: Session = Depends(get_db),
    buyer_id: int = Depends(require_buyer_or_agent),
):
    """Record that the caller TAPPED a Call button for this listing (not that
    a call happened). 404 unless the listing is publicly visible. A repeat
    within 60 s returns the existing event with 200 (no new row)."""
    # Agents tap the same button; the tapper's role lets owners tell agent calls apart.
    role = _agent_area_gate(db, buyer_id, listing_id)
    event, created = call_svc.record_call_tap(db, buyer_id, listing_id, source_role=role or "BUYER")
    db.commit()
    if not created:
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=schemas.CallEventOut(**event).model_dump(mode="json"),
        )
    return event


@router.get("/call-events", response_model=schemas.CallEventsResponse)
def list_call_events(
    db: Session = Depends(get_db),
    buyer_id: int = Depends(require_buyer_or_agent),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """The caller's own call-button taps, newest first (listing card or null)."""
    items, total = call_svc.list_call_events(db, buyer_id, page, page_size)
    return schemas.CallEventsResponse(items=items, page=page, page_size=page_size, total=total)
