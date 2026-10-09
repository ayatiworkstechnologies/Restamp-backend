"""RESTAMP Buyer V1 — public listing read queries (Phases 6B-1/6B-2/6B-3).

Visibility (VERIFIED + AVAILABLE) is enforced inside every builder here, never by callers.
Reads use explicit column lists only (no SELECT *); cover media is fetched in ONE batched
query (no N+1); private data (phones, identities, owners, payments, audits) is never selected.
"""
from sqlalchemy import asc, desc, func, or_
from sqlalchemy.orm import Session

from ..models import (
    City,
    District,
    Locality,
    PhysicalProperty,
    Postcode,
    PropertyListing,
    PropertyMedia,
    Taluk,
    Village,
)

MAX_PAGE_SIZE = 100


def format_price(price_paise: int, period: str) -> str:
    """Deterministic INR display: paise -> Cr/L/count + /mo for MONTHLY."""
    rupees = price_paise // 100
    if rupees >= 10_000_000:
        main = f"₹{rupees / 10_000_000:.2f} Cr"
    elif rupees >= 100_000:
        main = f"₹{rupees / 100_000:.2f} L"
    else:
        main = f"₹{rupees:,}"
    return f"{main}/mo" if period == "MONTHLY" else main


def _location_joins(q):
    return (
        q.join(PhysicalProperty, PropertyListing.physical_property_id == PhysicalProperty.id)
        .join(Postcode, PhysicalProperty.postcode_id == Postcode.id)
        .join(Locality, Postcode.locality_id == Locality.id)
        .join(Village, Locality.village_id == Village.id)
        .join(Taluk, Village.taluk_id == Taluk.id)
        .join(District, Taluk.district_id == District.id)
        .join(City, District.city_id == City.id)
    )


def _visible(q):
    return q.filter(
        PropertyListing.verification_status == "VERIFIED",
        PropertyListing.listing_status == "AVAILABLE",
    )


def apply_listing_filters(
    q,
    deal: str | None = None,
    property_type: str | None = None,
    locality: str | None = None,
    city: str | None = None,
    bedrooms: int | None = None,
    bedrooms_min: int | None = None,
    min_price_paise: int | None = None,
    max_price_paise: int | None = None,
    query: str | None = None,
):
    if deal is not None:
        q = q.filter(PropertyListing.transaction_type == deal)
    if property_type is not None:
        q = q.filter(PropertyListing.property_type == property_type)
    if locality is not None:
        q = q.filter(Locality.name.ilike(f"%{locality}%"))
    if city is not None:
        q = q.filter(City.name.ilike(f"%{city}%"))
    if bedrooms is not None:
        q = q.filter(PhysicalProperty.bedrooms == bedrooms)
    if bedrooms_min is not None:
        q = q.filter(PhysicalProperty.bedrooms >= bedrooms_min)
    if min_price_paise is not None:
        q = q.filter(PropertyListing.price_paise >= min_price_paise)
    if max_price_paise is not None:
        q = q.filter(PropertyListing.price_paise <= max_price_paise)
    if query is not None:
        like = f"%{query}%"
        q = q.filter(
            or_(
                PropertyListing.title.ilike(like),
                PropertyListing.description.ilike(like),
                Locality.name.ilike(like),
            )
        )
    return q


def _card_row(row) -> dict:
    (listing, address_line, area_value, area_unit, bedrooms, bathrooms, locality, city, pincode) = row
    return {
        "listing_id": listing.id,
        "title": listing.title,
        "price_paise": listing.price_paise,
        "price_period": listing.price_period,
        "price_display": format_price(listing.price_paise, listing.price_period),
        "transaction_type": listing.transaction_type,
        "property_type": listing.property_type,
        "construction_status": listing.construction_status,
        "description": listing.description,
        "area_value": area_value,
        "area_unit": area_unit,
        "bedrooms": bedrooms,
        "bathrooms": bathrooms,
        "address_line": address_line,
        "locality": locality,
        "city": city,
        "pincode": pincode,
        "location_label": f"{locality}, {city} {pincode}",
        "cover_image_url": None,  # filled by attach_covers()
    }


_CARD_COLUMNS = (
    PropertyListing,
    PhysicalProperty.address_line,
    PhysicalProperty.area_value,
    PhysicalProperty.area_unit,
    PhysicalProperty.bedrooms,
    PhysicalProperty.bathrooms,
    Locality.name,
    City.name,
    Postcode.pincode,
)


def fetch_cover_map(db: Session, listing_ids: list[int]) -> dict[int, str]:
    """One batched query: first media (order_index, id) per listing."""
    if not listing_ids:
        return {}
    rows = (
        db.query(PropertyMedia.property_listing_id, PropertyMedia.url)
        .filter(PropertyMedia.property_listing_id.in_(listing_ids))
        .order_by(PropertyMedia.order_index, PropertyMedia.id)
        .all()
    )
    covers: dict[int, str] = {}
    for listing_id, url in rows:
        covers.setdefault(listing_id, url)
    return covers


def attach_covers(cards: list[dict], covers: dict[int, str]) -> list[dict]:
    for card in cards:
        card["cover_image_url"] = covers.get(card["listing_id"])
    return cards


def apply_sort(q, sort: str):
    if sort == "price_asc":
        return q.order_by(asc(PropertyListing.price_paise), desc(PropertyListing.id))
    if sort == "price_desc":
        return q.order_by(desc(PropertyListing.price_paise), desc(PropertyListing.id))
    if sort == "area_asc":
        return q.order_by(asc(PhysicalProperty.area_value), desc(PropertyListing.id))
    if sort == "area_desc":
        return q.order_by(desc(PhysicalProperty.area_value), desc(PropertyListing.id))
    return q.order_by(desc(PropertyListing.id))  # relevance: newest first, deterministic


def search_listings(db: Session, page: int, page_size: int, **filters) -> tuple[list[dict], int]:
    """Paged cards + total. Returns (cards_without_covers, total)."""
    sort = filters.pop("sort", "relevance")
    base = _visible(_location_joins(db.query(*_CARD_COLUMNS)))
    base = apply_listing_filters(base, **filters)
    total = base.with_entities(func.count(func.distinct(PropertyListing.id))).scalar() or 0
    rows = (
        apply_sort(base, sort)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return [_card_row(r) for r in rows], total


def get_listing_detail(db: Session, listing_id: int) -> dict | None:
    """Full public detail + ordered gallery, or None when not publicly visible."""
    row = (
        _visible(_location_joins(db.query(*_CARD_COLUMNS)))
        .filter(PropertyListing.id == listing_id)
        .first()
    )
    if row is None:
        return None
    (listing, *_rest) = row
    gallery = [
        r[0]
        for r in db.query(PropertyMedia.url)
        .filter(PropertyMedia.property_listing_id == listing_id)
        .order_by(PropertyMedia.order_index, PropertyMedia.id)
        .all()
    ]
    card = _card_row(row)
    card["gallery"] = gallery
    card["verification_status"] = listing.verification_status
    card["listing_status"] = listing.listing_status
    return card


def find_similar(db: Session, listing_id: int, limit: int = 5) -> list[dict] | None:
    """Deterministic similarity over authoritative fields only (no ML/AI).

    Candidate pool: VERIFIED + AVAILABLE listings sharing the source's
    property_type and transaction_type. Ranking: relative price proximity,
    then area proximity, then id — fully deterministic (stable sort over a
    total key order). Locality is NOT a ranking factor (pool membership is
    type+deal only). Returns None when the source is not publicly visible.
    """
    source = get_listing_detail(db, listing_id)
    if source is None:
        return None
    price, area = source["price_paise"], source["area_value"]

    def base_pool():
        return (
            _visible(_location_joins(db.query(*_CARD_COLUMNS)))
            .filter(
                PropertyListing.id != listing_id,
                PropertyListing.property_type == source["property_type"],
                PropertyListing.transaction_type == source["transaction_type"],
            )
        )

    pool = base_pool().filter(Locality.name == source["locality"]).all()
    pool += base_pool().filter(Locality.name != source["locality"]).all()

    def proximity(row) -> tuple:
        listing = row[0]
        dp = abs(listing.price_paise - price) / max(price, 1)
        da = 0.0 if area is None else abs((row[2] or 0) - area) / max(area, 1)
        return (dp, da, listing.id)

    pool.sort(key=proximity)
    chosen = pool[:limit]
    cards = [_card_row(r) for r in chosen]
    return attach_covers(cards, fetch_cover_map(db, [c["listing_id"] for c in cards]))
