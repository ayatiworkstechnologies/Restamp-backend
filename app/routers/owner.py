"""RESTAMP Owner V1 — property listing management routes.

Endpoints:
  POST /owner/listings       — create a new property listing (OWNER role required)
  GET  /owner/listings       — list caller's own listings (all statuses; pure read)
  GET  /owner/listings/{id}  — single owner listing detail (pure read)
  PATCH /owner/listings/{id} — update own listing in place (OWNER, partial;
                               never changes verification/listing status)
  POST /owner/listings/{id}/submit  — resubmit REJECTED -> PENDING for re-review
  POST /owner/listings/{id}/verify  — explicit PENDING -> VERIFIED
  POST /owner/listings/{id}/reject  — explicit PENDING -> REJECTED (optional reason)
  POST /owner/listings/{id}/publish — publish CLOSED -> AVAILABLE (VERIFIED only)
  POST /owner/listings/{id}/archive — archive AVAILABLE -> CLOSED
  POST /owner/listings/{id}/status  — validated AVAILABLE/CLOSED/SOLD/RENTED/LEASED
  POST /owner/listings/{id}/photos — upload a photo for own listing (multipart)
  DELETE /owner/listings/{id}/photos/{media_id} — remove one photo (OWNER)
  DELETE /owner/listings/{id}         — permanently delete own listing (OWNER)

No admin endpoints here; see users.py for admin grants.
No schema changes; all writes use existing property_listings / physical_properties tables.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from sqlalchemy.orm import Session
from typing import Literal

from .. import schemas
from ..config import settings
from ..db import get_db
from ..deps import require_owner
from ..services import owner_listings as owner_svc
from ..services import enquiries as enquiry_svc
from ..services import visits as visit_svc
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


# ---------------------------------------------------------------------------
# Owner leads MVP (P3): read-only buyer-enquiry projection for the
# authenticated owner. No new tables or columns — enquiries, active_enquiries,
# and contact_reveal_audits already persist everything this returns.
# ---------------------------------------------------------------------------


@router.get("/leads", response_model=schemas.OwnerLeadsResponse)
def list_my_leads(
    db: Session = Depends(get_db),
    owner_id: int = Depends(require_owner),
    property_listing_id: int | None = Query(default=None, ge=1),
    status: Literal["NEW", "CONTACTED", "CLOSED"] | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """List buyer enquiries for the caller's own properties (newest first).

    Only rows with owner_user_id == caller are ever returned. An optional
    property filter is validated against the caller's owned properties: a
    foreign or missing id raises 404 (never an empty list). Only the
    existing backend statuses NEW/CONTACTED/CLOSED are accepted. Buyer
    phone numbers are never included (see OwnerLeadOut).
    """
    items, total = enquiry_svc.list_owner_leads(
        db, owner_id, page, page_size, property_listing_id, status
    )
    return schemas.OwnerLeadsResponse(items=items, page=page, page_size=page_size, total=total)


@router.patch("/leads/{lead_id}", response_model=schemas.OwnerLeadOut)
def update_my_lead(
    lead_id: int,
    body: schemas.OwnerLeadUpdate,
    db: Session = Depends(get_db),
    owner_id: int = Depends(require_owner),
):
    """Persist an owner-side lead status change (P4).

    Only NEW/CONTACTED/CLOSED are accepted (anything else is 422 via the
    schema). Returns 404 when the lead does not belong to the caller.
    CLOSING a lead stores the supplied closing_reason (e.g. "Not Converted"),
    frees the buyer's active-enquiry row so re-enquiry is possible, and never
    modifies the property listing. Phone-free OwnerLeadOut response.
    """
    # Savepoint (not a full rollback): same shared-test-session reason as the
    # listing PATCH above — only this request's work is discarded on failure.
    sp = db.begin_nested()
    try:
        card = enquiry_svc.transition_owner_lead(
            db, owner_id, lead_id, body.status, body.closing_reason
        )
    except HTTPException as e:
        sp.rollback()
        raise e
    except ValueError as e:
        sp.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    sp.commit()
    db.commit()
    return card


@router.post("/listings/{listing_id}/photos", response_model=schemas.PhotoUploadOut, status_code=201)
async def upload_listing_photo(
    listing_id: int,
    request: Request,
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

    # Ownership before external side effects: a foreign listing_id fails
    # with 404 before any Cloudinary upload is spent or stored.
    if not owner_svc.owner_owns_listing(db, owner_id, listing_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

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
    is_local = not bool(settings.CLOUDINARY_CLOUD_NAME)
    try:
        if is_local:
            storage_name, byte_size = storage_svc.save_listing_image(listing_id, data, extension)
            base_url = str(request.base_url).rstrip("/")
            secure_url = f"{base_url}{settings.MEDIA_URL_PREFIX}/{storage_name}"
            public_id = f"local:{storage_name}"
        else:
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
            public_id=public_id,
        )
        if media is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        db.commit()
        db.refresh(media)
    except HTTPException:
        if is_local:
            storage_svc.delete_stored_file(public_id.split("local:", 1)[1])
        else:
            photo_storage.delete_uploaded_image(public_id)
        db.rollback()
        raise
    except Exception:
        # DB failure after the upload landed: destroy the orphan, persist nothing.
        if is_local:
            storage_svc.delete_stored_file(public_id.split("local:", 1)[1])
        else:
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


@router.patch("/listings/{listing_id}", response_model=schemas.OwnerListingOut)
def update_listing(
    listing_id: int,
    body: schemas.OwnerListingUpdate,
    db: Session = Depends(get_db),
    owner_id: int = Depends(require_owner),
):
    """Update the caller's own listing in place (partial PATCH).

    Only keys present in the request are touched; omitted keys keep their
    stored values and ``0``/``False`` are preserved. The listing and
    physical-property IDs never change — no duplicate rows are created.
    Returns 404 when the listing does not belong to the caller, 422 on
    validation failures (nothing committed). Never falls back to creating
    a listing.
    """
    updates = body.model_dump(exclude_unset=True)
    if updates.get("rent_terms") is not None and "rent_terms" in updates:
        # Nested exclude_unset already applied recursively; normalize to a
        # plain dict of supplied sub-keys for the service merge.
        updates["rent_terms"] = dict(updates["rent_terms"])
    # Savepoint (not a full rollback): the shared test session reuses one
    # transaction per test, so a full rollback would wipe the test's own
    # setup rows. Rolling back only this request's work keeps setup intact
    # while still guaranteeing nothing partial commits. In production
    # (request-scoped sessions) this behaves like a normal rollback.
    sp = db.begin_nested()
    try:
        listing, removed_public_ids = owner_svc.update_owner_listing(
            db, owner_id, listing_id, updates
        )
        if listing is None:
            sp.rollback()
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        sp.commit()
        db.commit()
    except HTTPException:
        raise
    except ValueError as e:
        sp.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    # Best-effort Cloudinary cleanup for implicitly removed gallery rows:
    # the DB commit already succeeded, so a storage failure must never fail
    # the request — at worst an orphaned asset remains (documented).
    for public_id in removed_public_ids:
        try:
            photo_storage.delete_uploaded_image(public_id)
        except Exception:
            pass
    card = owner_svc.get_owner_listing(db, owner_id, listing.id)
    if card is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal error")
    return card


# ---------------------------------------------------------------------------
# Explicit lifecycle transitions (no automatic verification; GETs never write)
# ---------------------------------------------------------------------------


def _transition_card_or_500(db, owner_id: int, listing_id: int) -> schemas.OwnerListingOut:
    card = owner_svc.get_owner_listing(db, owner_id, listing_id)
    if card is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal error")
    return card


def _run_verification_transition(
    db, owner_id: int, listing_id: int, to_status: str, reason: str | None = None
):
    sp = db.begin_nested()
    try:
        listing = owner_svc.transition_verification(db, owner_id, listing_id, to_status, reason)
        if listing is None:
            sp.rollback()
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        sp.commit()
        db.commit()
    except HTTPException:
        raise
    except ValueError as e:
        sp.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    return _transition_card_or_500(db, owner_id, listing.id)


def _run_status_transition(
    db, owner_id: int, listing_id: int, to_status: str, closed_reason: str | None = None
):
    sp = db.begin_nested()
    try:
        listing = owner_svc.transition_listing_status(
            db, owner_id, listing_id, to_status, closed_reason
        )
        if listing is None:
            sp.rollback()
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        sp.commit()
        db.commit()
    except HTTPException:
        raise
    except ValueError as e:
        sp.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    return _transition_card_or_500(db, owner_id, listing.id)


@router.post("/listings/{listing_id}/submit", response_model=schemas.OwnerListingOut)
def submit_listing(
    listing_id: int,
    db: Session = Depends(get_db),
    owner_id: int = Depends(require_owner),
):
    """Resubmit a REJECTED listing as PENDING for re-review (refreshes submitted_at).

    Idempotent when already PENDING. Returns 422 when VERIFIED (nothing to
    resubmit). Returns 404 when the listing does not belong to the caller.
    """
    return _run_verification_transition(db, owner_id, listing_id, "PENDING")


@router.post("/listings/{listing_id}/verify", response_model=schemas.OwnerListingOut)
def verify_listing(
    listing_id: int,
    db: Session = Depends(get_db),
    owner_id: int = Depends(require_owner),
):
    """Explicitly verify a PENDING listing (PENDING -> VERIFIED, audited).

    Idempotent when already VERIFIED. Returns 422 from REJECTED (submit
    first) or other invalid states. Returns 404 when not owned by caller.
    Owner-driven (self-serve preserved); a future admin-moderation step can
    gate this route without changing the transition validation.
    """
    return _run_verification_transition(db, owner_id, listing_id, "VERIFIED")


@router.post("/listings/{listing_id}/reject", response_model=schemas.OwnerListingOut)
def reject_listing(
    listing_id: int,
    body: schemas.VerificationRejectIn | None = None,
    db: Session = Depends(get_db),
    owner_id: int = Depends(require_owner),
):
    """Explicitly reject a PENDING listing (PENDING -> REJECTED, audited).

    Accepts an optional ``{"reason": ...}`` stored in the verification audit
    row. Idempotent when already REJECTED. Returns 422 when VERIFIED.
    Returns 404 when the listing does not belong to the caller.
    """
    reason = body.reason if body is not None else None
    return _run_verification_transition(db, owner_id, listing_id, "REJECTED", reason)


@router.post("/listings/{listing_id}/publish", response_model=schemas.OwnerListingOut)
def publish_listing(
    listing_id: int,
    db: Session = Depends(get_db),
    owner_id: int = Depends(require_owner),
):
    """Publish an archived listing (CLOSED -> AVAILABLE). VERIFIED only.

    Idempotent when already AVAILABLE. Returns 422 when the listing is not
    VERIFIED or is in a terminal deal state (SOLD/RENTED/LEASED/EXPIRED).
    Returns 404 when the listing does not belong to the caller.
    """
    return _run_status_transition(db, owner_id, listing_id, "AVAILABLE")


@router.post("/listings/{listing_id}/archive", response_model=schemas.OwnerListingOut)
def archive_listing(
    listing_id: int,
    db: Session = Depends(get_db),
    owner_id: int = Depends(require_owner),
):
    """Archive a live listing (AVAILABLE -> CLOSED, reversible via publish).

    Idempotent when already CLOSED. Terminal deal states cannot be archived
    (422). Returns 404 when the listing does not belong to the caller.
    """
    return _run_status_transition(db, owner_id, listing_id, "CLOSED")


@router.post("/listings/{listing_id}/status", response_model=schemas.OwnerListingOut)
def update_listing_status(
    listing_id: int,
    body: schemas.ListingStatusUpdate,
    db: Session = Depends(get_db),
    owner_id: int = Depends(require_owner),
):
    """Validated listing-status transition (general form of publish/archive).

    AVAILABLE -> CLOSED/SOLD/RENTED/LEASED, CLOSED -> AVAILABLE (VERIFIED
    only for AVAILABLE/SOLD/RENTED/LEASED targets). SOLD/RENTED/LEASED are
    terminal. Same-state repeats are idempotent. Returns 404 when the
    listing does not belong to the caller, 422 for invalid transitions.
    """
    return _run_status_transition(db, owner_id, listing_id, body.status, body.closed_reason)


@router.delete("/listings/{listing_id}/photos/{media_id}", status_code=204)
def delete_listing_photo(
    listing_id: int,
    media_id: int,
    db: Session = Depends(get_db),
    owner_id: int = Depends(require_owner),
):
    """Remove one gallery photo from the caller's own listing.

    The media row is deleted and siblings renumbered (cover stays index 0);
    the Cloudinary asset is destroyed best-effort after the DB commit.
    Returns 404 when the listing/photo does not belong to the caller.
    """
    sp = db.begin_nested()
    try:
        found, public_id = owner_svc.delete_listing_photo(db, owner_id, listing_id, media_id)
        if not found:
            sp.rollback()
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        sp.commit()
        db.commit()
    except HTTPException:
        raise
    except Exception:
        sp.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Delete failed")
    if public_id:
        try:
            photo_storage.delete_uploaded_image(public_id)
        except Exception:
            pass
    return None


@router.delete("/listings/{listing_id}", status_code=204)
def delete_listing(
    listing_id: int,
    db: Session = Depends(get_db),
    owner_id: int = Depends(require_owner),
):
    """Permanently delete the caller's own listing.

    Enforces dual-authority ownership (PropertyListing.user_id AND
    PhysicalProperty.user_id both equal the caller). Returns 404 when the
    listing does not belong to the caller or does not exist — never a
    cross-owner leak.

    Deletes the listing row and its cascade-related records per the existing
    FK constraints (enquiries, visits, verifications, media, documents,
    rent terms, amenities, saved properties are cascade-deleted;
    contact_reveal_audits.listing_id is set to NULL, preserving the audit
    row). Cloudinary media assets are destroyed best-effort after the DB
    commit; a storage failure never fails the request.

    Returns 204 No Content on success.
    """
    sp = db.begin_nested()
    try:
        deleted, public_ids = owner_svc.delete_owner_listing(db, owner_id, listing_id)
        if not deleted:
            sp.rollback()
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        sp.commit()
        db.commit()
    except HTTPException:
        raise
    except Exception:
        sp.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Delete failed")
    # Best-effort Cloudinary cleanup after the DB commit succeeded. A storage
    # failure leaves orphaned assets but never fails the deletion request.
    for public_id in public_ids:
        try:
            photo_storage.delete_uploaded_image(public_id)
        except Exception:
            pass
    return None


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


# ---------------------------------------------------------------------------
# Property visits (owner side): propose to related buyers; confirm, decline,
# reschedule, or complete per the state rules. Dual-authority ownership and
# buyer-relationship checks are enforced in the service.
# ---------------------------------------------------------------------------


@router.post("/visits", response_model=schemas.VisitOut, status_code=201)
def propose_owner_visit(
    body: schemas.OwnerVisitCreate,
    db: Session = Depends(get_db),
    owner_id: int = Depends(require_owner),
):
    """Propose a visit to a buyer holding an enquiry or contact relationship
    on one of the caller's own verified, available listings (lands PROPOSED).

    Returns 404 for foreign listings or unknown buyers, 422 for unrelated
    buyers, past times, or self-visits.
    """
    if body.property_listing_id <= 0 or body.buyer_user_id <= 0:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid request")
    try:
        visit = visit_svc.create_owner_visit(
            db, owner_id, body.property_listing_id, body.buyer_user_id,
            body.scheduled_at, body.note,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    db.commit()
    return visit


@router.get("/visits", response_model=schemas.VisitsResponse)
def list_my_visits(
    db: Session = Depends(get_db),
    owner_id: int = Depends(require_owner),
    property_listing_id: int | None = Query(default=None, ge=1),
    status: Literal["REQUESTED", "PROPOSED", "CONFIRMED", "DECLINED", "CANCELLED", "COMPLETED"] | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """List visits for the caller's own properties, newest first. A foreign
    property filter is 404 (never an empty list)."""
    items, total = visit_svc.list_owner_visits(
        db, owner_id, page, page_size, property_listing_id, status
    )
    return schemas.VisitsResponse(items=items, page=page, page_size=page_size, total=total)


@router.get("/visits/{visit_id}", response_model=schemas.VisitOut)
def get_my_visit(
    visit_id: int,
    db: Session = Depends(get_db),
    owner_id: int = Depends(require_owner),
):
    """One visit for the caller's own properties; 404 otherwise."""
    return visit_svc.get_owner_visit(db, owner_id, visit_id)


@router.patch("/visits/{visit_id}", response_model=schemas.VisitOut)
def update_my_visit(
    visit_id: int,
    body: schemas.VisitUpdate,
    db: Session = Depends(get_db),
    owner_id: int = Depends(require_owner),
):
    """Confirm/decline buyer-requested visits, reschedule proposed/confirmed
    times, cancel open visits, or complete confirmed ones — per the state
    rules. 404 when the visit is not on the caller's property, 422 for
    invalid moves, past times, or wrong-actor transitions."""
    sp = db.begin_nested()
    try:
        visit = visit_svc.transition_visit(
            db, "owner", owner_id, visit_id, body.scheduled_at, body.status
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
