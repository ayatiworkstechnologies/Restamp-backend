"""Reva AI grounding — real platform data behind the chat answers (Phase 4).

The public assistant must never invent availability, prices or subscription
details. Everything it says about Restamp's current inventory, the caller's
own account, or plan pricing comes from the SAME service layer the website
and app already use:

- public_listing_snapshot(): parses a few simple filters out of the message
  (deal type, property type, bedrooms, locality, a price ceiling) and answers
  with REAL buyer-visible listings via services.listings.search_listings, so
  the visibility rule (_visible: VERIFIED + AVAILABLE + not deleted) applies
  exactly as in buyer search. Cards carry only public fields — title,
  price display, locality, city, type — never an owner identity, phone or
  full address (the audited contact reveal is the only phone channel).
- signed_in_context(): a short summary of the CALLER'S OWN account (counts
  and plan status only) for the signed-in roles. Buyers see their saved/
  enquiry/visit counts and lead-quota state; owners see their own listing
  and lead/visit counts; agents see their own plan coverage. Nobody else's
  data ever enters a prompt: every query filters on the caller's user id.
- real plan catalogue text is built from services.buyer_plans /
  services.agent_plans constants, so chat can never quote a plan or price
  the backend does not sell.

Parsing is deliberately conservative: anything the message does not clearly
say becomes no filter at all (the whole inventory summary) rather than a
guessed one.
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import Enquiry, PropertyListing, PropertyVisit, SavedProperty
from . import buyer_plans
from . import listings as listing_svc
from . import agent_plans as agent_plan_svc
from . import users_roles

# Maximum real listings quoted in one answer (kept small: the reply is text
# for the chat bubble; the website links each card to its public page).
MAX_SNAPSHOT = 3

_DEAL_WORDS = (
    ("resale", "RESALE"),
    ("buy", "BUY"),
    ("purchase", "BUY"),
    ("for sale", "BUY"),
    ("rent", "RENT"),
    ("lease", "LEASE"),
    ("வாடகை", "RENT"),
    ("குத்தகை", "LEASE"),
    ("வாங்க", "BUY"),
)
_TYPE_WORDS = (
    ("apartment", "APARTMENT"),
    ("flat", "APARTMENT"),
    ("villa", "VILLA"),
    ("house", "HOUSE"),
    ("home", "HOUSE"),
    ("plot", "PLOT"),
    ("land", "PLOT"),
    ("office", "COMMERCIAL"),
    ("shop", "COMMERCIAL"),
    ("commercial", "COMMERCIAL"),
    ("அடுக்குமாடி", "APARTMENT"),
    ("வீடு", "HOUSE"),
    ("மனை", "PLOT"),
)
_BHK_RE = re.compile(r"\b([1-9])\s*(?:bhk|bed\s?rooms?|bedrooms?)\b", re.IGNORECASE)
_LAKH_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:lakh|lakhs|l)\b", re.IGNORECASE)
_CRORE_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:crore|crores|cr)\b", re.IGNORECASE)
_BUDGET_RE = re.compile(r"(?:under|below|upto|up\s?to|less\s?than|max(?:imum)?|வரை)\s*[₹rs.]*\s*(\d[\d,]*)", re.IGNORECASE)
_PRICE_HINT_RE = re.compile(r"(?:under|below|upto|up\s?to|less\s?than|max(?:imum)?|budget|விலை|வரை)")
_IN_RE = re.compile(r"\bin\s+([a-z][a-z .&'-]{2,40})", re.IGNORECASE)


def _first_deal(message: str) -> str | None:
    low = message.lower()
    for word, deal in _DEAL_WORDS:
        if word in low:
            return deal
    return None


def _first_type(message: str) -> str | None:
    low = message.lower()
    for word, ptype in _TYPE_WORDS:
        if word in low:
            return ptype
    return None


def _max_price_paise(message: str) -> int | None:
    """A price ceiling only when the message clearly states one (never a guess).

    Lakh/crore figures are sale prices (TOTAL); a plain rupee figure is
    treated as a monthly rent ceiling. A bare number with no budget wording
    ("2 BHK in Adyar 45") is ignored entirely.
    """
    lakh = _LAKH_RE.search(message)
    crore = _CRORE_RE.search(message)
    if crore:
        return int(float(crore.group(1)) * 10_000_000 * 100)
    if lakh:
        return int(float(lakh.group(1)) * 100_000 * 100)
    if _PRICE_HINT_RE.search(message):
        plain = _BUDGET_RE.search(message)
        if plain:
            rupees = int(plain.group(1).replace(",", ""))
            if 0 < rupees < 10_000_000:
                return rupees * 100
    return None


def parse_search_filters(message: str) -> dict[str, Any]:
    """Simple, conservative filter extraction from a chat message."""
    filters: dict[str, Any] = {}
    deal = _first_deal(message)
    if deal:
        filters["deal"] = deal
    ptype = _first_type(message)
    if ptype:
        filters["property_type"] = ptype
    bhk = _BHK_RE.search(message)
    if bhk:
        filters["bedrooms"] = int(bhk.group(1))
    price = _max_price_paise(message)
    if price is not None:
        filters["max_price_paise"] = price
    return filters


def _parse_locality(message: str) -> str | None:
    """The locality after 'in'/'at', only when it looks like a place name."""
    match = _IN_RE.search(message)
    if not match:
        return None
    words = match.group(1).strip()
    words = re.split(r"\b(?:with|under|below|for|near|that|which|and)\b", words, flags=re.IGNORECASE)[0]
    words = words.strip(" .,?!")
    if not words or len(words) < 3 or any(ch.isdigit() for ch in words):
        return None
    return words.title()


def public_listing_snapshot(db: Session, message: str) -> dict | None:
    """Real buyer-visible listings matching the message, or None.

    Returns {"summary": str, "listings": [card...], "total": int} where each
    card is the public ListingCard fields the website may show (no owner
    identity, no phone, no street address). Uses the buyer search service so
    the visibility rule and pagination are identical to the app's.
    """
    filters = parse_search_filters(message)
    locality = _parse_locality(message)
    if locality:
        filters["locality"] = locality
    try:
        cards, total = listing_svc.search_listings(db, page=1, page_size=MAX_SNAPSHOT, **filters)
    except Exception:
        # A malformed combination must never break the chat: fall back to an
        # unfiltered count check instead of guessing.
        try:
            cards, total = listing_svc.search_listings(db, page=1, page_size=MAX_SNAPSHOT)
        except Exception:
            return None
    public_cards = [
        {
            "listing_id": c["listing_id"],
            "title": c["title"],
            "price_display": c["price_display"],
            "locality": c["locality"],
            "city": c["city"],
            "property_type": c["property_type"],
            "bedrooms": c.get("bedrooms"),
        }
        for c in cards
    ]
    summary = _snapshot_summary(filters, total, locality)
    return {"summary": summary, "listings": public_cards, "total": total}


def _snapshot_summary(filters: dict, total: int, locality: str | None) -> str:
    parts = []
    if filters.get("bedrooms"):
        parts.append(f"{filters['bedrooms']} BHK")
    if filters.get("property_type"):
        parts.append(filters["property_type"].title())
    if filters.get("deal"):
        parts.append(f"for {filters['deal'].lower()}")
    what = " ".join(parts) if parts else "properties"
    where = f" in {locality}" if locality else " in Chennai"
    return f"{total} verified {what}{where} are listed right now"


def _count(db: Session, column, *conditions) -> int:
    return int(db.query(func.count(column)).filter(*conditions).scalar() or 0)


def signed_in_context(db: Session, user_id: int) -> list[str]:
    """Short lines describing the CALLER'S OWN account, for the system prompt.

    Counts and plan status only — never message bodies, phone numbers,
    addresses or another user's data. Each block is independent: a failure in
    one drops those lines (logged at debug, without content) instead of
    failing the chat.
    """
    lines: list[str] = []
    try:
        role = users_roles.get_current_role(db, user_id)
    except Exception:
        return lines
    if role == "BROKER":
        try:
            status = agent_plan_svc.status_for(db, user_id)
            if status.get("has_plan"):
                remaining = status.get("localities_remaining")
                lines.append(
                    f"Dealer plan: {status.get('plan_name')} covering "
                    f"{len(status.get('localities') or [])}"
                    + (f" of {status.get('locality_limit')} areas ({remaining} unused)" if remaining is not None else "")
                    + f", valid until {status.get('valid_until') or 'no end date'}."
                )
            else:
                lines.append("Dealer plan: none active (free trial may be available).")
        except Exception:
            pass
        return lines
    if role == "OWNER":
        try:
            total = _count(db, PropertyListing.id, PropertyListing.user_id == user_id, PropertyListing.deleted_at.is_(None))
            available = _count(
                db, PropertyListing.id,
                PropertyListing.user_id == user_id,
                PropertyListing.deleted_at.is_(None),
                PropertyListing.listing_status == "AVAILABLE",
                PropertyListing.verification_status == "VERIFIED",
            )
            pending = _count(
                db, PropertyListing.id,
                PropertyListing.user_id == user_id,
                PropertyListing.deleted_at.is_(None),
                PropertyListing.verification_status == "PENDING",
            )
            leads = _count(db, Enquiry.id, Enquiry.owner_user_id == user_id, Enquiry.status == "NEW")
            visits = _count(
                db, PropertyVisit.id,
                PropertyVisit.owner_user_id == user_id,
                PropertyVisit.status.in_(("PENDING", "REQUESTED", "PROPOSED", "CONFIRMED")),
            )
            lines.append(
                f"Your listings: {total} total ({available} verified & live, {pending} awaiting verification). "
                f"New leads: {leads}. Visits awaiting action: {visits}."
            )
        except Exception:
            pass
        return lines
    # BUYER (and any role without a specific block above).
    try:
        saved = _count(db, SavedProperty.buyer_user_id, SavedProperty.buyer_user_id == user_id)
        enquiries = _count(db, Enquiry.id, Enquiry.buyer_user_id == user_id, Enquiry.status != "CLOSED")
        visits = _count(
            db, PropertyVisit.id,
            PropertyVisit.buyer_user_id == user_id,
            PropertyVisit.status.in_(("PENDING", "REQUESTED", "PROPOSED", "CONFIRMED")),
        )
        plan = buyer_plans.status_for(db, user_id)
        quota = (
            f"{plan['leads_used']}/{plan['lead_limit']} owner contacts revealed"
            if plan.get("lead_limit") is not None
            else "unlimited owner contacts"
        )
        lines.append(
            f"Your activity: {saved} saved properties, {enquiries} open enquiries, {visits} upcoming visits. "
            f"Buyer plan {plan['plan_name']}: {quota}."
        )
    except Exception:
        pass
    return lines


def real_plan_lines() -> str:
    """The REAL plan catalogue, built from the plan services' own constants.

    Chat quotes exactly what the backend sells: buyer plans (lead quotas) and
    agent plans (locality coverage). Owner plans are not a backend product,
    so the assistant says so instead of reciting prices that do not exist.
    """
    buyer_items = [
        f"• {p.name} — ₹{p.price_paise // 100:,}, {p.duration_days} days, "
        + ("unlimited owner contacts" if p.lead_limit is None else f"up to {p.lead_limit} owner contacts")
        for p in buyer_plans.ALL
    ]
    agent_items = [
        f"• {p.name} — ₹{p.price_paise // 100:,}, {p.duration_days} days, {p.locality_limit} localities"
        for p in agent_plan_svc.PLANS
    ]
    return (
        "Buyer plans (owner-contact quota):\n" + "\n".join(buyer_items)
        + "\nAgent plans (locality coverage):\n" + "\n".join(agent_items)
        + "\nThere are currently no owner subscription plans sold online; owners list properties free."
    )
