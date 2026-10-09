from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, UniqueConstraint, Enum, JSON, text
from sqlalchemy.dialects import mysql
from sqlalchemy.sql import func
from .users import Base
class AuthIdentity(Base):
    __tablename__ = "auth_identities"
    id = Column(mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True, nullable=False)
    user_id = Column(mysql.BIGINT(unsigned=True), ForeignKey("users.id", ondelete="CASCADE", onupdate="CASCADE"), nullable=False)
    provider = Column(Enum("phone","google"), nullable=False)
    provider_identifier = Column(String(500), nullable=False)
    verified_at = Column(DateTime, nullable=True)
    verified_by = Column(String(255), nullable=True)
    metadata_json = Column("metadata", JSON, nullable=True)
    created_at = Column(DateTime, server_default=func.current_timestamp(), nullable=False)
    updated_at = Column(mysql.TIMESTAMP(), server_default=text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'), nullable=False)
    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uk_auth_identities_provider_user"),
        UniqueConstraint("provider", "provider_identifier", name="uk_auth_provider_identifier_scoped"),
    )
