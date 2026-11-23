"""Shared fixed-window rate-limit counters for the public Reva endpoints.

One row per (bucket_key, window_start) holding the hit count for that fixed
window. Rows are ephemeral bookkeeping (never user data): the service prunes
a bucket's stale windows as it moves to a new one, and the table is safe to
truncate. Stored in the database rather than process memory so the limit is
enforced across backend instances and survives restarts (no Redis in this
stack; MySQL is the shared store every instance already has).
"""
from sqlalchemy import Column, Integer, String, DateTime, UniqueConstraint
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import declarative_base
from .users import Base


class RateLimitWindow(Base):
    __tablename__ = "rate_limit_windows"
    id = Column(mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    # Bucket identity, e.g. "chat:ip:203.0.113.7" or "chat:user:42". Never a
    # phone number or a token; the raw IP is the least-identifying stable key
    # available and is already used for abuse control elsewhere.
    bucket_key = Column(String(191), nullable=False)
    # UTC window start (naive UTC like every other timestamp in this codebase).
    window_start = Column(DateTime, nullable=False)
    hit_count = Column(Integer, nullable=False, server_default="0")
    __table_args__ = (
        UniqueConstraint("bucket_key", "window_start", name="uk_rate_limit_bucket_window"),
    )
