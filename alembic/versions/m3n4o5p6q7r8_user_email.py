"""RESTAMP user email: optional contact email on users.

Revision ID: m3n4o5p6q7r8
Revises: l2m3n4o5p6q7
Create Date: 2026-11-13

ADDITIVE ONLY. Nullable users.email set by the user via PATCH /users/me.
Existing rows stay NULL. Downgrade drops only this column.
"""
from alembic import op
import sqlalchemy as sa

revision = 'm3n4o5p6q7r8'
down_revision = 'l2m3n4o5p6q7'
branch_labels = None
depends_on = None


def upgrade():
    # Skip when the app's startup guard already added it.
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("users")}
    if "email" not in cols:
        op.add_column('users', sa.Column('email', sa.String(255), nullable=True))


def downgrade():
    op.drop_column('users', 'email')
