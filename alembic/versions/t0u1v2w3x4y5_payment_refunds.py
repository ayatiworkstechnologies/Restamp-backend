"""RESTAMP payment_refunds: audited admin refunds of verified payments that
were flagged for manual review (Phase 2 remaining fixes).

Revision ID: t0u1v2w3x4y5
Revises: s9t0u1v2w3x4
Create Date: 2026-11-20

ADDITIVE ONLY: one new table, nothing existing is altered. Why: a verified
payment that could not be activated (Owner/Dealer conflict) needs a safe,
auditable resolution. The refund lives in its own row (one per payment, the
gateway refund id unique) so the original SUCCESS payment is never rewritten
and a refund can never be requested twice. downgrade() drops the table.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = 't0u1v2w3x4y5'
down_revision = 's9t0u1v2w3x4'
branch_labels = None
depends_on = None

_ID = mysql.BIGINT(unsigned=True)


def upgrade():
    op.create_table(
        'payment_refunds',
        sa.Column('id', _ID, primary_key=True, autoincrement=True),
        sa.Column('payment_id', _ID, sa.ForeignKey('payments.id', ondelete='RESTRICT', onupdate='CASCADE'), nullable=False),
        sa.Column('user_id', _ID, sa.ForeignKey('users.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('plan_id', _ID, sa.ForeignKey('plans.id', ondelete='SET NULL', onupdate='CASCADE'), nullable=True),
        sa.Column('intended_poster_type', sa.String(10), nullable=True),
        sa.Column('original_payment_status', sa.String(20), nullable=False),
        sa.Column('review_reason', sa.String(255), nullable=True),
        sa.Column('status', sa.Enum('REQUESTED', 'PENDING', 'PROCESSED', 'FAILED'), nullable=False, server_default='REQUESTED'),
        sa.Column('amount_paise', sa.BigInteger(), nullable=False),
        sa.Column('provider', sa.String(50), nullable=False),
        sa.Column('provider_refund_id', sa.String(255), nullable=True),
        sa.Column('gateway_status', sa.String(30), nullable=True),
        sa.Column('failure_code', sa.String(100), nullable=True),
        sa.Column('admin_user_id', _ID, sa.ForeignKey('users.id', ondelete='RESTRICT', onupdate='CASCADE'), nullable=False),
        sa.Column('admin_reason', sa.String(500), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('requested_at', sa.DateTime(), nullable=False),
        sa.Column('processed_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('payment_id', name='uk_payment_refunds_payment'),
        sa.UniqueConstraint('provider', 'provider_refund_id', name='uk_payment_refunds_provider_refund'),
    )
    op.create_index('idx_payment_refunds_status', 'payment_refunds', ['status'])


def downgrade():
    op.drop_table('payment_refunds')  # drops its indexes too
