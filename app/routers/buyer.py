"""RESTAMP Buyer V1 — discovery/detail/similar + contact + enquiries + saved (6B-1..6)."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from .. import schemas
from ..db import get_db
from ..deps import require_buyer
from ..services import listings as listing_svc
from ..services import contact as contact_svc
from ..services import enquiries as enquiry_svc
from ..services import saved as saved_svc

router = APIRouter(prefix="/buyer", tags=["buyer"])

Deal = Literal["BUY", "RESALE", "RENT", "LEASE"]
PropType = Literal["APARTMENT", "VILLA", "HOUSE", "COMMERCIAL", "PLOT", "AGRICULTURAL_LAND"]
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


@router.post("/listings/{listing_id}/contact", response_model=schemas.ContactRevealOut)
def reveal_listing_contact(
    listing_id: int,
    db: Session = Depends(get_db),
    buyer_id: int = Depends(require_buyer),
):
    phone, _audit = contact_svc.reveal_contact(db, buyer_id, listing_id)
    db.commit()
    return schemas.ContactRevealOut(phone=phone)


@router.post("/enquiries", response_model=schemas.EnquiryOut, status_code=201)
def create_buyer_enquiry(
    body: schemas.EnquiryCreate,
    db: Session = Depends(get_db),
    buyer_id: int = Depends(require_buyer),
):
    if body.property_listing_id <= 0:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid request")
    enquiry, created = enquiry_svc.create_enquiry(db, buyer_id, body.property_listing_id, body.message)
    db.commit()
    if not created:
        # Idempotent duplicate: same validated body, 200 instead of 201.
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=schemas.EnquiryOut(**enquiry).model_dump(),
        )
    return enquiry


@router.get("/enquiries", response_model=schemas.EnquiriesResponse)
def list_buyer_enquiries(
    db: Session = Depends(get_db),
    buyer_id: int = Depends(require_buyer),
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
    buyer_id: int = Depends(require_buyer),
):
    return enquiry_svc.get_enquiry(db, buyer_id, enquiry_id)


@router.post("/saved/{listing_id}", response_model=schemas.ListingCard, status_code=201)
def save_buyer_listing(
    listing_id: int,
    db: Session = Depends(get_db),
    buyer_id: int = Depends(require_buyer),
):
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
    buyer_id: int = Depends(require_buyer),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    items, total = saved_svc.list_saved(db, buyer_id, page, page_size)
    return schemas.ListingsResponse(items=items, page=page, page_size=page_size, total=total)


@router.delete("/saved/{listing_id}", status_code=204)
def unsave_buyer_listing(
    listing_id: int,
    db: Session = Depends(get_db),
    buyer_id: int = Depends(require_buyer),
):
    if saved_svc.unsave_listing(db, buyer_id, listing_id):
        db.commit()
    return None
