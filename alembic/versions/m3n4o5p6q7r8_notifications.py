"""RESTAMP in-app notifications table (notification system, Phase 1).

Revision ID: m3n4o5p6q7r8
Revises: l2m3n4o5p6q7
Create Date: 2026-11-14

ADDITIVE ONLY. Creates the new `notifications` table; no existing table,
column or row is modified, so every existing row is unaffected. The table
starts empty: notifications are generated only by business events that
happen after this revision (nothing is back-filled or invented).

UNIQUE (user_id, dedupe_key) makes notification creation idempotent per
recipient; the two composite indexes serve the newest-first list and the
unread count, and property_listing_id is indexed for its foreign key
(ON DELETE SET NULL: a notification outlives a hard-deleted listing row,
although listings are only ever soft-deleted). Approved schema addition
outside RESTAMP_DATABASE_ARCHITECTURE_V2.4.2 (notification review, 2026-11-14).
downgrade() drops only this table.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = 'm3n4o5p6q7r8'
down_revision = 'l2m3n4o5p6q7'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'notifications',
        sa.Column('id', mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column('user_id', mysql.BIGINT(unsigned=True),
                  sa.ForeignKey('users.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('audience', sa.Enum('BUYER', 'OWNER', 'ACCOUNT'), nullable=False),
        sa.Column('type', sa.String(50), nullable=False),
        sa.Column('category', sa.Enum('PROPERTIES', 'VISITS', 'SYSTEM'), nullable=False),
        sa.Column('title', sa.String(160), nullable=False),
        sa.Column('body', sa.String(500), nullable=False),
        sa.Column('entity_type', sa.String(30), nullable=True),
        sa.Column('entity_id', mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column('property_listing_id', mysql.BIGINT(unsigned=True),
                  sa.ForeignKey('property_listings.id', ondelete='SET NULL', onupdate='CASCADE'),
                  nullable=True),
        sa.Column('dedupe_key', sa.String(191), nullable=False),
        sa.Column('read_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('user_id', 'dedupe_key', name='uk_notifications_user_dedupe'),
    )
    op.create_index('idx_notifications_user_list', 'notifications',
                    ['user_id', 'deleted_at', 'id'])
    op.create_index('idx_notifications_user_unread', 'notifications',
                    ['user_id', 'read_at', 'deleted_at', 'audience'])
    op.create_index('idx_notifications_listing', 'notifications', ['property_listing_id'])


def downgrade():
    op.drop_table('notifications')
