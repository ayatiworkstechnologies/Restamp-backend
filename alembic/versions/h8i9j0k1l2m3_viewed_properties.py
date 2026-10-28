"""RESTAMP buyer viewed-property history.

Revision ID: h8i9j0k1l2m3
Revises: g7h8i9j0k1l2
Create Date: 2026-10-26

ADDITIVE ONLY: creates viewed_properties (buyer viewed-property history).
No existing table/column/data touched. Retention (20 distinct properties per
buyer) is enforced in app/services/viewed.py, never by the database.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = 'h8i9j0k1l2m3'
down_revision = 'g7h8i9j0k1l2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'viewed_properties',
        sa.Column('id', mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column('buyer_user_id', mysql.BIGINT(unsigned=True),
                  sa.ForeignKey('users.id', ondelete='CASCADE', onupdate='CASCADE'),
                  nullable=False),
        sa.Column('property_listing_id', mysql.BIGINT(unsigned=True),
                  sa.ForeignKey('property_listings.id', ondelete='CASCADE', onupdate='CASCADE'),
                  nullable=False),
        sa.Column('viewed_at', sa.DateTime(),
                  server_default=sa.func.current_timestamp(),
                  nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('buyer_user_id', 'property_listing_id', name='uq_viewed_buyer_listing'),
        sa.Index('ix_viewed_properties_buyer_viewed_at', 'buyer_user_id', 'viewed_at'),
    )


def downgrade():
    op.drop_table('viewed_properties')
