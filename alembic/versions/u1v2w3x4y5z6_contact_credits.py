"""RESTAMP Phase 3: permanent property-contact unlocks, lifetime free unlocks
and purchased contact credits (ledger).

Revision ID: u1v2w3x4y5z6
Revises: t0u1v2w3x4y5
Create Date: 2026-11-20

ADDITIVE ONLY (three new tables + one hidden plan row). Why:
- property_contact_unlocks: the permanent entitlement, UNIQUE(user, listing),
  so one user can never be charged twice for one property.
- contact_credit_accounts: one row per user holding the lifetime free-unlock
  usage (0..3) and the purchased credit balance (CHECK >= 0). It is the row
  every unlock/purchase locks, which serialises them per user.
- contact_credit_ledger: append-only audit of every unlock (UNIQUE unlock_id)
  and every credit purchase (UNIQUE payment_id -> a payment credits once).
- plans row RESTAMP_CONTACT_CREDITS_25: the 25-credit pack. role NULL keeps it
  out of the subscription catalogues; status INACTIVE and price 0 until an
  admin sets the commercial price (not decided yet), so it cannot be bought.

Backfill (existing access is never removed, no free unlocks are added):
- every DISTINCT (user, listing) pair in contact_reveal_audits becomes a
  permanent LEGACY unlock (unlocked_at = first reveal). Repeated reveals of the
  same property count once.
- each such user gets an account with free_unlocks_used = LEAST(3, distinct
  pairs): a user who revealed 1 keeps 2 free unlocks, 3 or more keeps none
  (all of them stay unlocked).
- one LEGACY_UNLOCK ledger row per backfilled unlock; the first three (by
  first reveal) carry free_delta 1, later ones 0.
Existing subscriptions, payments and audit rows are not touched.
downgrade() drops the three tables and the pack row (only if no payment
references it).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = 'u1v2w3x4y5z6'
down_revision = 't0u1v2w3x4y5'
branch_labels = None
depends_on = None

_ID = mysql.BIGINT(unsigned=True)
PACK_CODE = 'RESTAMP_CONTACT_CREDITS_25'


def upgrade():
    op.create_table(
        'contact_credit_accounts',
        sa.Column('user_id', _ID, sa.ForeignKey('users.id', ondelete='CASCADE', onupdate='CASCADE'), primary_key=True),
        sa.Column('free_unlocks_used', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('credit_balance', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint('credit_balance >= 0', name='ck_contact_credit_balance_nonneg'),
        sa.CheckConstraint('free_unlocks_used >= 0 AND free_unlocks_used <= 3', name='ck_contact_free_used_range'),
    )
    op.create_table(
        'property_contact_unlocks',
        sa.Column('id', _ID, primary_key=True, autoincrement=True),
        sa.Column('user_id', _ID, sa.ForeignKey('users.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('property_listing_id', _ID, sa.ForeignKey('property_listings.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('source', sa.Enum('FREE', 'CREDIT', 'PLAN', 'LEGACY'), nullable=False),
        sa.Column('subscription_id', _ID, sa.ForeignKey('subscriptions.id', ondelete='SET NULL', onupdate='CASCADE'), nullable=True),
        sa.Column('unlocked_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('user_id', 'property_listing_id', name='uk_contact_unlock_user_listing'),
    )
    op.create_index('idx_contact_unlocks_listing', 'property_contact_unlocks', ['property_listing_id'])
    op.create_table(
        'contact_credit_ledger',
        sa.Column('id', _ID, primary_key=True, autoincrement=True),
        sa.Column('user_id', _ID, sa.ForeignKey('users.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('entry_type', sa.Enum('PURCHASE', 'FREE_UNLOCK', 'CREDIT_UNLOCK', 'PLAN_UNLOCK', 'LEGACY_UNLOCK',
                                        'ADJUSTMENT'), nullable=False),
        sa.Column('credits_delta', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('free_delta', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('balance_after', sa.Integer(), nullable=False),
        sa.Column('payment_id', _ID, sa.ForeignKey('payments.id', ondelete='SET NULL', onupdate='CASCADE'), nullable=True),
        sa.Column('unlock_id', _ID, sa.ForeignKey('property_contact_unlocks.id', ondelete='SET NULL', onupdate='CASCADE'), nullable=True),
        sa.Column('reference', sa.String(100), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('payment_id', name='uk_contact_ledger_payment'),
        sa.UniqueConstraint('unlock_id', name='uk_contact_ledger_unlock'),
    )
    op.create_index('idx_contact_ledger_user', 'contact_credit_ledger', ['user_id', 'id'])

    op.execute(
        "INSERT INTO plans (code, name, description, price_paise, duration_days, status, role, billing_cycle,"
        " features, visible, recommended, sort_order, version, created_at)"
        f" SELECT '{PACK_CODE}', 'RESTAMP Contact Credits (25)',"
        " '25 credits; one credit permanently unlocks one property contact. Credits never expire.',"
        " 0, NULL, 'INACTIVE', NULL, 'one_time', JSON_OBJECT('credits', 25), 0, 0, 0, 1, UTC_TIMESTAMP()"
        f" FROM DUAL WHERE NOT EXISTS (SELECT 1 FROM plans WHERE code = '{PACK_CODE}')"
    )
    # Permanent LEGACY unlocks from distinct historical reveals.
    op.execute(
        "INSERT INTO property_contact_unlocks (user_id, property_listing_id, source, unlocked_at)"
        " SELECT a.user_id, a.property_listing_id, 'LEGACY', MIN(a.revealed_at)"
        " FROM contact_reveal_audits a JOIN property_listings l ON l.id = a.property_listing_id"
        " WHERE a.property_listing_id IS NOT NULL GROUP BY a.user_id, a.property_listing_id"
    )
    op.execute(
        "INSERT INTO contact_credit_accounts (user_id, free_unlocks_used, credit_balance, created_at, updated_at)"
        " SELECT user_id, LEAST(3, COUNT(*)), 0, UTC_TIMESTAMP(), UTC_TIMESTAMP()"
        " FROM property_contact_unlocks GROUP BY user_id"
    )
    op.execute(
        "INSERT INTO contact_credit_ledger (user_id, entry_type, credits_delta, free_delta, balance_after, unlock_id,"
        " reference, created_at)"
        " SELECT user_id, 'LEGACY_UNLOCK', 0, IF(rn <= 3, 1, 0), 0, id, 'migration:u1v2w3x4y5z6', unlocked_at FROM ("
        "  SELECT id, user_id, unlocked_at, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY unlocked_at, id) rn"
        "  FROM property_contact_unlocks WHERE source = 'LEGACY') t"
    )


def downgrade():
    # Dropping a table drops its indexes (MySQL refuses to drop an index
    # that backs a foreign key on its own).
    op.drop_table('contact_credit_ledger')
    op.drop_table('property_contact_unlocks')
    op.drop_table('contact_credit_accounts')
    op.execute(
        f"DELETE FROM plan_versions WHERE plan_id IN (SELECT id FROM (SELECT id FROM plans WHERE code = '{PACK_CODE}') p)"
    )
    op.execute(
        f"DELETE FROM plans WHERE code = '{PACK_CODE}'"
        f" AND NOT EXISTS (SELECT 1 FROM payments WHERE plan_id = plans.id)"
    )
