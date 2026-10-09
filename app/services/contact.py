"""RESTAMP Buyer V1 — audited contact reveal (Phase 6B-4).

Owner authority: property_listings -> physical_properties.user_id (NEVER
property_listings.user_id). Phone authority: auth_identities phone identity.
Every successful reveal inserts exactly one contact_reveal_audits row;
failed/invisible lookups return before any write, leaving no audit rows.
"""
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from ..models import AuthIdentity, ContactRevealAudit, PhysicalProperty, PropertyListing


def reveal_contact(db: Session, buyer_id: int, listing_id: int) -> tuple[str, object]:
    """Return (owner phone, audit row). Raises 404 for invisible/uncontactable listings."""
    listing = (
        db.query(PropertyListing)
        .filter(
            PropertyListing.id == listing_id,
            PropertyListing.verification_status == "VERIFIED",
            PropertyListing.listing_status == "AVAILABLE",
        )
        .first()
    )
    if listing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    owner_id = db.get(PhysicalProperty, listing.physical_property_id).user_id
    phone_row = (
        db.query(AuthIdentity)
        .filter(AuthIdentity.user_id == owner_id, AuthIdentity.provider == "phone")
        .first()
    )
    if phone_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    audit = ContactRevealAudit(user_id=buyer_id, property_listing_id=listing_id)
    db.add(audit)
    db.flush()
    return phone_row.provider_identifier, audit
