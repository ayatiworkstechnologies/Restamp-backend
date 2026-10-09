"""RESTAMP persistent owner drafts (drafts phase)

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-10-08

ADDITIVE ONLY: creates listing_drafts (owner-scoped, JSON form state).
No existing table/column/data touched. form_data holds the complete
7-step form snapshot; photos persist as metadata (remote URLs stay valid,
device files re-upload at publish via the existing upload endpoint).
No fake PropertyListing rows are created for drafts.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = 'd5e6f7a8b9c0'
down_revision = 'c4d5e6f7a8b9'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'listing_drafts',
        sa.Column('id', mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column('user_id', mysql.BIGINT(unsigned=True),
                  sa.ForeignKey('users.id', ondelete='CASCADE', onupdate='CASCADE'),
                  nullable=False),
        sa.Column('transaction_type', sa.String(length=20), nullable=False, server_default='RENT'),
        sa.Column('title', sa.String(length=255), nullable=True),
        sa.Column('current_step', mysql.SMALLINT(unsigned=True), nullable=False, server_default='1'),
        sa.Column('form_data', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.Index('idx_listing_drafts_user_updated', 'user_id', 'updated_at'),
    )


def downgrade():
    op.drop_table('listing_drafts')
