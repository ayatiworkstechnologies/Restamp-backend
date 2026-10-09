from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, text
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import declarative_base
from sqlalchemy.sql import func
from .users import Base
class UserCurrentRole(Base):
    __tablename__ = "user_current_role"
    user_id = Column(mysql.BIGINT(unsigned=True), ForeignKey("users.id", ondelete="CASCADE", onupdate="CASCADE"), primary_key=True, nullable=False)
    role = Column(String(50), nullable=False)
    assigned_at = Column(DateTime, server_default=func.current_timestamp(), nullable=False)
    updated_at = Column(mysql.TIMESTAMP(), server_default=text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'), nullable=False)
class RoleHistory(Base):
    __tablename__ = "role_history"
    id = Column(mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True, nullable=False)
    user_id = Column(mysql.BIGINT(unsigned=True), ForeignKey("users.id", ondelete="CASCADE", onupdate="CASCADE"), nullable=False)
    role = Column(String(50), nullable=False)
    changed_at = Column(DateTime, server_default=func.current_timestamp(), nullable=False)
class AdminAccount(Base):
    __tablename__ = "admin_accounts"
    user_id = Column(mysql.BIGINT(unsigned=True), ForeignKey("users.id", ondelete="CASCADE", onupdate="CASCADE"), primary_key=True, nullable=False)
    admin_role = Column(String(50), nullable=False, server_default="ADMIN")
    granted_at = Column(DateTime, server_default=func.current_timestamp(), nullable=False)