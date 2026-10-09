"""RESTAMP media attrs + lifecycle enums (DB layer step 3 of 3)

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
Create Date: 2026-10-07

- property_media: 3 new NULLABLE columns (category/mime_type/byte_size).
  Existing rows keep NULL; cover convention (order_index 0) unchanged.
- verification_status ENUM gains REJECTED; listing_status ENUM gains
  CLOSED/EXPIRED. Existing values, defaults, and nullability preserved;
  no row is modified (new values are opt-in for future moderation/close
  flows). Downgrade is safe only while no row uses the new values.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = 'b3c4d5e6f7a8'
down_revision = 'a2b3c4d5e6f7'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('property_media', sa.Column('category', sa.String(length=50), nullable=True))
    op.add_column('property_media', sa.Column('mime_type', sa.String(length=100), nullable=True))
    op.add_column('property_media', sa.Column('byte_size', sa.BigInteger(), nullable=True))
    op.alter_column(
        'property_listings', 'verification_status',
        existing_type=mysql.ENUM('PENDING', 'VERIFIED'),
        type_=mysql.ENUM('PENDING', 'VERIFIED', 'REJECTED'),
        existing_nullable=False, server_default='PENDING',
    )
    op.alter_column(
        'property_listings', 'listing_status',
        existing_type=mysql.ENUM('AVAILABLE', 'SOLD', 'RENTED', 'LEASED'),
        type_=mysql.ENUM('AVAILABLE', 'SOLD', 'RENTED', 'LEASED', 'CLOSED', 'EXPIRED'),
        existing_nullable=False, server_default='AVAILABLE',
    )


def downgrade():
    op.alter_column(
        'property_listings', 'listing_status',
        existing_type=mysql.ENUM('AVAILABLE', 'SOLD', 'RENTED', 'LEASED', 'CLOSED', 'EXPIRED'),
        type_=mysql.ENUM('AVAILABLE', 'SOLD', 'RENTED', 'LEASED'),
        existing_nullable=False, server_default='AVAILABLE',
    )
    op.alter_column(
        'property_listings', 'verification_status',
        existing_type=mysql.ENUM('PENDING', 'VERIFIED', 'REJECTED'),
        type_=mysql.ENUM('PENDING', 'VERIFIED'),
        existing_nullable=False, server_default='PENDING',
    )
    op.drop_column('property_media', 'byte_size')
    op.drop_column('property_media', 'mime_type')
    op.drop_column('property_media', 'category')
