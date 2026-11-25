"""Initialise and maintain the Agent Location Master (operator script).

  python -m scripts.init_location_master            # seed once from
                                                    # agent-locations/*.xlsx
                                                    # (idempotent; a re-run
                                                    # only applies reviewed
                                                    # primary-name changes)
  python -m scripts.init_location_master --dry-run  # report only, no writes
  python -m scripts.init_location_master --status   # versions / block state
  python -m scripts.init_location_master --resolve  # after reviewing a
                                                    # quarantined workbook:
                                                    # clear the block and
                                                    # republish from the DB

Requires the b8d4f0a2c6e1 migration and, outside development,
RESTAMP_LOCATION_MASTER_PATH (a persistent .xlsx path outside the repo).
This is NOT a bulk import: it only loads the reviewed source workbook.
"""
from __future__ import annotations

import argparse
import json
import sys


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--status", action="store_true")
    g.add_argument("--resolve", action="store_true")
    args = ap.parse_args()

    from app.db import SessionLocal
    from app.services import location_master as lm

    if args.dry_run:
        src = lm.read_source_workbook()
        groups, conflicts = lm.group_source(src)
        names = lm.read_primary_names()
        missing = [p for p, v in groups.items() if len(v) > 1 and p not in names]
        print(json.dumps({
            "records": len(src), "pincode_groups": len(groups), "zone_conflicts": conflicts,
            "multi_locality_groups_without_primary_name": missing,
            "needs_review": sorted(p for p, (_n, r) in names.items() if r),
            "workbook_path": str(lm.workbook_path()), "storage_problem": lm.storage_problem(),
        }, indent=2))
        return 0 if not conflicts and not missing else 1

    problem = lm.storage_problem()
    if problem and not args.status:
        print(f"Refusing: {problem}", file=sys.stderr)
        return 2
    db = SessionLocal()
    try:
        if args.status:
            out = lm.status_summary(db)
        elif args.resolve:
            out = lm.resolve_blocked(db)
        else:
            out = lm.seed(db)
        print(json.dumps(out, indent=2, default=str))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
