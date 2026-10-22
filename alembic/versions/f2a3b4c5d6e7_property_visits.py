"""RESTAMP property visits (visits phase)

Revision ID: f2a3b4c5d6e7
Revises: e6f7a8b9c0d1
Create Date: 2026-10-17

ADDITIVE ONLY: creates property_visits (buyer/owner scheduled viewings).
No existing table/column/data touched. Status lifecycle (REQUESTED,
PROPOSED, CONFIRMED, DECLINED, CANCELLED, COMPLETED) is enforced in
app/services/visits.py, never by the database beyond the ENUM membership.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = 'f2a3b4c5d6e7'
down_revision = 'e6f7a8b9c0d1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'property_visits',
        sa.Column('id', mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column('property_listing_id', mysql.BIGINT(unsigned=True),
                  sa.ForeignKey('property_listings.id', ondelete='CASCADE', onupdate='CASCADE'),
                  nullable=False),
        sa.Column('buyer_user_id', mysql.BIGINT(unsigned=True),
                  sa.ForeignKey('users.id', ondelete='CASCADE', onupdate='CASCADE'),
                  nullable=False),
        sa.Column('owner_user_id', mysql.BIGINT(unsigned=True),
                  sa.ForeignKey('users.id', ondelete='CASCADE', onupdate='CASCADE'),
                  nullable=False),
        sa.Column('scheduled_at', sa.DateTime(), nullable=False),
        sa.Column('status', mysql.ENUM('REQUESTED', 'PROPOSED', 'CONFIRMED', 'DECLINED', 'CANCELLED', 'COMPLETED'),
                  nullable=False, server_default='REQUESTED'),
        sa.Column('initiator', mysql.ENUM('BUYER', 'OWNER'), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.Index('idx_visits_buyer', 'buyer_user_id'),
        sa.Index('idx_visits_owner', 'owner_user_id'),
        sa.Index('idx_visits_listing', 'property_listing_id'),
    )


def downgrade():
    op.drop_table('property_visits')
