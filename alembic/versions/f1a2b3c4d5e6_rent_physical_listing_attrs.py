"""RESTAMP RENT physical/listing attributes (DB layer step 1 of 3)

Revision ID: f1a2b3c4d5e6
Revises: e7f8a9b0c1d2
Create Date: 2026-10-07

ADDITIVE ONLY: new NULLABLE columns on physical_properties and
property_listings (except is_duplex, which is NOT NULL DEFAULT FALSE —
safe: FALSE is the correct default for all existing rows).
No existing column/constraint/data change. No backfill of demo values.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = 'f1a2b3c4d5e6'
down_revision = 'e7f8a9b0c1d2'
branch_labels = None
depends_on = None


def upgrade():
    # physical_properties: RENT step-3/5/6 physical facts + structured address
    op.add_column('physical_properties', sa.Column('balconies', mysql.TINYINT(unsigned=True), nullable=True))
    op.add_column('physical_properties', sa.Column('built_up_area', mysql.INTEGER(unsigned=True), nullable=True))
    op.add_column('physical_properties', sa.Column('super_built_up_area', mysql.INTEGER(unsigned=True), nullable=True))
    op.add_column('physical_properties', sa.Column('total_floors', mysql.SMALLINT(unsigned=True), nullable=True))
    op.add_column('physical_properties', sa.Column('floor_on', sa.String(length=10), nullable=True))
    op.add_column('physical_properties', sa.Column('is_duplex', sa.Boolean(), nullable=False, server_default="0"))
    op.add_column('physical_properties', sa.Column('property_age_band', sa.String(length=20), nullable=True))
    op.add_column('physical_properties', sa.Column('sub_locality', sa.String(length=255), nullable=True))
    op.add_column('physical_properties', sa.Column('society_name', sa.String(length=255), nullable=True))
    op.add_column('physical_properties', sa.Column('house_no', sa.String(length=100), nullable=True))
    op.add_column('physical_properties', sa.Column('landmark', sa.String(length=255), nullable=True))
    op.add_column('physical_properties', sa.Column('latitude', mysql.DECIMAL(10, 7), nullable=True))
    op.add_column('physical_properties', sa.Column('longitude', mysql.DECIMAL(10, 7), nullable=True))
    op.add_column('physical_properties', sa.Column('furnishing', sa.Enum('UNFURNISHED', 'SEMI_FURNISHED', 'FURNISHED'), nullable=True))
    op.add_column('physical_properties', sa.Column('covered_parking', mysql.TINYINT(unsigned=True), nullable=True, server_default="0"))
    op.add_column('physical_properties', sa.Column('open_parking', mysql.TINYINT(unsigned=True), nullable=True, server_default="0"))
    op.add_column('physical_properties', sa.Column('open_sides', sa.String(length=10), nullable=True))
    op.add_column('physical_properties', sa.Column('overlooking', sa.String(length=50), nullable=True))
    op.add_column('physical_properties', sa.Column('power_backup', sa.Enum('NONE', 'PARTIAL', 'FULL'), nullable=True))
    op.add_column('physical_properties', sa.Column('facing', sa.String(length=20), nullable=True))
    # property_listings: per-listing contact overrides + close reason
    op.add_column('property_listings', sa.Column('contact_phone', sa.String(length=20), nullable=True))
    op.add_column('property_listings', sa.Column('contact_email', sa.String(length=255), nullable=True))
    op.add_column('property_listings', sa.Column('closed_reason', sa.String(length=500), nullable=True))


def downgrade():
    op.drop_column('property_listings', 'closed_reason')
    op.drop_column('property_listings', 'contact_email')
    op.drop_column('property_listings', 'contact_phone')
    for col in ('facing', 'power_backup', 'overlooking', 'open_sides', 'open_parking',
                'covered_parking', 'furnishing', 'longitude', 'latitude', 'landmark',
                'house_no', 'society_name', 'sub_locality', 'property_age_band',
                'is_duplex', 'floor_on', 'total_floors', 'super_built_up_area',
                'built_up_area', 'balconies'):
        op.drop_column('physical_properties', col)
