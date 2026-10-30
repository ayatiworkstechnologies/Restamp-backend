"""RESTAMP enquiry messages, private lead notes, visit reschedule time, call-tap index.

Revision ID: j0k1l2m3n4o5
Revises: i9j0k1l2m3n4
Create Date: 2026-10-29

ADDITIVE ONLY (coordination/CONTRACT.md §1, decisions D1-D4):
- enquiry_messages: every distinct buyer message on an enquiry (D1). The
  opening message of enquiries created BEFORE this revision stays only in
  enquiries.message (no backfill); the API presents it as the first message
  with an unknown (null) time instead of inventing one.
- lead_notes: private owner notes on a lead (D4); never exposed to buyers.
- property_visits.rescheduled_at: NULL for every existing visit; set when a
  visit's time is changed and cleared when its status changes (D3 display).
- ix_activity_logs_action_entity: per-listing counts of call-button taps
  stored in the existing activity_logs table (D2; table already exists).
created_at values are app-set UTC (no server defaults: the MySQL server clock
is not UTC on every environment). No existing row or column is modified.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = 'j0k1l2m3n4o5'
down_revision = 'i9j0k1l2m3n4'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'enquiry_messages',
        sa.Column('id', mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column('enquiry_id', mysql.BIGINT(unsigned=True),
                  sa.ForeignKey('enquiries.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('sender_user_id', mysql.BIGINT(unsigned=True),
                  sa.ForeignKey('users.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Index('idx_enquiry_messages_enquiry', 'enquiry_id', 'id'),
    )
    op.create_table(
        'lead_notes',
        sa.Column('id', mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column('enquiry_id', mysql.BIGINT(unsigned=True),
                  sa.ForeignKey('enquiries.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('owner_user_id', mysql.BIGINT(unsigned=True),
                  sa.ForeignKey('users.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Index('idx_lead_notes_enquiry', 'enquiry_id', 'id'),
    )
    op.add_column('property_visits', sa.Column('rescheduled_at', sa.DateTime(), nullable=True))
    op.create_index('ix_activity_logs_action_entity', 'activity_logs',
                    ['action', 'entity_type', 'entity_id'])


def downgrade():
    op.drop_index('ix_activity_logs_action_entity', table_name='activity_logs')
    op.drop_column('property_visits', 'rescheduled_at')
    op.drop_table('lead_notes')
    op.drop_table('enquiry_messages')
