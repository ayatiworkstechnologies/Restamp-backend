"""RESTAMP account capabilities: the exclusive poster type (OWNER or BROKER).

Revision ID: r8s9t0u1v2w3
Revises: q7r8s9t0u1v2
Create Date: 2026-11-20

ADDITIVE ONLY. Why:
- users.poster_type: every account is a Buyer; an account may additionally be
  an Owner OR a Dealer (internal name BROKER), never both. Until now the only
  role state was user_current_role, a freely switchable UI mode, so nothing
  durable recorded which poster type an account is or kept the two exclusive.
  The column is nullable: NULL = Buyer only (no poster type yet). It is set at
  registration (declared account type) or by the first Owner/Dealer plan
  activation, and is never changed by a mode switch. It grants NO posting
  rights by itself; posting is decided from the verified subscription state.

Backfill (existing rows keep working, nothing is deleted):
- OWNER when the account has any property_listings row or any OWNER-plan
  subscription, and no Dealer evidence.
- BROKER when it has any broker_postcode_access_history row or any
  BROKER-plan subscription, and no Owner evidence.
- Accounts with BOTH kinds of evidence take their current mode when that mode
  is OWNER or BROKER; otherwise they stay NULL and are left for manual review
  (reported in the Phase 1 report). Accounts with no evidence stay NULL even
  if their current mode is OWNER/BROKER (a mode is not a capability).
downgrade() drops only the added column.
"""
from alembic import op
import sqlalchemy as sa

revision = 'r8s9t0u1v2w3'
down_revision = 'q7r8s9t0u1v2'
branch_labels = None
depends_on = None

_OWNER_EVIDENCE = (
    "(EXISTS (SELECT 1 FROM property_listings l WHERE l.user_id = u.id)"
    " OR EXISTS (SELECT 1 FROM subscriptions s JOIN plans p ON p.id = s.plan_id"
    " WHERE s.user_id = u.id AND p.role = 'OWNER'))"
)
_BROKER_EVIDENCE = (
    "(EXISTS (SELECT 1 FROM broker_postcode_access_history b WHERE b.broker_user_id = u.id)"
    " OR EXISTS (SELECT 1 FROM subscriptions s JOIN plans p ON p.id = s.plan_id"
    " WHERE s.user_id = u.id AND p.role = 'BROKER'))"
)


def upgrade():
    op.add_column(
        'users',
        sa.Column('poster_type', sa.Enum('OWNER', 'BROKER'), nullable=True),
    )
    op.execute(
        f"UPDATE users u SET u.poster_type = 'OWNER' WHERE {_OWNER_EVIDENCE} AND NOT {_BROKER_EVIDENCE}"
    )
    op.execute(
        f"UPDATE users u SET u.poster_type = 'BROKER' WHERE {_BROKER_EVIDENCE} AND NOT {_OWNER_EVIDENCE}"
    )
    op.execute(
        "UPDATE users u JOIN user_current_role r ON r.user_id = u.id"
        " SET u.poster_type = r.role"
        f" WHERE u.poster_type IS NULL AND r.role IN ('OWNER', 'BROKER')"
        f" AND {_OWNER_EVIDENCE} AND {_BROKER_EVIDENCE}"
    )


def downgrade():
    op.drop_column('users', 'poster_type')
