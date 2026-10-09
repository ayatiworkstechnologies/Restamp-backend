"""RESTAMP RENT terms + amenities M2M (DB layer step 2 of 3)

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6
Create Date: 2026-10-07

ADDITIVE ONLY: creates rent_terms (1:1 with property_listings),
amenity_master + listing_amenities (composite-PK M2M, same convention as
saved_properties), and idempotently seeds amenity_master with the exact
frontend vocabularies (INSERT IGNORE on uk_amenity_name: re-runs add
nothing, existing IDs preserved). No existing table/column/data touched.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = 'a2b3c4d5e6f7'
down_revision = 'f1a2b3c4d5e6'
branch_labels = None
depends_on = None

# Exact frontend vocabularies (OwnerAddPropertyScreen.js):
# INITIAL_AMENITIES(10) + EXTRA_AMENITIES(6),
# INITIAL_PROPERTY_FEATURES(6) + EXTRA_PROPERTY_FEATURES(8),
# OTHER_ROOMS_OPTIONS(5). Total 35 rows.
AMENITY_SEED = [
    ("Lift", "AMENITY"), ("Security Guard", "AMENITY"), ("CCTV", "AMENITY"),
    ("Club House", "AMENITY"), ("Gym", "AMENITY"), ("Power Backup", "AMENITY"),
    ("Park", "AMENITY"), ("Swimming Pool", "AMENITY"), ("Visitor Parking", "AMENITY"),
    ("Intercom", "AMENITY"), ("Children's Play Area", "AMENITY"),
    ("Fire Fighting System", "AMENITY"), ("Rainwater Harvesting", "AMENITY"),
    ("Piped Gas", "AMENITY"), ("Community Hall", "AMENITY"), ("Waste Disposal", "AMENITY"),
    ("Recently Renovated", "FEATURE"), ("Vastu Compliant", "FEATURE"),
    ("High Ceiling", "FEATURE"), ("False Ceiling Lighting", "FEATURE"),
    ("Corner Property", "FEATURE"), ("Pet Friendly", "FEATURE"),
    ("Gated Community", "FEATURE"), ("Modular Kitchen", "FEATURE"),
    ("Solar Water Heater", "FEATURE"), ("EV Charging Station", "FEATURE"),
    ("Wheelchair Accessible", "FEATURE"), ("Servant Quarters", "FEATURE"),
    ("Natural Daylight", "FEATURE"), ("Private Terrace", "FEATURE"),
    ("Pooja Room", "ROOM"), ("Study Room", "ROOM"), ("Servant Room", "ROOM"),
    ("Store Room", "ROOM"), ("Others", "ROOM"),
]


def upgrade():
    op.create_table(
        'rent_terms',
        sa.Column('id', mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column('property_listing_id', mysql.BIGINT(unsigned=True),
                  sa.ForeignKey('property_listings.id', ondelete='CASCADE', onupdate='CASCADE'),
                  nullable=False, unique=True),
        sa.Column('security_deposit_paise', sa.BigInteger(), nullable=True),
        sa.Column('maintenance_paise', sa.BigInteger(), nullable=True),
        sa.Column('maintenance_period', sa.Enum('MONTHLY', 'QUARTERLY', 'YEARLY'), nullable=True),
        sa.Column('is_negotiable', sa.Boolean(), nullable=False, server_default="0"),
        sa.Column('available_from', sa.Date(), nullable=True),
        sa.Column('tenant_preference', sa.Enum('FAMILY', 'BACHELOR', 'COMPANY', 'ANY'), nullable=True),
        sa.Column('lock_in_months', mysql.SMALLINT(unsigned=True), nullable=True),
        sa.Column('agreement_months', mysql.SMALLINT(unsigned=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'amenity_master',
        sa.Column('id', mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('kind', sa.Enum('AMENITY', 'FEATURE', 'ROOM'), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', name='uk_amenity_name'),
    )
    op.create_table(
        'listing_amenities',
        sa.Column('property_listing_id', mysql.BIGINT(unsigned=True),
                  sa.ForeignKey('property_listings.id', ondelete='CASCADE', onupdate='CASCADE'),
                  primary_key=True, nullable=False),
        sa.Column('amenity_id', mysql.BIGINT(unsigned=True),
                  sa.ForeignKey('amenity_master.id', ondelete='CASCADE', onupdate='CASCADE'),
                  primary_key=True, nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
    )
    # Idempotent seed: INSERT IGNORE keeps existing IDs, adds only missing names.
    bind = op.get_bind()
    for name, kind in AMENITY_SEED:
        bind.execute(
            sa.text("INSERT IGNORE INTO amenity_master (name, kind) VALUES (:n, :k)"),
            {"n": name, "k": kind},
        )


def downgrade():
    op.drop_table('listing_amenities')
    op.drop_table('amenity_master')
    op.drop_table('rent_terms')
