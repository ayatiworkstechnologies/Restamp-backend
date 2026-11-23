"""RESTAMP push notifications: devices + preferences + outbox (notification system, Phase 4).

Revision ID: o5p6q7r8s9t1
Revises: o5p6q7r8s9t0 (shared rate-limit windows; chained after it to keep a
single linear head — both revisions are additive and independent)
Create Date: 2026-11-16

ADDITIVE ONLY. Creates three new tables; no existing table, column or row is
modified, so every existing row is unaffected. All three start empty: no
device is invented for an existing account and nothing is queued for events
that happened before this revision.

- push_devices: registered Expo devices. The full token is stored ONLY in
  encrypted form (token_enc); token_hash (SHA-256) is UNIQUE globally so one
  physical token is one row and a device that switches accounts takes over
  its row; token_prefix is a masked display form. status ACTIVE/INACTIVE/
  INVALID tracks the device lifecycle.
- push_preferences: one row per user (PRIMARY KEY user_id) with the user's
  push preferences, independent of email and in-app preferences. Rows are
  created lazily; no row means every optional push is on (safe default).
- push_outbox: one row per push to deliver, sent by the background worker.
  UNIQUE (user_id, dedupe_key) makes queuing idempotent per recipient;
  (status, next_attempt_at) serves the worker's claim query. notification_id
  is ON DELETE SET NULL (a delivery record outlives its in-app row).

Approved schema addition outside RESTAMP_DATABASE_ARCHITECTURE_V2.4.2
(notification system Phase 4). downgrade() drops only these three tables.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = 'o5p6q7r8s9t1'
down_revision = 'o5p6q7r8s9t0'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'push_devices',
        sa.Column('id', mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column('user_id', mysql.BIGINT(unsigned=True),
                  sa.ForeignKey('users.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('platform', sa.Enum('ios', 'android'), nullable=False),
        sa.Column('token_hash', sa.String(64), nullable=False),
        sa.Column('token_enc', sa.String(500), nullable=False),
        sa.Column('token_prefix', sa.String(80), nullable=False),
        sa.Column('app_version', sa.String(50), nullable=True),
        sa.Column('device_name', sa.String(100), nullable=True),
        sa.Column('status', sa.Enum('ACTIVE', 'INACTIVE', 'INVALID'), nullable=False,
                  server_default='ACTIVE'),
        sa.Column('invalid_reason', sa.String(60), nullable=True),
        sa.Column('registered_at', sa.DateTime(), nullable=False),
        sa.Column('refreshed_at', sa.DateTime(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('token_hash', name='uk_push_devices_token'),
    )
    op.create_index('idx_push_devices_user', 'push_devices', ['user_id', 'status'])
    op.create_table(
        'push_preferences',
        sa.Column('user_id', mysql.BIGINT(unsigned=True),
                  sa.ForeignKey('users.id', ondelete='CASCADE', onupdate='CASCADE'),
                  primary_key=True, autoincrement=False),
        sa.Column('pref_enquiries', sa.Boolean(), nullable=False, server_default=sa.text('1')),
        sa.Column('pref_visits', sa.Boolean(), nullable=False, server_default=sa.text('1')),
        sa.Column('pref_listings', sa.Boolean(), nullable=False, server_default=sa.text('1')),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_table(
        'push_outbox',
        sa.Column('id', mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column('user_id', mysql.BIGINT(unsigned=True),
                  sa.ForeignKey('users.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('notification_id', mysql.BIGINT(unsigned=True),
                  sa.ForeignKey('notifications.id', ondelete='SET NULL', onupdate='CASCADE'), nullable=True),
        sa.Column('category', sa.Enum('ENQUIRY', 'VISIT', 'LISTING', 'ACCOUNT'), nullable=False),
        sa.Column('template', sa.String(50), nullable=False),
        sa.Column('title', sa.String(160), nullable=False),
        sa.Column('body', sa.String(500), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=True),
        sa.Column('dedupe_key', sa.String(191), nullable=False),
        sa.Column('status', sa.Enum('PENDING', 'PROCESSING', 'SENT', 'FAILED'), nullable=False,
                  server_default='PENDING'),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('next_attempt_at', sa.DateTime(), nullable=False),
        sa.Column('locked_at', sa.DateTime(), nullable=True),
        sa.Column('provider', sa.String(30), nullable=True),
        sa.Column('provider_message_id', sa.String(191), nullable=True),
        sa.Column('last_error', sa.String(60), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('sent_at', sa.DateTime(), nullable=True),
        sa.Column('failed_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('user_id', 'dedupe_key', name='uk_push_outbox_user_dedupe'),
    )
    op.create_index('idx_push_outbox_due', 'push_outbox', ['status', 'next_attempt_at'])
    op.create_index('idx_push_outbox_notification', 'push_outbox', ['notification_id'])
    op.create_index('idx_push_outbox_created', 'push_outbox', ['created_at'])


def downgrade():
    op.drop_table('push_outbox')
    op.drop_table('push_preferences')
    op.drop_table('push_devices')
