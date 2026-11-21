"""Shared rate-limit windows for the public Reva chat/transcribe endpoints.

Revision ID: o5p6q7r8s9t0
Revises: n4o5p6q7r8s9
Create Date: 2026-11-16

ADDITIVE ONLY. Creates one new table; no existing table, column or row is
modified, so every existing row is unaffected.

- rate_limit_windows: one row per (bucket_key, window_start) holding a
  fixed-window hit counter for the public Reva AI endpoints (POST /chat,
  POST /transcribe). The counter lives in the database — not process memory —
  so the limit is enforced across backend instances and survives restarts
  (there is no Redis in this stack; MySQL is the shared store every instance
  already has). Rows hold no user content: bucket keys are the caller IP or
  user id, which the endpoints already need for abuse control. The service
  prunes a bucket's superseded windows, so the table stays tiny.

downgrade() drops only this table.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = 'o5p6q7r8s9t0'
down_revision = 'n4o5p6q7r8s9'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'rate_limit_windows',
        sa.Column('id', mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column('bucket_key', sa.String(191), nullable=False),
        sa.Column('window_start', sa.DateTime(), nullable=False),
        sa.Column('hit_count', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.UniqueConstraint('bucket_key', 'window_start', name='uk_rate_limit_bucket_window'),
    )


def downgrade():
    op.drop_table('rate_limit_windows')
