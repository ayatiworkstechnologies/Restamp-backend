"""Agent Location Master: original locality records, PIN-code groups,
dataset version and publish log (additive only).

No existing table is altered and no row is changed or deleted. The 298
seed records are loaded by scripts/init_location_master.py, not here.

Revision ID: b8d4f0a2c6e1
Revises: a7c3e9d2b4f1
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "b8d4f0a2c6e1"
down_revision = "a7c3e9d2b4f1"
branch_labels = None
depends_on = None

ZONES = ("North Chennai", "Central Chennai", "South Chennai")
UBIG = mysql.BIGINT(unsigned=True)


def upgrade() -> None:
    op.create_table(
        "location_master_records",
        sa.Column("id", UBIG, primary_key=True, autoincrement=True),
        sa.Column("record_no", sa.Integer, nullable=False),
        sa.Column("raw_name", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("pincode", sa.String(6), nullable=False),
        sa.Column("zone", sa.Enum(*ZONES), nullable=False),
        sa.Column("source", sa.Enum("SEED", "ADMIN"), nullable=False),
        sa.Column("dedupe_key", sa.String(300), nullable=True),
        sa.Column("idempotency_key", sa.String(64), nullable=True),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("created_by", UBIG, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.current_timestamp(), nullable=False),
        sa.UniqueConstraint("record_no", name="uk_lmr_record_no"),
        sa.UniqueConstraint("dedupe_key", name="uk_lmr_dedupe_key"),
        sa.UniqueConstraint("idempotency_key", name="uk_lmr_idempotency_key"),
        sa.CheckConstraint("CHAR_LENGTH(pincode) = 6", name="ck_lmr_pincode_len"),
    )
    op.create_index("idx_lmr_pincode", "location_master_records", ["pincode"])
    op.create_index("idx_lmr_version", "location_master_records", ["version"])

    op.create_table(
        "agent_location_groups",
        sa.Column("id", UBIG, primary_key=True, autoincrement=True),
        sa.Column("pincode", sa.String(6), nullable=False),
        sa.Column(
            "postcode_id", UBIG,
            sa.ForeignKey("postcodes.id", ondelete="RESTRICT", onupdate="RESTRICT"),
            nullable=False,
        ),
        sa.Column("primary_name", sa.String(255), nullable=False),
        sa.Column("zone", sa.Enum(*ZONES), nullable=False),
        sa.Column("needs_review", sa.Boolean, nullable=False, server_default=sa.text("0")),
        sa.Column("created_version", sa.Integer, nullable=False),
        sa.Column("updated_version", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.current_timestamp(), nullable=False),
        sa.UniqueConstraint("pincode", name="uk_alg_pincode"),
        sa.UniqueConstraint("postcode_id", name="uk_alg_postcode"),
    )
    op.create_index("idx_alg_version", "agent_location_groups", ["created_version"])

    op.create_table(
        "location_master_state",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("committed_version", sa.Integer, nullable=False, server_default="0"),
        sa.Column("published_version", sa.Integer, nullable=False, server_default="0"),
        sa.Column("blocked_reason", sa.String(500), nullable=True),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.current_timestamp(), nullable=False),
    )
    op.execute("INSERT INTO location_master_state (id, committed_version, published_version) VALUES (1, 0, 0)")

    op.create_table(
        "location_publish_log",
        sa.Column("id", UBIG, primary_key=True, autoincrement=True),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("status", sa.Enum("PENDING", "PUBLISHED", "FAILED"), nullable=False),
        sa.Column("file_sha256", sa.String(64), nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("published_at", sa.DateTime, nullable=True),
        sa.UniqueConstraint("version", name="uk_lpl_version"),
    )


def downgrade() -> None:
    # Drops only the tables this revision created (agent grants and the
    # postcodes/localities rows are untouched).
    op.drop_table("location_publish_log")
    op.drop_table("location_master_state")
    op.drop_index("idx_alg_version", table_name="agent_location_groups")
    op.drop_table("agent_location_groups")
    op.drop_index("idx_lmr_version", table_name="location_master_records")
    op.drop_index("idx_lmr_pincode", table_name="location_master_records")
    op.drop_table("location_master_records")
