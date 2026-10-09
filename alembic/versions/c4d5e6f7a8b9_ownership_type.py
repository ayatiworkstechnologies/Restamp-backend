"""RESTAMP ownership type (API create/read phase gap fill)

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-10-07

ADDITIVE ONLY: one NULLABLE ENUM column on physical_properties.
No existing column/constraint/data change; existing rows keep NULL.
Downgrade removes only this column.
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'c4d5e6f7a8b9'
down_revision = 'b3c4d5e6f7a8'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'physical_properties',
        sa.Column(
            'ownership_type',
            sa.Enum('FREEHOLD', 'LEASEHOLD', 'COOPERATIVE_SOCIETY', 'POWER_OF_ATTORNEY'),
            nullable=True,
        ),
    )


def downgrade():
    op.drop_column('physical_properties', 'ownership_type')
