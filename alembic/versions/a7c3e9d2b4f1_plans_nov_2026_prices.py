"""Plan prices, Nov 2026: buyer and owner (seller) plan sheet.

- plans.mrp_paise: optional original price shown struck through next to the
  offer price (price_paise stays the amount charged).
- Buyer plans: FREE 3 leads / 15 days; PLUS 10 leads / 30 days / Rs 1,499;
  PRO 30 leads / 60 days / Rs 2,499; UNLIMITED / 90 days / Rs 4,999.
- Owner plans: Super, Premium, Elite, Special, Business, Unlimited Business at
  the sheet's offer prices, validity and post counts; the Startup plan is
  archived (hidden and inactive; existing subscriptions keep their terms).

Rows are matched by plan code and only updated if they exist, so this is safe
on databases that were never seeded. Active subscriptions are not changed:
they keep the terms they were bought on.

Revision ID: a7c3e9d2b4f1
Revises: f4445dd86627
"""
from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = 'a7c3e9d2b4f1'
down_revision = 'f4445dd86627'
branch_labels = None
depends_on = None

_BASE = ["Unlimited Leads", "City Limit Reach"]
_EXTRA = _BASE + ["Relationship Manager Assistance", "Social Media Promotion"]

# code -> (price rupees, mrp rupees, days, listing limit or None=unlimited, features, recommended)
_OWNER = {
    'RESTAMP_OWNER_SUPER': (999, 1600, 45, 3, _BASE, False),
    'RESTAMP_OWNER_PREMIUM': (1799, 2000, 60, 5, _BASE, False),
    'RESTAMP_OWNER_ELITE': (2499, 3000, 90, 20, _EXTRA, True),
    'RESTAMP_OWNER_SPECIAL': (3499, 4000, 90, 50, _EXTRA, False),
    'RESTAMP_OWNER_BUSINESS': (49999, 70000, 90, 250, _EXTRA, False),
    'RESTAMP_OWNER_UNLIMITED_BUSINESS': (99999, 125000, 180, None, _EXTRA, False),
}

# code -> (price rupees, days, lead limit or None=unlimited, label)
_BUYER = {
    'RESTAMP_BUYER_FREE': (0, 15, 3, '0 – 3 leads'),
    'RESTAMP_BUYER_PLUS': (1499, 30, 10, '4 – 10 leads'),
    'RESTAMP_BUYER_PRO': (2499, 60, 30, '11 – 30 leads'),
    'RESTAMP_BUYER_UNLIMITED': (4999, 90, None, 'Unlimited leads'),
}


def upgrade():
    op.add_column('plans', sa.Column('mrp_paise', sa.BigInteger(), nullable=True))
    conn = op.get_bind()

    for code, (price, mrp, days, limit, feats, rec) in _OWNER.items():
        desc = 'Unlimited property posts' if limit is None else f'{limit} property posts'
        conn.execute(
            sa.text(
                "UPDATE plans SET price_paise=:p, mrp_paise=:m, duration_days=:d, listing_limit=:l, "
                "description=:desc, features=:f, recommended=:r, version=version+1 WHERE code=:c"
            ),
            {'p': price * 100, 'm': mrp * 100, 'd': days, 'l': limit, 'desc': desc,
             'f': json.dumps(feats), 'r': rec, 'c': code},
        )

    conn.execute(sa.text(
        "UPDATE plans SET status='INACTIVE', visible=:v, archived_at=CURRENT_TIMESTAMP, version=version+1 "
        "WHERE code='RESTAMP_OWNER_STARTUP'"
    ), {'v': False})

    for code, (price, days, limit, label) in _BUYER.items():
        conn.execute(
            sa.text(
                "UPDATE plans SET price_paise=:p, duration_days=:d, lead_limit=:l, description=:desc, "
                "features=:f, version=version+1 WHERE code=:c"
            ),
            {'p': price * 100, 'd': days, 'l': limit, 'desc': label, 'f': json.dumps([label]), 'c': code},
        )


def downgrade():
    # Prices are business data; earlier values are kept in plan_versions history.
    op.drop_column('plans', 'mrp_paise')
