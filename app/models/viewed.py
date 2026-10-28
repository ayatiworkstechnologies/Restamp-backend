"""RESTAMP Buyer viewed-property history model.

Tracks which properties a buyer has actually opened and viewed (detail
screen). Each row is uniquely keyed by (buyer_user_id, property_listing_id);
repeat views update viewed_at instead of duplicating rows. Per-buyer
retention is capped at the 20 most recent distinct properties.
"""

from sqlalchemy import Column, DateTime, ForeignKey, Index, PrimaryKeyConstraint, UniqueConstraint
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from .users import Base


class ViewedProperty(Base):
    """A buyer's view of a property listing detail."""

    __tablename__ = "viewed_properties"

    id = Column(
        mysql.BIGINT(unsigned=True),
        primary_key=True,
        autoincrement=True,
    )
    buyer_user_id = Column(
        mysql.BIGINT(unsigned=True),
        ForeignKey("users.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
    )
    property_listing_id = Column(
        mysql.BIGINT(unsigned=True),
        ForeignKey("property_listings.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
    )
    viewed_at = Column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )

    __table_args__ = (
        PrimaryKeyConstraint("id"),
        UniqueConstraint("buyer_user_id", "property_listing_id", name="uq_viewed_buyer_listing"),
        Index("ix_viewed_properties_buyer_viewed_at", "buyer_user_id", "viewed_at"),
    )

    buyer = relationship("User", foreign_keys=[buyer_user_id])
    listing = relationship("PropertyListing", foreign_keys=[property_listing_id])
