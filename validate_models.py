#!/usr/bin/env python3
# Phase 1 validation — verify 28 tables exist, required columns, no forbidden columns
from sqlalchemy import create_engine, inspect, MetaData
from sqlalchemy.orm import sessionmaker
import os, sys
sys.path.insert(0, "/Users/isaacvineeth/Downloads/Restamp-backend/app")
from models import Base
DB_URL = os.getenv("RESTAMP_DATABASE_URL", "sqlite:///:memory:")
engine = create_engine(DB_URL)
Base.metadata.create_all(engine, checkfirst=True)
inspector = inspect(engine)
all_tables = set(inspector.get_table_names())
expected = {
    "users","auth_identities","user_current_role","role_history","admin_accounts",
    "cities","districts","taluks","villages","localities","postcodes",
    "plans","payments","payment_webhook_events","subscriptions","entitlements",
    "physical_properties","property_listings","verifications","property_media","property_documents",
    "enquiries","active_enquiries","broker_postcode_access_current","broker_postcode_access_history",
    "contact_reveal_audits","activity_logs","audit_logs"
}
missing = expected - all_tables
extra = all_tables - expected
print(f"Expected tables: {len(expected)}")
print(f"Found tables: {len(all_tables)}")
print(f"Matched: {len(expected & all_tables)}")
if missing: print(f"MISSING: {missing}")
if extra: print(f"EXTRA: {extra}")
# Check key columns on property_listings
cols = [c["name"] for c in inspector.get_columns("property_listings")]
required_cols = ["id","title","transaction_type","property_type","listing_status","verification_status","physical_property_id","user_id"]
missing_cols = [c for c in required_cols if c not in cols]
print(f"property_listings columns present: {len(cols)}; required missing: {missing_cols}")
# Forbidden columns check
forbidden_on_listings = ["published_at","publication_status","offmarket_reason","verification_id","property_id","locality_id"]
found_forbidden = [c for c in forbidden_on_listings if c in cols]
print(f"Forbidden columns found on property_listings: {found_forbidden}")
# Verify title is document-specific on property_documents
pd_cols = [c["name"] for c in inspector.get_columns("property_documents")]
print(f"property_documents.title present (document-only): {'title' in pd_cols}")
# Verify physical_properties has user_id and postcode_id, NO locality_id
pp_cols = [c["name"] for c in inspector.get_columns("physical_properties")]
print(f"physical_properties.user_id present: {'user_id' in pp_cols}")
print(f"physical_properties.postcode_id present: {'postcode_id' in pp_cols}")
print(f"physical_properties.locality_id absent: {'locality_id' not in pp_cols}")
# Auth uniqueness constraints
constraints = inspector.get_unique_constraints("auth_identities")
print(f"auth_identities unique constraints: {[c['name'] for c in constraints]}")
# Final status
status = "PASS" if (not missing and not extra and not missing_cols and not found_forbidden) else "BLOCKED"
print(f"\nPHASE 1 VALIDATION STATUS: {status}")
