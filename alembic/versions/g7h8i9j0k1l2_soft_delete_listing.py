"""RESTAMP soft-delete column on property_listings.

Revision ID: g7h8i9j0k1l2
Revises: f2a3b4c5d6e7
Create Date: 2026-10-26

ADDITIVE ONLY: 1 nullable column on property_listings (deleted_at).
No existing column/constraint/data change:
- The column is NULLABLE with NO server default, so every pre-existing row
  keeps deleted_at = NULL (visible). A listing is soft-removed only when
  the Owner explicitly calls DELETE /owner/listings/{id}, which sets
  deleted_at = now() instead of hard-deleting the row.
- All buyer-facing visibility filters (_visible in app/services/listings.py)
  gain deleted_at IS NULL so soft-removed listings never appear in buyer
  discovery, detail, similar, contact reveal, save, enquiry, or visit flows.
- Auto-verification (worker) also filters deleted_at IS NULL.
- Owner-side reads/list mutations keep their own scope; a soft-removed
  listing remains accessible to its owner for history/audit but cannot be
  mutated (transition_verification/transition_listing_status exclude it).
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'g7h8i9j0k1l2'
down_revision = 'f2a3b4c5d6e7'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('property_listings', sa.Column('deleted_at', sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column('property_listings', 'deleted_at')
