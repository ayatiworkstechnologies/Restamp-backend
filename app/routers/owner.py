"""RESTAMP Owner V1 — property listing management routes.

Endpoints:
  POST /owner/listings       — create a new property listing (OWNER role required)
  GET  /owner/listings       — list caller's own listings (all statuses)
  GET  /owner/listings/{id}  — single owner listing detail
  POST /owner/listings/{id}/photos — upload a photo for own listing (multipart)

No admin endpoints here; see users.py for admin grants.
No schema changes; all writes use existing property_listings / physical_properties tables.
"""
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy.orm import Session

from .. import schemas
from ..db import get_db
from ..deps import require_owner
from ..services import owner_listings as owner_svc
from ..services import photo_storage
from ..services import storage as storage_svc

router = APIRouter(prefix="/owner", tags=["owner"])


@router.post("/listings", response_model=schemas.OwnerListingOut, status_code=201)
def create_listing(
    body: schemas.OwnerListingCreate,
    db: Session = Depends(get_db),
    owner_id: int = Depends(require_owner),
):
    """Create a new property listing. Lands in PENDING / AVAILABLE state."""
    price_paise = body.price_rupees * 100
    try:
        listing = owner_svc.create_owner_listing(
            db=db,
            owner_id=owner_id,
            title=body.title,
            transaction_type=body.transaction_type,
            property_type=body.property_type,
            price_paise=price_paise,
            price_period=body.price_period,
            locality=body.locality,
            city=body.city,
            district=body.district,
            pincode=body.pincode,
            address_line=body.address_line,
            sub_locality=body.sub_locality,
            society_name=body.society_name,
            house_no=body.house_no,
            landmark=body.landmark,
            latitude=body.latitude,
            longitude=body.longitude,
            area_value=body.area_value,
            area_unit=body.area_unit,
            bedrooms=body.bedrooms,
            bathrooms=body.bathrooms,
            balconies=body.balconies,
            built_up_area=body.built_up_area,
            super_built_up_area=body.super_built_up_area,
            total_floors=body.total_floors,
            floor_on=body.floor_on,
            is_duplex=body.is_duplex,
            property_age_band=body.property_age_band,
            furnishing=body.furnishing,
            covered_parking=body.covered_parking,
            open_parking=body.open_parking,
            open_sides=body.open_sides,
            overlooking=body.overlooking,
            power_backup=body.power_backup,
            facing=body.facing,
            ownership_type=body.ownership_type,
            contact_phone=body.contact_phone,
            contact_email=body.contact_email,
            description=body.description,
            construction_status=body.construction_status,
            image_urls=body.images,
            rent_terms=body.rent_terms.model_dump() if body.rent_terms else None,
            amenities=body.amenities,
            property_features=body.property_features,
            other_rooms=body.other_rooms,
            photos=[p.model_dump() for p in body.photos] if body.photos else None,
        )
    except ValueError as e:
        # Service-level validation (unknown amenity, unresolvable district,
        # bad media): nothing is committed — router commits only on success.
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    db.commit()
    db.refresh(listing)

    # Build the response dict using the same reader
    card = owner_svc.get_owner_listing(db, owner_id, listing.id)
    if card is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal error")
    return card


@router.get("/listings", response_model=schemas.OwnerListingsResponse)
def list_my_listings(
    db: Session = Depends(get_db),
    owner_id: int = Depends(require_owner),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """List all listings posted by the authenticated owner."""
    items, total = owner_svc.list_owner_listings(db, owner_id, page, page_size)
    return schemas.OwnerListingsResponse(items=items, page=page, page_size=page_size, total=total)


@router.get("/listings/{listing_id}", response_model=schemas.OwnerListingOut)
def get_my_listing(
    listing_id: int,
    db: Session = Depends(get_db),
    owner_id: int = Depends(require_owner),
):
    """Get a single listing detail; returns 404 if not owned by caller."""
    card = owner_svc.get_owner_listing(db, owner_id, listing_id)
    if card is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return card


@router.post("/listings/{listing_id}/photos", response_model=schemas.PhotoUploadOut, status_code=201)
async def upload_listing_photo(
    listing_id: int,
    file: UploadFile = File(...),
    category: str = Form("Living Room"),
    db: Session = Depends(get_db),
    owner_id: int = Depends(require_owner),
):
    """Upload one photo file for the caller's own listing (multipart/form-data).

    Fields: file (image bytes), category (one of the 7 photo categories).
    The listing must belong to the authenticated owner (else 404, same as the
    detail endpoint). First photo on a listing becomes cover (order_index 0);
    later uploads append without touching the existing cover.
    """
    try:
        canonical_category = schemas.normalize_media_category(category)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    if canonical_category is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="category is required")
    try:
        extension = storage_svc.validate_image_upload(file.content_type, file.filename)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))

    # Stream with a hard cap (MIME validated above; never trust the filename).
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(storage_svc.MAX_IMAGE_BYTES // 10 or 65536)
        if not chunk:
            break
        total += len(chunk)
        if total > storage_svc.MAX_IMAGE_BYTES:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail="Image exceeds 10 MB")
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Empty file")

    mime = file.content_type.split(";")[0].strip().lower()
    try:
        secure_url, public_id, byte_size = photo_storage.upload_listing_image(
            listing_id, data, extension, mime
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    except OSError:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Upload failed")

    try:
        media = owner_svc.add_listing_photo(
            db, owner_id, listing_id,
            url=secure_url,
            category=canonical_category, mime_type=mime, byte_size=byte_size,
        )
        if media is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        db.commit()
        db.refresh(media)
    except HTTPException:
        photo_storage.delete_uploaded_image(public_id)
        db.rollback()
        raise
    except Exception:
        # DB failure after the upload landed: destroy the orphan, persist nothing.
        photo_storage.delete_uploaded_image(public_id)
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Upload failed")

    return schemas.PhotoUploadOut(
        id=media.id,
        url=media.url,
        category=media.category,
        mime_type=media.mime_type,
        byte_size=media.byte_size,
        order_index=media.order_index,
        is_cover=media.order_index == 0,
    )


# ---------------------------------------------------------------------------
# Persistent drafts (owner-scoped JSON snapshots; see owner_listings service)
# ---------------------------------------------------------------------------


@router.post("/drafts", response_model=schemas.DraftOut, status_code=201)
def create_draft(
    body: schemas.DraftCreate,
    db: Session = Depends(get_db),
    owner_id: int = Depends(require_owner),
):
    """Save a new draft (always creates; repeat saves update via PUT)."""
    card = owner_svc.create_draft(
        db,
        owner_id,
        transaction_type=body.transaction_type,
        title=body.title,
        current_step=body.current_step,
        form_data=body.form_data,
    )
    db.commit()
    return card


@router.get("/drafts", response_model=schemas.DraftListOut)
def list_drafts(
    db: Session = Depends(get_db),
    owner_id: int = Depends(require_owner),
):
    """List the caller's drafts, most recently updated first."""
    items, total = owner_svc.list_drafts(db, owner_id)
    return schemas.DraftListOut(items=items, total=total)


@router.get("/drafts/{draft_id}", response_model=schemas.DraftOut)
def get_draft(
    draft_id: int,
    db: Session = Depends(get_db),
    owner_id: int = Depends(require_owner),
):
    """Get one draft; 404 if missing or owned by someone else."""
    card = owner_svc.get_draft(db, owner_id, draft_id)
    if card is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return card


@router.put("/drafts/{draft_id}", response_model=schemas.DraftOut)
def update_draft(
    draft_id: int,
    body: schemas.DraftUpdate,
    db: Session = Depends(get_db),
    owner_id: int = Depends(require_owner),
):
    """Replace a draft's snapshot; 404 if missing or owned by someone else."""
    card = owner_svc.update_draft(
        db,
        owner_id,
        draft_id,
        transaction_type=body.transaction_type,
        title=body.title,
        current_step=body.current_step,
        form_data=body.form_data,
    )
    if card is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    db.commit()
    return card


@router.delete("/drafts/{draft_id}", status_code=204)
def delete_draft(
    draft_id: int,
    db: Session = Depends(get_db),
    owner_id: int = Depends(require_owner),
):
    """Discard a draft; 404 if missing or owned by someone else."""
    if not owner_svc.delete_draft(db, owner_id, draft_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    db.commit()
    return None
