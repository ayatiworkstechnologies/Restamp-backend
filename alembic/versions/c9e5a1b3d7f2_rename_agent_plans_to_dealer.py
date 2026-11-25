"""Rename the Agent plan display names to Dealer (names only).

Only plans.name changes for the four RESTAMP_AGENT_* plan codes, and only
while the name is still the original "Agent ..." text (an admin-edited name
is left alone). Codes, prices, limits and subscriptions are untouched.

Revision ID: c9e5a1b3d7f2
Revises: b8d4f0a2c6e1
"""
from alembic import op
import sqlalchemy as sa

revision = "c9e5a1b3d7f2"
down_revision = "b8d4f0a2c6e1"
branch_labels = None
depends_on = None

NAMES = [
    ("RESTAMP_AGENT_STARTER", "Agent Starter Plan", "Dealer Starter Plan"),
    ("RESTAMP_AGENT_PRO", "Agent Pro Plan", "Dealer Pro Plan"),
    ("RESTAMP_AGENT_ELITE", "Agent Elite Plan", "Dealer Elite Plan"),
    ("RESTAMP_AGENT_TRIAL", "Agent Free Trial", "Dealer Free Trial"),
]


def _rename(pairs):
    stmt = sa.text("UPDATE plans SET name = :new WHERE code = :code AND name = :old")
    for code, old, new in pairs:
        op.execute(stmt.bindparams(code=code, old=old, new=new))


def upgrade():
    _rename(NAMES)


def downgrade():
    _rename([(c, new, old) for c, old, new in NAMES])
