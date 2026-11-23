from __future__ import annotations
# RESTAMP Phase 1 — SQLAlchemy DB Models (approved architecture only)
# Design-only upstream: RESTAMP_DATABASE_ARCHITECTURE_V2.4.2_FINAL.md
# No APIs, auth flows, payments, business services, AI, chat, site visits,
# negotiation, broker push/feed, or extra tables/columns created.
from .users import User
from .auth import AuthIdentity
from .roles import UserCurrentRole, RoleHistory, AdminAccount
from .locations import City, District, Taluk, Village, Locality, Postcode, LocalityAlias
from .contact_credits import ContactCreditAccount, ContactCreditLedger, PropertyContactUnlock
from .billing import Plan, PlanVersion, Payment, PaymentRefund, PaymentWebhookEvent, Subscription, Entitlement
from .properties import PhysicalProperty, PropertyListing, Verification, PropertyMedia, PropertyDocument, RentTerms, AmenityMaster, ListingAmenity, ListingDraft
from .enquiries import Enquiry, ActiveEnquiry, EnquiryMessage, LeadNote
from .visits import PropertyVisit
from .brokers import BrokerPostcodeAccessCurrent, BrokerPostcodeAccessHistory
from .logs import ContactRevealAudit, ActivityLog, AuditLog
from .conversations import Conversation, Message, ConversationReadState
from .saved import SavedProperty
from .viewed import ViewedProperty
from .notifications import Notification
from .email import UserEmail, EmailVerificationCode, EmailOutbox
from .push import PushDevice, PushPreference, PushOutbox, PushDelivery
from .rate_limit import RateLimitWindow
__all__ = [
    "User","AuthIdentity","UserCurrentRole","RoleHistory","AdminAccount",
    "City","District","Taluk","Village","Locality","Postcode","LocalityAlias",
    "ContactCreditAccount","ContactCreditLedger","PropertyContactUnlock",
    "Plan","PlanVersion","Payment","PaymentRefund","PaymentWebhookEvent","Subscription","Entitlement",
    "PhysicalProperty","PropertyListing","Verification","PropertyMedia","PropertyDocument",
    "RentTerms","AmenityMaster","ListingAmenity","ListingDraft",
    "Enquiry","ActiveEnquiry","EnquiryMessage","LeadNote",
    "PropertyVisit",
    "BrokerPostcodeAccessCurrent","BrokerPostcodeAccessHistory",
    "ContactRevealAudit","ActivityLog","AuditLog",
    "Conversation","Message","ConversationReadState",
    "SavedProperty",
    "ViewedProperty",
    "Notification",
    "UserEmail","EmailVerificationCode","EmailOutbox",
    "PushDevice","PushPreference","PushOutbox","PushDelivery",
    "RateLimitWindow",
]
from .users import Base, User
from .refresh_tokens import RefreshToken  # Phase 6: refresh-token model
