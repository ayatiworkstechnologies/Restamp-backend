"""RESTAMP Agent (role BROKER) foundation routes.

Endpoints (all require an authenticated, active account whose CURRENT role is
BROKER — require_broker reads the role from the DB, never the token):
  GET /broker/profile    — the caller's agent profile
  GET /broker/dashboard  — real counts; unsupported metrics null + listed
  GET /broker/locations  — the caller's covered postcodes (unexpired grants)
  GET /broker/listings   — buyer-visible listings inside the caller's coverage

Read-only by design: no endpoint creates or changes coverage, subscriptions,
leads or listings. Coverage grants come from broker_postcode_access_current
only (see services/brokers.py); an agent without grants sees empty results.
Property detail stays on the public GET /buyer/listings/{id}. GETs never
write. GET /broker/scope (users.py) is unchanged.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from .. import schemas
from ..db import get_db
from ..deps import require_broker
from ..services import brokers as broker_svc
from ..services import listings as listing_svc
from .buyer import ConstructionStatus, Deal, PropType, Sort

router = APIRouter(prefix="/broker", tags=["broker"])


@router.get("/profile", response_model=schemas.BrokerProfileOut)
def get_broker_profile(db: Session = Depends(get_db), broker_id: int = Depends(require_broker)):
    """The caller's agent profile. Verification and subscription have no
    backend source yet and are listed in ``unavailable``."""
    return broker_svc.get_profile(db, broker_id)


@router.get("/dashboard", response_model=schemas.BrokerDashboardOut)
def get_broker_dashboard(db: Session = Depends(get_db), broker_id: int = Depends(require_broker)):
    """Available-property and covered-location counts from real data; coverage
    limit, subscription, new leads and earnings are null (``unavailable``)."""
    return broker_svc.get_dashboard(db, broker_id)


@router.get("/locations", response_model=schemas.BrokerLocationsResponse)
def list_broker_locations(db: Session = Depends(get_db), broker_id: int = Depends(require_broker)):
    """The caller's own unexpired postcode grants, ordered by pincode."""
    items = broker_svc.list_covered_locations(db, broker_id)
    return schemas.BrokerLocationsResponse(items=items, total=len(items))


@router.get("/listings", response_model=schemas.ListingsResponse)
def list_broker_listings(
    db: Session = Depends(get_db),
    broker_id: int = Depends(require_broker),
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
    """Same filters, sorting, paging and visibility as GET /buyer/listings,
    restricted to the caller's covered postcodes (none covered -> empty)."""
    if min_price_paise is not None and max_price_paise is not None and min_price_paise > max_price_paise:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid request")
    cards, total = listing_svc.search_listings(
        db,
        page=page,
        page_size=0 if count_only else page_size,
        postcode_ids=broker_svc.active_postcode_ids(db, broker_id),
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
