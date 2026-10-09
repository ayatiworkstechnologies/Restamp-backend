"""RESTAMP Buyer V1 schema — 9 columns + saved_properties + price index (Phase 6A)

Revision ID: d4e5f6a7b8c9
Revises: 1a2b3c4d5e6f
Create Date: 2026-10-06

Approved source: report/RESTAMP_BUYER_SCHEMA_DECISION_FINAL.md (frozen Buyer V1 delta).
ADDITIVE ONLY: 4 columns on property_listings, 5 on physical_properties,
new saved_properties table (composite PK + 2 CASCADE FKs), 1 price index.
No existing column/constraint/data change. No buyer APIs (next phase).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = 'd4e5f6a7b8c9'
down_revision = '1a2b3c4d5e6f'
branch_labels = None
depends_on = None


class NotEmptyForNotNull(RuntimeError):
    """Raised when a NOT NULL column cannot be added to a populated table."""


def _assert_empty_property_listings(bind):
    """F1 guard: price_paise is NOT NULL with no default and no backfillable
    source (no price_range column exists anywhere — verified by audit), so the
    table must be empty. Runs BEFORE any schema op: on failure nothing has been
    modified. Returns the row count (0) when safe."""
    count = bind.exec_driver_sql("SELECT COUNT(*) FROM property_listings").scalar()
    if count:
        raise NotEmptyForNotNull(
            f"Refusing to add property_listings.price_paise NOT NULL: table holds "
            f"{count} row(s) with no authoritative price source to backfill from "
            f"(no price_range column exists). Run a dedicated data migration that "
            f"assigns every row a verified price_paise first, then re-run this "
            f"revision. No schema change was applied."
        )
    return count


def _guard_bind():
    """Real connection in online mode; None when rendering offline SQL."""
    try:
        bind = op.get_bind()
    except Exception:
        return None
    return bind if bind is not None and hasattr(bind, "exec_driver_sql") else None


def upgrade():
    # F1 precondition FIRST: atomic-safe — nothing is modified if this raises.
    bind = _guard_bind()
    if bind is not None:
        _assert_empty_property_listings(bind)
    # property_listings: price + description + construction status (ADD only)
    op.add_column('property_listings', sa.Column('price_paise', sa.BigInteger(), nullable=False))
    op.add_column('property_listings', sa.Column('price_period', sa.Enum('TOTAL', 'MONTHLY'), nullable=False, server_default='TOTAL'))
    op.add_column('property_listings', sa.Column('description', sa.Text(), nullable=True))
    op.add_column('property_listings', sa.Column('construction_status', sa.Enum('READY_TO_MOVE', 'UNDER_CONSTRUCTION', 'NEW_LAUNCH'), nullable=True))
    op.create_index('idx_property_listings_price_paise', 'property_listings', ['price_paise'])

    # physical_properties: address + area + specs (ADD only)
    op.add_column('physical_properties', sa.Column('address_line', sa.String(length=500), nullable=True))
    op.add_column('physical_properties', sa.Column('area_value', mysql.INTEGER(unsigned=True), nullable=True))
    op.add_column('physical_properties', sa.Column('area_unit', sa.String(length=20), nullable=True))
    op.add_column('physical_properties', sa.Column('bedrooms', mysql.TINYINT(unsigned=True), nullable=True))
    op.add_column('physical_properties', sa.Column('bathrooms', mysql.TINYINT(unsigned=True), nullable=True))

    # saved_properties: minimal buyer -> saved listing (composite PK, CASCADE FKs)
    op.create_table('saved_properties',
        sa.Column('buyer_user_id', mysql.BIGINT(unsigned=True), sa.ForeignKey('users.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('property_listing_id', mysql.BIGINT(unsigned=True), sa.ForeignKey('property_listings.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('saved_at', mysql.TIMESTAMP(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.PrimaryKeyConstraint('buyer_user_id', 'property_listing_id'),
    )


def downgrade():
    # Exact reverse: drop table/index first, then added columns (reverse order).
    op.drop_table('saved_properties')
    op.drop_index('idx_property_listings_price_paise', table_name='property_listings')
    op.drop_column('physical_properties', 'bathrooms')
    op.drop_column('physical_properties', 'bedrooms')
    op.drop_column('physical_properties', 'area_unit')
    op.drop_column('physical_properties', 'area_value')
    op.drop_column('physical_properties', 'address_line')
    op.drop_column('property_listings', 'construction_status')
    op.drop_column('property_listings', 'description')
    op.drop_column('property_listings', 'price_period')
    op.drop_column('property_listings', 'price_paise')
