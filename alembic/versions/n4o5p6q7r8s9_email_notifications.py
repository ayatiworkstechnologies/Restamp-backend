"""RESTAMP email notifications: address + verification + outbox (notification system, Phase 3).

Revision ID: n4o5p6q7r8s9
Revises: m3n4o5p6q7r8
Create Date: 2026-11-15

ADDITIVE ONLY. Creates three new tables; no existing table, column or row is
modified, so every existing row is unaffected. All three start empty: no
address is back-filled or invented, and nothing is queued for events that
happened before this revision.

- user_emails: at most one contact email per user (PRIMARY KEY user_id) with
  its verification time and the user's optional-email preferences. The
  address is a contact channel, not a login identity, so it is deliberately
  NOT unique across accounts (a uniqueness answer would let anyone probe
  which addresses are registered).
- email_verification_codes: expiring, single-use verification codes. Only a
  keyed hash (HMAC-SHA256) of the code is stored, never the code.
- email_outbox: one row per email to deliver, sent by the background worker.
  UNIQUE (user_id, dedupe_key) makes queuing idempotent per recipient;
  (status, next_attempt_at) serves the worker's claim query. notification_id
  is ON DELETE SET NULL (a delivery record outlives its in-app row).

Approved schema addition outside RESTAMP_DATABASE_ARCHITECTURE_V2.4.2
(notification review, Phase 3). downgrade() drops only these three tables.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = 'n4o5p6q7r8s9'
down_revision = 'm3n4o5p6q7r8'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'user_emails',
        sa.Column('user_id', mysql.BIGINT(unsigned=True),
                  sa.ForeignKey('users.id', ondelete='CASCADE', onupdate='CASCADE'),
                  primary_key=True, autoincrement=False),
        sa.Column('email', sa.String(254), nullable=False),
        sa.Column('verified_at', sa.DateTime(), nullable=True),
        sa.Column('pref_enquiries', sa.Boolean(), nullable=False, server_default=sa.text('1')),
        sa.Column('pref_visits', sa.Boolean(), nullable=False, server_default=sa.text('1')),
        sa.Column('pref_listings', sa.Boolean(), nullable=False, server_default=sa.text('1')),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_table(
        'email_verification_codes',
        sa.Column('id', mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column('user_id', mysql.BIGINT(unsigned=True),
                  sa.ForeignKey('users.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('email', sa.String(254), nullable=False),
        sa.Column('code_hash', sa.String(64), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('used_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('idx_email_codes_user_created', 'email_verification_codes', ['user_id', 'created_at'])
    op.create_table(
        'email_outbox',
        sa.Column('id', mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column('user_id', mysql.BIGINT(unsigned=True),
                  sa.ForeignKey('users.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('notification_id', mysql.BIGINT(unsigned=True),
                  sa.ForeignKey('notifications.id', ondelete='SET NULL', onupdate='CASCADE'), nullable=True),
        sa.Column('category', sa.Enum('VERIFICATION', 'ENQUIRY', 'VISIT', 'LISTING', 'ACCOUNT'), nullable=False),
        sa.Column('template', sa.String(50), nullable=False),
        sa.Column('to_email', sa.String(254), nullable=False),
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
        sa.UniqueConstraint('user_id', 'dedupe_key', name='uk_email_outbox_user_dedupe'),
    )
    op.create_index('idx_email_outbox_due', 'email_outbox', ['status', 'next_attempt_at'])
    op.create_index('idx_email_outbox_notification', 'email_outbox', ['notification_id'])
    op.create_index('idx_email_outbox_created', 'email_outbox', ['created_at'])


def downgrade():
    op.drop_table('email_outbox')
    op.drop_table('email_verification_codes')
    op.drop_table('user_emails')
