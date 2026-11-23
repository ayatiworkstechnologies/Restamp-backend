"""Phase 3 contact entitlements: permanent property unlocks, the per-user
credit account (lifetime free usage + purchased balance) and the append-only
ledger. See services/contact_credits.py and migration u1v2w3x4y5z6."""
from sqlalchemy import CheckConstraint, Column, DateTime, Enum, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects import mysql

from .users import Base

_ID = mysql.BIGINT(unsigned=True)


class ContactCreditAccount(Base):
    __tablename__ = "contact_credit_accounts"
    user_id = Column(_ID, ForeignKey("users.id", ondelete="CASCADE", onupdate="CASCADE"), primary_key=True)
    free_unlocks_used = Column(Integer, nullable=False, server_default="0")
    credit_balance = Column(Integer, nullable=False, server_default="0")
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)
    __table_args__ = (
        CheckConstraint("credit_balance >= 0", name="ck_contact_credit_balance_nonneg"),
        CheckConstraint("free_unlocks_used >= 0 AND free_unlocks_used <= 3", name="ck_contact_free_used_range"),
    )


class PropertyContactUnlock(Base):
    __tablename__ = "property_contact_unlocks"
    id = Column(_ID, primary_key=True, autoincrement=True)
    user_id = Column(_ID, ForeignKey("users.id", ondelete="CASCADE", onupdate="CASCADE"), nullable=False)
    property_listing_id = Column(_ID, ForeignKey("property_listings.id", ondelete="CASCADE", onupdate="CASCADE"), nullable=False)
    source = Column(Enum("FREE", "CREDIT", "PLAN", "LEGACY"), nullable=False)
    subscription_id = Column(_ID, ForeignKey("subscriptions.id", ondelete="SET NULL", onupdate="CASCADE"), nullable=True)
    unlocked_at = Column(DateTime, nullable=False)
    __table_args__ = (
        UniqueConstraint("user_id", "property_listing_id", name="uk_contact_unlock_user_listing"),
        Index("idx_contact_unlocks_listing", "property_listing_id"),
    )


class ContactCreditLedger(Base):
    __tablename__ = "contact_credit_ledger"
    id = Column(_ID, primary_key=True, autoincrement=True)
    user_id = Column(_ID, ForeignKey("users.id", ondelete="CASCADE", onupdate="CASCADE"), nullable=False)
    entry_type = Column(Enum("PURCHASE", "FREE_UNLOCK", "CREDIT_UNLOCK", "PLAN_UNLOCK", "LEGACY_UNLOCK", "ADJUSTMENT"),
                        nullable=False)
    credits_delta = Column(Integer, nullable=False, server_default="0")
    free_delta = Column(Integer, nullable=False, server_default="0")
    balance_after = Column(Integer, nullable=False)
    payment_id = Column(_ID, ForeignKey("payments.id", ondelete="SET NULL", onupdate="CASCADE"), nullable=True)
    unlock_id = Column(_ID, ForeignKey("property_contact_unlocks.id", ondelete="SET NULL", onupdate="CASCADE"), nullable=True)
    reference = Column(String(100), nullable=True)
    created_at = Column(DateTime, nullable=False)
    __table_args__ = (
        UniqueConstraint("payment_id", name="uk_contact_ledger_payment"),
        UniqueConstraint("unlock_id", name="uk_contact_ledger_unlock"),
        Index("idx_contact_ledger_user", "user_id", "id"),
    )
