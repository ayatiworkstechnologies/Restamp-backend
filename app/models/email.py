"""Email notification tables (Phase 3 of the notification system; migration n4o5p6q7r8s9).

- UserEmail: at most one contact email per user, its verification time and
  the user's optional-email preferences. Only a VERIFIED address ever
  receives transactional email; changing the address clears `verified_at`.
  The address is a contact channel, not a login identity: it is not unique
  across accounts, so no endpoint can be used to probe registered addresses.
- EmailVerificationCode: expiring, single-use, attempt-limited codes. Only a
  keyed hash of the code is stored (`code_hash`), bound to the user AND the
  address it was issued for.
- EmailOutbox: one row per email to deliver. UNIQUE (user_id, dedupe_key)
  makes queuing idempotent; the worker claims rows with a row lock and a
  PROCESSING lease (`locked_at`), so two workers never send the same row.
  `payload` holds only template parameters (listing label, formatted time,
  target kind/id); a verification code is stored encrypted and wiped once
  the row is SENT or FAILED. `last_error` is a short error kind, never
  provider output. All timestamps are app-set naive UTC.
"""
from sqlalchemy import JSON, Boolean, Column, DateTime, Enum, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects import mysql

from .users import Base


class UserEmail(Base):
    __tablename__ = "user_emails"
    user_id = Column(mysql.BIGINT(unsigned=True), ForeignKey("users.id", ondelete="CASCADE", onupdate="CASCADE"),
                     primary_key=True, autoincrement=False)
    email = Column(String(254), nullable=False)
    verified_at = Column(DateTime, nullable=True)
    pref_enquiries = Column(Boolean, nullable=False, server_default=text("1"))
    pref_visits = Column(Boolean, nullable=False, server_default=text("1"))
    pref_listings = Column(Boolean, nullable=False, server_default=text("1"))
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)


class EmailVerificationCode(Base):
    __tablename__ = "email_verification_codes"
    id = Column(mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    user_id = Column(mysql.BIGINT(unsigned=True), ForeignKey("users.id", ondelete="CASCADE", onupdate="CASCADE"),
                     nullable=False)
    email = Column(String(254), nullable=False)
    code_hash = Column(String(64), nullable=False)
    attempts = Column(Integer, nullable=False, server_default=text("0"))
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False)
    __table_args__ = (Index("idx_email_codes_user_created", "user_id", "created_at"),)


class EmailOutbox(Base):
    __tablename__ = "email_outbox"
    id = Column(mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    user_id = Column(mysql.BIGINT(unsigned=True), ForeignKey("users.id", ondelete="CASCADE", onupdate="CASCADE"),
                     nullable=False)
    notification_id = Column(mysql.BIGINT(unsigned=True),
                             ForeignKey("notifications.id", ondelete="SET NULL", onupdate="CASCADE"), nullable=True)
    category = Column(Enum("VERIFICATION", "ENQUIRY", "VISIT", "LISTING", "ACCOUNT"), nullable=False)
    template = Column(String(50), nullable=False)
    to_email = Column(String(254), nullable=False)
    payload = Column(JSON, nullable=True)
    dedupe_key = Column(String(191), nullable=False)
    status = Column(Enum("PENDING", "PROCESSING", "SENT", "FAILED"), nullable=False, server_default="PENDING")
    attempts = Column(Integer, nullable=False, server_default=text("0"))
    next_attempt_at = Column(DateTime, nullable=False)
    locked_at = Column(DateTime, nullable=True)
    provider = Column(String(30), nullable=True)
    provider_message_id = Column(String(191), nullable=True)
    last_error = Column(String(60), nullable=True)
    created_at = Column(DateTime, nullable=False)
    sent_at = Column(DateTime, nullable=True)
    failed_at = Column(DateTime, nullable=True)
    __table_args__ = (
        UniqueConstraint("user_id", "dedupe_key", name="uk_email_outbox_user_dedupe"),
        Index("idx_email_outbox_due", "status", "next_attempt_at"),
        Index("idx_email_outbox_notification", "notification_id"),
        Index("idx_email_outbox_created", "created_at"),
    )
