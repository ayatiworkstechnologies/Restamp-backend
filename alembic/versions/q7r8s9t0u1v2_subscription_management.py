"""RESTAMP subscription management: admin-managed plans, plan history, admin
subscription changes and the temporary payment bypass.

Revision ID: q7r8s9t0u1v2
Revises: p6q7r8s9t0u1
Create Date: 2026-11-19

ADDITIVE ONLY. Why each change is needed:
- plans: the catalogue rows only held code/name/price/duration. Admin plan
  management needs the role a plan is sold to, its billing type, entitlement
  limits (listings / buyer leads / agent localities), marketing features,
  visibility, a version number, an archive marker and who last changed it.
  All new columns are nullable or have server defaults, so existing rows stay
  valid. Existing buyer/agent catalogue rows are backfilled with the role and
  limits their code-defined plans already enforce (no behaviour change).
- plan_versions: pricing history. One immutable snapshot per plan version.
- subscriptions: admin suspension (suspended_at), how the subscription was
  activated (source: PAYMENT / BYPASS / ADMIN / TRIAL / SIMULATED), who
  assigned it, why it ended, and an update timestamp. All nullable: existing
  subscriptions are unaffected (NULL suspended_at = not suspended).
No existing column, index or row is removed. downgrade() drops only what is added.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = 'q7r8s9t0u1v2'
down_revision = 'p6q7r8s9t0u1'
branch_labels = None
depends_on = None

_BUYER_LEADS = {  # code -> lead limit (NULL = unlimited), as enforced by services/buyer_plans.py
    'RESTAMP_BUYER_FREE': 3, 'RESTAMP_BUYER_PLUS': 10, 'RESTAMP_BUYER_PRO': 20, 'RESTAMP_BUYER_UNLIMITED': None,
}
_AGENT_LOCALITIES = {  # code -> locality limit, as enforced by services/agent_plans.py
    'RESTAMP_AGENT_STARTER': 5, 'RESTAMP_AGENT_PRO': 10, 'RESTAMP_AGENT_ELITE': 15, 'RESTAMP_AGENT_TRIAL': 1,
}


def upgrade():
    op.add_column('plans', sa.Column('role', sa.String(10), nullable=True))
    op.add_column('plans', sa.Column('billing_cycle', sa.String(20), nullable=False, server_default='one_time'))
    op.add_column('plans', sa.Column('listing_limit', sa.Integer(), nullable=True))
    op.add_column('plans', sa.Column('lead_limit', sa.Integer(), nullable=True))
    op.add_column('plans', sa.Column('location_limit', sa.Integer(), nullable=True))
    op.add_column('plans', sa.Column('features', sa.JSON(), nullable=True))
    op.add_column('plans', sa.Column('visible', sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column('plans', sa.Column('recommended', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('plans', sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('plans', sa.Column('version', sa.Integer(), nullable=False, server_default='1'))
    op.add_column('plans', sa.Column('archived_at', sa.DateTime(), nullable=True))
    op.add_column('plans', sa.Column('updated_at', sa.DateTime(), nullable=True))
    op.add_column('plans', sa.Column('updated_by_user_id', mysql.BIGINT(unsigned=True), nullable=True))
    op.create_index('idx_plans_role', 'plans', ['role'])

    op.create_table(
        'plan_versions',
        sa.Column('id', mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column('plan_id', mysql.BIGINT(unsigned=True),
                  sa.ForeignKey('plans.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('snapshot', sa.JSON(), nullable=False),
        sa.Column('change_note', sa.String(500), nullable=True),
        sa.Column('changed_by_user_id', mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.UniqueConstraint('plan_id', 'version', name='uk_plan_versions_plan_version'),
    )

    op.add_column('subscriptions', sa.Column('suspended_at', sa.DateTime(), nullable=True))
    op.add_column('subscriptions', sa.Column('source', sa.String(20), nullable=True))
    op.add_column('subscriptions', sa.Column('assigned_by_user_id', mysql.BIGINT(unsigned=True), nullable=True))
    op.add_column('subscriptions', sa.Column('ended_reason', sa.String(255), nullable=True))
    op.add_column('subscriptions', sa.Column('updated_at', sa.DateTime(), nullable=True))
    op.create_index('idx_subscriptions_user_status', 'subscriptions', ['user_id', 'status'])

    # Backfill the role/limits the code-defined plans already enforce.
    conn = op.get_bind()
    for code, leads in _BUYER_LEADS.items():
        conn.execute(sa.text("UPDATE plans SET role='BUYER', lead_limit=:l WHERE code=:c"), {'l': leads, 'c': code})
    for code, locs in _AGENT_LOCALITIES.items():
        conn.execute(sa.text("UPDATE plans SET role='BROKER', location_limit=:l WHERE code=:c"), {'l': locs, 'c': code})


def downgrade():
    op.drop_index('idx_subscriptions_user_status', table_name='subscriptions')
    for col in ('updated_at', 'ended_reason', 'assigned_by_user_id', 'source', 'suspended_at'):
        op.drop_column('subscriptions', col)
    op.drop_table('plan_versions')
    op.drop_index('idx_plans_role', table_name='plans')
    for col in ('updated_by_user_id', 'updated_at', 'archived_at', 'version', 'sort_order', 'recommended',
                'visible', 'features', 'location_limit', 'lead_limit', 'listing_limit', 'billing_cycle', 'role'):
        op.drop_column('plans', col)
