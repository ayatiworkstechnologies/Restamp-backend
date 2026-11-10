#!/usr/bin/env python3
"""Load Chennai locality master data (localities, postcodes, aliases).

Idempotent and additive (services/localities.load_master): existing
localities are reused by spelling-insensitive name, nothing is renamed,
moved or deleted, and a pincode already bound to a different locality is
reported as a conflict instead of being rebound. Running it twice with the
same file changes nothing the second time.

Input (one CSV file, --csv):
  1. India Post "All India Pincode Directory" CSV as published on the Open
     Government Data platform (data.gov.in). Only rows whose district column
     ("districtname" or "district") is CHENNAI and statename TAMIL NADU are
     used; per pincode the head/sub post
     office name is the canonical locality and the other office names become
     aliases. Office suffixes ("S.O", "B.O", "H.O", "G.P.O") are stripped.
  2. RESTAMP curated CSV: header `locality,pincode,aliases` with aliases
     separated by "|", for reviewed corrections. With a review_status
     column, only VERIFIED rows are loaded; the rest are listed under
     "not_loaded_pending_review".
With --names-file (e.g. scripts/agent_picker_localities.txt, the app's
agent picker names), ONLY those names are loaded: each keeps its given
spelling as the canonical name and gets the pincodes of source rows with the
same spelling key (no substring matching). Names the source doesn't contain
are listed under "names_not_in_source" and are never guessed.
Only pincodes inside the app's Chennai service-area policy are loaded
(app.schemas.is_chennai_service_pincode: 600001-600118 plus explicitly
approved extras, the same rule owner listings use);
others are reported as skipped. Record the file's source URL and download
date with --source/--source-date: they are printed in the report so the run
log documents the data provenance.

Safety:
  - DRY RUN by default: everything runs inside a transaction that is rolled
    back; the report shows what WOULD change. Pass --apply to commit.
  - A non-local database (anything app.config does not treat as local) also
    needs --confirm-remote. Run it there only after a dry run was reviewed.
  - The database URL comes from RESTAMP_DATABASE_URL in the environment and
    is never printed. The report contains only public place names/pincodes.

Usage (backend root, venv active):
    python3 scripts/load_chennai_localities.py --csv path.csv \\
        --source "https://data.gov.in/..." --source-date 2026-11-04 \\
        [--names-file scripts/agent_picker_localities.txt] [--apply]
Exit codes: 0 ok, 2 refused / bad input (nothing changed), 1 unexpected error.
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
    parser.add_argument("--csv", required=True, help="India Post directory CSV or RESTAMP curated CSV")
    parser.add_argument("--source", required=True, help="where the file came from (URL)")
    parser.add_argument("--source-date", required=True, help="download/publication date of the file")
    parser.add_argument("--names-file", help="load only these locality names (one per line, # comments)")
    parser.add_argument("--city", default="Chennai")
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
    try:
        with open(args.csv, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        print("refused: cannot read --csv file", file=sys.stderr)
        return 2

    from app.db import SessionLocal
    from app.services import localities as locality_svc

    missing: list[str] | None = None
    try:
        if args.names_file:
            with open(args.names_file, encoding="utf-8") as fh:
                names = [ln.strip() for ln in fh if ln.strip() and not ln.lstrip().startswith("#")]
            rows, missing = locality_svc.rows_for_names(text, names)
        else:
            rows = locality_svc.parse_master_csv(text)
    except OSError:
        print("refused: cannot read --names-file", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    if not rows:
        print("refused: no Chennai rows in the file", file=sys.stderr)
        return 2

    db = SessionLocal()
    try:
        report = locality_svc.load_master(db, rows, city=args.city)
        if args.apply:
            db.commit()
        else:
            db.rollback()
    except Exception:
        db.rollback()
        print("error: load failed; nothing was committed", file=sys.stderr)
        return 1
    finally:
        db.close()

    summary = {k: len(v) for k, v in report.items()}
    print(json.dumps({
        "mode": "APPLIED" if args.apply else "DRY RUN (rolled back)",
        "source": args.source,
        "source_date": args.source_date,
        "rows_in_file": len(rows),
        "names_not_in_source": missing,
        "not_loaded_pending_review": locality_svc.pending_review(text),
        "counts": summary,
        "details": report,
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
