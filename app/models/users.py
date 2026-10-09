from sqlalchemy import Column, Index, String, DateTime, Text, Enum, ForeignKey, text
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import declarative_base
from sqlalchemy.sql import func
Base = declarative_base()
__all__ = ["User", "Base"]
class User(Base):
    __tablename__ = "users"
    id = Column(mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True, nullable=False)
    display_name = Column(String(255), nullable=False)
    avatar_url = Column(String(500), nullable=True)
    status = Column(Enum("active","suspended","closed"), nullable=False, server_default="active")
    created_at = Column(DateTime, server_default=func.current_timestamp(), nullable=False)
    updated_at = Column(mysql.TIMESTAMP(), server_default=text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'), nullable=False)
    
    __table_args__ = (
        Index('idx_users_status', 'status'),
        Index('idx_users_created_at', 'created_at'),
        Index('idx_users_updated_at', 'updated_at'),
    )
