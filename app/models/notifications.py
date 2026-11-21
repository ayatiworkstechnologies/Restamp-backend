"""In-app notifications (Phase 1 of the notification system; migration m3n4o5p6q7r8).

One row per recipient per qualifying business event. Ownership is the single
`user_id` column: every read/update/delete is scoped by it (another user's row
is a 404, never a leak). `audience` says which app mode the row belongs to
(BUYER covers agents acting buyer-side; ACCOUNT is account-level), `category`
matches the app's filter pills. Title/body are rendered server-side from fixed
templates (no phone numbers, names or message bodies). The UNIQUE
(user_id, dedupe_key) constraint makes every event idempotent: a repeated or
retried event can never create a second row. Rows are soft-deleted
(`deleted_at`) so a deleted notification's key still blocks re-creation.
Later delivery channels (email, push) reference these rows; nothing here is
channel-specific. All timestamps are app-set naive UTC.
"""
from sqlalchemy import Column, DateTime, Enum, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects import mysql

from .users import Base


class Notification(Base): __tablename__ = "notifications"; id = Column(mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True); user_id = Column(mysql.BIGINT(unsigned=True), ForeignKey("users.id", ondelete="CASCADE", onupdate="CASCADE"), nullable=False); audience = Column(Enum("BUYER","OWNER","ACCOUNT"), nullable=False); type = Column(String(50), nullable=False); category = Column(Enum("PROPERTIES","VISITS","SYSTEM"), nullable=False); title = Column(String(160), nullable=False); body = Column(String(500), nullable=False); entity_type = Column(String(30), nullable=True); entity_id = Column(mysql.BIGINT(unsigned=True), nullable=True); property_listing_id = Column(mysql.BIGINT(unsigned=True), ForeignKey("property_listings.id", ondelete="SET NULL", onupdate="CASCADE"), nullable=True); dedupe_key = Column(String(191), nullable=False); read_at = Column(DateTime, nullable=True); deleted_at = Column(DateTime, nullable=True); created_at = Column(DateTime, nullable=False); __table_args__ = (UniqueConstraint("user_id", "dedupe_key", name="uk_notifications_user_dedupe"), Index("idx_notifications_user_list", "user_id", "deleted_at", "id"), Index("idx_notifications_user_unread", "user_id", "read_at", "deleted_at", "audience"), Index("idx_notifications_listing", "property_listing_id"))
