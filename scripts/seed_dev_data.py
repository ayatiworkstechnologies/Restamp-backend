#!/usr/bin/env python3
"""RESTAMP development-only seed for local Swagger validation (NOT for production).

- Local database only: refuses URLs that are not localhost/127.0.0.1/unix-socket,
  and refuses database names outside an explicit dev allow-list.
- Idempotent: re-running detects the seed marker (buyer phone identity) and exits
  without creating duplicates. Seeding refuses to attach seed listings to a
  pincode that already belongs to a non-seed locality (real catalog data).
- `--clean` removes ONLY seed-owned rows: users/listings found through the
  seed marker phones, and location rows that sit inside the exact seed chain
  ('Seed Chennai' -> 'Seed Central' -> 'Seed Taluk' -> 'Seed Village') with a
  seed name and seed pincode. Nothing is ever deleted by a bare pincode or
  locality name. If anything outside the seed references those rows (another
  owner's property, an agent grant, an alias, a real pincode attached to a
  seed locality), the whole clean is refused before any delete.
- Uses only approved schema columns/enums; no invented fields.

Usage:
  RESTAMP_DATABASE_URL='mysql+pymysql://restamp_dev:...@localhost/restamp_dev?unix_socket=/tmp/mysql.sock' \\
      python3 scripts/seed_dev_data.py [--clean]

Marker identity: buyer phone +16501110001 / owner phone +16501110002,
city 'Seed Chennai', pincodes 600091/600092 (all unique, never used by tests).

Home batch (Phase 2): ~21 additional VERIFIED+AVAILABLE listings under the same
city/village with exact Chennai locality names (Anna Nagar, OMR, Velachery,
T. Nagar, Tambaram, Adyar, ECR, Guindy) and pincodes 600093-600100. Home
listings are titled 'Home ...' so re-runs can detect and top them up without
duplicating the original 7 visibility-demo rows.
"""
import os
import sys

BUYER_PHONE = "+16501110001"
OWNER_PHONE = "+16501110002"
CITY = "Seed Chennai"
PINCODES = ("600091", "600092")
DEV_DATABASES = {"restamp_dev", "restamp_test"}

# Home batch: exact Chennai locality names (each gets >=1 listing) with fresh
# numeric pincodes that never collide with tests (tests use dashed tags).
HOME_LOCALITIES = (
    "Anna Nagar",
    "OMR",
    "Velachery",
    "T. Nagar",
    "Tambaram",
    "Adyar",
    "ECR",
    "Guindy",
)
HOME_PINCODES = (
    "600093",
    "600094",
    "600095",
    "600096",
    "600097",
    "600098",
    "600099",
    "600100",
)

# title, deal, ptype, vstat, lstat, paise, period, cstat, beds, baths, area,
# locality-name, address, description, media-count.
# Prices: TOTAL in paise (rupees*100), MONTHLY in paise/month.
HOME_SPECS = [
    ("Home Emerald 3BHK Anna Nagar", "BUY", "APARTMENT", "VERIFIED", "AVAILABLE",
     1450000000, "TOTAL", "READY_TO_MOVE", 3, 2, 1850, "Anna Nagar",
     "2nd Avenue, Anna Nagar, Chennai", "Sunlit corner flat near the metro station.", 2),
    ("Home OMR Tech 2BHK", "BUY", "APARTMENT", "VERIFIED", "AVAILABLE",
     920000000, "TOTAL", "UNDER_CONSTRUCTION", 2, 2, 1100, "OMR",
     "Padur, OMR, Chennai", "New-tower flat minutes from the IT corridor.", 1),
    ("Home Velachery Lake Villa", "BUY", "VILLA", "VERIFIED", "AVAILABLE",
     2200000000, "TOTAL", "READY_TO_MOVE", 4, 3, 3200, "Velachery",
     "Lake View Road, Velachery, Chennai", "Gated villa with private garden.", 2),
    ("Home Tambaram Independent House", "BUY", "HOUSE", "VERIFIED", "AVAILABLE",
     850000000, "TOTAL", "READY_TO_MOVE", 3, 2, 1800, "Tambaram",
     "Mudichur Road, Tambaram, Chennai", "Independent house near the railway station.", 1),
    ("Home Guindy Layout Plot", "BUY", "PLOT", "VERIFIED", "AVAILABLE",
     600000000, "TOTAL", "NEW_LAUNCH", None, None, 2400, "Guindy",
     "Industrial Estate Layout, Guindy, Chennai", "CMDA-approved clear-title plot.", 1),
    ("Home Adyar Sea Breeze 4BHK", "BUY", "APARTMENT", "VERIFIED", "AVAILABLE",
     3100000000, "TOTAL", "READY_TO_MOVE", 4, 4, 2850, "Adyar",
     "LB Road, Adyar, Chennai", "Sea-breeze flat with clubhouse access.", 2),
    ("Home ECR Starter 1BHK", "BUY", "APARTMENT", "VERIFIED", "AVAILABLE",
     380000000, "TOTAL", "READY_TO_MOVE", 1, 1, 650, "ECR",
     "Kottivakkam, ECR, Chennai", "Compact beachside starter home.", 1),
    ("Home Anna Nagar Resale 2BHK", "RESALE", "APARTMENT", "VERIFIED", "AVAILABLE",
     950000000, "TOTAL", "READY_TO_MOVE", 2, 2, 1200, "Anna Nagar",
     "Shanthi Colony, Anna Nagar, Chennai", "Well-kept resale with covered parking.", 1),
    ("Home T Nagar Resale House", "RESALE", "HOUSE", "VERIFIED", "AVAILABLE",
     1800000000, "TOTAL", "READY_TO_MOVE", 3, 2, 2100, "T. Nagar",
     "GN Chetty Road, T. Nagar, Chennai", "Resale independent house, vastu aligned.", 1),
    ("Home Velachery Resale Villa", "RESALE", "VILLA", "VERIFIED", "AVAILABLE",
     2650000000, "TOTAL", "UNDER_CONSTRUCTION", 4, 4, 3400, "Velachery",
     "Taramani Link Road, Velachery, Chennai", "Resale villa in a gated enclave.", 1),
    ("Home Tambaram Resale Plot", "RESALE", "PLOT", "VERIFIED", "AVAILABLE",
     450000000, "TOTAL", "NEW_LAUNCH", None, None, 2000, "Tambaram",
     "Perungalathur Layout, Tambaram, Chennai", "Resale plot with fencing and gate.", 1),
    ("Home OMR Corridor Rental 2BHK", "RENT", "APARTMENT", "VERIFIED", "AVAILABLE",
     2500000, "MONTHLY", "READY_TO_MOVE", 2, 2, 1100, "OMR",
     "Sholinganallur, OMR, Chennai", "Bright rental near the corridor.", 1),
    ("Home Velachery Rental 3BHK", "RENT", "APARTMENT", "VERIFIED", "AVAILABLE",
     3500000, "MONTHLY", "READY_TO_MOVE", 3, 2, 1500, "Velachery",
     "100 Feet Road, Velachery, Chennai", "Family rental by the lake.", 1),
    ("Home Adyar Rental 1BHK", "RENT", "APARTMENT", "VERIFIED", "AVAILABLE",
     1800000, "MONTHLY", "READY_TO_MOVE", 1, 1, 700, "Adyar",
     "Gandhi Nagar, Adyar, Chennai", "Cozy rental for working professionals.", 1),
    ("Home T Nagar Rental House", "RENT", "HOUSE", "VERIFIED", "AVAILABLE",
     2200000, "MONTHLY", "READY_TO_MOVE", 2, 1, 950, "T. Nagar",
     "Burkit Road, T. Nagar, Chennai", "Independent-floor rental near shops.", 1),
    ("Home Guindy Studio Rental", "RENT", "APARTMENT", "VERIFIED", "AVAILABLE",
     1200000, "MONTHLY", "UNDER_CONSTRUCTION", 1, 1, 550, "Guindy",
     "Ekkattuthangal, Guindy, Chennai", "New studio tower near the metro.", 1),
    ("Home ECR Beach Villa Rental", "RENT", "VILLA", "VERIFIED", "AVAILABLE",
     15000000, "MONTHLY", "READY_TO_MOVE", 4, 3, 3000, "ECR",
     "Neelankarai, ECR, Chennai", "Beachside villa, long-lease friendly.", 1),
    ("Home OMR Tech Park Lease", "LEASE", "COMMERCIAL", "VERIFIED", "AVAILABLE",
     8000000, "MONTHLY", "UNDER_CONSTRUCTION", None, None, 5000, "OMR",
     "SIPCOT Tech Park, OMR, Chennai", "Grade-A office floor, warm shell.", 1),
    ("Home Guindy Retail Lease", "LEASE", "COMMERCIAL", "VERIFIED", "AVAILABLE",
     5500000, "MONTHLY", "READY_TO_MOVE", None, None, 1200, "Guindy",
     "Mount Road Frontage, Guindy, Chennai", "High-street retail frontage.", 1),
    ("Home Anna Nagar Sky Penthouse", "BUY", "APARTMENT", "VERIFIED", "AVAILABLE",
     4200000000, "TOTAL", "NEW_LAUNCH", 4, 4, 3100, "Anna Nagar",
     "Tower Park Road, Anna Nagar, Chennai", "New-launch sky residence, top floor.", 1),
    ("Home Tambaram Budget Rental", "RENT", "APARTMENT", "VERIFIED", "AVAILABLE",
     1000000, "MONTHLY", "READY_TO_MOVE", 1, 1, 600, "Tambaram",
     "East Tambaram, Chennai", "Budget rental near the station.", 1),
]

def _lit_ints(vals):
    return "(" + ",".join(str(int(x)) for x in vals) + ")"


def _url():
    url = os.environ.get("RESTAMP_DATABASE_URL", "")
    if not url:
        sys.exit("REFUSED: RESTAMP_DATABASE_URL is not set.")
    low = url.lower()
    if "localhost" not in low and "127.0.0.1" not in low and "unix_socket" not in low:
        sys.exit("REFUSED: URL is not local (need localhost/127.0.0.1/unix_socket).")
    from urllib.parse import urlparse

    parts = urlparse(url)
    db = (parts.path or "").lstrip("/").split("?", 1)[0]
    if db not in DEV_DATABASES:
        sys.exit(f"REFUSED: database '{db}' is not an approved dev database.")
    return url


def _engine():
    from sqlalchemy import create_engine

    return create_engine(_url())


def _seed_ids(conn):
    """Return existing seed IDs if the marker buyer exists, else None."""
    from sqlalchemy import text

    row = conn.execute(
        text("SELECT user_id FROM auth_identities WHERE provider='phone' "
             "AND provider_identifier=:p"),
        {"p": BUYER_PHONE},
    ).fetchone()
    if not row:
        return None
    buyer = row[0]
    owner = conn.execute(
        text("SELECT user_id FROM auth_identities WHERE provider='phone' "
             "AND provider_identifier=:p"),
        {"p": OWNER_PHONE},
    ).fetchone()[0]
    listings = [r[0] for r in conn.execute(
        text("SELECT id FROM property_listings WHERE user_id=:u ORDER BY id"), {"u": owner}
    ).fetchall()]
    return {"buyer_id": buyer, "owner_id": owner, "listing_ids": listings}


def _get_or_create_locality(conn, ins, village_id, name):
    """Idempotent locality lookup/creation scoped to one village."""
    from sqlalchemy import text

    row = conn.execute(
        text("SELECT id FROM localities WHERE village_id=:v AND name=:n"),
        {"v": village_id, "n": name},
    ).fetchone()
    if row:
        return row[0]
    return ins("INSERT INTO localities (village_id, name) VALUES (:v, :n)",
               {"v": village_id, "n": name})


def _get_or_create_postcode(conn, ins, locality_id, pincode):
    """Idempotent postcode lookup/creation (pincode is globally unique).

    A pincode that already belongs to a different (non-seed) locality is
    real catalog data: refuse instead of attaching seed listings to it."""
    from sqlalchemy import text

    row = conn.execute(
        text("SELECT id, locality_id FROM postcodes WHERE pincode=:p"), {"p": pincode}
    ).fetchone()
    if row:
        if row[1] != locality_id:
            raise SystemExit(f"REFUSED: pincode {pincode} already belongs to a non-seed locality.")
        return row[0]
    return ins("INSERT INTO postcodes (locality_id, pincode) VALUES (:l, :p)",
               {"l": locality_id, "p": pincode})


REAL_PROPERTY_IMAGES = {
    "APARTMENT": [
        "https://images.unsplash.com/photo-1545324418-cc1a3fa10c00?auto=format&fit=crop&w=1000&q=80",
        "https://images.unsplash.com/photo-1502672260266-1c1ef2d93688?auto=format&fit=crop&w=1000&q=80",
        "https://images.unsplash.com/photo-1560448204-e02f11c3d0e2?auto=format&fit=crop&w=1000&q=80",
        "https://images.unsplash.com/photo-1522708323590-d24dbb6b0267?auto=format&fit=crop&w=1000&q=80",
        "https://images.unsplash.com/photo-1567496898669-ee935f5f647a?auto=format&fit=crop&w=1000&q=80",
    ],
    "VILLA": [
        "https://images.unsplash.com/photo-1613490493576-7fde63acd811?auto=format&fit=crop&w=1000&q=80",
        "https://images.unsplash.com/photo-1600596542815-ffad4c1539a9?auto=format&fit=crop&w=1000&q=80",
        "https://images.unsplash.com/photo-1613977257363-707ba9348227?auto=format&fit=crop&w=1000&q=80",
        "https://images.unsplash.com/photo-1512917774080-9991f1c4c750?auto=format&fit=crop&w=1000&q=80",
    ],
    "HOUSE": [
        "https://images.unsplash.com/photo-1600585154340-be6161a56a0c?auto=format&fit=crop&w=1000&q=80",
        "https://images.unsplash.com/photo-1580587771525-78b9dba3b914?auto=format&fit=crop&w=1000&q=80",
        "https://images.unsplash.com/photo-1600607687939-ce8a6c25118c?auto=format&fit=crop&w=1000&q=80",
        "https://images.unsplash.com/photo-1513694203232-719a280e022f?auto=format&fit=crop&w=1000&q=80",
    ],
    "PLOT": [
        "https://images.unsplash.com/photo-1500382017468-9049fed747ef?auto=format&fit=crop&w=1000&q=80",
        "https://images.unsplash.com/photo-1500530855697-b586d89ba3ee?auto=format&fit=crop&w=1000&q=80",
        "https://images.unsplash.com/photo-1524813686514-a57563d77d61?auto=format&fit=crop&w=1000&q=80",
    ],
    "COMMERCIAL": [
        "https://images.unsplash.com/photo-1486406146926-c627a92ad1ab?auto=format&fit=crop&w=1000&q=80",
        "https://images.unsplash.com/photo-1497366811353-6870744d04b2?auto=format&fit=crop&w=1000&q=80",
        "https://images.unsplash.com/photo-1497366216548-37526070297c?auto=format&fit=crop&w=1000&q=80",
    ],
}


def _get_seed_image(ptype: str, lid: int, order: int) -> str:
    pool = REAL_PROPERTY_IMAGES.get(ptype, REAL_PROPERTY_IMAGES["APARTMENT"])
    return pool[(lid + order) % len(pool)]


def _insert_spec(conn, ins, owner, pc_id, spec):
    """Insert one (physical + listing + media) spec; skip when title exists.

    Returns the listing id, or None when the title was already seeded.
    """
    from sqlalchemy import text

    (title, deal, ptype, vstat, lstat, paise, period, cstat, beds, baths,
     area, _loc, addr, desc, nmedia) = spec
    dup = conn.execute(
        text("SELECT id FROM property_listings WHERE user_id=:u AND title=:t"),
        {"u": owner, "t": title},
    ).fetchone()
    if dup:
        return None
    pp = ins("INSERT INTO physical_properties (postcode_id, user_id, address_line, "
             "area_value, area_unit, bedrooms, bathrooms) "
             "VALUES (:pc, :u, :a, :av, 'SQFT', :b, :ba)",
             {"pc": pc_id, "u": owner, "a": addr, "av": area, "b": beds, "ba": baths})
    lid = ins("INSERT INTO property_listings (physical_property_id, user_id, title, "
              "price_paise, price_period, description, construction_status, "
              "verification_status, listing_status, transaction_type, property_type) "
              "VALUES (:pp, :u, :t, :pr, :per, :d, :c, :v, :s, :tt, :pt)",
              {"pp": pp, "u": owner, "t": title, "pr": paise, "per": period, "d": desc,
               "c": cstat, "v": vstat, "s": lstat, "tt": deal, "pt": ptype})
    for i in range(nmedia):
        ins("INSERT INTO property_media (property_listing_id, url, media_type, order_index) "
            "VALUES (:l, :u, 'image', :o)",
            {"l": lid, "u": _get_seed_image(ptype, lid, i), "o": i})
    return lid


def _ensure_home_batch(conn, ins, owner, village_id):
    """Create the 8 Home localities/postcodes and insert missing Home specs.

    Returns the list of newly inserted listing ids (empty on re-run).
    """
    pc_by_loc = {}
    for loc_name, pin in zip(HOME_LOCALITIES, HOME_PINCODES):
        loc_id = _get_or_create_locality(conn, ins, village_id, loc_name)
        pc_by_loc[loc_name] = _get_or_create_postcode(conn, ins, loc_id, pin)
    added = []
    for spec in HOME_SPECS:
        lid = _insert_spec(conn, ins, owner, pc_by_loc[spec[11]], spec)
        if lid is not None:
            added.append(lid)
    return added


def seed(conn):
    from sqlalchemy import text

    existing = _seed_ids(conn)

    def ins(sql, params):
        conn.execute(text(sql), params)
        return conn.execute(text("SELECT LAST_INSERT_ID()")).fetchone()[0]

    if existing:
        # Upgrade path: original seed ran before the Home batch existed.
        # Top up only the missing Home rows; re-runs add nothing.
        vil = conn.execute(
            text("SELECT village_id FROM localities WHERE name='Seed Anna Nagar'")
        ).fetchone()
        if not vil:
            print("Seed marker present but Seed Village lookup failed; skipping Home top-up.")
            return existing
        added = _ensure_home_batch(conn, ins, existing["owner_id"], vil[0])
        conn.commit()
        listings = [r[0] for r in conn.execute(
            text("SELECT id FROM property_listings WHERE user_id=:u ORDER BY id"),
            {"u": existing["owner_id"]}).fetchall()]
        print(f"Seed already present; Home top-up added {len(added)} listings:", added)
        return {"buyer_id": existing["buyer_id"], "owner_id": existing["owner_id"],
                "listing_ids": listings}

    buyer = ins("INSERT INTO users (display_name) VALUES ('Seed Buyer')", {})
    owner = ins("INSERT INTO users (display_name) VALUES ('Seed Owner')", {})
    for uid, phone in ((buyer, BUYER_PHONE), (owner, OWNER_PHONE)):
        ins("INSERT INTO auth_identities (user_id, provider, provider_identifier) "
            "VALUES (:u, 'phone', :p)", {"u": uid, "p": phone})
    for uid in (buyer, owner):
        ins("INSERT INTO user_current_role (user_id, role) VALUES (:u, 'BUYER')", {"u": uid})
        ins("INSERT INTO role_history (user_id, role) VALUES (:u, 'BUYER')", {"u": uid})
    conn.execute(text("UPDATE user_current_role SET role='OWNER' WHERE user_id=:u"), {"u": owner})
    conn.execute(text("INSERT INTO role_history (user_id, role) VALUES (:u, 'OWNER')"), {"u": owner})

    city = ins("INSERT INTO cities (name) VALUES (:n)", {"n": CITY})
    dist = ins("INSERT INTO districts (city_id, name) VALUES (:c, 'Seed Central')", {"c": city})
    taluk = ins("INSERT INTO taluks (district_id, name) VALUES (:d, 'Seed Taluk')", {"d": dist})
    vil = ins("INSERT INTO villages (taluk_id, name) VALUES (:t, 'Seed Village')", {"t": taluk})
    loc1 = ins("INSERT INTO localities (village_id, name) VALUES (:v, 'Seed Anna Nagar')", {"v": vil})
    loc2 = ins("INSERT INTO localities (village_id, name) VALUES (:v, 'Seed OMR')", {"v": vil})
    pc1 = ins("INSERT INTO postcodes (locality_id, pincode) VALUES (:l, :p)", {"l": loc1, "p": PINCODES[0]})
    pc2 = ins("INSERT INTO postcodes (locality_id, pincode) VALUES (:l, :p)", {"l": loc2, "p": PINCODES[1]})

    specs = [
        # title, deal, ptype, vstat, lstat, paise, period, cstat, beds, baths, area, pc, addr, desc, media
        ("Seed Emerald 3BHK", "BUY", "APARTMENT", "VERIFIED", "AVAILABLE",
         1450000000, "TOTAL", "READY_TO_MOVE", 3, 2, 1850, pc1,
         "2nd Avenue, Seed Anna Nagar", "Sunlit corner flat near the metro.", 2),
        ("Seed Resale Villa", "RESALE", "VILLA", "VERIFIED", "AVAILABLE",
         2200000000, "TOTAL", "READY_TO_MOVE", 4, 3, 3200, pc1,
         "Garden Row, Seed Anna Nagar", "Resale villa with private garden.", 1),
        ("Seed OMR Rental", "RENT", "APARTMENT", "VERIFIED", "AVAILABLE",
         2500000, "MONTHLY", "READY_TO_MOVE", 2, 2, 1100, pc2,
         "IT Corridor, Seed OMR", "Bright rental near the corridor.", 1),
        ("Seed OMR Office", "LEASE", "COMMERCIAL", "VERIFIED", "AVAILABLE",
         8000000, "MONTHLY", "UNDER_CONSTRUCTION", None, None, 5000, pc2,
         "Tech Park, Seed OMR", "Grade-A office floor.", 0),
        ("Seed Layout Plot", "BUY", "PLOT", "VERIFIED", "AVAILABLE",
         600000000, "TOTAL", "NEW_LAUNCH", None, None, 2400, pc1,
         "Layout Block C, Seed Anna Nagar", "CMDA-approved clear-title plot.", 0),
        ("Seed Sold Flat", "BUY", "APARTMENT", "VERIFIED", "SOLD",
         900000000, "TOTAL", "READY_TO_MOVE", 2, 2, 1200, pc1,
         "Old Block, Seed Anna Nagar", "Already sold (visibility demo).", 0),
        ("Seed Draft Flat", "BUY", "APARTMENT", "PENDING", "AVAILABLE",
         950000000, "TOTAL", "UNDER_CONSTRUCTION", 2, 2, 1250, pc2,
         "New Wing, Seed OMR", "Pending verification (visibility demo).", 0),
    ]
    listing_ids = []
    for (title, deal, ptype, vstat, lstat, paise, period, cstat, beds, baths,
         area, pc, addr, desc, nmedia) in specs:
        pp = ins("INSERT INTO physical_properties (postcode_id, user_id, address_line, "
                 "area_value, area_unit, bedrooms, bathrooms) "
                 "VALUES (:pc, :u, :a, :av, 'SQFT', :b, :ba)",
                 {"pc": pc, "u": owner, "a": addr, "av": area, "b": beds, "ba": baths})
        lid = ins("INSERT INTO property_listings (physical_property_id, user_id, title, "
                  "price_paise, price_period, description, construction_status, "
                  "verification_status, listing_status, transaction_type, property_type) "
                  "VALUES (:pp, :u, :t, :pr, :per, :d, :c, :v, :s, :tt, :pt)",
                  {"pp": pp, "u": owner, "t": title, "pr": paise, "per": period, "d": desc,
                   "c": cstat, "v": vstat, "s": lstat, "tt": deal, "pt": ptype})
        for i in range(nmedia):
            ins("INSERT INTO property_media (property_listing_id, url, media_type, order_index) "
                "VALUES (:l, :u, 'image', :o)",
                {"l": lid, "u": _get_seed_image(ptype, lid, i), "o": i})
        listing_ids.append(lid)

    # Home batch on the same village (fresh installs get everything at once).
    listing_ids.extend(_ensure_home_batch(conn, ins, owner, vil))

    conn.commit()
    ids = {"buyer_id": buyer, "owner_id": owner, "listing_ids": listing_ids}
    print("Seeded IDs:", ids)
    return ids


SEED_LOCALITY_NAMES = ("Seed Anna Nagar", "Seed OMR") + HOME_LOCALITIES
SEED_CHAIN = ("Seed Central", "Seed Taluk", "Seed Village")


class SeedCleanRefused(RuntimeError):
    """Cleaning would touch rows that are not provably seed-owned."""


def seed_location_plan(conn, city=CITY, pincodes=PINCODES + HOME_PINCODES,
                       locality_names=SEED_LOCALITY_NAMES, seed_owner_id=None):
    """Seed-owned location rows (ids) plus blockers that forbid deleting them.

    Read-only. A row is seed-owned only when it sits inside the exact seed
    chain city -> SEED_CHAIN AND carries a seed locality name / seed pincode.
    Blockers: a non-seed locality or pincode inside that chain, a property
    not owned by the seed owner, an agent grant (current or history), or an
    alias on a seed locality."""
    from sqlalchemy import bindparam, text

    def ids(sql, **p):
        stmt = text(sql)
        for k, v in p.items():
            if isinstance(v, (list, tuple)):
                stmt = stmt.bindparams(bindparam(k, expanding=True))
        return [r[0] for r in conn.execute(stmt, p).fetchall()] if all(p.values()) else []

    plan = {"city_ids": [], "district_ids": [], "taluk_ids": [], "village_ids": [],
            "locality_ids": [], "postcode_ids": [], "blockers": []}
    plan["city_ids"] = ids("SELECT id FROM cities WHERE name=:n", n=city)
    plan["district_ids"] = ids("SELECT id FROM districts WHERE city_id IN :c AND name=:n",
                               c=plan["city_ids"], n=SEED_CHAIN[0])
    plan["taluk_ids"] = ids("SELECT id FROM taluks WHERE district_id IN :d AND name=:n",
                            d=plan["district_ids"], n=SEED_CHAIN[1])
    plan["village_ids"] = ids("SELECT id FROM villages WHERE taluk_id IN :t AND name=:n",
                              t=plan["taluk_ids"], n=SEED_CHAIN[2])
    if not plan["village_ids"]:
        return plan
    stmt = text("SELECT id, name FROM localities WHERE village_id IN :v").bindparams(
        bindparam("v", expanding=True))
    for lid, name in conn.execute(stmt, {"v": plan["village_ids"]}).fetchall():
        if name in locality_names:
            plan["locality_ids"].append(lid)
        else:
            plan["blockers"].append(f"non-seed locality '{name}' inside the seed chain")
    if not plan["locality_ids"]:
        return plan
    stmt = text("SELECT id, pincode FROM postcodes WHERE locality_id IN :l").bindparams(
        bindparam("l", expanding=True))
    for pid, pin in conn.execute(stmt, {"l": plan["locality_ids"]}).fetchall():
        if pin in pincodes:
            plan["postcode_ids"].append(pid)
        else:
            plan["blockers"].append(f"non-seed pincode {pin} attached to a seed locality")
    checks = [
        ("properties not owned by the seed owner",
         "SELECT COUNT(*) FROM physical_properties WHERE postcode_id IN :x AND (:o IS NULL OR user_id <> :o)",
         plan["postcode_ids"]),
        ("current agent grants", "SELECT COUNT(*) FROM broker_postcode_access_current WHERE postcode_id IN :x",
         plan["postcode_ids"]),
        ("agent grant history", "SELECT COUNT(*) FROM broker_postcode_access_history WHERE postcode_id IN :x",
         plan["postcode_ids"]),
        ("locality aliases", "SELECT COUNT(*) FROM locality_aliases WHERE locality_id IN :x",
         plan["locality_ids"]),
    ]
    for label, sql, values in checks:
        if not values:
            continue
        n = conn.execute(text(sql).bindparams(bindparam("x", expanding=True)),
                         {"x": values, "o": seed_owner_id}).scalar()
        if n:
            plan["blockers"].append(f"{n} {label} reference seed location rows")
    return plan


def clean_locations(conn, plan):
    """Delete exactly the plan's rows by id, children first; a parent row is
    deleted only when nothing else is left under it. No commit."""
    from sqlalchemy import bindparam, text

    if plan["blockers"]:
        raise SeedCleanRefused("; ".join(plan["blockers"]))
    counts = {}

    def delete(table, values):
        if values:
            counts[table] = conn.execute(
                text(f"DELETE FROM {table} WHERE id IN :x").bindparams(bindparam("x", expanding=True)),
                {"x": values}).rowcount

    delete("postcodes", plan["postcode_ids"])
    delete("localities", plan["locality_ids"])
    for table, ids_key, child, fk in (("villages", "village_ids", "localities", "village_id"),
                                      ("taluks", "taluk_ids", "villages", "taluk_id"),
                                      ("districts", "district_ids", "taluks", "district_id"),
                                      ("cities", "city_ids", "districts", "city_id")):
        empty = [i for i in plan[ids_key]
                 if not conn.execute(text(f"SELECT COUNT(*) FROM {child} WHERE {fk}=:i"), {"i": i}).scalar()]
        delete(table, empty)
    return counts


def clean_rows(conn):
    """Delete ONLY seed-owned rows, bottom-up, without committing. Raises
    SeedCleanRefused (before any delete) when location rows are referenced
    by anything outside the seed."""
    from sqlalchemy import text

    ids = _seed_ids(conn)
    plan = seed_location_plan(conn, seed_owner_id=ids["owner_id"] if ids else None)
    if plan["blockers"]:
        raise SeedCleanRefused("; ".join(plan["blockers"]))
    counts = {}
    if ids:
        owner, buyer, listings = ids["owner_id"], ids["buyer_id"], ids["listing_ids"]
        counts["contact_reveal_audits(buyer)"] = conn.execute(
            text(f"DELETE FROM contact_reveal_audits WHERE user_id IN {_lit_ints((buyer,))}")).rowcount
        counts["saved_properties(buyer)"] = conn.execute(
            text(f"DELETE FROM saved_properties WHERE buyer_user_id IN {_lit_ints((buyer,))}")).rowcount
        counts["enquiries(buyer)"] = conn.execute(
            text(f"DELETE FROM enquiries WHERE buyer_user_id IN {_lit_ints((buyer,))}")).rowcount
        if listings:
            ph = _lit_ints(listings)
            counts["contact_reveal_audits(listing)"] = conn.execute(
                text(f"DELETE FROM contact_reveal_audits WHERE property_listing_id IN {ph}")).rowcount
            for tbl in ("active_enquiries", "property_media", "saved_properties"):
                counts[tbl + "(listing)"] = conn.execute(
                    text(f"DELETE FROM {tbl} WHERE property_listing_id IN {ph}")).rowcount
            conn.execute(text(f"DELETE FROM property_listings WHERE id IN {ph}"))
            pps = [r[0] for r in conn.execute(
                text("SELECT id FROM physical_properties WHERE user_id=:u"), {"u": owner}).fetchall()]
            if pps:
                conn.execute(text(f"DELETE FROM physical_properties WHERE id IN {_lit_ints(pps)}"))
        pair = _lit_ints((buyer, owner))
        for tbl in ("auth_identities", "user_current_role", "role_history"):
            conn.execute(text(f"DELETE FROM {tbl} WHERE user_id IN {pair}"))
        conn.execute(text(f"DELETE FROM users WHERE id IN {pair}"))
    counts.update(clean_locations(conn, plan))
    return counts


def clean(conn):
    """Delete ONLY seed-owned rows and commit, or refuse without changes."""
    try:
        counts = clean_rows(conn)
    except SeedCleanRefused as exc:
        conn.rollback()
        print(f"REFUSED (nothing deleted): {exc}")
        return
    conn.commit()
    print("Cleaned seed rows (scoped deletes):", counts)


def counts(conn):
    from sqlalchemy import text

    out = {}
    for t in ("users", "physical_properties", "property_listings", "property_media",
              "enquiries", "saved_properties"):
        out[t] = conn.execute(text(f"SELECT COUNT(*) FROM {t}")).fetchone()[0]
    return out


if __name__ == "__main__":
    eng = _engine()
    with eng.connect() as conn:
        if "--clean" in sys.argv:
            clean(conn)
        else:
            print("Pre-seed counts:", counts(conn))
            seed(conn)
            print("Post-seed counts:", counts(conn))
