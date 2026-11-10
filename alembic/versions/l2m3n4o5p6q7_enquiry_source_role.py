"""RESTAMP enquiry source role: was the lead sent by a buyer or an agent?

Revision ID: l2m3n4o5p6q7
Revises: k1l2m3n4o5p6
Create Date: 2026-11-05

ADDITIVE ONLY. enquiries.source_role records the sender's current role
(BUYER or BROKER = agent) when the enquiry is created, so an owner can tell
the two apart even after the sender later switches modes (the current role
alone would relabel old leads).

Existing rows stay NULL and the API reports them as BUYER. That is a fact,
not a guess: before this revision POST /buyer/enquiries accepted only the
BUYER role, so every existing enquiry was sent by an account in BUYER mode.
No existing row or column is modified; downgrade drops only this column.
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'l2m3n4o5p6q7'
down_revision = 'k1l2m3n4o5p6'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('enquiries', sa.Column('source_role', sa.String(20), nullable=True))


def downgrade():
    op.drop_column('enquiries', 'source_role')
