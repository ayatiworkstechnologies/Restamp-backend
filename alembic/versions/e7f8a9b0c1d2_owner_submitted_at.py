"""RESTAMP owner auto-verification timestamp (Phase 7 delta)

Revision ID: e7f8a9b0c1d2
Revises: d4e5f6a7b8c9
Create Date: 2026-10-07

ADDITIVE ONLY: 1 nullable column on property_listings (submitted_at).
No existing column/constraint/data change:
- The column is NULLABLE with NO server default, so every pre-existing row
  (seeded VERIFIED listings, the seeded PENDING "Seed Draft Flat", and any
  owner listings created before this revision) keeps submitted_at = NULL and
  is therefore EXCLUDED from the 2-minute auto-verification rule, which only
  applies to rows with a recorded submission time.
- New owner listings created via POST /owner/listings record submitted_at at
  creation time (service layer, UTC).
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'e7f8a9b0c1d2'
down_revision = 'd4e5f6a7b8c9'
branch_labels = None
depends_on = None


def upgrade():
    # property_listings: submission timestamp for the 2-minute PENDING ->
    # VERIFIED auto-transition (ADD only; existing rows stay NULL).
    op.add_column('property_listings', sa.Column('submitted_at', sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column('property_listings', 'submitted_at')
