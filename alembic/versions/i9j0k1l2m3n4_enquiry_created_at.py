"""RESTAMP enquiry creation timestamp.

Revision ID: i9j0k1l2m3n4
Revises: h8i9j0k1l2m3
Create Date: 2026-10-28

ADDITIVE ONLY: 1 nullable column on enquiries (created_at, naive UTC).
No existing column/constraint/data change:
- The column is NULLABLE with NO server default, so every pre-existing row
  keeps created_at = NULL. Their real creation times were never stored and
  cannot be reconstructed (active_enquiries.started_at uses the MySQL server
  clock, whose zone differs per environment, and only exists for open
  leads), so they are not backfilled: the API returns null ("unknown")
  instead of a fabricated date such as the migration time.
- New enquiries are stamped by app/services/enquiries.py with UTC (whole
  seconds), the same convention as property_visits. A server default is
  deliberately not used: CURRENT_TIMESTAMP follows the server time zone,
  which is not UTC on every environment.
- Forward-only: chained on the current single head; downgrade drops the
  column (and with it any recorded timestamps).
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'i9j0k1l2m3n4'
down_revision = 'h8i9j0k1l2m3'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('enquiries', sa.Column('created_at', sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column('enquiries', 'created_at')
