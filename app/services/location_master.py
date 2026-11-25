"""RESTAMP Agent Location Master — Excel workbook <-> database.

AUTHORITY
  - Writes are recorded in the database (location_master_records is
    append-only; every change bumps location_master_state.committed_version).
  - The workbook is the PUBLISHED dataset: each publication is an exact
    rendering of one committed version (version + content digest are stamped
    in its hidden "_meta" sheet).
  - Agents only ever see rows whose version <= published_version, so the
    agent options always equal the published workbook.

WRITE PATH (add_locality / seed), serialised across workers and servers by a
MySQL named lock held on a dedicated connection:
  1. reconcile (refuse while blocked)      4. COMMIT (failure -> rollback,
  2. validate, insert record, update group,    temp file removed, nothing
     bump committed_version, log PENDING       changed)
  3. render the new workbook to a temp     5. atomic os.replace + fsync,
     file beside the live one (fsync)          versioned backup, log PUBLISHED,
                                               published_version = version
A crash between 4 and 5 leaves the database one version ahead with a
PENDING log row; reconcile() (startup and before every write) rebuilds the
workbook from the database. The live workbook is never ahead of the database.

RECONCILIATION never overwrites or discards a workbook whose content it
cannot account for (hand-edited, newer than the database, wrong digest): it
copies it to quarantine/, sets blocked_reason and refuses admin additions
until an operator resolves it (scripts/init_location_master.py --resolve).

Bulk import is intentionally not implemented: records are added one at a
time by an admin, or seeded once from the source workbook.
"""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, status
from sqlalchemy import func, or_, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import (
    AgentLocationGroup,
    LocationMasterRecord,
    LocationMasterState,
    LocationPublishLog,
    Postcode,
)
from ..models.location_master import CHENNAI_ZONES

log = logging.getLogger("restamp.location_master")

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIR = REPO_ROOT / "agent-locations"
SEED_WORKBOOK = SOURCE_DIR / "Chennai_Pincodes_North_Central_South_298.xlsx"
PRIMARY_NAMES_FILE = SOURCE_DIR / "agent_location_primary_names.csv"
DEV_WORKBOOK = REPO_ROOT / "var" / "location-master" / "agent_location_master.xlsx"

SHEET_MASTER = "Original Location Master"
SHEET_GROUPS = "Agent Location Selection"
SHEET_META = "_meta"
ZONES = CHENNAI_ZONES
LOCK_NAME = "restamp_location_master"
LOCK_TIMEOUT_S = 15
BACKUPS_KEPT = 50

PIN_RE = re.compile(r"^\d{6}$")
NAME_MAX = 120

LOCATION_UNAVAILABLE = "LOCATION_MASTER_UNAVAILABLE"
LOCATION_PUBLISH_PENDING = "LOCATION_PUBLISH_PENDING"
LOCATION_DUPLICATE = "LOCATION_DUPLICATE"
LOCATION_ZONE_CONFLICT = "LOCATION_ZONE_CONFLICT"


# ---------------------------------------------------------------- helpers ---

def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def normalize_name(value) -> str:
    """Display form: surrounding/internal whitespace collapsed. The raw value
    is stored separately and never changed."""
    return " ".join(str(value or "").split())


def name_key(value) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def display_name(primary: str, localities: list[str], pincode: str) -> str:
    """'Primary (A, B, C) - 600053', or 'Locality - 600001' for one locality."""
    if len(localities) <= 1:
        return f"{(localities[0] if localities else primary)} - {pincode}"
    return f"{primary} ({', '.join(localities)}) - {pincode}"


@dataclass(frozen=True)
class SourceRecord:
    record_no: int
    raw_name: str
    pincode: str
    zone: str

    @property
    def display(self) -> str:
        return normalize_name(self.raw_name)


def _bad(detail, code: int = status.HTTP_422_UNPROCESSABLE_ENTITY) -> HTTPException:
    return HTTPException(status_code=code, detail=detail)


# ----------------------------------------------------------- source files ---

def read_source_workbook(path: Path | str = SEED_WORKBOOK) -> list[SourceRecord]:
    """The original records from the source workbook's main sheet
    ("Chennai Pincodes", else the first sheet): S.No., Location, Pincode,
    Region. Raises ValueError on any malformed row (nothing is guessed)."""
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb["Chennai Pincodes"] if "Chennai Pincodes" in wb.sheetnames else wb.worksheets[0]
    rows = ws.iter_rows(values_only=True)
    header = [normalize_name(h).lower() for h in next(rows)]
    try:
        i_no, i_name, i_pin, i_zone = (
            header.index("s.no."), header.index("location"), header.index("pincode"), header.index("region"),
        )
    except ValueError as e:
        raise ValueError(f"Unexpected source header: {header}") from e
    out: list[SourceRecord] = []
    for n, r in enumerate(rows, start=2):
        if r is None or all(v is None or str(v).strip() == "" for v in r):
            continue
        no, name, pin, zone = r[i_no], r[i_name], r[i_pin], r[i_zone]
        pin = str(pin).strip() if pin is not None else ""
        if isinstance(r[i_pin], (int, float)):
            pin = str(int(r[i_pin])).zfill(6)
        zone = normalize_name(zone)
        if not isinstance(no, (int, float)) or not normalize_name(name) or not PIN_RE.match(pin) or zone not in ZONES:
            raise ValueError(f"Source row {n} is invalid: {r!r}")
        out.append(SourceRecord(int(no), str(name), pin, zone))
    wb.close()
    nos = [s.record_no for s in out]
    if len(set(nos)) != len(nos):
        raise ValueError("Source has duplicate S.No. values")
    return out


def read_primary_names(path: Path | str = PRIMARY_NAMES_FILE) -> dict[str, tuple[str, bool]]:
    """{pincode: (primary name, needs_review)} from the reviewed names file."""
    out: dict[str, tuple[str, bool]] = {}
    if not Path(path).exists():
        return out
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            pin = (row.get("PIN Code") or "").strip()
            name = normalize_name(row.get("Primary Location Name"))
            if PIN_RE.match(pin) and name:
                out[pin] = (name, (row.get("Needs Review") or "").strip().lower() == "yes")
    return out


def group_source(records: list[SourceRecord]) -> tuple["OrderedDict[str, list[SourceRecord]]", dict[str, list[str]]]:
    """Group by PIN code (the ONLY grouping key). Returns (groups in PIN
    order, {pincode: zones} for PIN codes with conflicting zones)."""
    groups: dict[str, list[SourceRecord]] = {}
    for r in records:
        groups.setdefault(r.pincode, []).append(r)
    conflicts = {p: sorted({r.zone for r in v}) for p, v in groups.items() if len({r.zone for r in v}) > 1}
    return OrderedDict(sorted(groups.items())), conflicts


# ------------------------------------------------------------ storage path ---

def workbook_path() -> Path:
    """Where the live workbook lives. Production must set
    RESTAMP_LOCATION_MASTER_PATH to a persistent file outside the repository
    (see storage_problem). Development defaults to ./var/location-master/."""
    raw = os.environ.get("RESTAMP_LOCATION_MASTER_PATH", "").strip()
    return Path(raw) if raw else DEV_WORKBOOK


def storage_problem() -> str | None:
    """Why the workbook path cannot be used here, or None. Production and
    staging refuse an unset, relative, temporary or in-repository path (the
    same rules as photo storage)."""
    from ..config import is_development, unsafe_media_root_reason

    raw = os.environ.get("RESTAMP_LOCATION_MASTER_PATH", "").strip()
    if not raw:
        return None if is_development() else "RESTAMP_LOCATION_MASTER_PATH is not set"
    if not raw.endswith(".xlsx"):
        return "RESTAMP_LOCATION_MASTER_PATH must name an .xlsx file"
    if not is_development():
        reason = unsafe_media_root_reason(str(Path(raw).parent))
        if reason:
            return f"RESTAMP_LOCATION_MASTER_PATH directory {reason}"
    return None


def _require_storage() -> Path:
    problem = storage_problem()
    if problem:
        raise _bad({"code": LOCATION_UNAVAILABLE, "message": "Location management is not configured on this server."},
                   status.HTTP_503_SERVICE_UNAVAILABLE)
    path = workbook_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


# ------------------------------------------------------------------- lock ---

@contextmanager
def master_lock(db: Session):
    """Cluster-wide write lock: MySQL GET_LOCK on a DEDICATED connection (a
    session's connection is returned to the pool on commit, which would
    strand a session-scoped named lock)."""
    from sqlalchemy.engine import Connection

    bind = db.get_bind()
    # A session bound to an explicit Connection (tests, scripts) keeps that
    # connection for its whole life, so the lock can live on it directly.
    own = not isinstance(bind, Connection)
    conn = bind.connect() if own else bind
    try:
        got = conn.execute(text("SELECT GET_LOCK(:n, :t)"), {"n": LOCK_NAME, "t": LOCK_TIMEOUT_S}).scalar()
        if got != 1:
            raise _bad({"code": LOCATION_UNAVAILABLE, "message": "Another location update is in progress. Please retry."},
                       status.HTTP_503_SERVICE_UNAVAILABLE)
        try:
            yield
        finally:
            conn.execute(text("SELECT RELEASE_LOCK(:n)"), {"n": LOCK_NAME})
    finally:
        if own:
            conn.close()


# ------------------------------------------------------------------ state ---

def _state(db: Session, *, lock: bool = False) -> LocationMasterState:
    q = db.query(LocationMasterState).filter(LocationMasterState.id == 1)
    if lock:
        q = q.with_for_update()
    st = q.first()
    if st is None:
        st = LocationMasterState(id=1, committed_version=0, published_version=0)
        db.add(st)
        db.flush()
    return st


def published_version(db: Session) -> int:
    st = db.query(LocationMasterState.published_version).filter(LocationMasterState.id == 1).scalar()
    return int(st or 0)


# --------------------------------------------------------------- snapshot ---

def _snapshot(db: Session, version: int) -> tuple[list[LocationMasterRecord], list[AgentLocationGroup]]:
    recs = (
        db.query(LocationMasterRecord)
        .filter(LocationMasterRecord.version <= version)
        .order_by(LocationMasterRecord.record_no)
        .all()
    )
    groups = (
        db.query(AgentLocationGroup)
        .filter(AgentLocationGroup.created_version <= version)
        .order_by(AgentLocationGroup.pincode)
        .all()
    )
    return recs, groups


def _group_rows(recs, groups) -> list[dict]:
    by_pin: dict[str, list[LocationMasterRecord]] = {}
    for r in recs:
        by_pin.setdefault(r.pincode, []).append(r)
    rows = []
    for g in groups:
        names = [r.display_name for r in by_pin.get(g.pincode, [])]
        rows.append({
            "id": int(g.id),
            "pincode": g.pincode,
            "primary_name": g.primary_name,
            "zone": g.zone,
            "localities": names,
            "locality_count": len(names),
            "display_name": display_name(g.primary_name, names, g.pincode),
            "needs_review": bool(g.needs_review),
        })
    return rows


def content_digest(recs, groups) -> str:
    """Digest of exactly what the workbook shows (records + groups)."""
    payload = {
        "records": [[r.record_no, r.raw_name, r.pincode, r.zone] for r in recs],
        "groups": [[g["id"], g["pincode"], g["primary_name"], g["zone"], g["localities"]] for g in _group_rows(recs, groups)],
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


# --------------------------------------------------------------- workbook ---

def build_workbook(recs, groups, version: int):
    """Sheet 1 Original Location Master, Sheet 2 Agent Location Selection
    (derived), North/Central/South Chennai sheets (derived) and a hidden
    _meta sheet with the version and content digest."""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_MASTER
    ws.append(["Record ID", "Locality Name", "PIN Code", "Zone"])
    for r in recs:
        ws.append([r.record_no, r.raw_name, r.pincode, r.zone])

    g = wb.create_sheet(SHEET_GROUPS)
    g.append(["Location ID", "Primary Location Name", "Associated Localities", "PIN Code", "Zone", "Display Name"])
    for row in _group_rows(recs, groups):
        g.append([row["id"], row["primary_name"], ", ".join(row["localities"]), row["pincode"], row["zone"], row["display_name"]])

    for zone in ZONES:
        z = wb.create_sheet(zone)
        z.append(["S.No.", "Location", "Pincode"])
        for r in recs:
            if r.zone == zone:
                z.append([r.record_no, r.raw_name, r.pincode])

    for sheet in wb.worksheets:
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        # PIN codes are text: never let Excel turn 600001 into a number.
        for col in sheet.iter_cols(min_row=2):
            for cell in col:
                if isinstance(cell.value, str) and PIN_RE.match(cell.value):
                    cell.number_format = "@"

    meta = wb.create_sheet(SHEET_META)
    meta.append(["version", version])
    meta.append(["digest", content_digest(recs, groups)])
    meta.append(["records", len(recs)])
    meta.append(["groups", len(groups)])
    meta.append(["generated_at", _now().isoformat()])
    meta.sheet_state = "hidden"
    return wb


def read_meta(path: Path) -> dict | None:
    """{version, digest, records, groups} from a workbook, or None when the
    file is missing; {"unreadable": True} when it cannot be parsed."""
    if not path.exists():
        return None
    try:
        from openpyxl import load_workbook

        wb = load_workbook(path, read_only=True, data_only=True)
        if SHEET_META not in wb.sheetnames:
            wb.close()
            return {"unreadable": True}
        meta = {str(k): v for k, v in wb[SHEET_META].iter_rows(values_only=True) if k}
        wb.close()
        return {"version": int(meta.get("version", -1)), "digest": str(meta.get("digest", ""))}
    except Exception:  # corrupt or not an xlsx
        return {"unreadable": True}


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_temp(wb, path: Path) -> tuple[Path, str]:
    """Save to a temp file beside ``path`` (same filesystem, so the later
    rename is atomic) and fsync it. Returns (temp path, sha256 of the file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".lm-", suffix=".xlsx.tmp", dir=str(path.parent))
    os.close(fd)
    try:
        wb.save(tmp)
        with open(tmp, "rb+") as f:
            os.fsync(f.fileno())
            f.seek(0)
            sha = hashlib.sha256(f.read()).hexdigest()
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
    return Path(tmp), sha


def swap_in(tmp: Path, path: Path, version: int) -> None:
    """Atomically replace the live workbook, fsync the directory and keep a
    versioned backup (newest BACKUPS_KEPT)."""
    os.replace(tmp, path)
    _fsync_dir(path.parent)
    backups = path.parent / "backups"
    backups.mkdir(exist_ok=True)
    shutil.copy2(path, backups / f"{path.stem}_v{version:06d}.xlsx")
    old = sorted(backups.glob(f"{path.stem}_v*.xlsx"))[:-BACKUPS_KEPT]
    for f in old:
        f.unlink(missing_ok=True)


def _quarantine(path: Path, reason: str) -> Path:
    q = path.parent / "quarantine"
    q.mkdir(exist_ok=True)
    dest = q / f"{path.stem}_{_now().strftime('%Y%m%dT%H%M%S')}.xlsx"
    shutil.copy2(path, dest)  # copy: the live file is left exactly as found
    log.error("Location master workbook quarantined (%s): %s", reason, dest)
    return dest


# ----------------------------------------------------------- publication ---

def _publish_committed(db: Session, path: Path, version: int) -> str:
    """Render ``version`` from the database and make it the live workbook,
    then record it (own transaction). Used after a commit and by reconcile."""
    recs, groups = _snapshot(db, version)
    tmp, sha = write_temp(build_workbook(recs, groups, version), path)
    try:
        swap_in(tmp, path, version)
    finally:
        tmp.unlink(missing_ok=True)
    st = _state(db, lock=True)
    if version > st.published_version:
        st.published_version = version
    st.updated_at = _now()
    row = db.query(LocationPublishLog).filter(LocationPublishLog.version == version).first()
    if row is None:
        row = LocationPublishLog(version=version, status="PENDING")
        db.add(row)
    row.status, row.file_sha256, row.error, row.published_at = "PUBLISHED", sha, None, _now()
    db.commit()
    return sha


def reconcile(db: Session) -> dict:
    """Bring the workbook and database to the same version WITHOUT discarding
    anything. Safe to run any time (startup, before every write)."""
    path = workbook_path()
    st = _state(db)
    committed, blocked = int(st.committed_version), st.blocked_reason
    db.commit()
    if blocked:
        return {"status": "blocked", "reason": blocked, "committed_version": committed}
    if committed == 0:
        return {"status": "empty", "committed_version": 0}
    meta = read_meta(path)
    recs, groups = _snapshot(db, committed)
    expected = content_digest(recs, groups)

    def block(reason: str) -> dict:
        if path.exists():
            _quarantine(path, reason)
        s = _state(db, lock=True)
        s.blocked_reason = reason[:500]
        db.commit()
        return {"status": "blocked", "reason": reason, "committed_version": committed}

    if meta is None:
        log.warning("Location master workbook missing; rebuilding version %s from the database.", committed)
        _publish_committed(db, path, committed)
        return {"status": "restored", "version": committed}
    if meta.get("unreadable"):
        return block("The workbook cannot be read (corrupt, or not produced by RESTAMP).")
    if meta["version"] > committed:
        return block(f"The workbook (version {meta['version']}) is newer than the database (version {committed}).")
    if meta["version"] < committed:
        # Interrupted publication: the database already holds every record
        # the workbook has, so rebuilding loses nothing. Verify that first.
        old_recs, old_groups = _snapshot(db, meta["version"])
        if content_digest(old_recs, old_groups) != meta["digest"]:
            return block(f"The workbook (version {meta['version']}) was changed outside RESTAMP.")
        _publish_committed(db, path, committed)
        return {"status": "republished", "from": meta["version"], "version": committed}
    if meta["digest"] != expected:
        return block("The workbook was changed outside RESTAMP.")
    if published_version(db) < committed:
        _publish_committed(db, path, committed)  # file is right; record it
    return {"status": "ok", "version": committed}


def resolve_blocked(db: Session) -> dict:
    """Operator action: keep the quarantined copy, clear the block and
    republish the workbook from the database."""
    path = _require_storage()
    with master_lock(db):
        st = _state(db, lock=True)
        reason = st.blocked_reason
        if path.exists():
            _quarantine(path, "operator resolve")
        st.blocked_reason = None
        db.commit()
        version = int(_state(db).committed_version)
        db.commit()
        if version:
            _publish_committed(db, path, version)
    return {"resolved": reason, "version": version}


def _commit_and_publish(db: Session, path: Path, version: int) -> str:
    """Steps 3-5 of the write path. Before the commit, any failure rolls the
    transaction back and removes the temp file (nothing changes)."""
    db.add(LocationPublishLog(version=version, status="PENDING"))
    db.flush()
    recs, groups = _snapshot(db, version)
    try:
        tmp, _sha = write_temp(build_workbook(recs, groups, version), path)
    except Exception as e:
        db.rollback()
        log.error("Location master workbook could not be written: %s", e.__class__.__name__)
        raise _bad({"code": LOCATION_UNAVAILABLE, "message": "The location could not be saved (workbook write failed)."},
                   status.HTTP_503_SERVICE_UNAVAILABLE)
    try:
        db.commit()
    except Exception:
        db.rollback()
        tmp.unlink(missing_ok=True)
        raise
    # Committed. From here the database is authoritative; publish (retry).
    last = None
    for _attempt in range(3):
        try:
            swap_in(tmp, path, version)
            break
        except Exception as e:  # pragma: no cover - filesystem failure
            last = e
    else:
        tmp.unlink(missing_ok=True)
        row = db.query(LocationPublishLog).filter(LocationPublishLog.version == version).first()
        if row is not None:
            row.error = f"publish failed: {last.__class__.__name__}"
            db.commit()
        raise _bad({
            "code": LOCATION_PUBLISH_PENDING,
            "message": "Saved, but the workbook could not be updated yet. It is not visible to agents "
                       "until publishing completes (retried automatically).",
            "version": version,
        }, status.HTTP_503_SERVICE_UNAVAILABLE)
    st = _state(db, lock=True)
    st.published_version = max(int(st.published_version), version)
    st.updated_at = _now()
    row = db.query(LocationPublishLog).filter(LocationPublishLog.version == version).first()
    row.status, row.published_at = "PUBLISHED", _now()
    with open(path, "rb") as f:
        row.file_sha256 = hashlib.sha256(f.read()).hexdigest()
    db.commit()
    return row.file_sha256


# ------------------------------------------------------------- postcodes ---

def _postcode_for(db: Session, pincode: str, locality_name: str) -> int:
    """The existing postcodes row for this PIN code, or a new one under a new
    locality (existing rows are never changed)."""
    from .localities import _create_locality

    row = db.query(Postcode).filter(Postcode.pincode == pincode).first()
    if row is not None:
        return int(row.id)
    loc = _create_locality(db, locality_name, "Chennai")
    pc = Postcode(locality_id=loc.id, pincode=pincode)
    db.add(pc)
    db.flush()
    return int(pc.id)


# ------------------------------------------------------------------- seed ---

def seed(db: Session, source: Path | str = SEED_WORKBOOK, names: Path | str = PRIMARY_NAMES_FILE) -> dict:
    """Load the original records once (idempotent). A second run verifies the
    stored records against the source and only applies primary-name changes
    from the names file (as a new version). Never deletes or edits records."""
    path = _require_storage()
    src = read_source_workbook(source)
    groups_src, conflicts = group_source(src)
    if conflicts:
        raise ValueError(f"Zone conflicts within PIN codes must be resolved first: {conflicts}")
    primary = read_primary_names(names)
    report: dict = {"source_records": len(src), "source_pincodes": len(groups_src), "postcodes_created": [],
                    "existing_name_differences": [], "names_updated": []}
    with master_lock(db):
        rec = reconcile(db)
        if rec["status"] == "blocked":
            raise RuntimeError(f"Location master is blocked: {rec['reason']}")
        st = _state(db, lock=True)
        existing = db.query(func.count(LocationMasterRecord.id)).filter(LocationMasterRecord.source == "SEED").scalar()
        if existing:
            stored = {
                r.record_no: (r.raw_name, r.pincode, r.zone)
                for r in db.query(LocationMasterRecord).filter(LocationMasterRecord.source == "SEED")
            }
            mismatched = [s.record_no for s in src if stored.get(s.record_no) != (s.raw_name, s.pincode, s.zone)]
            if mismatched or len(stored) != len(src):
                raise RuntimeError(f"Stored seed records differ from the source (records {mismatched[:10]}...). Nothing changed.")
            version = int(st.committed_version) + 1
            for g in db.query(AgentLocationGroup).with_for_update():
                want = primary.get(g.pincode)
                if want and (want[0] != g.primary_name or want[1] != bool(g.needs_review)):
                    report["names_updated"].append([g.pincode, g.primary_name, want[0]])
                    g.primary_name, g.needs_review, g.updated_version, g.updated_at = want[0], want[1], version, _now()
            if not report["names_updated"]:
                db.rollback()
                report["status"] = "already seeded"
                return report
            st.committed_version = version
            _commit_and_publish(db, path, version)
            report["status"] = "names updated"
            report["version"] = version
            return report

        version = int(st.committed_version) + 1
        for s in src:
            db.add(LocationMasterRecord(
                record_no=s.record_no, raw_name=s.raw_name, display_name=s.display, pincode=s.pincode,
                zone=s.zone, source="SEED", version=version,
            ))
        for pin, recs in groups_src.items():
            names_here = [r.display for r in recs]
            name, review = primary.get(pin, (names_here[0] if len(recs) == 1 else None, len(recs) > 1))
            if not name:
                raise ValueError(f"PIN {pin} has {len(recs)} localities but no reviewed primary name")
            existing_pc = db.query(Postcode).filter(Postcode.pincode == pin).first()
            if existing_pc is None:
                report["postcodes_created"].append(pin)
            else:
                from ..models import Locality

                loc_name = db.query(Locality.name).filter(Locality.id == existing_pc.locality_id).scalar()
                if name_key(loc_name) not in {name_key(n) for n in names_here} | {name_key(name)}:
                    report["existing_name_differences"].append([pin, loc_name, name])
            db.add(AgentLocationGroup(
                pincode=pin, postcode_id=_postcode_for(db, pin, name), primary_name=name, zone=recs[0].zone,
                needs_review=review, created_version=version, updated_version=version,
            ))
        st.committed_version = version
        st.updated_at = _now()
        _commit_and_publish(db, path, version)
    report["status"] = "seeded"
    report["version"] = version
    return report


# -------------------------------------------------------------- admin add ---

def validate_input(locality_name, pincode, zone) -> tuple[str, str, str]:
    name = normalize_name(locality_name)
    pin = str(pincode or "").strip()
    zone = normalize_name(zone)
    errors = {}
    if not name:
        errors["locality_name"] = "Locality name is required."
    elif len(name) > NAME_MAX:
        errors["locality_name"] = f"Locality name must be at most {NAME_MAX} characters."
    elif not re.search(r"[A-Za-z]", name):
        errors["locality_name"] = "Locality name must contain letters."
    if not PIN_RE.match(pin):
        errors["pincode"] = "PIN code must be exactly six digits."
    if zone not in ZONES:
        errors["zone"] = "Zone must be North Chennai, Central Chennai or South Chennai."
    if errors:
        raise _bad({"code": "LOCATION_INVALID", "message": "Please correct the highlighted fields.", "errors": errors})
    return name, pin, zone


def add_locality(db: Session, admin_id: int, locality_name, pincode, zone, idempotency_key: str | None) -> dict:
    """Append one locality; update or create its PIN-code group; publish.
    Returns {"record": ..., "group": ..., "created_group": bool, "replayed": bool}."""
    name, pin, zone = validate_input(locality_name, pincode, zone)
    idem = (idempotency_key or "").strip()[:64] or None
    path = _require_storage()
    with master_lock(db):
        rec = reconcile(db)
        if rec["status"] == "blocked":
            raise _bad({"code": LOCATION_UNAVAILABLE,
                        "message": "Location management is paused: the workbook needs operator review."},
                       status.HTTP_503_SERVICE_UNAVAILABLE)
        if rec["status"] == "empty":
            raise _bad({"code": LOCATION_UNAVAILABLE, "message": "The location master has not been initialised."},
                       status.HTTP_503_SERVICE_UNAVAILABLE)
        if idem:
            prior = db.query(LocationMasterRecord).filter(LocationMasterRecord.idempotency_key == idem).first()
            if prior is not None:
                if (prior.display_name, prior.pincode, prior.zone) != (name, pin, zone):
                    raise _bad({"code": LOCATION_DUPLICATE, "message": "This request key was already used for a different location."},
                               status.HTTP_409_CONFLICT)
                return {**_result(db, prior), "replayed": True}
        same_pin = db.query(LocationMasterRecord).filter(LocationMasterRecord.pincode == pin).all()
        if any(name_key(r.display_name) == name_key(name) for r in same_pin):
            raise _bad({"code": LOCATION_DUPLICATE,
                        "message": f"{name} is already listed under PIN code {pin}."}, status.HTTP_409_CONFLICT)
        group = db.query(AgentLocationGroup).filter(AgentLocationGroup.pincode == pin).with_for_update().first()
        if group is not None and group.zone != zone:
            raise _bad({"code": LOCATION_ZONE_CONFLICT,
                        "message": f"PIN code {pin} is in {group.zone}. A PIN code belongs to one zone; "
                                   "check the zone or raise it for review."}, status.HTTP_409_CONFLICT)
        st = _state(db, lock=True)
        version = int(st.committed_version) + 1
        next_no = int(db.query(func.coalesce(func.max(LocationMasterRecord.record_no), 0)).scalar()) + 1
        record = LocationMasterRecord(
            record_no=next_no, raw_name=name, display_name=name, pincode=pin, zone=zone, source="ADMIN",
            dedupe_key=f"{name_key(name)}|{pin}|{zone}"[:300], idempotency_key=idem, version=version,
            created_by=admin_id,
        )
        db.add(record)
        created = group is None
        if created:
            group = AgentLocationGroup(
                pincode=pin, postcode_id=_postcode_for(db, pin, name), primary_name=name, zone=zone,
                needs_review=False, created_version=version, updated_version=version,
            )
            db.add(group)
        else:
            group.updated_version, group.updated_at = version, _now()
        st.committed_version, st.updated_at = version, _now()
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            raise _bad({"code": LOCATION_DUPLICATE, "message": f"{name} is already listed under PIN code {pin}."},
                       status.HTTP_409_CONFLICT)
        from ..models import AuditLog

        db.add(AuditLog(
            user_id=admin_id, action="LOCATION_MASTER_ADD", entity="location_master_records",
            before_json=None,
            after_json={"record_no": next_no, "locality": name, "pincode": pin, "zone": zone,
                        "group_created": created, "version": version},
            created_at=_now(),
        ))
        _commit_and_publish(db, path, version)
        return {**_result(db, record), "created_group": created, "replayed": False}


def _result(db: Session, record: LocationMasterRecord) -> dict:
    group = db.query(AgentLocationGroup).filter(AgentLocationGroup.pincode == record.pincode).first()
    recs = (
        db.query(LocationMasterRecord)
        .filter(LocationMasterRecord.pincode == record.pincode)
        .order_by(LocationMasterRecord.record_no)
        .all()
    )
    return {
        "record": _record_dict(record),
        "group": _group_rows(recs, [group])[0] if group is not None else None,
    }


def _record_dict(r: LocationMasterRecord) -> dict:
    return {
        "record_id": r.record_no,
        "locality_name": r.display_name,
        "raw_name": r.raw_name,
        "pincode": r.pincode,
        "zone": r.zone,
        "source": r.source,
        "version": r.version,
        "created_at": r.created_at,
    }


# --------------------------------------------------------------- queries ---

def list_records(db: Session, *, q: str | None, zone: str | None, pincode: str | None,
                 page: int, page_size: int) -> dict:
    """Admin view of individual records (all committed versions)."""
    base = db.query(LocationMasterRecord)
    if zone:
        base = base.filter(LocationMasterRecord.zone == zone)
    if pincode:
        base = base.filter(LocationMasterRecord.pincode.like(f"{pincode.strip()}%"))
    if q and q.strip():
        term = f"%{q.strip()}%"
        base = base.filter(or_(LocationMasterRecord.display_name.like(term), LocationMasterRecord.pincode.like(term)))
    total = base.count()
    rows = base.order_by(LocationMasterRecord.record_no).offset((page - 1) * page_size).limit(page_size).all()
    pub = published_version(db)
    return {
        "items": [{**_record_dict(r), "published": r.version <= pub} for r in rows],
        "total": total, "page": page, "page_size": page_size, "published_version": pub,
    }


def list_groups(db: Session, *, q: str | None = None, zone: str | None = None) -> dict:
    """Agent location options: exactly one per PIN code, only the published
    dataset. Search matches PIN code, primary name and any locality."""
    version = published_version(db)
    recs, groups = _snapshot(db, version)
    rows = _group_rows(recs, groups)
    if zone:
        rows = [r for r in rows if r["zone"] == zone]
    if q and q.strip():
        term = q.strip().lower()
        key = name_key(term)
        rows = [
            r for r in rows
            if term in r["pincode"]
            or term in r["primary_name"].lower()
            or any(term in n.lower() for n in r["localities"])
            or (key and (key in name_key(r["primary_name"]) or any(key in name_key(n) for n in r["localities"])))
        ]
    for r in rows:
        r.pop("needs_review", None)
    return {"items": rows, "total": len(rows), "version": version, "zones": list(ZONES)}


def status_summary(db: Session) -> dict:
    st = _state(db)
    pending = db.query(func.count(LocationPublishLog.id)).filter(LocationPublishLog.status == "PENDING").scalar()
    out = {
        "committed_version": int(st.committed_version),
        "published_version": int(st.published_version),
        "blocked_reason": st.blocked_reason,
        "pending_publications": int(pending or 0),
        "records": int(db.query(func.count(LocationMasterRecord.id)).scalar() or 0),
        "groups": int(db.query(func.count(AgentLocationGroup.id)).scalar() or 0),
        "storage_problem": storage_problem(),
    }
    db.commit()
    return out


# ---------------------------------------------------- agent slot helpers ---

def groups_for_postcodes(db: Session, postcode_ids) -> dict[int, AgentLocationGroup]:
    ids = list(postcode_ids)
    if not ids:
        return {}
    return {int(g.postcode_id): g for g in db.query(AgentLocationGroup).filter(AgentLocationGroup.postcode_id.in_(ids))}


def resolve_group_ids(db: Session, group_ids) -> tuple[list[AgentLocationGroup], list[int]]:
    """(published groups for the ids, unknown ids). Unpublished groups are
    treated as unknown (agents never see them)."""
    ids = sorted({int(i) for i in group_ids or [] if str(i).strip().isdigit()})
    if not ids:
        return [], []
    version = published_version(db)
    found = (
        db.query(AgentLocationGroup)
        .filter(AgentLocationGroup.id.in_(ids), AgentLocationGroup.created_version <= version)
        .all()
    )
    have = {int(g.id) for g in found}
    return found, [i for i in ids if i not in have]
