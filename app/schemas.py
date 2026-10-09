"""RESTAMP Phase 4 — request/response schemas (pydantic v2)."""
import re
from datetime import date, datetime, timedelta, timezone

from pydantic import BaseModel, Field, field_validator, model_validator

PHONE_PATTERN = r"^\+[1-9]\d{7,14}$"


class UserCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=255)
    avatar_url: str | None = Field(default=None, max_length=500)


class UserOut(BaseModel):
    id: int
    display_name: str
    avatar_url: str | None
    status: str


class UserUpdate(BaseModel):
    display_name: str = Field(min_length=1, max_length=255)

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, v: str) -> str:
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("display_name cannot be empty or whitespace-only")
        if len(trimmed) > 255:
            raise ValueError("display_name cannot exceed 255 characters")
        return trimmed


class RoleSet(BaseModel):
    role: str = Field(pattern="^(BUYER|OWNER|BROKER)$")


class RoleOut(BaseModel):
    user_id: int
    role: str


class OTPRequest(BaseModel):
    phone: str = Field(pattern=PHONE_PATTERN)


class OTPVerify(BaseModel):
    phone: str = Field(pattern=PHONE_PATTERN)
    code: str = Field(pattern=r"^\d{6}$")


class GoogleAuthIn(BaseModel):
    id_token: str = Field(min_length=1, max_length=4096)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user_id: int
    role: str | None


class MeOut(BaseModel):
    id: int
    display_name: str
    role: str | None
    providers: list[str]
    is_admin: bool


class AdminGrant(BaseModel):
    user_id: int


class ErrorOut(BaseModel):
    detail: str


# --- Buyer V1 public discovery (Phase 6B) -------------------------------------


class ListingCard(BaseModel):
    listing_id: int
    title: str
    price_paise: int
    price_period: str
    price_display: str
    transaction_type: str
    property_type: str
    construction_status: str | None
    description: str | None
    area_value: int | None
    area_unit: str | None
    bedrooms: int | None
    bathrooms: int | None
    address_line: str | None
    locality: str
    city: str
    pincode: str
    location_label: str
    cover_image_url: str | None


class ListingsResponse(BaseModel):
    items: list[ListingCard]
    page: int
    page_size: int
    total: int


class ListingDetail(ListingCard):
    gallery: list[str]
    verification_status: str
    listing_status: str


class ContactRevealOut(BaseModel):
    phone: str


class EnquiryCreate(BaseModel):
    property_listing_id: int
    message: str | None = Field(default=None, max_length=2000)


class EnquiryListingSnapshot(BaseModel):
    title: str
    price_paise: int
    price_period: str


class EnquiryOut(BaseModel):
    id: int
    property_listing_id: int
    message: str | None
    status: str
    closing_reason: str | None
    listing: EnquiryListingSnapshot


class EnquiriesResponse(BaseModel):
    items: list[EnquiryOut]
    page: int
    page_size: int
    total: int


# --- Owner V1 listing management (Phase 7) ------------------------------------

_TX_TYPES = {"BUY", "RESALE", "RENT", "LEASE"}
_PROP_TYPES = {"APARTMENT", "VILLA", "HOUSE", "COMMERCIAL", "PLOT", "AGRICULTURAL_LAND"}
_CONST_STATUSES = {"READY_TO_MOVE", "UNDER_CONSTRUCTION", "NEW_LAUNCH"}

# Closed vocabularies for the RENT form (labels verified against the
# 7-step frontend; canonical DB values on the right).
_FURNISHING_LABELS = {
    "unfurnished": "UNFURNISHED",
    "semi-furnished": "SEMI_FURNISHED",
    "semi furnished": "SEMI_FURNISHED",
    "furnished": "FURNISHED",
}
_POWER_LABELS = {"none": "NONE", "partial": "PARTIAL", "full": "FULL"}
_MAINT_PERIOD_LABELS = {"monthly": "MONTHLY", "quarterly": "QUARTERLY", "yearly": "YEARLY"}
_TENANT_LABELS = {"family": "FAMILY", "bachelor": "BACHELOR", "company": "COMPANY", "any": "ANY"}
_OWNERSHIP_LABELS = {
    "freehold": "FREEHOLD",
    "leasehold": "LEASEHOLD",
    "co-operative society": "COOPERATIVE_SOCIETY",
    "cooperative society": "COOPERATIVE_SOCIETY",
    "power of attorney": "POWER_OF_ATTORNEY",
}
# Canonical age bands use an en-dash; accept hyphen-minus equivalents too.
_AGE_BANDS = {"0–1 Year", "1–5 Years", "5–10 Years", "10+ Years"}
_OPEN_SIDES = {"1", "2", "3", "3+"}
_OVERLOOKING = {"Pool", "Park", "Club", "Main Road", "Sea Facing", "Others"}
_FACING = {"North", "South", "East", "West", "North-East", "North-West", "South-East", "South-West"}
_MEDIA_CATEGORIES = {"Living Room", "Bedroom", "Kitchen", "Bathroom", "Balcony", "Exterior", "Floor Plan"}

# Explicit month mappings for the frontend's duration chips.
_LOCK_IN_MONTHS = {"none": None, "6 months": 6, "1 year": 12, "2 years": 24}
_AGREEMENT_MONTHS = {"11 months": 11, "1 year": 12, "2 years": 24, "3 years": 36, "5 years": 60}

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def _normalize_label(value: str | None, mapping: dict[str, str | None], field: str) -> str | None:
    """Map a frontend chip label to its canonical DB value (case-insensitive)."""
    if value is None:
        return None
    key = " ".join(str(value).strip().split()).lower()
    if key in mapping:
        return mapping[key]
    raise ValueError(f"{field} must be one of {sorted(mapping)}")


def normalize_available_from(value: str | date | None) -> date | None:
    """Normalize frontend available-from labels to a DATE (UTC-based).

    Supported (verified against the 7-step form plus required aliases):
    Immediately/Today/Tomorrow/Within 1 Week/Within 15 Days/Within 1 Month,
    generic "Within N day(s)/week(s)/month(s)", "DD Mon YYYY" ("25 Sep 2026"),
    and ISO "YYYY-MM-DD". Anything else raises ValueError (→ 422).
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = " ".join(str(value).strip().split())
    if not s:
        return None
    low = s.lower()
    today = datetime.now(timezone.utc).date()
    if low in ("immediately", "today"):
        return today
    if low == "tomorrow":
        return today + timedelta(days=1)
    m = re.fullmatch(r"within\s+(\d+)\s+days?", low)
    if m:
        return today + timedelta(days=int(m.group(1)))
    m = re.fullmatch(r"within\s+(\d+)\s+weeks?", low)
    if m:
        return today + timedelta(weeks=int(m.group(1)))
    m = re.fullmatch(r"within\s+(\d+)\s+months?", low)
    if m:
        return today + timedelta(days=30 * int(m.group(1)))
    try:
        return date.fromisoformat(s)
    except ValueError:
        pass
    for fmt in ("%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(
        "available_from must be Immediately/Today/Tomorrow, "
        "'Within N days/weeks/months', 'DD Mon YYYY', or YYYY-MM-DD"
    )


def normalize_indian_phone(value: str | None) -> str | None:
    """Accept +91/91-prefixed or bare 10-digit Indian mobiles (6-9 start),
    plus generic +E.164 international numbers; returns canonical form."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        raise ValueError("contact_phone cannot be empty")
    has_plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if digits.startswith("91") and len(digits) == 12:
        core = digits[2:]
    elif len(digits) == 10:
        core = digits
    else:
        if has_plus and 6 <= len(digits) <= 15:
            return "+" + digits
        raise ValueError("Enter a valid phone number")
    if not re.fullmatch(r"[6-9]\d{9}", core):
        raise ValueError("Enter a valid 10-digit Indian mobile number")
    return "+91" + core


def _normalize_months(value: int | str | None, mapping: dict[str, int | None], field: str) -> int | None:
    """Accept integer months or an explicit frontend chip label."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field} must be months or a duration label")
    if isinstance(value, int):
        if value < 0:
            raise ValueError(f"{field} must be >= 0")
        return value
    key = " ".join(str(value).strip().split()).lower()
    if key in mapping:
        return mapping[key]
    if re.fullmatch(r"\d+", key):
        return int(key)
    raise ValueError(f"{field} must be months or one of {sorted(mapping)}")


class RentTermsIn(BaseModel):
    """RENT terms as rupees/labels; service converts to paise/months."""

    security_deposit_rupees: int | None = Field(default=None, ge=0)
    maintenance_rupees: int | None = Field(default=None, ge=0)
    maintenance_period: str | None = None
    is_negotiable: bool | None = None
    available_from: str | date | None = None
    tenant_preference: str | None = None
    lock_in_months: int | str | None = None
    agreement_months: int | str | None = None

    @field_validator("maintenance_period", mode="before")
    @classmethod
    def _period(cls, v):
        return _normalize_label(v, _MAINT_PERIOD_LABELS, "maintenance_period")

    @field_validator("tenant_preference", mode="before")
    @classmethod
    def _tenant(cls, v):
        return _normalize_label(v, _TENANT_LABELS, "tenant_preference")

    @field_validator("available_from", mode="before")
    @classmethod
    def _avail(cls, v):
        return normalize_available_from(v)

    @field_validator("lock_in_months", mode="before")
    @classmethod
    def _lockin(cls, v):
        return _normalize_months(v, _LOCK_IN_MONTHS, "lock_in_months")

    @field_validator("agreement_months", mode="before")
    @classmethod
    def _agreement(cls, v):
        return _normalize_months(v, _AGREEMENT_MONTHS, "agreement_months")

    def is_empty(self) -> bool:
        return all(
            getattr(self, f) is None
            for f in ("security_deposit_rupees", "maintenance_rupees", "maintenance_period",
                      "is_negotiable", "available_from", "tenant_preference",
                      "lock_in_months", "agreement_months")
        )


class ListingPhotoIn(BaseModel):
    """One photo: URL (legacy behavior) plus optional category for persistence."""

    url: str = Field(min_length=1, max_length=500)

    url: str = Field(min_length=1, max_length=500)
    category: str | None = Field(default=None, max_length=50)

    @field_validator("category", mode="before")
    @classmethod
    def _category(cls, v):
        if v is None:
            return None
        return normalize_media_category(v)


def normalize_media_category(value: str | None) -> str | None:
    """Canonicalize a photo category (case-insensitive); None stays None."""
    if value is None:
        return None
    key = " ".join(str(value).strip().split())
    if not key:
        return None
    for canonical in _MEDIA_CATEGORIES:
        if key.lower() == canonical.lower():
            return canonical
    raise ValueError(f"category must be one of {sorted(_MEDIA_CATEGORIES)}")


class OwnerListingCreate(BaseModel):
    """Payload sent by the owner when posting a property."""

    title: str = Field(min_length=1, max_length=255)
    transaction_type: str = Field(default="BUY")
    property_type: str = Field(default="APARTMENT")
    price_rupees: int = Field(ge=1)  # frontend sends rupees; we convert to paise
    price_period: str = Field(default="TOTAL")  # TOTAL | MONTHLY
    description: str | None = Field(default=None, max_length=5000)
    construction_status: str | None = None
    # Location (free-text; service does best-effort lookup/create)
    locality: str = Field(min_length=1, max_length=255)
    city: str = Field(min_length=1, max_length=255)
    district: str | None = Field(default=None, max_length=255)
    pincode: str = Field(pattern=r"^\d{6}$", max_length=10)
    address_line: str | None = Field(default=None, max_length=500)
    sub_locality: str | None = Field(default=None, max_length=255)
    society_name: str | None = Field(default=None, max_length=255)
    house_no: str | None = Field(default=None, max_length=100)
    landmark: str | None = Field(default=None, max_length=255)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    # Physical attributes
    area_value: int | None = Field(default=None, ge=1)
    area_unit: str | None = Field(default=None, max_length=20)
    bedrooms: int | None = Field(default=None, ge=0, le=99)
    bathrooms: int | None = Field(default=None, ge=0, le=99)
    balconies: int | None = Field(default=None, ge=0, le=99)
    built_up_area: int | None = Field(default=None, ge=1)
    super_built_up_area: int | None = Field(default=None, ge=1)
    total_floors: int | None = Field(default=None, ge=0)
    floor_on: str | None = Field(default=None, max_length=10)
    is_duplex: bool | None = None
    property_age_band: str | None = None
    furnishing: str | None = None
    covered_parking: int | None = Field(default=None, ge=0, le=99)
    open_parking: int | None = Field(default=None, ge=0, le=99)
    open_sides: str | None = Field(default=None, max_length=10)
    overlooking: str | None = Field(default=None, max_length=50)
    power_backup: str | None = None
    facing: str | None = Field(default=None, max_length=20)
    ownership_type: str | None = None
    # Per-listing contact overrides (identity stays in auth_identities)
    contact_phone: str | None = Field(default=None, max_length=20)
    contact_email: str | None = Field(default=None, max_length=255)
    # RENT terms (only persisted for RENT listings; see service)
    rent_terms: RentTermsIn | None = None
    # Amenity/feature/room NAMES (never client IDs; resolved server-side)
    amenities: list[str] = Field(default_factory=list)
    property_features: list[str] = Field(default_factory=list)
    other_rooms: list[str] = Field(default_factory=list)
    # Media URLs (legacy) + per-photo metadata (category persisted)
    images: list[str] = Field(default_factory=list)
    photos: list[ListingPhotoIn] | None = None

    @field_validator("transaction_type")
    @classmethod
    def validate_tx(cls, v: str) -> str:
        u = v.upper()
        if u not in _TX_TYPES:
            raise ValueError(f"transaction_type must be one of {_TX_TYPES}")
        return u

    @field_validator("property_type")
    @classmethod
    def validate_prop(cls, v: str) -> str:
        u = v.upper()
        if u not in _PROP_TYPES:
            raise ValueError(f"property_type must be one of {_PROP_TYPES}")
        return u

    @field_validator("price_period")
    @classmethod
    def validate_period(cls, v: str) -> str:
        u = v.upper()
        if u not in {"TOTAL", "MONTHLY"}:
            raise ValueError("price_period must be TOTAL or MONTHLY")
        return u

    @field_validator("construction_status")
    @classmethod
    def validate_const(cls, v: str | None) -> str | None:
        if v is None:
            return None
        u = v.upper()
        if u not in _CONST_STATUSES:
            raise ValueError(f"construction_status must be one of {_CONST_STATUSES}")
        return u

    @field_validator("district", mode="before")
    @classmethod
    def _district(cls, v):
        if v is None:
            return None
        s = str(v).strip()
        if not s:
            raise ValueError("district cannot be empty; omit it to fall back to city")
        return s

    @field_validator("furnishing", mode="before")
    @classmethod
    def _furnishing(cls, v):
        return _normalize_label(v, _FURNISHING_LABELS, "furnishing")

    @field_validator("power_backup", mode="before")
    @classmethod
    def _power(cls, v):
        return _normalize_label(v, _POWER_LABELS, "power_backup")

    @field_validator("ownership_type", mode="before")
    @classmethod
    def _ownership(cls, v):
        return _normalize_label(v, _OWNERSHIP_LABELS, "ownership_type")

    @field_validator("property_age_band", mode="before")
    @classmethod
    def _age_band(cls, v):
        if v is None:
            return None
        key = " ".join(str(v).strip().split()).lower().replace("-", "–")
        for canonical in _AGE_BANDS:
            if key == canonical.lower():
                return canonical
        raise ValueError(f"property_age_band must be one of {sorted(_AGE_BANDS)}")

    @field_validator("open_sides", mode="before")
    @classmethod
    def _open_sides(cls, v):
        if v is None:
            return None
        s = str(v).strip()
        if s not in _OPEN_SIDES:
            raise ValueError(f"open_sides must be one of {sorted(_OPEN_SIDES)}")
        return s

    @field_validator("overlooking", mode="before")
    @classmethod
    def _overlooking(cls, v):
        if v is None:
            return None
        s = " ".join(str(v).strip().split())
        if s not in _OVERLOOKING:
            raise ValueError(f"overlooking must be one of {sorted(_OVERLOOKING)}")
        return s

    @field_validator("facing", mode="before")
    @classmethod
    def _facing(cls, v):
        if v is None:
            return None
        s = " ".join(str(v).strip().split())
        for canonical in _FACING:
            if s.lower() == canonical.lower():
                return canonical
        raise ValueError(f"facing must be one of {sorted(_FACING)}")

    @field_validator("contact_phone", mode="before")
    @classmethod
    def _phone(cls, v):
        return normalize_indian_phone(v)

    @field_validator("contact_email", mode="before")
    @classmethod
    def _email(cls, v):
        if v is None:
            return None
        s = str(v).strip()
        if not s:
            raise ValueError("contact_email cannot be empty")
        if not _EMAIL_RE.fullmatch(s):
            raise ValueError("Enter a valid email address")
        return s

    @model_validator(mode="after")
    def _floor_within_building(self):
        total = self.total_floors
        floor = (self.floor_on or "").strip() if self.floor_on else ""
        if total is not None and re.fullmatch(r"\d+", floor) and int(floor) > total:
            raise ValueError("floor_on cannot exceed total_floors")
        return self


class RentTermsOut(BaseModel):
    """Persisted RENT terms (paise/months/DATE — canonical DB form)."""

    security_deposit_paise: int | None = None
    maintenance_paise: int | None = None
    maintenance_period: str | None = None
    is_negotiable: bool = False
    available_from: date | None = None
    tenant_preference: str | None = None
    lock_in_months: int | None = None
    agreement_months: int | None = None


class ListingPhotoOut(BaseModel):
    """One gallery photo with its persisted category (cover = index 0)."""

    url: str
    category: str | None = None


class OwnerListingOut(BaseModel):
    """A listing row visible to its owner (all statuses, no enquiry data)."""

    listing_id: int
    title: str
    transaction_type: str
    property_type: str
    price_paise: int
    price_period: str
    price_display: str
    construction_status: str | None
    verification_status: str
    listing_status: str
    description: str | None
    locality: str
    city: str
    district: str = ""
    pincode: str
    address_line: str | None
    sub_locality: str | None = None
    society_name: str | None = None
    house_no: str | None = None
    landmark: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    area_value: int | None
    area_unit: str | None
    bedrooms: int | None
    bathrooms: int | None
    balconies: int | None = None
    built_up_area: int | None = None
    super_built_up_area: int | None = None
    total_floors: int | None = None
    floor_on: str | None = None
    is_duplex: bool = False
    property_age_band: str | None = None
    furnishing: str | None = None
    covered_parking: int | None = None
    open_parking: int | None = None
    open_sides: str | None = None
    overlooking: str | None = None
    power_backup: str | None = None
    facing: str | None = None
    ownership_type: str | None = None
    contact_phone: str | None = None
    contact_email: str | None = None
    submitted_at: datetime | None = None
    rent_terms: RentTermsOut | None = None
    amenities: list[str] = Field(default_factory=list)
    property_features: list[str] = Field(default_factory=list)
    other_rooms: list[str] = Field(default_factory=list)
    cover_image_url: str | None
    gallery: list[str] = Field(default_factory=list)
    photos: list[ListingPhotoOut] = Field(default_factory=list)


class OwnerListingsResponse(BaseModel):
    items: list[OwnerListingOut]
    page: int
    page_size: int
    total: int


class PhotoUploadOut(BaseModel):
    """One uploaded listing photo (cover = order_index 0)."""

    id: int
    url: str
    category: str | None = None
    mime_type: str | None = None
    byte_size: int | None = None
    order_index: int
    is_cover: bool


class DraftCreate(BaseModel):
    """Save a new owner draft. form_data is the complete 7-step form snapshot
    (all values optional — drafts may be partial)."""

    transaction_type: str = Field(default="RENT")
    title: str | None = Field(default=None, max_length=255)
    current_step: int = Field(default=1, ge=1, le=7)
    form_data: dict | None = None

    @field_validator("transaction_type", mode="before")
    @classmethod
    def _tx(cls, v):
        if v is None:
            return "RENT"
        u = str(v).strip().upper()
        if u not in _TX_TYPES:
            raise ValueError(f"transaction_type must be one of {_TX_TYPES}")
        return u


class DraftUpdate(BaseModel):
    """Replace a draft's snapshot (full replace, not merge). All optional."""

    transaction_type: str | None = None
    title: str | None = Field(default=None, max_length=255)
    current_step: int | None = Field(default=None, ge=1, le=7)
    form_data: dict | None = None

    @field_validator("transaction_type", mode="before")
    @classmethod
    def _tx(cls, v):
        if v is None:
            return None
        u = str(v).strip().upper()
        if u not in _TX_TYPES:
            raise ValueError(f"transaction_type must be one of {_TX_TYPES}")
        return u


class DraftOut(BaseModel):
    """One owner draft with its full form snapshot."""

    id: int
    transaction_type: str
    title: str | None = None
    current_step: int
    form_data: dict | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class DraftListOut(BaseModel):
    items: list[DraftOut]
    total: int
