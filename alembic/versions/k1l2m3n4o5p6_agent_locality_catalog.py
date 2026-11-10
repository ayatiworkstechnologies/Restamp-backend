"""RESTAMP agent locality catalog: admin enable/disable flag + locality aliases.

Revision ID: k1l2m3n4o5p6
Revises: j0k1l2m3n4o5
Create Date: 2026-11-04

ADDITIVE ONLY. Three separate facts are kept apart:
  - a locality EXISTS in the master data      -> localities row (unchanged)
  - it is ENABLED for agent selection          -> localities.agent_enabled (new)
  - it is ASSIGNED to an agent                 -> broker_postcode_access_current
                                                  rows (unchanged)

- localities.agent_enabled: TINYINT(1) NOT NULL, server default 1, so every
  existing locality stays selectable exactly as before this revision; only
  an admin action (audited in audit_logs) turns one off. Disabling never
  touches existing grants (see services/localities.py).
- locality_aliases: alternative spellings / other names for a canonical
  locality ("Pondy Bazaar" -> T. Nagar), so variants resolve without creating
  duplicate locality rows. alias_key is the normalised alias (lower case,
  letters and digits only) and is UNIQUE, so an alias can never point at two
  localities. created_at is app-set UTC (no server default).
No existing row or column is modified; downgrade drops only what is added.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = 'k1l2m3n4o5p6'
down_revision = 'j0k1l2m3n4o5'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'localities',
        sa.Column('agent_enabled', mysql.TINYINT(1), nullable=False, server_default=sa.text('1')),
    )
    op.create_table(
        'locality_aliases',
        sa.Column('id', mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column('locality_id', mysql.BIGINT(unsigned=True),
                  sa.ForeignKey('localities.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('alias', sa.String(255), nullable=False),
        sa.Column('alias_key', sa.String(255), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('alias_key', name='uk_locality_alias_key'),
        sa.Index('idx_locality_aliases_locality', 'locality_id'),
    )


def downgrade():
    op.drop_table('locality_aliases')
    op.drop_column('localities', 'agent_enabled')
