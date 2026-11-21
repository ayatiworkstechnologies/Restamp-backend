"""RESTAMP payments: manual-review marker for verified payments that could not
be activated safely.

Revision ID: s9t0u1v2w3x4
Revises: r8s9t0u1v2w3
Create Date: 2026-11-20

ADDITIVE ONLY. Why: a Razorpay payment can be verified (money captured) for a
poster plan (Owner or Dealer) while the account's exclusive poster type has
meanwhile become the other one (Phase 1). Activating it would grant the wrong
poster type; raising would roll back the SUCCESS record. The payment is kept
as SUCCESS and flagged here instead, so nothing is lost and an admin can
resolve it. All three columns are nullable: existing payments are unaffected
(NULL = no review needed). downgrade() drops only these columns.
"""
from alembic import op
import sqlalchemy as sa

revision = 's9t0u1v2w3x4'
down_revision = 'r8s9t0u1v2w3'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('payments', sa.Column('activation_review_status', sa.Enum('PENDING', 'RESOLVED'), nullable=True))
    op.add_column('payments', sa.Column('activation_review_reason', sa.String(255), nullable=True))
    op.add_column('payments', sa.Column('activation_review_at', sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column('payments', 'activation_review_at')
    op.drop_column('payments', 'activation_review_reason')
    op.drop_column('payments', 'activation_review_status')
