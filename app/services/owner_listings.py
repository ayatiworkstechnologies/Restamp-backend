"""RESTAMP Owner V1 — listing creation and management.

An owner can create a property listing (PhysicalProperty + PropertyListing +
PropertyMedia rows) and retrieve their own listings.

Location strategy:
  1. Try to find an exact postcode match by pincode.
  2. Fall back to a case-insensitive locality name match.
  3. If neither exists, auto-create the full location chain so the submission
     never fails for a valid but unseen locality — the admin can clean/merge
     location data independently.

New listings land in PENDING/AVAILABLE state (submitted, not yet visible).
Buyer visibility requires VERIFIED + AVAILABLE; the explicit lifecycle is:

  verification: PENDING -> VERIFIED (verify) | PENDING -> REJECTED (reject)
                REJECTED -> PENDING (resubmit for re-review)
  listing:      AVAILABLE -> CLOSED (archive) | CLOSED -> AVAILABLE (publish,
                VERIFIED only) | AVAILABLE -> SOLD/RENTED/LEASED (deal close,
                VERIFIED only, terminal)

Reads never write: GET endpoints perform no verification flips and no
commits. All transitions are explicit POST operations with ownership checks
and validation; same-state repeats are idempotent no-ops.

Ownership authority: a listing belongs to an owner only when BOTH
property_listings.user_id AND its physical_properties.user_id equal the
caller (creation sets both; all reads/transitions require both via join).
A diverged row matches neither party (fail-closed 404, never a leak).
"""
from __future__ import annotations

from datetime import datetime, timezone
import re

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
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
from ..schemas import CHENNAI_PINCODE_ERROR, normalize_chennai_pincode
from ..services.listings import format_price

# MySQL duplicate-key error code (ER_DUP_ENTRY), surfaced by the pymysql
# driver as ``IntegrityError.orig.args[0]``. It is the only integrity error
# the location-chain helper can recover from (via a re-lookup); every other
# integrity error re-raises — see _get_or_create_chain_row.
MYSQL_ERR_DUP_ENTRY = 1062


def _utcnow_naive() -> datetime:
    """Current UTC as a naive datetime, matching MySQL DATETIME storage.

    submitted_at is written and compared in UTC on the Python side only, so
    no database clock/timezone is ever involved.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Location helpers
# ---------------------------------------------------------------------------


def _get_or_create_chain_row(db: Session, lookup, build):
    """Get-or-create one location-chain row, tolerant of a concurrent commit.

    The unlocked check-then-flush pattern can race exactly with another
    request's COMMIT: under REPEATABLE READ this transaction's snapshot
    hides the winner's row, yet the INSERT itself still fails with a
    duplicate-key IntegrityError (unique indexes are checked against the
    live index, not the snapshot).

    Recovery is savepoint-based and duplicate-key-specific: rollback to the
    savepoint (only the one insert is reverted; the surrounding listing
    transaction is untouched) and re-run the lookup as
    ``SELECT ... FOR UPDATE`` — a locking read reads the newest committed
    data, not the snapshot. Only a duplicate-key error (MySQL 1062) is
    recoverable that way: any other IntegrityError (e.g. a foreign-key
    violation) cannot succeed on any retry, so it re-raises the original
    error immediately — no retry, no refetch (the same failure surface that
    existed before this helper). If the winner rolled back instead, the
    insert is retried, bounded at three attempts; exhausted attempts
    re-raise the duplicate-key error. Postcode conflicts are
    only adopted for the same (locality, pincode) pair, because the lookup
    is keyed on exactly that pair; a pincode claimed by a *different*
    locality re-raises — deliberate, per the reviewed plan.

    Locality has no unique key, so an exact race can still leave a
    duplicate locality row (documented residual; adding a unique index
    would be a separately-reviewed migration).
    """
    last_error: IntegrityError | None = None
    for _ in range(3):
        row = lookup().first()
        if row is not None:
            return row
        # Savepoint BEFORE adding: Session.begin_nested() autoflushes, so it
        # must open on a clean pending-set; the one candidate it scopes is
        # added afterward and flushed strictly inside the savepoint.
        sp = db.begin_nested()
        candidate = build()
        db.add(candidate)
        try:
            db.flush()
        except IntegrityError as e:
            sp.rollback()
            # Rollback-to-savepoint removes the candidate (added within the
            # savepoint) from the session, so a later autoflush never
            # retries the failed insert — even when re-raising below, this
            # keeps the surrounding listing transaction consistent.
            # Only a duplicate-key error can be recovered by the refetch
            # below: pymysql exposes the MySQL code at e.orig.args[0]. An
            # uninspectable driver error counts as non-duplicate (fail
            # closed): re-raise the original error immediately — no retry,
            # no refetch.
            orig = getattr(e, "orig", None)
            args = getattr(orig, "args", ()) or ()
            orig_code = args[0] if args else None
            if orig_code != MYSQL_ERR_DUP_ENTRY:
                raise
            last_error = e
            # Locking read: sees rows committed after our snapshot was taken.
            row = lookup().with_for_update().first()
            if row is not None:
                return row
            continue  # the winner rolled back (or nothing was committed); retry
        sp.commit()
        return candidate
    raise last_error


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

    city_row = _get_or_create_chain_row(
        db,
        lookup=lambda: db.query(City).filter(func.lower(City.name) == city_name.lower()),
        build=lambda: City(name=city_name),
    )

    dist_row = _get_or_create_chain_row(
        db,
        lookup=lambda: db.query(District).filter(
            District.city_id == city_row.id, func.lower(District.name) == district_name.lower()
        ),
        build=lambda: District(city_id=city_row.id, name=district_name),
    )

    taluk_row = _get_or_create_chain_row(
        db,
        lookup=lambda: db.query(Taluk).filter(
            Taluk.district_id == dist_row.id, func.lower(Taluk.name) == locality_name.lower()
        ),
        build=lambda: Taluk(district_id=dist_row.id, name=locality_name),
    )

    village_row = _get_or_create_chain_row(
        db,
        lookup=lambda: db.query(Village).filter(
            Village.taluk_id == taluk_row.id, func.lower(Village.name) == locality_name.lower()
        ),
        build=lambda: Village(taluk_id=taluk_row.id, name=locality_name),
    )

    loc_row = _get_or_create_chain_row(
        db,
        lookup=lambda: db.query(Locality).filter(
            Locality.village_id == village_row.id, func.lower(Locality.name) == locality_name.lower()
        ),
        build=lambda: Locality(village_id=village_row.id, name=locality_name),
    )

    pc_row = _get_or_create_chain_row(
        db,
        lookup=lambda: db.query(Postcode).filter(
            Postcode.locality_id == loc_row.id, Postcode.pincode == pincode_val
        ),
        build=lambda: Postcode(locality_id=loc_row.id, pincode=pincode_val),
    )

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
            # Static detail only (422 hygiene): never echo the client-supplied
            # name back into the response body.
            raise ValueError(f"Unknown {kind.lower()}: name not in the master list")
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
    # Service-boundary Chennai rule (mirrors the schema validator so direct
    # service calls cannot bypass it). Runs before any mutation, so invalid
    # values fail atomically with nothing flushed.
    pincode = normalize_chennai_pincode(pincode)
    if not pincode:
        raise ValueError(CHENNAI_PINCODE_ERROR)
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

    # Terms row for RENT/LEASE listings with at least one supplied term.
    # Non-empty terms on RESALE/BUY are an explicit error, never a silent
    # drop — the caller must omit rent_terms (or send null) for those types.
    terms = rent_terms or {}
    if _has_terms(terms) and transaction_type not in TERMED_TX_TYPES:
        raise ValueError("rent_terms are only supported for RENT and LEASE listings")
    if _has_terms(terms):
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


# Transaction types that persist a rent_terms row. RENT and LEASE share the
# same terms table/validation; RESALE/BUY must not carry terms — non-empty
# terms on those types are rejected explicitly (never silently dropped).
TERMED_TX_TYPES = {"RENT", "LEASE"}

_TERM_FIELDS = (
    "security_deposit_rupees", "maintenance_rupees", "maintenance_period",
    "is_negotiable", "available_from", "tenant_preference",
    "lock_in_months", "agreement_months")


def _has_terms(terms: dict | None) -> bool:
    """True when at least one term key is supplied non-null.

    Note ``is_negotiable=False`` counts as supplied (``False is not None``),
    matching the create path.
    """
    terms = terms or {}
    return any(terms.get(f) is not None for f in _TERM_FIELDS)


def _write_rent_terms_row(
    db: Session, listing_id: int, terms: dict, existing: RentTerms | None
) -> None:
    """Upsert a RentTerms row, merging provided keys over the existing row.

    Only keys present in ``terms`` are touched (omitted keys keep their
    stored values); explicit null clears a nullable column. Rupees convert
    to paise; ``is_negotiable`` coerces via bool() (create-path parity).
    """
    row = existing
    if row is None:
        row = RentTerms(property_listing_id=listing_id)
        db.add(row)
    if "security_deposit_rupees" in terms:
        v = terms["security_deposit_rupees"]
        row.security_deposit_paise = v * 100 if v is not None else None
    if "maintenance_rupees" in terms:
        v = terms["maintenance_rupees"]
        row.maintenance_paise = v * 100 if v is not None else None
    if "maintenance_period" in terms:
        row.maintenance_period = terms["maintenance_period"]
    if "is_negotiable" in terms:
        row.is_negotiable = bool(terms["is_negotiable"])
    if "available_from" in terms:
        row.available_from = terms["available_from"]
    if "tenant_preference" in terms:
        row.tenant_preference = terms["tenant_preference"]
    if "lock_in_months" in terms:
        row.lock_in_months = terms["lock_in_months"]
    if "agreement_months" in terms:
        row.agreement_months = terms["agreement_months"]
    db.flush()


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
    public_id: str | None = None,
) -> PropertyMedia | None:
    """Attach one uploaded photo to an owner listing.

    Returns None when the listing does not belong to the owner (router maps
    to 404 — the same ownership-safe response as the detail endpoint).
    order_index continues after existing media (0 when first → cover); an
    existing cover is never reset. Flushes; the caller commits.
    """
    listing = _get_owned_listing_for_update(db, owner_id, listing_id)
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
        public_id=public_id,
        uploaded_by=owner_id,
    )
    db.add(media)
    db.flush()  # get media.id
    return media


# ---------------------------------------------------------------------------
# Update (Edit mode): in-place PATCH, never inserts new listing/physical rows
# ---------------------------------------------------------------------------

# Non-nullable columns: an explicit null in the PATCH body is a 422.
_NON_NULLABLE_UPDATE_FIELDS = {
    "title", "price_rupees", "price_period", "transaction_type",
    "property_type", "locality", "city", "pincode",
}

# PhysicalProperty scalar columns writable via PATCH (postcode_id handled
# separately through the location-chain resolver).
_PHYS_UPDATE_FIELDS = {
    "address_line", "area_value", "bedrooms", "bathrooms", "balconies",
    "built_up_area", "super_built_up_area", "total_floors", "floor_on",
    "is_duplex", "property_age_band", "sub_locality", "society_name",
    "house_no", "landmark", "latitude", "longitude", "furnishing",
    "covered_parking", "open_parking", "open_sides", "overlooking",
    "power_backup", "facing", "ownership_type",
}

# PropertyListing scalar columns writable via PATCH (price_rupees arrives as
# price_paise; verification/listing status and submitted_at are never
# writable here).
_LISTING_UPDATE_FIELDS = {
    "title", "price_period", "description", "construction_status",
    "transaction_type", "property_type", "contact_phone", "contact_email",
}


def _reconcile_media(
    db: Session,
    owner_id: int,
    listing_id: int,
    image_urls: list[str] | None,
    photos: list[dict] | None,
) -> list[str]:
    """Reconcile the gallery to the desired ordered end-state.

    Rows are matched to desired items by URL (first-unused wins); matched
    rows keep their id/mime/byte_size/public_id/uploaded_by and take the new
    position/category. Desired URLs with no match are inserted. Stored rows
    with no matching desired URL are deleted. ``order_index`` is compacted
    to 0..n-1 so cover (= index 0) stays well-defined.

    Returns the Cloudinary ``public_id`` values of deleted rows (non-null
    only) so the caller can destroy the assets *after* committing.
    Flushes; the caller commits.
    """
    desired = _normalize_media_items(image_urls, photos)
    existing = (
        db.query(PropertyMedia)
        .filter(PropertyMedia.property_listing_id == listing_id)
        .order_by(PropertyMedia.order_index, PropertyMedia.id)
        .all()
    )
    by_url: dict[str, list] = {}
    for row in existing:
        by_url.setdefault(row.url, []).append(row)
    kept: list = []
    for item in desired:
        candidates = by_url.get(item["url"], [])
        row = candidates.pop(0) if candidates else None
        if row is None:
            row = PropertyMedia(
                property_listing_id=listing_id,
                url=item["url"],
                media_type="IMAGE",
                order_index=0,
                category=item["category"],
                uploaded_by=owner_id,
            )
            db.add(row)
        else:
            row.category = item["category"]
        kept.append(row)
    removed_public_ids: list[str] = []
    for leftover in [r for rows in by_url.values() for r in rows]:
        if leftover not in kept:
            if leftover.public_id:
                removed_public_ids.append(leftover.public_id)
            db.delete(leftover)
    db.flush()
    for idx, row in enumerate(kept):
        row.order_index = idx
    db.flush()
    return removed_public_ids


def delete_listing_photo(
    db: Session, owner_id: int, listing_id: int, media_id: int
) -> tuple[bool, str | None]:
    """Delete one gallery photo owned by the caller.

    Returns ``(found, public_id)``. ``found`` is False when the listing (or
    the photo on it) does not belong to the owner — the router maps that to
    404. Remaining siblings are renumbered 0..n-1 so the cover invariant
    (cover = order_index 0) holds. Flushes; the caller commits, then destroys
    the Cloudinary asset best-effort (see router).
    """
    listing = _get_owned_listing_for_update(db, owner_id, listing_id)
    if listing is None:
        return False, None
    media = (
        db.query(PropertyMedia)
        .filter(PropertyMedia.id == media_id,
                PropertyMedia.property_listing_id == listing_id)
        .first()
    )
    if media is None:
        return False, None
    public_id = media.public_id
    db.delete(media)
    db.flush()
    siblings = (
        db.query(PropertyMedia)
        .filter(PropertyMedia.property_listing_id == listing_id)
        .order_by(PropertyMedia.order_index, PropertyMedia.id)
        .all()
    )
    for idx, row in enumerate(siblings):
        row.order_index = idx
    db.flush()
    return True, public_id


def update_owner_listing(
    db: Session,
    owner_id: int,
    listing_id: int,
    updates: dict,
) -> tuple[PropertyListing | None, list[str]]:
    """Apply a partial update to an existing owner listing in place.

    ``updates`` uses OwnerListingCreate field names with ``price_rupees``
    (not paise) and a ``rent_terms`` dict-or-None; it must already be
    ``exclude_unset``-filtered so omitted keys are never touched. Only
    keys present are written: explicit null clears a nullable column,
    explicit null on a non-nullable column raises ValueError (→ 422), and
    ``0``/``False`` are preserved (all checks are key-presence based, never
    truthiness based).

    Returns ``(listing, removed_public_ids)``; ``listing`` is None when the
    listing does not belong to the owner (router maps to 404). IDs and
    relationships are preserved: no new property_listings/physical_properties
    rows, amenity links are diffed (no duplicates, retry-idempotent), media
    is reconciled to the end-state when ``photos``/``images`` is supplied,
    and verification/listing status plus submitted_at are never modified
    here (use the explicit transition operations instead).
    Flushes; the caller commits (rolling back on ValueError first).
    """
    listing = _get_owned_listing_for_update(db, owner_id, listing_id)
    if listing is None:
        return None, []
    phys = (
        db.query(PhysicalProperty)
        .filter(PhysicalProperty.id == listing.physical_property_id)
        .first()
    )
    if phys is None:
        raise ValueError("listing has no physical property record")

    for field in _NON_NULLABLE_UPDATE_FIELDS:
        if field in updates and updates[field] is None:
            raise ValueError(f"{field} cannot be null")

    if "pincode" in updates and updates["pincode"] is not None:
        # Service-boundary Chennai rule for supplied pincodes (mirrors the
        # schema validator so direct service calls cannot bypass it).
        # Omitted pincodes preserve the stored value; explicit null stays a
        # 422 via the check above. Runs before any flush, so failures are
        # atomic. The normalized (trimmed) value is stored back so the
        # resolver below sees exactly what was validated.
        updates["pincode"] = normalize_chennai_pincode(updates["pincode"])

    for field in _LISTING_UPDATE_FIELDS:
        if field not in updates:
            continue
        value = updates[field]
        if field == "price_period" and value is not None:
            value = str(value).upper()
        setattr(listing, field, value)
    if "price_rupees" in updates:
        listing.price_paise = updates["price_rupees"] * 100

    if "area_value" in updates or "area_unit" in updates:
        area_value = updates["area_value"] if "area_value" in updates else phys.area_value
        if area_value is not None:
            raw_unit = updates["area_unit"] if "area_unit" in updates else phys.area_unit
            phys.area_value = area_value
            phys.area_unit = (raw_unit or "sqft").strip().lower() or "sqft"
        else:
            if "area_value" in updates:
                phys.area_value = None
            if "area_unit" in updates:
                unit = updates["area_unit"]
                phys.area_unit = unit.strip().lower() or None if unit is not None else None
    for field in _PHYS_UPDATE_FIELDS - {"area_value", "area_unit"}:
        if field not in updates:
            continue
        value = updates[field]
        if field == "is_duplex":
            # Column is NOT NULL: explicit null falls back to False
            # (create-path parity), omitted keeps the stored value.
            value = bool(value) if value is not None else False
        setattr(phys, field, value)

    # Location: any supplied part re-resolves the postcode from merged
    # (supplied over stored) values. Known limitation (no redesign here):
    # the resolver tries exact-pincode first, then falls back to a
    # locality-name match, so a submitted pincode paired with an already
    # stored locality can resolve to that locality's previously stored
    # postcode — i.e. a different pincode than submitted. Locality to
    # pincode consistency needs a product decision in the future
    # area-lookup phase; callers/tests pair unique localities with new
    # pincodes to avoid the fallback.
    loc_keys = {"city", "district", "locality", "pincode"}
    if updates.keys() & loc_keys:
        cur = _location_of(db, listing_id)
        city = updates["city"] if "city" in updates else cur["city"]
        district = updates["district"] if "district" in updates else cur["district"]
        locality = updates["locality"] if "locality" in updates else cur["locality"]
        pincode = updates["pincode"] if "pincode" in updates else cur["pincode"]
        phys.postcode_id = _find_or_create_postcode(db, city, district, locality, pincode)

    # Floor consistency against merged values (schema only sees the patch).
    total = updates["total_floors"] if "total_floors" in updates else phys.total_floors
    floor = updates["floor_on"] if "floor_on" in updates else phys.floor_on
    floor_s = (floor or "").strip() if floor else ""
    if total is not None and re.fullmatch(r"\d+", floor_s) and int(floor_s) > total:
        raise ValueError("floor_on cannot exceed total_floors")

    # Terms use the effective transaction type (patched or stored).
    effective_tx = updates.get("transaction_type", listing.transaction_type)
    if effective_tx is None:
        effective_tx = listing.transaction_type
    existing_terms = (
        db.query(RentTerms)
        .filter(RentTerms.property_listing_id == listing_id)
        .first()
    )
    if "rent_terms" in updates:
        terms = updates["rent_terms"] or {}
        if updates["rent_terms"] is not None and not isinstance(terms, dict):
            raise ValueError("rent_terms must be an object or null")
        supplied = [k for k in _TERM_FIELDS if k in terms]
        if _has_terms(terms) and effective_tx not in TERMED_TX_TYPES:
            raise ValueError("rent_terms are only supported for RENT and LEASE listings")
        if _has_terms(terms) or (supplied and existing_terms is not None
                                 and effective_tx in TERMED_TX_TYPES):
            # Merge supplied sub-keys over the existing row: an explicit null
            # clears that column, omitted sub-keys keep their stored values,
            # and 0/False are preserved (key-presence checks only, never
            # truthiness). A null-only object with no existing row is a no-op.
            _write_rent_terms_row(db, listing_id, terms, existing_terms)
        elif existing_terms is not None and (
            terms == {} or updates["rent_terms"] is None or effective_tx not in TERMED_TX_TYPES
        ):
            # Explicit null, explicit empty dict, or a move away from a
            # termed type clears the now-inapplicable row.
            db.delete(existing_terms)
            db.flush()
    elif effective_tx not in TERMED_TX_TYPES and existing_terms is not None:
        # Type moved away from RENT/LEASE without touching terms: drop the
        # stale row rather than reporting terms for an unsupported type.
        db.delete(existing_terms)
        db.flush()

    # Amenities: full replace per supplied kind (diffed, idempotent).
    kind_map = (("amenities", "AMENITY"), ("property_features", "FEATURE"),
                ("other_rooms", "ROOM"))
    for key, kind in kind_map:
        if key not in updates:
            continue
        wanted = _resolve_amenity_ids(db, updates[key], kind)
        current = {
            r[0] for r in db.query(ListingAmenity.amenity_id)
            .filter(ListingAmenity.property_listing_id == listing_id).all()
        }
        master_ids = {r.id for r in db.query(AmenityMaster.id)
                      .filter(AmenityMaster.kind == kind).all()}
        # Only diff within this kind: never touch other kinds' links.
        current_of_kind = current & master_ids
        for aid in sorted(set(wanted) - current_of_kind):
            db.add(ListingAmenity(property_listing_id=listing_id, amenity_id=aid))
        for aid in sorted(current_of_kind - set(wanted)):
            (db.query(ListingAmenity)
               .filter(ListingAmenity.property_listing_id == listing_id,
                       ListingAmenity.amenity_id == aid)
               .delete(synchronize_session=False))
        db.flush()

    # Media end-state (absent/null = untouched; [] = clear gallery).
    removed_public_ids: list[str] = []
    if "photos" in updates and updates["photos"] is not None:
        removed_public_ids = _reconcile_media(
            db, owner_id, listing_id, updates.get("images"), updates["photos"])
    elif "images" in updates and updates["images"] is not None:
        removed_public_ids = _reconcile_media(
            db, owner_id, listing_id, updates["images"], None)

    db.flush()
    return listing, removed_public_ids


def _location_of(db: Session, listing_id: int) -> dict:
    """Current city/district/locality/pincode labels for a listing."""
    from ..models.locations import City, District, Locality, Postcode, Taluk, Village

    row = (
        db.query(City.name, District.name, Locality.name, Postcode.pincode)
        .select_from(PropertyListing)
        .join(PhysicalProperty, PropertyListing.physical_property_id == PhysicalProperty.id)
        .join(Postcode, PhysicalProperty.postcode_id == Postcode.id)
        .join(Locality, Postcode.locality_id == Locality.id)
        .join(Village, Locality.village_id == Village.id)
        .join(Taluk, Village.taluk_id == Taluk.id)
        .join(District, Taluk.district_id == District.id)
        .join(City, District.city_id == City.id)
        .filter(PropertyListing.id == listing_id)
        .first()
    )
    if row is None:
        raise ValueError("listing location not found")
    city, district, locality, pincode = row
    return {"city": city or "", "district": district or "",
            "locality": locality or "", "pincode": pincode or ""}


# ---------------------------------------------------------------------------
# Ownership + explicit lifecycle transitions (no automatic verification)
# ---------------------------------------------------------------------------

# Owner-driven verification transitions. VERIFIED is terminal here (a
# verified listing is unpublished/archived via listing_status, never by
# flipping verification back). Same-state repeats are idempotent no-ops.
_VERIFICATION_TRANSITIONS = {
    "PENDING": {"PENDING", "VERIFIED", "REJECTED"},
    "REJECTED": {"REJECTED", "PENDING"},
    "VERIFIED": {"VERIFIED"},
}

# Owner-driven listing-status transitions. Publishing (CLOSED -> AVAILABLE)
# and deal closes (AVAILABLE -> SOLD/RENTED/LEASED) additionally require
# verification_status == VERIFIED (enforced below, not just here).
# Same-state repeats are idempotent no-ops.
_LISTING_STATUS_TRANSITIONS = {
    "AVAILABLE": {"AVAILABLE", "CLOSED", "SOLD", "RENTED", "LEASED"},
    "CLOSED": {"CLOSED", "AVAILABLE"},
    "SOLD": {"SOLD"},
    "RENTED": {"RENTED"},
    "LEASED": {"LEASED"},
    "EXPIRED": {"EXPIRED"},
}

_VERIFICATION_RULESET = "owner-explicit-v1"

# Automatic verification: after this many minutes a pending listing becomes
# VERIFIED + AVAILABLE. The deadline is derived from submitted_at.
_AUTO_VERIFY_MINUTES = 2


def _owned_listing_query(db: Session, owner_id: int, listing_id: int):
    """Dual-authority ownership query (no lock); the locked variant below
    adds FOR UPDATE so racing transitions serialize on the row in InnoDB."""
    return (
        db.query(PropertyListing)
        .join(PhysicalProperty, PhysicalProperty.id == PropertyListing.physical_property_id)
        .filter(
            PropertyListing.id == listing_id,
            PropertyListing.user_id == owner_id,
            PhysicalProperty.user_id == owner_id,
        )
    )


def _get_owned_listing_for_update(
    db: Session, owner_id: int, listing_id: int
) -> PropertyListing | None:
    """Fetch the listing row for a mutating operation, ownership-checked.

    Requires BOTH property_listings.user_id AND its physical_properties
    user_id to equal the caller (consistent dual-authority check with row
    lock). Returns None when missing or not owned by the caller (router
    maps to 404 — never a cross-owner leak, never an oracle beyond 404).
    """
    row = (
        _owned_listing_query(db, owner_id, listing_id)
        .with_for_update()
        .first()
    )
    return row


def owner_owns_listing(db: Session, owner_id: int, listing_id: int) -> bool:
    """Read-only dual-authority ownership check (no lock, no write).

    Used to authorize before external side effects (e.g. Cloudinary upload)
    so foreign IDs fail with 404 before anything is spent or stored.
    """
    return (
        db.query(PropertyListing.id)
        .join(PhysicalProperty, PhysicalProperty.id == PropertyListing.physical_property_id)
        .filter(
            PropertyListing.id == listing_id,
            PropertyListing.user_id == owner_id,
            PhysicalProperty.user_id == owner_id,
        )
        .first()
        is not None
    )


def transition_verification(
    db: Session, owner_id: int, listing_id: int, to_status: str, reason: str | None = None
) -> PropertyListing | None:
    """Apply an explicit owner-driven verification transition.

    Allowed: PENDING -> VERIFIED | PENDING -> REJECTED | REJECTED -> PENDING
    (resubmit; refreshes submitted_at). Same-state repeats are idempotent
    no-ops returning the row. Anything else raises ValueError (-> 422).
    Records a verifications audit row (PASS for VERIFIED/PENDING-resubmit,
    FAIL for REJECTED) with attempt_no incrementing per listing.
    Returns None when the listing does not belong to the caller.
    Flushes; the caller commits.
    """
    from ..models.properties import Verification

    if to_status not in ("PENDING", "VERIFIED", "REJECTED"):
        raise ValueError(f"verification_status must be one of PENDING, VERIFIED, REJECTED")
    listing = _get_owned_listing_for_update(db, owner_id, listing_id)
    if listing is None:
        return None
    if listing.deleted_at is not None:
        return None  # cannot verify a removed listing
    allowed = _VERIFICATION_TRANSITIONS.get(listing.verification_status, set())
    if to_status not in allowed:
        raise ValueError(
            f"Cannot move verification from {listing.verification_status} to {to_status}"
        )
    if listing.verification_status == to_status:
        return listing  # idempotent repeat
    listing.verification_status = to_status
    if to_status == "PENDING":
        listing.submitted_at = _utcnow_naive()  # resubmit refreshes the audit clock
    last_attempt = (
        db.query(func.max(Verification.attempt_no))
        .filter(Verification.property_listing_id == listing_id)
        .scalar()
    )
    checks = {"action": to_status.lower(), "actor": "owner"}
    if reason:
        checks["reason"] = reason[:500]
    db.add(Verification(
        property_listing_id=listing_id,
        attempt_no=int(last_attempt or 0) + 1,
        ruleset_version=_VERIFICATION_RULESET,
        checks_json=checks,
        result="PASS" if to_status in ("VERIFIED", "PENDING") else "FAIL",
        triggered_at=_utcnow_naive(),
        triggered_by=owner_id,
    ))
    db.flush()
    return listing


def transition_verification_automatic(
    db: Session, listing_id: int
) -> PropertyListing | None:
    """Apply automatic verification for a pending listing that has passed the
    2-minute deadline. This is called by a background worker, not by an owner.

    Only transitions PENDING -> VERIFIED when the listing is still eligible:
    - verification_status == PENDING
    - listing_status == AVAILABLE (not archived/closed/sold/etc.)
    - submitted_at is at least _AUTO_VERIFY_MINUTES in the past

    Records a verifications audit row with actor="auto" and result="PASS".
    Idempotent: returns the listing if already VERIFIED or if transitioned now.
    Returns None when the listing doesn't exist or isn't eligible.
    Flushes; the caller commits.
    """
    from ..models.properties import Verification
    from datetime import timedelta

    listing = (
        db.query(PropertyListing)
        .filter(PropertyListing.id == listing_id)
        .with_for_update()
        .first()
    )
    if listing is None:
        return None

    # Only auto-verify eligible pending listings
    if listing.verification_status != "PENDING":
        return None  # not pending (verified/rejected/etc.) - not eligible
    if listing.deleted_at is not None:
        return None  # removed listing - never auto-verify
    if listing.listing_status != "AVAILABLE":
        return None  # archived, sold, etc. - not eligible

    # Check if 2 minutes have passed since submission
    if listing.submitted_at is None:
        return None
    deadline = listing.submitted_at + timedelta(minutes=_AUTO_VERIFY_MINUTES)
    if _utcnow_naive() < deadline:
        return None  # not yet due

    # Transition to VERIFIED
    listing.verification_status = "VERIFIED"
    # listing_status stays AVAILABLE (already verified + available = visible)

    # Audit the automatic transition
    last_attempt = (
        db.query(func.max(Verification.attempt_no))
        .filter(Verification.property_listing_id == listing_id)
        .scalar()
    )
    checks = {"action": "verified", "actor": "auto"}
    db.add(Verification(
        property_listing_id=listing_id,
        attempt_no=int(last_attempt or 0) + 1,
        ruleset_version=_VERIFICATION_RULESET,
        checks_json=checks,
        result="PASS",
        triggered_at=_utcnow_naive(),
        triggered_by=None,  # system-triggered
    ))
    db.flush()
    return listing


def transition_listing_status(
    db: Session,
    owner_id: int,
    listing_id: int,
    to_status: str,
    closed_reason: str | None = None,
) -> PropertyListing | None:
    """Apply an explicit owner-driven listing-status transition.

    Allowed: AVAILABLE -> CLOSED/SOLD/RENTED/LEASED, CLOSED -> AVAILABLE.
    SOLD/RENTED/LEASED/EXPIRED are terminal (no exits). Publishing
    (CLOSED -> AVAILABLE) and deal closes require VERIFIED; anything else
    raises ValueError (-> 422). Same-state repeats are idempotent no-ops
    (closed_reason updated when supplied). Returns None when not owned.
    Flushes; the caller commits.
    """
    if to_status not in ("AVAILABLE", "CLOSED", "SOLD", "RENTED", "LEASED"):
        raise ValueError(
            "listing_status must be one of AVAILABLE, CLOSED, SOLD, RENTED, LEASED"
        )
    if closed_reason is not None and len(closed_reason) > 500:
        raise ValueError("closed_reason must be at most 500 characters")
    listing = _get_owned_listing_for_update(db, owner_id, listing_id)
    if listing is None:
        return None
    if listing.deleted_at is not None:
        return None  # cannot change status of a removed listing
    allowed = _LISTING_STATUS_TRANSITIONS.get(listing.listing_status, set())
    if to_status not in allowed:
        raise ValueError(
            f"Cannot move listing from {listing.listing_status} to {to_status}"
        )
    if listing.listing_status == to_status:
        # Idempotent repeat. A live listing carries no close note: repeats on
        # AVAILABLE always clear it (a supplied reason is ignored, never
        # stored on a live row); other states update it when supplied.
        if to_status == "AVAILABLE":
            if listing.closed_reason is not None:
                listing.closed_reason = None
                db.flush()
        elif closed_reason is not None:
            listing.closed_reason = closed_reason or None
            db.flush()
        return listing  # idempotent repeat
    if to_status in ("AVAILABLE", "SOLD", "RENTED", "LEASED"):
        # Publishing and deal closes expose the listing (or its outcome);
        # only verified listings may take these paths.
        if listing.verification_status != "VERIFIED":
            raise ValueError(
                f"Only VERIFIED listings can move to {to_status} "
                f"(current verification: {listing.verification_status})"
            )
    listing.listing_status = to_status
    if to_status == "AVAILABLE":
        listing.closed_reason = None  # live listings carry no close note
    elif closed_reason is not None:
        listing.closed_reason = closed_reason or None
    db.flush()
    return listing


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
        .filter(
            PropertyListing.user_id == owner_id,
            PhysicalProperty.user_id == owner_id,
        )
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
        # Soft-removed listings keep their row (and history) and stay in the
        # owner's own reads; this flag lets clients tell them apart from
        # live ones without changing which rows are returned.
        "removed": listing.deleted_at is not None,
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
    """listing_id -> ordered [{id, url, category, order_index, is_cover}].

    Cover = index 0. ``id`` is the stable property_media key Edit mode
    needs for removals, cover selection, and ordering.
    """
    if not listing_ids:
        return {}
    rows = (
        db.query(PropertyMedia.id, PropertyMedia.property_listing_id,
                 PropertyMedia.url, PropertyMedia.category,
                 PropertyMedia.order_index)
        .filter(PropertyMedia.property_listing_id.in_(listing_ids))
        .order_by(PropertyMedia.property_listing_id, PropertyMedia.order_index, PropertyMedia.id)
        .all()
    )
    grouped: dict[int, list] = {}
    for media_id, listing_id, url, category, order_index in rows:
        grouped.setdefault(listing_id, []).append({
            "id": media_id, "url": url, "category": category,
            "order_index": order_index or 0,
            "is_cover": (order_index or 0) == 0,
        })
    return grouped


def list_owner_listings(
    db: Session, owner_id: int, page: int = 1, page_size: int = 20
) -> tuple[list[dict], int]:
    """List the caller's own listings (all statuses). Pure read: no writes."""
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
        counts = _activity_counts_map(db, owner_id, listing_ids)
        for c in cards:
            c.update(counts.get(c["listing_id"], {"enquiry_count": 0, "visit_count": 0}))

    return cards, total


def _activity_counts_map(db: Session, owner_id: int, listing_ids: list[int]) -> dict:
    """listing_id -> {enquiry_count, visit_count} for the owner's own rows.

    Counts every enquiry/visit ever recorded for the listing (all statuses),
    scoped by the stored owner_user_id — the same authority GET /owner/leads
    and GET /owner/visits use — so the cards agree with those lists. Two
    grouped queries per page; no N+1.
    """
    from sqlalchemy import func as _func

    from ..models import Enquiry, PropertyVisit

    if not listing_ids:
        return {}
    out = {lid: {"enquiry_count": 0, "visit_count": 0} for lid in listing_ids}
    for model, key in ((Enquiry, "enquiry_count"), (PropertyVisit, "visit_count")):
        rows = (
            db.query(model.property_listing_id, _func.count(model.id))
            .filter(model.owner_user_id == owner_id, model.property_listing_id.in_(listing_ids))
            .group_by(model.property_listing_id)
            .all()
        )
        for lid, n in rows:
            out[int(lid)][key] = int(n)
    return out


def get_owner_listing(db: Session, owner_id: int, listing_id: int) -> dict | None:
    """Single owned detail, or None when missing/not owned. Pure read: no writes."""
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
    card.update(_activity_counts_map(db, owner_id, [listing_id])[listing_id])
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


# ---------------------------------------------------------------------------
# Listing hard deletion
# ---------------------------------------------------------------------------


def delete_owner_listing(
    db: Session, owner_id: int, listing_id: int
) -> tuple[bool, list[str]]:
    """Soft-delete (remove) an owned listing: set deleted_at = now().

    Enforces dual-authority: both PropertyListing.user_id AND its
    physical_properties.user_id must equal the caller. Uses FOR UPDATE to
    serialize racing removals on the same row.

    Returns (True, []) when removed or already removed (idempotent);
    (False, []) when not found or not owned by the caller (router → 404 —
    no cross-owner leak).

    Does NOT touch Cloudinary media: media rows are retained for history
    and possible restoration. The listing row and all historical
    relationships (enquiries, visits, verifications, saved properties,
    audits) are preserved; only deleted_at is stamped.

    Flushes; the caller commits.
    """
    listing = _get_owned_listing_for_update(db, owner_id, listing_id)
    if listing is None:
        return False, []
    if listing.deleted_at is not None:
        # Already soft-removed: idempotent repeat, no error.
        return True, []
    listing.deleted_at = _utcnow_naive()
    db.flush()
    return True, []
