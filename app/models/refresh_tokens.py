from sqlalchemy import Column, Index, String, DateTime, ForeignKey, Text, func
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import declarative_base
from sqlalchemy.sql import func

from ..models.users import Base


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    id = Column(mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False, primary_key=True)
    user_id = Column(
        mysql.BIGINT(unsigned=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash = Column(mysql.VARCHAR(length=128), nullable=False, index=True, unique=True)
    jti = Column(mysql.VARCHAR(length=64), nullable=False, index=True, unique=True)
    created_at = Column(DateTime, server_default=func.current_timestamp(), nullable=True)
    expires_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    ip_address = Column(mysql.VARCHAR(length=45), nullable=True)
    user_agent = Column(Text, nullable=True)

    __table_args__ = (
        Index("ix_refresh_tokens_user_id", "user_id"),
        Index("ix_refresh_tokens_token_hash", "token_hash", unique=True),
        Index("ix_refresh_tokens_jti", "jti", unique=True),
    )


__all__ = ["RefreshToken"]