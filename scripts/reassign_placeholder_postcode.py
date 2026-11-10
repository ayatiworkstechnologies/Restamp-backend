#!/usr/bin/env python3
"""Move one postcode row from a seed placeholder locality to its real locality.

Built for the reviewed Perungudi fix: in restamp_dev, pincode 600096 belongs
to the seed placeholder "T. Nagar" (city "Seed Chennai"), while India Post
(scripts/chennai_picker_localities_reviewed.csv) maps 600096 to Perungudi.
All checks live in services/localities.reassign_placeholder_postcode; any
failure refuses with nothing changed. The row keeps its id; the report prints
the old association and the exact rollback statement.

Order (one transaction): optionally load the reviewed catalog first (--catalog,
VERIFIED rows only) so the placeholder keeps its real pincode (T. Nagar
600017), then reassign, then load the catalog again so the target's row is
recorded as existing.

Safety: DRY RUN by default (rolled back). --apply commits; a non-local
database also needs --confirm-remote. The database URL comes from
RESTAMP_DATABASE_URL and is never printed.

Usage (backend root, venv active):
    python3 scripts/reassign_placeholder_postcode.py --pincode 600096 \\
        --placeholder "T. Nagar" --target Perungudi \\
        [--catalog scripts/chennai_picker_localities_reviewed.csv] [--apply]
Exit codes: 0 ok, 2 refused (nothing changed), 1 unexpected error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--pincode", required=True)
    parser.add_argument("--placeholder", required=True, help="placeholder locality name now owning the pincode")
    parser.add_argument("--target", required=True, help="real locality the pincode belongs to")
    parser.add_argument("--placeholder-city", default="Seed Chennai")
    parser.add_argument("--city", default="Chennai")
    parser.add_argument("--catalog", help="reviewed curated CSV to load in the same transaction")
    parser.add_argument("--apply", action="store_true", help="commit (default: dry run, rolled back)")
    parser.add_argument("--confirm-remote", action="store_true", help="required for a non-local database")
    args = parser.parse_args(argv)
    # TEMPORARILY DISABLED — Chennai area seeding paused.
    from app.config import area_seeding_enabled

    if not area_seeding_enabled():
        print("refused: TEMPORARILY DISABLED — Chennai area seeding paused "
              "(set RESTAMP_AREA_SEEDING_ENABLED=1 locally to use it; never available in production)",
              file=sys.stderr)
        return 2

    url = os.environ.get("RESTAMP_DATABASE_URL", "").strip()
    if not url:
        print("refused: RESTAMP_DATABASE_URL is not set", file=sys.stderr)
        return 2
    from app.config import _is_local_database_url

    if not _is_local_database_url(url) and not args.confirm_remote:
        print("refused: non-local database; re-run with --confirm-remote after a reviewed dry run", file=sys.stderr)
        return 2
    rows = []
    if args.catalog:
        from app.services import localities as locality_svc

        try:
            with open(args.catalog, encoding="utf-8") as fh:
                rows = locality_svc.parse_master_csv(fh.read())
        except (OSError, ValueError):
            print("refused: cannot read --catalog", file=sys.stderr)
            return 2

    from app.db import SessionLocal
    from app.services import localities as locality_svc

    db = SessionLocal()
    try:
        before = locality_svc.load_master(db, rows) if rows else None
        result = locality_svc.reassign_placeholder_postcode(
            db, args.pincode, placeholder_locality=args.placeholder, target_locality=args.target,
            placeholder_city=args.placeholder_city, city=args.city,
        )
        after = locality_svc.load_master(db, rows) if rows else None
        if args.apply:
            db.commit()
        else:
            db.rollback()
    except locality_svc.ReassignRefused as exc:
        db.rollback()
        print(f"refused (nothing changed): {exc}", file=sys.stderr)
        return 2
    except Exception:
        db.rollback()
        print("error: nothing was committed", file=sys.stderr)
        return 1
    finally:
        db.close()
    print(json.dumps({
        "mode": "APPLIED" if args.apply else "DRY RUN (rolled back)",
        "reassignment": result,
        "catalog_before": before and {k: len(v) for k, v in before.items()},
        "catalog_after": after and {k: len(v) for k, v in after.items()},
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
