"""RESTAMP owner photo delete support (edit phase)

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-10-12

ADDITIVE ONLY: adds property_media.public_id (nullable) so the photo-delete
endpoint can destroy the associated Cloudinary asset after the DB delete
commits. No existing column/data touched; existing rows keep NULL (cloud
cleanup skipped for those — documented external-storage limitation).
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'e6f7a8b9c0d1'
down_revision = 'd5e6f7a8b9c0'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'property_media',
        sa.Column('public_id', sa.String(length=255), nullable=True),
    )


def downgrade():
    op.drop_column('property_media', 'public_id')
