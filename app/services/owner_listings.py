"""RESTAMP Owner V1 — listing creation and management.

An owner can create a property listing (PhysicalProperty + PropertyListing +
PropertyMedia rows) and retrieve their own listings.

Location strategy:
  1. Try to find an exact postcode match by pincode.
  2. Fall back to a case-insensitive locality name match.
  3. If neither exists, auto-create the full location chain so the submission
     never fails for a valid but unseen locality — the admin can clean/merge
     location data independently.

New listings land in PENDING/AVAILABLE state; verification is done by admin.

Automatic verification (backend-driven, no scheduler):
  A newly owner-created listing (one with a recorded submitted_at) that is
  still PENDING/AVAILABLE once AUTO_VERIFY_AFTER has elapsed since submission
  is flipped to VERIFIED the next time the owner's listings are fetched
  (list or detail read). The flip is committed to MySQL, so it survives app
  restarts, and it is idempotent: a VERIFIED row never matches again.
  Rows without submitted_at (seed data and listings created before the
  submitted_at column existed) are never auto-verified.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models.locations import City, District, Locality, Postcode, Taluk, Village
from ..models.properties import (
    AmenityMaster,
    ListingAmenity,
    ListingDraft,
    PhysicalProperty,
    PropertyListing,
    PropertyMedia,
    RentTerms,
)
from ..services.listings import format_price

# Time a newly submitted owner listing spends in PENDING before the backend
# marks it VERIFIED on the next owner-listings fetch.
AUTO_VERIFY_AFTER = timedelta(minutes=2)


def _utcnow_naive() -> datetime:
    """Current UTC as a naive datetime, matching MySQL DATETIME storage.

    submitted_at is written and compared in UTC on the Python side only, so
    no database clock/timezone is ever involved.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Location helpers
# ---------------------------------------------------------------------------


def _find_or_create_postcode(
    db: Session, city: str, district: str | None, locality: str, pincode: str
) -> int:
    """Return postcode.id; creates the chain if the pincode is not in the DB.

    The district comes from the request (falling back to the city only when
    absent); a provided-but-blank district is a validation error, never
    silently replaced.
    """
    # 1. Exact pincode match
    row = db.query(Postcode).filter(Postcode.pincode == pincode.strip()).first()
    if row:
        return row.id

    # 2. Locality-name match (case-insensitive) — use first postcode in that locality
    row = (
        db.query(Postcode)
        .join(Locality, Postcode.locality_id == Locality.id)
        .filter(func.lower(Locality.name) == locality.strip().lower())
        .first()
    )
    if row:
        return row.id

    # 3. Create the full chain for this new location
    city_name = city.strip() or "Unknown City"
    locality_name = locality.strip() or "Unknown Locality"
    pincode_val = (pincode.strip() or "000000")[:10]
    if district is None:
        district_name = city_name
    elif not district.strip():
        raise ValueError("district cannot be empty; omit it to fall back to city")
    else:
        district_name = district.strip()

    city_row = db.query(City).filter(func.lower(City.name) == city_name.lower()).first()
    if not city_row:
        city_row = City(name=city_name)
        db.add(city_row)
        db.flush()

    dist_row = (
        db.query(District)
        .filter(District.city_id == city_row.id, func.lower(District.name) == district_name.lower())
        .first()
    )
    if not dist_row:
        dist_row = District(city_id=city_row.id, name=district_name)
        db.add(dist_row)
        db.flush()

    taluk_row = (
        db.query(Taluk)
        .filter(Taluk.district_id == dist_row.id, func.lower(Taluk.name) == locality_name.lower())
        .first()
    )
    if not taluk_row:
        taluk_row = Taluk(district_id=dist_row.id, name=locality_name)
        db.add(taluk_row)
        db.flush()

    village_row = (
        db.query(Village)
        .filter(Village.taluk_id == taluk_row.id, func.lower(Village.name) == locality_name.lower())
        .first()
    )
    if not village_row:
        village_row = Village(taluk_id=taluk_row.id, name=locality_name)
        db.add(village_row)
        db.flush()

    loc_row = (
        db.query(Locality)
        .filter(Locality.village_id == village_row.id, func.lower(Locality.name) == locality_name.lower())
        .first()
    )
    if not loc_row:
        loc_row = Locality(village_id=village_row.id, name=locality_name)
        db.add(loc_row)
        db.flush()

    pc_row = (
        db.query(Postcode)
        .filter(Postcode.locality_id == loc_row.id, Postcode.pincode == pincode_val)
        .first()
    )
    if not pc_row:
        pc_row = Postcode(locality_id=loc_row.id, pincode=pincode_val)
        db.add(pc_row)
        db.flush()

    return pc_row.id


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def _resolve_amenity_ids(db: Session, names: list[str] | None, kind: str) -> list[int]:
    """Resolve amenity FEATURE/ROOM NAMES to master ids (case-insensitive).

    Client IDs are never trusted. Empty strings are skipped; duplicates
    (case-insensitive) link once; unknown names raise ValueError (→ 422).
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in names or []:
        name = " ".join(str(raw).strip().split())
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        cleaned.append(name)
    if not cleaned:
        return []
    rows = (
        db.query(AmenityMaster)
        .filter(AmenityMaster.kind == kind,
                func.lower(AmenityMaster.name).in_([n.lower() for n in cleaned]))
        .all()
    )
    found = {r.name.lower(): r.id for r in rows}
    ids = []
    for name in cleaned:
        if name.lower() not in found:
            raise ValueError(f"Unknown {kind.lower()}: {name!r}")
        ids.append(found[name.lower()])
    return ids


def _normalize_media_items(
    image_urls: list[str] | None,
    photos: list[dict] | None,
) -> list[dict]:
    """Build ordered media items from per-photo metadata (preferred) or the
    legacy URL list. Cover stays index 0 by array position."""
    if photos:
        items = []
        for p in photos:
            if isinstance(p, dict):
                url, category = p.get("url"), p.get("category")
            else:
                url, category = p, None
            if not url or not str(url).strip():
                raise ValueError("photo url cannot be empty")
            url = str(url).strip()
            if len(url) > 500:
                raise ValueError("photo url must be at most 500 characters")
            items.append({"url": url, "category": category})
        return items
    items = []
    for url in image_urls or []:
        if not url:
            continue  # legacy leniency: skip falsy entries
        url = str(url).strip()
        if len(url) > 500:
            raise ValueError("photo url must be at most 500 characters")
        items.append({"url": url, "category": None})
    return items


def create_owner_listing(
    db: Session,
    owner_id: int,
    title: str,
    transaction_type: str,
    property_type: str,
    price_paise: int,
    price_period: str,
    locality: str,
    city: str,
    pincode: str,
    address_line: str | None,
    area_value: int | None,
    area_unit: str | None,
    bedrooms: int | None,
    bathrooms: int | None,
    description: str | None,
    construction_status: str | None,
    image_urls: list[str],
    district: str | None = None,
    sub_locality: str | None = None,
    society_name: str | None = None,
    house_no: str | None = None,
    landmark: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    balconies: int | None = None,
    built_up_area: int | None = None,
    super_built_up_area: int | None = None,
    total_floors: int | None = None,
    floor_on: str | None = None,
    is_duplex: bool | None = None,
    property_age_band: str | None = None,
    furnishing: str | None = None,
    covered_parking: int | None = None,
    open_parking: int | None = None,
    open_sides: str | None = None,
    overlooking: str | None = None,
    power_backup: str | None = None,
    facing: str | None = None,
    ownership_type: str | None = None,
    contact_phone: str | None = None,
    contact_email: str | None = None,
    rent_terms: dict | None = None,
    amenities: list[str] | None = None,
    property_features: list[str] | None = None,
    other_rooms: list[str] | None = None,
    photos: list[dict] | None = None,
) -> PropertyListing:
    """Creates PhysicalProperty + PropertyListing + optional RentTerms +
    PropertyMedia + ListingAmenity rows atomically (single router commit)."""
    postcode_id = _find_or_create_postcode(db, city, district, locality, pincode)

    # Canonical area unit: lowercase 'sqft' for new writes (existing 'SQFT'
    # rows are left untouched; reads render both identically).
    if area_value is not None:
        area_unit = (area_unit or "sqft").strip().lower() or "sqft"
    elif area_unit:
        area_unit = area_unit.strip().lower() or None

    phys = PhysicalProperty(
        postcode_id=postcode_id,
        user_id=owner_id,
        address_line=address_line,
        area_value=area_value,
        area_unit=area_unit,
        bedrooms=bedrooms,
        bathrooms=bathrooms,
        balconies=balconies,
        built_up_area=built_up_area,
        super_built_up_area=super_built_up_area,
        total_floors=total_floors,
        floor_on=floor_on,
        is_duplex=bool(is_duplex),
        property_age_band=property_age_band,
        sub_locality=sub_locality,
        society_name=society_name,
        house_no=house_no,
        landmark=landmark,
        latitude=latitude,
        longitude=longitude,
        furnishing=furnishing,
        covered_parking=covered_parking,
        open_parking=open_parking,
        open_sides=open_sides,
        overlooking=overlooking,
        power_backup=power_backup,
        facing=facing,
        ownership_type=ownership_type,
    )
    db.add(phys)
    db.flush()  # get phys.id

    listing = PropertyListing(
        physical_property_id=phys.id,
        user_id=owner_id,
        title=title,
        price_paise=price_paise,
        price_period=price_period,
        description=description,
        construction_status=construction_status,
        transaction_type=transaction_type,
        property_type=property_type,
        verification_status="PENDING",
        listing_status="AVAILABLE",
        submitted_at=_utcnow_naive(),
        contact_phone=contact_phone,
        contact_email=contact_email,
    )
    db.add(listing)
    db.flush()  # get listing.id

    # RENT-only terms row, created only when at least one term is supplied.
    terms = rent_terms or {}
    has_terms = any(terms.get(f) is not None for f in (
        "security_deposit_rupees", "maintenance_rupees", "maintenance_period",
        "is_negotiable", "available_from", "tenant_preference",
        "lock_in_months", "agreement_months"))
    if has_terms and transaction_type == "RENT":
        db.add(RentTerms(
            property_listing_id=listing.id,
            security_deposit_paise=(terms["security_deposit_rupees"] * 100
                                    if terms.get("security_deposit_rupees") is not None else None),
            maintenance_paise=(terms["maintenance_rupees"] * 100
                               if terms.get("maintenance_rupees") is not None else None),
            maintenance_period=terms.get("maintenance_period"),
            is_negotiable=bool(terms.get("is_negotiable")),
            available_from=terms.get("available_from"),
            tenant_preference=terms.get("tenant_preference"),
            lock_in_months=terms.get("lock_in_months"),
            agreement_months=terms.get("agreement_months"),
        ))
        db.flush()

    media_items = _normalize_media_items(image_urls, photos)
    for idx, item in enumerate(media_items):
        media = PropertyMedia(
            property_listing_id=listing.id,
            url=item["url"],
            media_type="IMAGE",
            order_index=idx,
            category=item["category"],
            uploaded_by=owner_id,
        )
        db.add(media)

    # Amenity links resolved by NAME after the listing exists; unknown names
    # raise ValueError here so the router returns 422 with nothing committed.
    for amenity_id in _resolve_amenity_ids(db, amenities, "AMENITY"):
        db.add(ListingAmenity(property_listing_id=listing.id, amenity_id=amenity_id))
    for amenity_id in _resolve_amenity_ids(db, property_features, "FEATURE"):
        db.add(ListingAmenity(property_listing_id=listing.id, amenity_id=amenity_id))
    for amenity_id in _resolve_amenity_ids(db, other_rooms, "ROOM"):
        db.add(ListingAmenity(property_listing_id=listing.id, amenity_id=amenity_id))

    db.flush()
    return listing


# ---------------------------------------------------------------------------
# Photo upload attach (upload endpoint persists the file via storage.py)
# ---------------------------------------------------------------------------


def add_listing_photo(
    db: Session,
    owner_id: int,
    listing_id: int,
    url: str,
    category: str | None,
    mime_type: str | None,
    byte_size: int | None,
) -> PropertyMedia | None:
    """Attach one uploaded photo to an owner listing.

    Returns None when the listing does not belong to the owner (router maps
    to 404 — the same ownership-safe response as the detail endpoint).
    order_index continues after existing media (0 when first → cover); an
    existing cover is never reset. Flushes; the caller commits.
    """
    listing = (
        db.query(PropertyListing)
        .filter(PropertyListing.id == listing_id, PropertyListing.user_id == owner_id)
        .first()
    )
    if listing is None:
        return None
    max_order = (
        db.query(func.max(PropertyMedia.order_index))
        .filter(PropertyMedia.property_listing_id == listing_id)
        .scalar()
    )
    media = PropertyMedia(
        property_listing_id=listing_id,
        url=url,
        media_type="IMAGE",
        order_index=(max_order + 1 if max_order is not None else 0),
        category=category,
        mime_type=mime_type,
        byte_size=byte_size,
        uploaded_by=owner_id,
    )
    db.add(media)
    db.flush()  # get media.id
    return media


# ---------------------------------------------------------------------------
# Automatic verification (fetch-time, persisted, idempotent)
# ---------------------------------------------------------------------------


def auto_verify_due_owner_listings(
    db: Session,
    owner_id: int,
    listing_id: int | None = None,
    now: datetime | None = None,
) -> int:
    """Flip due PENDING owner listings to VERIFIED and persist the change.

    A listing is due when it is PENDING + AVAILABLE, has a recorded
    submitted_at, and submitted_at + AUTO_VERIFY_AFTER has elapsed. Only the
    verification_status column changes (PENDING -> VERIFIED); listing_status
    is already AVAILABLE, so the row becomes VERIFIED + AVAILABLE, which the
    frontend maps to Active. VERIFIED/SOLD/RENTED/LEASED rows and rows
    without submitted_at (seed data, pre-existing listings) never match, and
    once flipped a row never matches again — repeated calls are no-ops.

    Returns the number of listings transitioned (0 when nothing was due).
    """
    now = now or _utcnow_naive()
    cutoff = now - AUTO_VERIFY_AFTER
    q = db.query(PropertyListing).filter(
        PropertyListing.user_id == owner_id,
        PropertyListing.verification_status == "PENDING",
        PropertyListing.listing_status == "AVAILABLE",
        PropertyListing.submitted_at.isnot(None),
        PropertyListing.submitted_at <= cutoff,
    )
    if listing_id is not None:
        q = q.filter(PropertyListing.id == listing_id)
    due = q.all()
    for listing in due:
        listing.verification_status = "VERIFIED"
    if due:
        db.commit()
    return len(due)


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def _owner_listing_query(db: Session, owner_id: int):
    from ..models.locations import City, District, Locality, Postcode, Taluk, Village

    return (
        db.query(
            PropertyListing,
            PhysicalProperty.address_line,
            PhysicalProperty.area_value,
            PhysicalProperty.area_unit,
            PhysicalProperty.bedrooms,
            PhysicalProperty.bathrooms,
            PhysicalProperty.balconies,
            PhysicalProperty.built_up_area,
            PhysicalProperty.super_built_up_area,
            PhysicalProperty.total_floors,
            PhysicalProperty.floor_on,
            PhysicalProperty.is_duplex,
            PhysicalProperty.property_age_band,
            PhysicalProperty.sub_locality,
            PhysicalProperty.society_name,
            PhysicalProperty.house_no,
            PhysicalProperty.landmark,
            PhysicalProperty.latitude,
            PhysicalProperty.longitude,
            PhysicalProperty.furnishing,
            PhysicalProperty.covered_parking,
            PhysicalProperty.open_parking,
            PhysicalProperty.open_sides,
            PhysicalProperty.overlooking,
            PhysicalProperty.power_backup,
            PhysicalProperty.facing,
            PhysicalProperty.ownership_type,
            Locality.name.label("locality"),
            District.name.label("district"),
            City.name.label("city"),
            Postcode.pincode,
        )
        .join(PhysicalProperty, PropertyListing.physical_property_id == PhysicalProperty.id)
        .join(Postcode, PhysicalProperty.postcode_id == Postcode.id)
        .join(Locality, Postcode.locality_id == Locality.id)
        .join(Village, Locality.village_id == Village.id)
        .join(Taluk, Village.taluk_id == Taluk.id)
        .join(District, Taluk.district_id == District.id)
        .join(City, District.city_id == City.id)
        .filter(PropertyListing.user_id == owner_id)
    )


def _row_to_dict(row) -> dict:
    (listing, address_line, area_value, area_unit, bedrooms, bathrooms, balconies,
     built_up_area, super_built_up_area, total_floors, floor_on, is_duplex,
     property_age_band, sub_locality, society_name, house_no, landmark, latitude,
     longitude, furnishing, covered_parking, open_parking, open_sides, overlooking,
     power_backup, facing, ownership_type, locality, district, city, pincode) = row
    return {
        "listing_id": listing.id,
        "title": listing.title,
        "transaction_type": listing.transaction_type,
        "property_type": listing.property_type,
        "price_paise": listing.price_paise,
        "price_period": listing.price_period,
        "price_display": format_price(listing.price_paise, listing.price_period),
        "construction_status": listing.construction_status,
        "verification_status": listing.verification_status,
        "listing_status": listing.listing_status,
        "description": listing.description,
        "address_line": address_line,
        "locality": locality or "",
        "district": district or "",
        "city": city or "",
        "pincode": pincode or "",
        "sub_locality": sub_locality,
        "society_name": society_name,
        "house_no": house_no,
        "landmark": landmark,
        "latitude": float(latitude) if latitude is not None else None,
        "longitude": float(longitude) if longitude is not None else None,
        "area_value": area_value,
        "area_unit": area_unit,
        "bedrooms": bedrooms,
        "bathrooms": bathrooms,
        "balconies": balconies,
        "built_up_area": built_up_area,
        "super_built_up_area": super_built_up_area,
        "total_floors": total_floors,
        "floor_on": floor_on,
        "is_duplex": bool(is_duplex),
        "property_age_band": property_age_band,
        "furnishing": furnishing,
        "covered_parking": covered_parking,
        "open_parking": open_parking,
        "open_sides": open_sides,
        "overlooking": overlooking,
        "power_backup": power_backup,
        "facing": facing,
        "ownership_type": ownership_type,
        "contact_phone": listing.contact_phone,
        "contact_email": listing.contact_email,
        "submitted_at": listing.submitted_at,
        "cover_image_url": None,  # filled by caller
    }


def _rent_terms_map(db: Session, listing_ids: list[int]) -> dict:
    """listing_id -> rent_terms dict (paise/months/DATE canonical form)."""
    if not listing_ids:
        return {}
    rows = (
        db.query(RentTerms)
        .filter(RentTerms.property_listing_id.in_(listing_ids))
        .all()
    )
    return {
        r.property_listing_id: {
            "security_deposit_paise": r.security_deposit_paise,
            "maintenance_paise": r.maintenance_paise,
            "maintenance_period": r.maintenance_period,
            "is_negotiable": bool(r.is_negotiable),
            "available_from": r.available_from,
            "tenant_preference": r.tenant_preference,
            "lock_in_months": r.lock_in_months,
            "agreement_months": r.agreement_months,
        }
        for r in rows
    }


def _amenities_map(db: Session, listing_ids: list[int]) -> dict:
    """listing_id -> {"amenities": [...], "property_features": [...], "other_rooms": [...]}.

    Names grouped by master kind, ordered by link creation (listing_amenities
    has no position column; insertion order is the display order).
    """
    empty = {"amenities": [], "property_features": [], "other_rooms": []}
    if not listing_ids:
        return {}
    rows = (
        db.query(ListingAmenity.property_listing_id, AmenityMaster.name, AmenityMaster.kind)
        .join(AmenityMaster, ListingAmenity.amenity_id == AmenityMaster.id)
        .filter(ListingAmenity.property_listing_id.in_(listing_ids))
        .order_by(ListingAmenity.created_at, ListingAmenity.amenity_id)
        .all()
    )
    grouped: dict[int, dict] = {}
    for listing_id, name, kind in rows:
        bucket = grouped.setdefault(listing_id, {"amenities": [], "property_features": [], "other_rooms": []})
        if kind == "AMENITY":
            bucket["amenities"].append(name)
        elif kind == "FEATURE":
            bucket["property_features"].append(name)
        elif kind == "ROOM":
            bucket["other_rooms"].append(name)
    return grouped


def _media_map(db: Session, listing_ids: list[int]) -> dict:
    """listing_id -> ordered [{url, category}] (cover = index 0)."""
    if not listing_ids:
        return {}
    rows = (
        db.query(PropertyMedia.property_listing_id, PropertyMedia.url, PropertyMedia.category)
        .filter(PropertyMedia.property_listing_id.in_(listing_ids))
        .order_by(PropertyMedia.property_listing_id, PropertyMedia.order_index, PropertyMedia.id)
        .all()
    )
    grouped: dict[int, list] = {}
    for listing_id, url, category in rows:
        grouped.setdefault(listing_id, []).append({"url": url, "category": category})
    return grouped


def list_owner_listings(
    db: Session, owner_id: int, page: int = 1, page_size: int = 20
) -> tuple[list[dict], int]:
    # Fetch-time auto-verification: due PENDING rows become VERIFIED here and
    # the change is committed, so the next read (even after an app restart)
    # sees them as Active.
    auto_verify_due_owner_listings(db, owner_id)
    q = _owner_listing_query(db, owner_id)
    total = q.count()
    rows = q.order_by(PropertyListing.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    cards = [_row_to_dict(r) for r in rows]

    # Batch fetch covers, rent terms, amenities, and media (all owner-scoped
    # to the page's own listings).
    if cards:
        listing_ids = [c["listing_id"] for c in cards]
        media_by_listing = _media_map(db, listing_ids)
        terms_by_listing = _rent_terms_map(db, listing_ids)
        amenities_by_listing = _amenities_map(db, listing_ids)
        for c in cards:
            photos = media_by_listing.get(c["listing_id"], [])
            c["cover_image_url"] = photos[0]["url"] if photos else None
            c["gallery"] = [p["url"] for p in photos]
            c["photos"] = photos
            c["rent_terms"] = terms_by_listing.get(c["listing_id"])
            groups = amenities_by_listing.get(
                c["listing_id"], {"amenities": [], "property_features": [], "other_rooms": []})
            c.update(groups)

    return cards, total


def get_owner_listing(db: Session, owner_id: int, listing_id: int) -> dict | None:
    auto_verify_due_owner_listings(db, owner_id, listing_id=listing_id)
    q = _owner_listing_query(db, owner_id).filter(PropertyListing.id == listing_id)
    row = q.first()
    if row is None:
        return None
    card = _row_to_dict(row)
    photos = _media_map(db, [listing_id]).get(listing_id, [])
    card["cover_image_url"] = photos[0]["url"] if photos else None
    card["gallery"] = [p["url"] for p in photos]
    card["photos"] = photos
    card["rent_terms"] = _rent_terms_map(db, [listing_id]).get(listing_id)
    card.update(_amenities_map(db, [listing_id]).get(
        listing_id, {"amenities": [], "property_features": [], "other_rooms": []}))
    return card


# ---------------------------------------------------------------------------
# Persistent drafts (owner-scoped; JSON form snapshots, no fake listings)
# ---------------------------------------------------------------------------


def _draft_to_dict(draft: ListingDraft) -> dict:
    return {
        "id": draft.id,
        "transaction_type": draft.transaction_type,
        "title": draft.title,
        "current_step": draft.current_step,
        "form_data": draft.form_data,
        "created_at": draft.created_at,
        "updated_at": draft.updated_at,
    }


def create_draft(
    db: Session,
    owner_id: int,
    transaction_type: str = "RENT",
    title: str | None = None,
    current_step: int = 1,
    form_data: dict | None = None,
) -> dict:
    """Create a new draft (always a new row; updates use update_draft)."""
    draft = ListingDraft(
        user_id=owner_id,
        transaction_type=transaction_type,
        title=title or "Untitled Draft",
        current_step=current_step,
        form_data=form_data,
        created_at=_utcnow_naive(),
        updated_at=_utcnow_naive(),
    )
    db.add(draft)
    db.flush()
    return _draft_to_dict(draft)


def list_drafts(db: Session, owner_id: int) -> tuple[list[dict], int]:
    rows = (
        db.query(ListingDraft)
        .filter(ListingDraft.user_id == owner_id)
        .order_by(ListingDraft.updated_at.desc(), ListingDraft.id.desc())
        .all()
    )
    cards = [_draft_to_dict(r) for r in rows]
    return cards, len(cards)


def get_draft(db: Session, owner_id: int, draft_id: int) -> dict | None:
    row = (
        db.query(ListingDraft)
        .filter(ListingDraft.id == draft_id, ListingDraft.user_id == owner_id)
        .first()
    )
    return _draft_to_dict(row) if row is not None else None


def update_draft(
    db: Session,
    owner_id: int,
    draft_id: int,
    transaction_type: str | None = None,
    title: str | None = None,
    current_step: int | None = None,
    form_data: dict | None = None,
) -> dict | None:
    """Full-replace update of the snapshot; returns None when not owned."""
    row = (
        db.query(ListingDraft)
        .filter(ListingDraft.id == draft_id, ListingDraft.user_id == owner_id)
        .first()
    )
    if row is None:
        return None
    if transaction_type is not None:
        row.transaction_type = transaction_type
    if title is not None:
        row.title = title
    if current_step is not None:
        row.current_step = current_step
    if form_data is not None:
        row.form_data = form_data
    row.updated_at = _utcnow_naive()
    db.flush()
    return _draft_to_dict(row)


def delete_draft(db: Session, owner_id: int, draft_id: int) -> bool:
    """Delete an owned draft; False when missing/not owned (router → 404)."""
    row = (
        db.query(ListingDraft)
        .filter(ListingDraft.id == draft_id, ListingDraft.user_id == owner_id)
        .first()
    )
    if row is None:
        return False
    db.delete(row)
    db.flush()
    return True
