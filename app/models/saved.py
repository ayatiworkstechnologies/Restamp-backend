"""RESTAMP Buyer V1 — saved/wishlist persistence (approved decision document only).

Minimal normalized table: buyer -> saved listing. No notes/folders/categories/metadata.
Composite PK enforces one save per buyer+listing.
"""
from sqlalchemy import Column, DateTime, ForeignKey
from sqlalchemy.dialects import mysql
from sqlalchemy.sql import func
from .users import Base


class SavedProperty(Base):
    __tablename__ = "saved_properties"
    buyer_user_id = Column(
        mysql.BIGINT(unsigned=True),
        ForeignKey("users.id", ondelete="CASCADE", onupdate="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    property_listing_id = Column(
        mysql.BIGINT(unsigned=True),
        ForeignKey("property_listings.id", ondelete="CASCADE", onupdate="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    saved_at = Column(
        mysql.TIMESTAMP(), server_default=func.current_timestamp(), nullable=False
    )
