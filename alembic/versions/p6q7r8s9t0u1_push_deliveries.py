"""RESTAMP push delivery per device + Expo receipts (notification system Phase 4, revision 2).

Revision ID: p6q7r8s9t0u1
Revises: o5p6q7r8s9t1
Create Date: 2026-11-17

ADDITIVE ONLY. Creates `push_deliveries`: one row per (push_outbox row,
device). The recipient-level push_outbox keeps event dedupe; this table tracks
each device separately, so a device that already received its ticket is never
sent again when a sibling device fails, and Expo tickets/receipts are recorded
per device. UNIQUE (outbox_id, device_id) makes the fan-out idempotent across
worker crashes and concurrent workers. No existing table, column or row is
modified: outbox rows queued earlier get their device rows the next time the
worker processes them. downgrade() drops only this table.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = 'p6q7r8s9t0u1'
down_revision = 'o5p6q7r8s9t1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'push_deliveries',
        sa.Column('id', mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column('outbox_id', mysql.BIGINT(unsigned=True),
                  sa.ForeignKey('push_outbox.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('device_id', mysql.BIGINT(unsigned=True),
                  sa.ForeignKey('push_devices.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('status', sa.Enum('PENDING', 'PROCESSING', 'SENT', 'DELIVERED', 'FAILED'), nullable=False,
                  server_default='PENDING'),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('next_attempt_at', sa.DateTime(), nullable=False),
        sa.Column('locked_at', sa.DateTime(), nullable=True),
        sa.Column('ticket_id', sa.String(191), nullable=True),
        sa.Column('receipt_due_at', sa.DateTime(), nullable=True),
        sa.Column('receipt_attempts', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('last_error', sa.String(60), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('sent_at', sa.DateTime(), nullable=True),
        sa.Column('delivered_at', sa.DateTime(), nullable=True),
        sa.Column('failed_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('outbox_id', 'device_id', name='uk_push_deliveries_outbox_device'),
    )
    op.create_index('idx_push_deliveries_due', 'push_deliveries', ['status', 'next_attempt_at'])
    op.create_index('idx_push_deliveries_receipt', 'push_deliveries', ['status', 'receipt_due_at'])
    op.create_index('idx_push_deliveries_device', 'push_deliveries', ['device_id'])


def downgrade():
    op.drop_table('push_deliveries')
