"""RESTAMP locality master data, agent catalog and name resolution.

Three facts are kept separate (alembic k1l2m3n4o5p6):
  - EXISTS:    a localities row reachable through the location chain, with at
               least one postcode (coverage is granted per postcode, so a
               locality without one cannot be selected).
  - ENABLED:   localities.agent_enabled. Admin-managed (audited). Disabling
               only stops NEW selection; existing agent grants are untouched
               and expire with the agent's plan.
  - ASSIGNED:  broker_postcode_access_current rows (services/brokers.py,
               services/agent_plans.py).

Name resolution (resolve) is spelling-insensitive WITHOUT duplicate rows: a
name's key is its lower-case letters and digits only ("T. Nagar", "T Nagar",
"t-nagar" -> "tnagar"). A key matches every locality row whose name has that
key (duplicate rows created by owner listings collapse into one place), and
otherwise a locality_aliases row ("Pondy Bazaar" -> T. Nagar). There is no
substring matching: "Nagar" never resolves to every "... Nagar".

Master data (load_master) is idempotent and additive: it reuses existing
localities by key, never renames, moves or deletes a row, and never rebinds
a pincode that already belongs to another locality (reported as a conflict).
postcodes.pincode is globally UNIQUE (design V2.4.2), so one pincode belongs
to exactly one canonical locality; other names sharing that pincode are
stored as aliases.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import (
    AuditLog,
    BrokerPostcodeAccessCurrent,
    City,
    District,
    Locality,
    LocalityAlias,
    Postcode,
    Taluk,
    Village,
)
from .. import schemas
from ..config import area_seeding_enabled
from . import brokers as broker_svc

_KEY_STRIP = re.compile(r"[^a-z0-9]")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def name_key(name: str | None) -> str:
    """Spelling-insensitive key: lower-case ASCII letters and digits only."""
    return _KEY_STRIP.sub("", (name or "").lower())


def _sql_key(column):
    """SQL twin of name_key (MySQL 8 REGEXP_REPLACE)."""
    return func.regexp_replace(func.lower(column), "[^a-z0-9]", "")


# --- resolution ------------------------------------------------------------------

@dataclass
class Resolved:
    requested: str
    key: str
    status: str  # "ok" | "unknown" | "unavailable"
    name: str | None = None  # canonical display name when found
    locality_ids: list[int] = field(default_factory=list)
    postcode_ids: list[int] = field(default_factory=list)


def _localities_for_key(db: Session, key: str) -> list[tuple[int, str, int]]:
    """(id, name, agent_enabled) of every locality WITH a postcode for this key:
    by name first, then by alias."""
    with_postcode = db.query(Postcode.locality_id)
    rows = (
        db.query(Locality.id, Locality.name, Locality.agent_enabled)
        .filter(_sql_key(Locality.name) == key, Locality.id.in_(with_postcode))
        .order_by(Locality.id)
        .all()
    )
    # TEMPORARILY DISABLED — Chennai area seeding paused: aliases are only
    # consulted while the area-seeding flag is on.
    if not rows and area_seeding_enabled():
        alias_ids = db.query(LocalityAlias.locality_id).filter(LocalityAlias.alias_key == key)
        rows = (
            db.query(Locality.id, Locality.name, Locality.agent_enabled)
            .filter(Locality.id.in_(alias_ids), Locality.id.in_(with_postcode))
            .order_by(Locality.id)
            .all()
        )
    return [(int(r[0]), r[1], int(r[2] or 0)) for r in rows]


def resolve(db: Session, names: list[str], *, selectable_only: bool = True) -> list[Resolved]:
    """Resolve requested locality names, one result per distinct key (first
    spelling kept). With selectable_only, disabled localities are excluded
    and a name whose every match is disabled is "unavailable"."""
    out: list[Resolved] = []
    seen: set[str] = set()
    for raw in names:
        requested = (raw or "").strip()
        key = name_key(requested)
        if not requested or not key or key in seen:
            continue
        seen.add(key)
        rows = _localities_for_key(db, key)
        if not rows:
            out.append(Resolved(requested, key, "unknown"))
            continue
        usable = [r for r in rows if r[2]] if selectable_only else rows
        if not usable:
            out.append(Resolved(requested, key, "unavailable", name=rows[0][1]))
            continue
        ids = [r[0] for r in usable]
        pcs = [int(p[0]) for p in db.query(Postcode.id).filter(Postcode.locality_id.in_(ids)).order_by(Postcode.id)]
        out.append(Resolved(requested, key, "ok", name=usable[0][1], locality_ids=ids, postcode_ids=pcs))
    return out


def postcode_keys(db: Session, postcode_ids) -> dict[str, str]:
    """{locality key: display name} for the given postcodes (one place per key)."""
    ids = list(postcode_ids)
    if not ids:
        return {}
    rows = (
        db.query(Locality.name)
        .join(Postcode, Postcode.locality_id == Locality.id)
        .filter(Postcode.id.in_(ids))
        .order_by(Locality.id)
        .all()
    )
    out: dict[str, str] = {}
    for (name,) in rows:
        out.setdefault(name_key(name), name)
    return out


# --- admin catalog ---------------------------------------------------------------

def list_catalog(
    db: Session, *, q: str | None, enabled: bool | None, page: int, page_size: int
) -> tuple[list[dict], int]:
    """Localities that can be granted (>= 1 postcode), with enabled state,
    pincodes, aliases and how many agents currently cover them. Batched."""
    base = db.query(Locality).filter(Locality.id.in_(db.query(Postcode.locality_id)))
    if q and name_key(q):
        base = base.filter(_sql_key(Locality.name).like(f"%{name_key(q)}%"))
    if enabled is not None:
        base = base.filter(Locality.agent_enabled == (1 if enabled else 0))
    total = base.count()
    rows = base.order_by(Locality.name, Locality.id).offset((page - 1) * page_size).limit(page_size).all()
    ids = [r.id for r in rows]
    pincodes: dict[int, list[str]] = {i: [] for i in ids}
    aliases: dict[int, list[str]] = {i: [] for i in ids}
    agents: dict[int, int] = {}
    if ids:
        for lid, pin in db.query(Postcode.locality_id, Postcode.pincode).filter(
            Postcode.locality_id.in_(ids)
        ).order_by(Postcode.pincode):
            pincodes[int(lid)].append(pin)
        for lid, alias in db.query(LocalityAlias.locality_id, LocalityAlias.alias).filter(
            LocalityAlias.locality_id.in_(ids)
        ).order_by(LocalityAlias.alias):
            aliases[int(lid)].append(alias)
        counts = (
            db.query(Postcode.locality_id, func.count(func.distinct(BrokerPostcodeAccessCurrent.broker_user_id)))
            .join(BrokerPostcodeAccessCurrent, BrokerPostcodeAccessCurrent.postcode_id == Postcode.id)
            .filter(Postcode.locality_id.in_(ids), broker_svc._grant_is_active())
            .group_by(Postcode.locality_id)
            .all()
        )
        agents = {int(lid): int(n) for lid, n in counts}
    items = [
        {
            "locality_id": r.id,
            "name": r.name,
            "agent_enabled": bool(r.agent_enabled),
            "pincodes": pincodes[r.id],
            "aliases": aliases[r.id],
            "assigned_agent_count": agents.get(r.id, 0),
        }
        for r in rows
    ]
    return items, total


def set_agent_enabled(db: Session, admin_id: int, locality_id: int, enabled: bool, reason: str | None) -> dict:
    """Enable/disable a locality for NEW agent selection. Existing grants are
    deliberately left alone (agents keep paid coverage until plan expiry).
    Audited only when the state actually changes (idempotent repeat)."""
    loc = db.query(Locality).filter(Locality.id == locality_id).with_for_update().first()
    if loc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    before = bool(loc.agent_enabled)
    if before != enabled:
        loc.agent_enabled = 1 if enabled else 0
        db.add(AuditLog(
            user_id=admin_id,
            action="AGENT_LOCALITY_ENABLED" if enabled else "AGENT_LOCALITY_DISABLED",
            entity="locality",
            before_json={"locality_id": loc.id, "agent_enabled": before},
            after_json={"locality_id": loc.id, "agent_enabled": enabled, "reason": reason},
            created_at=_now(),
        ))
        db.flush()
    return {"locality_id": loc.id, "name": loc.name, "agent_enabled": bool(loc.agent_enabled)}


# --- master data loading ---------------------------------------------------------

@dataclass(frozen=True)
class MasterRow:
    locality: str
    pincode: str
    aliases: tuple[str, ...] = ()


# India Post office-name suffixes ("Adyar S.O", "Chennai G.P.O.", "X B.O").
_OFFICE_SUFFIX = re.compile(r"\s+(?:g\.?\s?p\.?\s?o|h\.?\s?o|s\.?\s?o|b\.?\s?o|p\.?\s?o)\.?\s*(?:\(.*\))?$", re.I)
_OFFICE_RANK = {"ho": 0, "h.o": 0, "so": 1, "s.o": 1, "po": 1, "bo": 2, "b.o": 2}


def clean_office_name(name: str) -> str:
    return re.sub(r"\s+", " ", _OFFICE_SUFFIX.sub("", (name or "").strip())).strip()


# India Post files name the district column either way: "districtname"
# (older directory / API) or "district" (current CSV download).
_DISTRICT_COLUMNS = ("districtname", "district")


def _district_of(rec: dict) -> str:
    for column in _DISTRICT_COLUMNS:
        if column in rec:
            return str(rec.get(column) or "").strip()
    return ""


def _india_post_offices(text: str, district: str) -> list[tuple[str, int, str]] | None:
    """(pincode, office rank, cleaned office name) for `district` in TAMIL NADU,
    from the India Post "All India Pincode Directory" as CSV or as data.gov.in
    API JSON ({"records": [...]}). Headers are matched case-insensitively and
    trimmed; the district column may be "districtname" or "district". None
    when `text` is not in that format (missing officename, pincode or a
    district column)."""
    stripped = text.lstrip("\ufeff").lstrip()
    if stripped.startswith("{"):
        import json

        try:
            records = json.loads(stripped).get("records")
        except ValueError:
            return None
        if not isinstance(records, list):
            return None
        records = [{str(k).strip().lower(): v for k, v in r.items()} for r in records if isinstance(r, dict)]
    else:
        reader = csv.DictReader(io.StringIO(stripped))
        fields = {h.strip().lower() for h in (reader.fieldnames or [])}
        if not {"officename", "pincode"} <= fields or not fields & set(_DISTRICT_COLUMNS):
            return None
        records = [{str(k).strip().lower(): v for k, v in r.items()} for r in reader]
    out = []
    for rec in records:
        if _district_of(rec).upper() != district.upper():
            continue
        if str(rec.get("statename") or "").strip().upper() not in ("TAMIL NADU", ""):
            continue
        pin = str(rec.get("pincode") or "").strip()
        name = clean_office_name(str(rec.get("officename") or ""))
        rank = _OFFICE_RANK.get(str(rec.get("officetype") or "").strip().lower(), 3)
        if pin and name:
            out.append((pin, rank, name))
    return out


def parse_master_csv(text: str, *, district: str = "CHENNAI") -> list[MasterRow]:
    """Parse either supported format into MasterRows (one per pincode).

    1. RESTAMP format, CSV header: locality,pincode[,aliases]   (aliases "|"-separated)
       Extra columns are ignored, except review_status: when that column is
       present ONLY rows marked VERIFIED are returned (pending_review lists
       the others), so an unapproved row can never be loaded.
    2. India Post "All India Pincode Directory" (data.gov.in), CSV with
       officename,pincode[,officetype],districtname|district[,statename] or the API's
       JSON. Rows are kept for `district` only; per pincode the head/sub
       office name becomes the canonical locality and the other office names
       become aliases.
    """
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    headers = {h.strip().lower(): h for h in (reader.fieldnames or [])}
    if "locality" in headers and "pincode" in headers:
        rows = []
        status_col = headers.get("review_status")
        for rec in reader:
            if status_col and (rec.get(status_col) or "").strip().upper() != "VERIFIED":
                continue
            aliases = tuple(a.strip() for a in (rec.get(headers.get("aliases", ""), "") or "").split("|") if a.strip())
            rows.append(MasterRow(rec[headers["locality"]].strip(), rec[headers["pincode"]].strip(), aliases))
        return rows
    offices = _india_post_offices(text, district)
    if offices is None:
        raise ValueError("Unrecognised locality CSV header")
    by_pin: dict[str, list[tuple[int, str]]] = {}
    for pin, rank, name in offices:
        by_pin.setdefault(pin, []).append((rank, name))
    out = []
    for pin in sorted(by_pin):
        ranked = sorted(set(by_pin[pin]))
        canonical = ranked[0][1]
        aliases = tuple(dict.fromkeys(n for _, n in ranked[1:] if name_key(n) != name_key(canonical)))
        out.append(MasterRow(canonical, pin, aliases))
    return out


def pending_review(text: str) -> list[dict]:
    """Rows of a curated CSV whose review_status is not VERIFIED (never loaded)."""
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    headers = {h.strip().lower(): h for h in (reader.fieldnames or [])}
    if not {"locality", "pincode", "review_status"} <= set(headers):
        return []
    return [
        {
            "locality": (rec.get(headers["locality"]) or "").strip(),
            "pincode": (rec.get(headers["pincode"]) or "").strip(),
            "review_status": (rec.get(headers["review_status"]) or "").strip(),
        }
        for rec in reader
        if (rec.get(headers["review_status"]) or "").strip().upper() != "VERIFIED"
    ]


def rows_for_names(text: str, names: list[str], *, district: str = "CHENNAI") -> tuple[list[MasterRow], list[str]]:
    """Master rows for EXACTLY these locality names (e.g. the app's picker),
    plus the names the source does not contain.

    The given spelling stays canonical. A name matches only rows whose name
    has the same spelling key ("T. Nagar" == "T.Nagar S.O"); never a
    substring ("Mogappair" does not match "Mogappair East"). India Post
    source: every pincode with an office of that name. RESTAMP curated
    source: rows whose locality or alias has that key, keeping that row's
    other aliases (e.g. the India Post spelling "Velacheri"). When two names share
    one pincode, the first (in `names` order) is canonical and the other is
    its alias, because a pincode belongs to one locality.
    """
    order: dict[str, str] = {}
    for n in names:
        n = re.sub(r"\s+", " ", (n or "").strip())
        if name_key(n) and name_key(n) not in order:
            order[name_key(n)] = n
    rank = {k: i for i, k in enumerate(order)}
    offices = _india_post_offices(text, district)
    pairs: list[tuple[str, str]] = []  # (pincode, wanted key)
    extra: dict[str, list[str]] = {}  # curated source: the row's own non-picker aliases
    if offices is not None:
        pairs = [(pin, name_key(office)) for pin, _, office in offices if name_key(office) in order]
    else:
        for row in parse_master_csv(text, district=district):
            names_here = (row.locality, *row.aliases)
            if not any(name_key(c) in order for c in names_here):
                continue
            for candidate in names_here:
                if name_key(candidate) in order:
                    pairs.append((row.pincode, name_key(candidate)))
                elif candidate not in extra.setdefault(row.pincode, []):
                    extra[row.pincode].append(candidate)
    by_pin: dict[str, list[str]] = {}
    for pin, key in pairs:
        if key not in by_pin.setdefault(pin, []):
            by_pin[pin].append(key)
    rows = []
    for pin in sorted(by_pin):
        keys = sorted(by_pin[pin], key=rank.__getitem__)
        rows.append(MasterRow(order[keys[0]], pin, tuple(order[k] for k in keys[1:]) + tuple(extra.get(pin, ()))))
    found = {k for keys in by_pin.values() for k in keys}
    missing = [n for k, n in order.items() if k not in found]
    return rows, missing


def _get_or_create(db: Session, query, build):
    row = query.first()
    if row is None:
        row = build()
        db.add(row)
        db.flush()
    return row


def _create_locality(db: Session, name: str, city_name: str) -> Locality:
    """Location chain for a new locality, mirroring owner_listings
    (district = city, taluk = village = locality name), reusing rows."""
    city = _get_or_create(db, db.query(City).filter(func.lower(City.name) == city_name.lower()),
                          lambda: City(name=city_name))
    dist = _get_or_create(db, db.query(District).filter(District.city_id == city.id,
                                                        func.lower(District.name) == city_name.lower()),
                          lambda: District(city_id=city.id, name=city_name))
    taluk = _get_or_create(db, db.query(Taluk).filter(Taluk.district_id == dist.id,
                                                      func.lower(Taluk.name) == name.lower()),
                           lambda: Taluk(district_id=dist.id, name=name))
    village = _get_or_create(db, db.query(Village).filter(Village.taluk_id == taluk.id,
                                                          func.lower(Village.name) == name.lower()),
                             lambda: Village(taluk_id=taluk.id, name=name))
    return _get_or_create(db, db.query(Locality).filter(Locality.village_id == village.id,
                                                        func.lower(Locality.name) == name.lower()),
                          lambda: Locality(village_id=village.id, name=name))


def load_master(db: Session, rows: list[MasterRow], *, city: str = "Chennai") -> dict:
    """Add missing localities, postcodes and aliases. Idempotent and additive:
    a second run with the same rows changes nothing. Returns a report; never
    raises for bad rows (they are reported and skipped). Caller commits."""
    report = {
        "localities_created": [], "localities_reused": [], "postcodes_created": [],
        "postcodes_existing": [], "aliases_created": [], "aliases_existing": [],
        "conflicts": [], "skipped": [],
    }
    for row in rows:
        name = re.sub(r"\s+", " ", (row.locality or "").strip())
        pin = (row.pincode or "").strip()
        key = name_key(name)
        if not key:
            report["skipped"].append({"locality": name, "pincode": pin, "reason": "invalid locality name"})
            continue
        if not schemas.is_chennai_service_pincode(pin):
            # Same policy as owner listings (schemas.is_chennai_service_pincode).
            report["skipped"].append({"locality": name, "pincode": pin, "reason": "pincode outside the approved Chennai service area"})
            continue
        try:
            with db.begin_nested():
                bound = (
                    db.query(Postcode.id, Locality.id, Locality.name)
                    .join(Locality, Locality.id == Postcode.locality_id)
                    .filter(Postcode.pincode == pin)
                    .first()
                )
                if bound is not None and name_key(bound[2]) != key:
                    # Never rebind a pincode: report it for a human decision.
                    report["conflicts"].append(
                        {"pincode": pin, "requested": name, "existing_locality": bound[2]}
                    )
                    continue
                if bound is not None:
                    loc_id, loc_name = int(bound[1]), bound[2]
                    report["postcodes_existing"].append(pin)
                else:
                    existing = (
                        db.query(Locality.id, Locality.name)
                        .filter(_sql_key(Locality.name) == key)
                        .order_by(Locality.id)
                        .first()
                    )
                    if existing is not None:
                        loc_id, loc_name = int(existing[0]), existing[1]
                        if loc_name not in report["localities_reused"]:
                            report["localities_reused"].append(loc_name)
                    else:
                        loc = _create_locality(db, name, city)
                        loc_id, loc_name = loc.id, loc.name
                        report["localities_created"].append(loc_name)
                    db.add(Postcode(locality_id=loc_id, pincode=pin))
                    db.flush()
                    report["postcodes_created"].append(pin)
                _add_aliases(db, loc_id, loc_name, row.aliases, report)
        except IntegrityError:
            # A concurrent writer took this pincode/alias first: nothing of
            # this row was written (savepoint rolled back).
            report["conflicts"].append({"pincode": pin, "requested": name, "existing_locality": None})
    return report


def _add_aliases(db: Session, locality_id: int, locality_name: str, aliases, report: dict) -> None:
    own = name_key(locality_name)
    for alias in aliases:
        alias = re.sub(r"\s+", " ", (alias or "").strip())
        akey = name_key(alias)
        if not akey or akey == own:
            continue  # same spelling key as the canonical name: already resolvable
        held = db.query(LocalityAlias.locality_id).filter(LocalityAlias.alias_key == akey).first()
        if held is not None:
            if int(held[0]) == locality_id:
                report["aliases_existing"].append(alias)
            else:
                report["conflicts"].append({"alias": alias, "requested": locality_name, "reason": "alias used by another locality"})
            continue
        other = (
            db.query(Locality.name)
            .filter(_sql_key(Locality.name) == akey, Locality.id != locality_id)
            .first()
        )
        if other is not None:
            # An alias may not shadow a real locality's own name.
            report["conflicts"].append({"alias": alias, "requested": locality_name, "reason": "alias is another locality's name"})
            continue
        db.add(LocalityAlias(locality_id=locality_id, alias=alias, alias_key=akey, created_at=_now()))
        db.flush()
        report["aliases_created"].append(alias)


# --- placeholder postcode reassignment (e.g. Perungudi 600096) ---------------------

class ReassignRefused(RuntimeError):
    """A reassignment check failed; nothing was changed."""


def _city_of(db: Session, locality_id: int) -> str | None:
    row = (
        db.query(City.name)
        .join(District, District.city_id == City.id)
        .join(Taluk, Taluk.district_id == District.id)
        .join(Village, Village.taluk_id == Taluk.id)
        .join(Locality, Locality.village_id == Village.id)
        .filter(Locality.id == locality_id)
        .first()
    )
    return row[0] if row else None


def reassign_placeholder_postcode(
    db: Session,
    pincode: str,
    *,
    placeholder_locality: str,
    target_locality: str,
    placeholder_city: str = "Seed Chennai",
    city: str = "Chennai",
) -> dict:
    """Move ONE postcode row from a placeholder locality to its real locality,
    keeping the row (same id). Every check runs under a row lock and any
    failure raises ReassignRefused before anything changes. Caller commits.

    Checks: the row exists and belongs to `placeholder_locality` inside
    `placeholder_city`; no property, agent grant (current or history) or
    audit row references it; the placeholder keeps another postcode (load the
    reviewed catalog first); the target resolves to at most one locality,
    which must be in `city` (none -> created under `city`).
    """
    pin = (pincode or "").strip()
    row = db.query(Postcode).filter(Postcode.pincode == pin).with_for_update().first()
    if row is None:
        raise ReassignRefused(f"pincode {pin} does not exist")
    current = db.get(Locality, row.locality_id)
    if name_key(current.name) != name_key(placeholder_locality):
        raise ReassignRefused(f"pincode {pin} belongs to '{current.name}', not '{placeholder_locality}'")
    if (_city_of(db, current.id) or "") != placeholder_city:
        raise ReassignRefused(f"'{current.name}' is not in the placeholder city '{placeholder_city}'")
    from ..models import BrokerPostcodeAccessHistory, PhysicalProperty

    references = {
        "properties": db.query(PhysicalProperty.id).filter(PhysicalProperty.postcode_id == row.id).count(),
        "agent grants": db.query(BrokerPostcodeAccessCurrent.postcode_id)
        .filter(BrokerPostcodeAccessCurrent.postcode_id == row.id).count(),
        "agent grant history": db.query(BrokerPostcodeAccessHistory.id)
        .filter(BrokerPostcodeAccessHistory.postcode_id == row.id).count(),
        "audit rows": db.query(AuditLog.id).filter(
            func.json_contains(func.coalesce(AuditLog.before_json, "{}"), str(row.id), "$.postcode_ids")
            | func.json_contains(func.coalesce(AuditLog.after_json, "{}"), str(row.id), "$.postcode_ids")
        ).count(),
    }
    used = {k: v for k, v in references.items() if v}
    if used:
        raise ReassignRefused(f"pincode {pin} is referenced: {used}")
    if db.query(Postcode.id).filter(Postcode.locality_id == current.id, Postcode.id != row.id).count() == 0:
        raise ReassignRefused(f"'{current.name}' would be left without a pincode; load the reviewed catalog first")

    target_key = name_key(target_locality)
    matches = (
        db.query(Locality)
        .filter(_sql_key(Locality.name) == target_key, Locality.id != current.id)
        .order_by(Locality.id)
        .all()
    )
    in_city = [m for m in matches if _city_of(db, m.id) == city]
    if len(in_city) > 1 or (matches and not in_city):
        raise ReassignRefused(f"target '{target_locality}' is ambiguous ({len(matches)} existing rows)")
    created = not in_city
    target = in_city[0] if in_city else _create_locality(db, target_locality.strip(), city)

    old_locality_id = row.locality_id
    row.locality_id = target.id
    db.flush()
    return {
        "postcode_id": row.id,
        "pincode": pin,
        "old_locality_id": old_locality_id,
        "old_locality": current.name,
        "new_locality_id": target.id,
        "new_locality": target.name,
        "target_created": created,
        "rollback_sql": (
            f"UPDATE postcodes SET locality_id={old_locality_id} "
            f"WHERE id={row.id} AND locality_id={target.id};"
        ),
    }
