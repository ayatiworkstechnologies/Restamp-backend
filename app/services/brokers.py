"""RESTAMP Agent (role BROKER) foundation: profile, coverage, dashboard, browsing.

The Agent role is the existing BROKER role (users_roles.USER_ROLES); there is
no separate agent table. What an agent may see is decided ONLY by the
existing broker_postcode_access_current rows (design V2.4.2 §24): a grant
names one postcode, who granted it, and an optional expiry. Invariants:

- Coverage is server data, never client input. Self-switching to BROKER
  (POST /users/me/role, unchanged) grants no coverage, so a new agent sees
  zero locations and zero properties until a grant exists. No endpoint here
  creates, extends or removes grants.
- A grant counts while expires_at is NULL or in the future (naive UTC, like
  every app-written timestamp).
- Browsing reuses the buyer search (listings.search_listings) restricted to
  the covered postcodes, so buyer visibility (_visible: VERIFIED + AVAILABLE
  + not deleted) and every existing filter apply unchanged. No covered
  postcodes -> no rows (never "unrestricted").
- Nothing is invented. Facts with no backend source today are named in
  ``unavailable``: the dashboard returns its unsupported metrics (coverage
  limit, subscription, new leads, earnings) as null fields; the profile has
  no fields for verification or subscription at all and only lists them.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..models import (
    BrokerPostcodeAccessCurrent,
    City,
    District,
    Locality,
    Postcode,
    Taluk,
    Village,
)
from . import listings as listing_svc
from . import users_roles

# Dashboard/profile facts the backend cannot answer yet. Returned verbatim so
# the client can show "not available" instead of a number.
UNAVAILABLE_DASHBOARD = ("coverage_limit", "subscription", "new_leads", "earnings")
UNAVAILABLE_PROFILE = ("verification", "subscription")


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _grant_is_active():
    """SQL condition for an unexpired grant: no expiry, or an expiry strictly
    after the current UTC time (a grant expiring exactly now is inactive).
    The single definition every coverage query uses."""
    return or_(
        BrokerPostcodeAccessCurrent.expires_at.is_(None),
        BrokerPostcodeAccessCurrent.expires_at > _utcnow_naive(),
    )


def _active_grants(db: Session, broker_id: int):
    return db.query(BrokerPostcodeAccessCurrent).filter(
        BrokerPostcodeAccessCurrent.broker_user_id == broker_id,
        _grant_is_active(),
    )


def active_postcode_ids(db: Session, broker_id: int) -> list[int]:
    """Postcode ids the broker currently covers (unexpired grants only)."""
    return sorted(int(g.postcode_id) for g in _active_grants(db, broker_id).all())


def list_covered_locations(db: Session, broker_id: int) -> list[dict]:
    """The broker's own unexpired grants with pincode/locality/city names,
    ordered by pincode. Only the caller's rows are ever read."""
    rows = (
        db.query(
            BrokerPostcodeAccessCurrent.postcode_id,
            BrokerPostcodeAccessCurrent.expires_at,
            Postcode.pincode,
            Locality.name.label("locality"),
            City.name.label("city"),
        )
        .join(Postcode, BrokerPostcodeAccessCurrent.postcode_id == Postcode.id)
        .join(Locality, Postcode.locality_id == Locality.id)
        .join(Village, Locality.village_id == Village.id)
        .join(Taluk, Village.taluk_id == Taluk.id)
        .join(District, Taluk.district_id == District.id)
        .join(City, District.city_id == City.id)
        .filter(
            BrokerPostcodeAccessCurrent.broker_user_id == broker_id,
            _grant_is_active(),
        )
        .order_by(Postcode.pincode)
        .all()
    )
    return [
        {
            "postcode_id": int(r.postcode_id),
            "pincode": r.pincode,
            "locality": r.locality,
            "city": r.city,
            # Naive UTC in the DB (the same rule _active_grants compares
            # with); returned as explicit UTC. granted_at is deliberately not
            # returned: it is a MySQL-server-clock default, not app-set UTC.
            "expires_at": r.expires_at.replace(tzinfo=timezone.utc) if r.expires_at else None,
        }
        for r in rows
    ]


def get_profile(db: Session, broker_id: int) -> dict:
    user = users_roles.get_user(db, broker_id)
    return {
        "user_id": user.id,
        "display_name": user.display_name,
        "role": "BROKER",
        "covered_location_count": len(active_postcode_ids(db, broker_id)),
        "unavailable": list(UNAVAILABLE_PROFILE),
    }


def get_dashboard(db: Session, broker_id: int) -> dict:
    ids = active_postcode_ids(db, broker_id)
    _, available = listing_svc.search_listings(db, page=1, page_size=0, postcode_ids=ids)
    return {
        "available_property_count": available,
        "covered_location_count": len(ids),
        "coverage_limit": None,
        "subscription": None,
        "new_lead_count": None,
        "earnings": None,
        "unavailable": list(UNAVAILABLE_DASHBOARD),
    }
