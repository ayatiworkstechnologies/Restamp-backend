#!/usr/bin/env python3
"""RESTAMP plan catalogue seed — LOCAL development databases ONLY.

Inserts the system subscription plans that are MISSING (matched by plan code):
  - Buyer plans and agent plans: from the code catalogues the backend already
    enforces (services/buyer_plans.py, services/agent_plans.py).
  - Owner plans: the owner tiers the website previously hard-coded
    (src/lib/subscription-seed.ts / ownerPlan.ts in restamp-website-clean).
Idempotent and non-destructive: an existing plan row (including one an admin has
customised) is NEVER updated. No subscriptions or payments are created. Refuses
any non-local database URL.

    cd Restamp-backend && set -a && source .env && set +a
    python3 scripts/seed_plans.py            # insert missing plans
    python3 scripts/seed_plans.py --dry-run  # show what would be inserted
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import _is_local_database_url, settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Plan, PlanVersion  # noqa: E402
from app.services import agent_plans, buyer_plans  # noqa: E402
from app.services.subscriptions import _snapshot  # noqa: E402

_OWNER_FEATURES = ["Unlimited Leads", "City Limit Reach"]
_RM = ["Relationship Manager Assistance"]
_SOCIAL = ["Social Media Promotion"]
# Seller plan sheet (Nov 2026).
# code, name, original price (rupees), offer price (rupees), validity days,
# listing limit (-1 = unlimited), features
OWNER_TIERS = [
    ("RESTAMP_OWNER_SUPER", "Super Plan", 1600, 999, 45, 3, _OWNER_FEATURES),
    ("RESTAMP_OWNER_PREMIUM", "Premium Plan", 2000, 1799, 60, 5, _OWNER_FEATURES),
    ("RESTAMP_OWNER_ELITE", "Elite Plan", 3000, 2499, 90, 20, _OWNER_FEATURES + _RM + _SOCIAL),
    ("RESTAMP_OWNER_SPECIAL", "Special Plan", 4000, 3499, 90, 50, _OWNER_FEATURES + _RM + _SOCIAL),
    ("RESTAMP_OWNER_BUSINESS", "Business Plan", 70000, 49999, 90, 250, _OWNER_FEATURES + _RM + _SOCIAL),
    ("RESTAMP_OWNER_UNLIMITED_BUSINESS", "Unlimited Business Plan", 125000, 99999, 180, -1, _OWNER_FEATURES + _RM + _SOCIAL),
]


def catalogue() -> list[dict]:
    rows: list[dict] = []
    for i, p in enumerate(buyer_plans.ALL):
        rows.append(dict(code=p.code, role="BUYER", name=p.name, description=p.leads_label, price_paise=p.price_paise,
                         duration_days=p.duration_days, lead_limit=p.lead_limit, sort_order=i,
                         features=[p.leads_label]))
    for i, p in enumerate(agent_plans.PLANS):
        rows.append(dict(code=p.code, role="BROKER", name=p.name, description=f"{p.locality_limit} localities",
                         price_paise=p.price_paise, duration_days=p.duration_days, location_limit=p.locality_limit,
                         sort_order=i, features=[f"{p.locality_limit} localities"]))
    t = agent_plans.TRIAL
    rows.append(dict(code=t.code, role="BROKER", name=t.name, description="1 locality, free trial", price_paise=0,
                     duration_days=None, location_limit=t.locality_limit, sort_order=99, visible=False,
                     features=["1 locality"]))
    for i, (code, name, mrp, rupees, days, limit, feats) in enumerate(OWNER_TIERS):
        rows.append(dict(code=code, role="OWNER", name=name,
                         description="Unlimited property posts" if limit == -1 else f"{limit} property post{'s' if limit != 1 else ''}",
                         price_paise=rupees * 100, mrp_paise=mrp * 100, recommended=code == "RESTAMP_OWNER_ELITE",
                         duration_days=days, listing_limit=None if limit == -1 else limit,
                         sort_order=i, features=list(feats)))
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    if not _is_local_database_url(settings.DATABASE_URL):
        print("Refusing: RESTAMP_DATABASE_URL is not a local database.")
        return 2
    db = SessionLocal()
    try:
        existing = {c for (c,) in db.query(Plan.code).all()}
        added = 0
        for row in catalogue():
            if row["code"] in existing:
                continue  # never touch an existing (possibly customised) plan
            added += 1
            print(f"+ {row['role']:6} {row['code']}")
            if args.dry_run:
                continue
            plan = Plan(status="ACTIVE", billing_cycle="one_time", version=1, **row)
            db.add(plan)
            db.flush()
            db.add(PlanVersion(plan_id=plan.id, version=1, snapshot=_snapshot(plan), change_note="Seeded"))
        if not args.dry_run:
            db.commit()
        print(f"{added} plan(s) {'would be ' if args.dry_run else ''}added; existing plans untouched.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
