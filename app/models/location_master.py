"""Agent Location Master (alembic b8d4f0a2c6e1).

Three separate facts:
  - location_master_records  every individual locality record (Sheet 1,
                             "Original Location Master"). Append-only: the
                             application never updates or deletes a row.
  - agent_location_groups    one selectable agent location per PIN code
                             (Sheet 2, "Agent Location Selection"). Associated
                             localities and the display name are DERIVED from
                             the records; only the primary name is stored.
                             postcode_id is the stable link to the existing
                             postcodes row, which agent grants already use, so
                             adding localities never invalidates a selection.
  - broker_postcode_access_*  agent selections (unchanged tables).

Versioning: every committed change bumps location_master_state.committed_version;
rows carry the version they arrived in. Agents only see rows whose version
is <= published_version, i.e. what the published workbook contains.
location_publish_log records each workbook publication for crash recovery.
"""
from sqlalchemy import Boolean, Column, DateTime, Enum, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects import mysql
from sqlalchemy.sql import func

from .users import Base

# The three fixed Chennai zones (the only enum; locality names and PIN codes
# are data, never enums).
CHENNAI_ZONES = ("North Chennai", "Central Chennai", "South Chennai")


class LocationMasterRecord(Base):
    __tablename__ = "location_master_records"
    id = Column(mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    # "Record ID" in Sheet 1: the source S.No. for seed rows, then max + 1.
    record_no = Column(Integer, nullable=False, unique=True)
    raw_name = Column(String(255), nullable=False)       # exactly as given
    display_name = Column(String(255), nullable=False)   # whitespace-normalised
    pincode = Column(String(6), nullable=False)
    zone = Column(Enum(*CHENNAI_ZONES, name="chennai_zone"), nullable=False)
    source = Column(Enum("SEED", "ADMIN", name="location_record_source"), nullable=False)
    # ADMIN rows only: "<name key>|<pincode>|<zone>" — stops an accidental
    # repeat of the same new locality. NULL for seed rows (never deduplicated).
    dedupe_key = Column(String(300), nullable=True, unique=True)
    idempotency_key = Column(String(64), nullable=True, unique=True)
    version = Column(Integer, nullable=False)
    created_by = Column(mysql.BIGINT(unsigned=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, server_default=func.current_timestamp(), nullable=False)
    __table_args__ = (
        Index("idx_lmr_pincode", "pincode"),
        Index("idx_lmr_version", "version"),
    )


class AgentLocationGroup(Base):
    __tablename__ = "agent_location_groups"
    id = Column(mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    pincode = Column(String(6), nullable=False, unique=True)
    postcode_id = Column(
        mysql.BIGINT(unsigned=True),
        ForeignKey("postcodes.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
        unique=True,
    )
    primary_name = Column(String(255), nullable=False)
    zone = Column(Enum(*CHENNAI_ZONES, name="chennai_zone"), nullable=False)
    needs_review = Column(Boolean, nullable=False, default=False, server_default="0")
    created_version = Column(Integer, nullable=False)
    updated_version = Column(Integer, nullable=False)
    created_at = Column(DateTime, server_default=func.current_timestamp(), nullable=False)
    updated_at = Column(DateTime, server_default=func.current_timestamp(), nullable=False)
    __table_args__ = (Index("idx_alg_version", "created_version"),)


class LocationMasterState(Base):
    __tablename__ = "location_master_state"
    id = Column(Integer, primary_key=True)  # always 1
    committed_version = Column(Integer, nullable=False, default=0)
    published_version = Column(Integer, nullable=False, default=0)
    # Set when reconciliation finds a workbook it must not overwrite;
    # admin additions are refused until an operator resolves it.
    blocked_reason = Column(String(500), nullable=True)
    updated_at = Column(DateTime, server_default=func.current_timestamp(), nullable=False)


class LocationPublishLog(Base):
    __tablename__ = "location_publish_log"
    id = Column(mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    version = Column(Integer, nullable=False, unique=True)
    status = Column(Enum("PENDING", "PUBLISHED", "FAILED", name="location_publish_status"), nullable=False)
    file_sha256 = Column(String(64), nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.current_timestamp(), nullable=False)
    published_at = Column(DateTime, nullable=True)
