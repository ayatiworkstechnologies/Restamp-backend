"""Push notification tables (Phase 4 of the notification system; migration o5p6q7r8s9t1).

- PushDevice: one registered Expo device per row. The FULL push token is never
  stored in the clear or exposed: `token_hash` (SHA-256) identifies it,
  `token_enc` (Fernet, key derived like the email verification codes) is the
  encrypted form the delivery worker decrypts to send, and `token_prefix` is a
  short masked form for admin display. token_hash is UNIQUE globally, so a
  device that switches accounts takes over its row — one token can never
  deliver for two accounts. `status` is ACTIVE / INACTIVE (logged out) /
  INVALID (provider refused the token; `invalid_reason` names the kind).
- PushPreference: the user's push preferences, one row per account (mirrors
  user_emails). Deliberately INDEPENDENT of email and in-app preferences:
  these flags govern push delivery only. No row means every optional push is
  on (safe default).
- PushOutbox: one row per push to deliver, sent by the background worker —
  same shape and contract as email_outbox: UNIQUE (user_id, dedupe_key)
  makes queuing idempotent, the worker claims rows with a row lock and a
  PROCESSING lease (`locked_at`), `payload` holds only target/notification
  ids and reminder eligibility facts (never tokens or message text),
  `last_error` is a short error kind, `provider_message_id` the provider's
  ticket id. All timestamps are app-set naive UTC.
"""
from sqlalchemy import JSON, Boolean, Column, DateTime, Enum, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects import mysql

from .users import Base


class PushDevice(Base):
    __tablename__ = "push_devices"
    id = Column(mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    user_id = Column(mysql.BIGINT(unsigned=True), ForeignKey("users.id", ondelete="CASCADE", onupdate="CASCADE"),
                     nullable=False)
    platform = Column(Enum("ios", "android"), nullable=False)
    token_hash = Column(String(64), nullable=False)
    token_enc = Column(String(500), nullable=False)
    token_prefix = Column(String(80), nullable=False)
    app_version = Column(String(50), nullable=True)
    device_name = Column(String(100), nullable=True)
    status = Column(Enum("ACTIVE", "INACTIVE", "INVALID"), nullable=False, server_default="ACTIVE")
    invalid_reason = Column(String(60), nullable=True)
    registered_at = Column(DateTime, nullable=False)
    refreshed_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)
    __table_args__ = (
        UniqueConstraint("token_hash", name="uk_push_devices_token"),
        Index("idx_push_devices_user", "user_id", "status"),
    )


class PushPreference(Base):
    __tablename__ = "push_preferences"
    user_id = Column(mysql.BIGINT(unsigned=True), ForeignKey("users.id", ondelete="CASCADE", onupdate="CASCADE"),
                     primary_key=True, autoincrement=False)
    pref_enquiries = Column(Boolean, nullable=False, server_default=text("1"))
    pref_visits = Column(Boolean, nullable=False, server_default=text("1"))
    pref_listings = Column(Boolean, nullable=False, server_default=text("1"))
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)


class PushOutbox(Base):
    __tablename__ = "push_outbox"
    id = Column(mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    user_id = Column(mysql.BIGINT(unsigned=True), ForeignKey("users.id", ondelete="CASCADE", onupdate="CASCADE"),
                     nullable=False)
    notification_id = Column(mysql.BIGINT(unsigned=True),
                             ForeignKey("notifications.id", ondelete="SET NULL", onupdate="CASCADE"), nullable=True)
    category = Column(Enum("ENQUIRY", "VISIT", "LISTING", "ACCOUNT"), nullable=False)
    template = Column(String(50), nullable=False)
    title = Column(String(160), nullable=False)
    body = Column(String(500), nullable=False)
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
        UniqueConstraint("user_id", "dedupe_key", name="uk_push_outbox_user_dedupe"),
        Index("idx_push_outbox_due", "status", "next_attempt_at"),
        Index("idx_push_outbox_notification", "notification_id"),
        Index("idx_push_outbox_created", "created_at"),
    )


class PushDelivery(Base):
    """One device's share of a PushOutbox row (migration p6q7r8s9t0u1).

    PENDING -> PROCESSING -> SENT (Expo accepted: `ticket_id`) -> DELIVERED
    (receipt ok) | FAILED. A ticket is NOT delivery; only an ok receipt is.
    UNIQUE (outbox_id, device_id): the fan-out is idempotent, so a device is
    never given a second copy of the same push. `receipt_due_at` schedules the
    receipt poll; `last_error` is a short kind, never provider text."""
    __tablename__ = "push_deliveries"
    id = Column(mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    outbox_id = Column(mysql.BIGINT(unsigned=True), ForeignKey("push_outbox.id", ondelete="CASCADE", onupdate="CASCADE"),
                       nullable=False)
    device_id = Column(mysql.BIGINT(unsigned=True), ForeignKey("push_devices.id", ondelete="CASCADE", onupdate="CASCADE"),
                       nullable=False)
    status = Column(Enum("PENDING", "PROCESSING", "SENT", "DELIVERED", "FAILED"), nullable=False,
                    server_default="PENDING")
    attempts = Column(Integer, nullable=False, server_default=text("0"))
    next_attempt_at = Column(DateTime, nullable=False)
    locked_at = Column(DateTime, nullable=True)
    ticket_id = Column(String(191), nullable=True)
    receipt_due_at = Column(DateTime, nullable=True)
    receipt_attempts = Column(Integer, nullable=False, server_default=text("0"))
    last_error = Column(String(60), nullable=True)
    created_at = Column(DateTime, nullable=False)
    sent_at = Column(DateTime, nullable=True)
    delivered_at = Column(DateTime, nullable=True)
    failed_at = Column(DateTime, nullable=True)
    __table_args__ = (
        UniqueConstraint("outbox_id", "device_id", name="uk_push_deliveries_outbox_device"),
        Index("idx_push_deliveries_due", "status", "next_attempt_at"),
        Index("idx_push_deliveries_receipt", "status", "receipt_due_at"),
        Index("idx_push_deliveries_device", "device_id"),
    )
