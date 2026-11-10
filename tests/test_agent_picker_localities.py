"""The app's agent picker localities in the backend catalog.

scripts/agent_picker_localities.txt is the frontend-derived list (verbatim
from the app's agent picker). localities.rows_for_names loads exactly those
names from a verified source file: picker spelling stays canonical, only
same-spelling-key office names match (no substrings), missing names are
reported, existing locality->pincode mappings are never overwritten, and a
re-run changes nothing. Loading catalog data never touches plans, payments
or expiry.

FIXTURE DATA: the India Post-format rows below use free test pincodes, NOT
the real pincodes of these places. Rollback-isolated.
"""
import os
import re

import pytest
from sqlalchemy import text

from app.services import agent_plans as plan_svc
from app.services import localities as loc_svc
from tests.conftest import auth_header, register_phone

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(HERE, "..", "scripts", "agent_picker_localities.txt")
FRONTEND = os.path.join(HERE, "..", "..", "Restamp-latest", "src", "features", "agent", "screens",
                        "AgentLocationSelectScreen.js")
HEADER = "officename,pincode,officetype,districtname,statename\n"
_n = {"i": 0}


# TEMPORARILY DISABLED — Chennai area seeding paused. The implementation is
# kept and tested here with the flag switched on for these tests only.
@pytest.fixture(autouse=True)
def _area_seeding_on_for_these_tests(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "AREA_SEEDING_ENABLED", True)


def _picker_names():
    with open(MANIFEST, encoding="utf-8") as fh:
        return [ln.strip() for ln in fh if ln.strip() and not ln.lstrip().startswith("#")]


def _free_pins(session, n):
    used = {r[0] for r in session.execute(text("SELECT pincode FROM postcodes"))}
    free = [str(p) for p in range(600001, 600119) if str(p) not in used]
    assert len(free) >= n, "restamp_test has too few free Chennai pincodes"
    return free[:n]


def _office(name):
    """India Post office spelling for a picker name ("T. Nagar" -> "T.Nagar S.O")."""
    return f"{name.replace('. ', '.')} S.O"


def _fixture(session, names, extra=""):
    pins = _free_pins(session, len(names) + 2)
    lines = [f"{_office(n)},{p},SO,CHENNAI,TAMIL NADU" for n, p in zip(names, pins)]
    return HEADER + "\n".join(lines) + "\n" + extra, dict(zip(names, pins)), pins[len(names):]


def _key_count(session, name):
    return session.execute(
        text("SELECT COUNT(*) FROM localities WHERE REGEXP_REPLACE(LOWER(name), '[^a-z0-9]', '') = :k"),
        {"k": loc_svc.name_key(name)},
    ).scalar()


def test_manifest_is_the_frontend_picker_list_verbatim():
    names = _picker_names()
    assert len(names) == 25 and len(set(names)) == 25
    assert {"Kodambakkam", "Perambur", "Adyar", "Anna Nagar", "T. Nagar"} <= set(names)
    if not os.path.exists(FRONTEND):
        pytest.skip("mobile repo not checked out next to the backend")
    with open(FRONTEND, encoding="utf-8") as fh:
        block = re.search(r"const ALL_LOCALITY_NAMES = \[(.*?)\];", fh.read(), re.S).group(1)
    assert re.findall(r'"([^"]+)"', block) == names


def test_every_picker_name_is_loaded_and_resolves(client):
    _, session = client
    names = _picker_names()
    before = {n: _key_count(session, n) for n in names}
    csv_text, pins, _ = _fixture(session, names)
    rows, missing = loc_svc.rows_for_names(csv_text, names)
    assert missing == [] and sorted(r.locality for r in rows) == sorted(names)
    report = loc_svc.load_master(session, rows)
    assert not report["conflicts"]
    for n in names:
        assert _key_count(session, n) == max(before[n], 1), n  # existing rows reused, never duplicated
    resolved = loc_svc.resolve(session, names)
    assert [r.status for r in resolved] == ["ok"] * 25
    for r in resolved:
        bound = session.execute(
            text("SELECT l.name FROM postcodes p JOIN localities l ON l.id=p.locality_id WHERE p.pincode=:p"),
            {"p": pins[r.requested]},
        ).scalar()
        assert loc_svc.name_key(bound) == r.key, r.requested
    # Newly created records keep the exact picker spelling.
    created = set(report["localities_created"])
    assert created <= set(names)


def test_only_exact_names_match_and_missing_names_are_reported(client):
    _, session = client
    names = _picker_names()
    present = [n for n in names if n not in ("ECR", "OMR", "Mogappair")]
    pins = _free_pins(session, len(present) + 2)
    p_east, p_cbe = pins[-2:]
    csv_text = HEADER + "".join(f"{_office(n)},{p},SO,CHENNAI,TAMIL NADU\n" for n, p in zip(present, pins)) + (
        f"Mogappair East S.O,{p_east},SO,CHENNAI,TAMIL NADU\n"
        f"Kilpauk S.O,{p_cbe},SO,COIMBATORE,TAMIL NADU\n"
    )
    rows, missing = loc_svc.rows_for_names(csv_text, names)
    assert missing == ["Mogappair", "ECR", "OMR"]  # no substring match, nothing guessed
    assert p_east not in {r.pincode for r in rows}  # "Mogappair East" is not "Mogappair"
    assert p_cbe not in {r.pincode for r in rows}   # other districts ignored
    assert all(r.locality in names for r in rows)


def test_two_picker_names_on_one_pincode_become_canonical_and_alias(client):
    _, session = client
    (pin,) = _free_pins(session, 1)
    csv_text = HEADER + f"Kotturpuram S.O,{pin},SO,CHENNAI,TAMIL NADU\nAdyar B.O,{pin},BO,CHENNAI,TAMIL NADU\n"
    rows, missing = loc_svc.rows_for_names(csv_text, ["Adyar", "Kotturpuram"])
    assert rows == [loc_svc.MasterRow("Adyar", pin, ("Kotturpuram",))] and missing == []


def test_reload_is_idempotent_and_existing_mappings_are_never_overwritten(client):
    _, session = client
    names = _picker_names()
    csv_text, pins, _ = _fixture(session, names)
    rows, _ = loc_svc.rows_for_names(csv_text, names)
    loc_svc.load_master(session, rows)
    tables = ("localities", "postcodes", "locality_aliases")
    counts = {t: session.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() for t in tables}
    mapping = dict(session.execute(text("SELECT pincode, locality_id FROM postcodes")).fetchall())
    again = loc_svc.load_master(session, rows)
    assert again["localities_created"] == again["postcodes_created"] == again["aliases_created"] == []
    assert {t: session.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() for t in tables} == counts
    # A source claiming an already-mapped pincode for another picker name is
    # reported, and the existing mapping stays as it was.
    clash = HEADER + f"Perambur S.O,{pins['Kilpauk']},SO,CHENNAI,TAMIL NADU\n"
    report = loc_svc.load_master(session, loc_svc.rows_for_names(clash, names)[0])
    assert report["conflicts"] == [{"pincode": pins["Kilpauk"], "requested": "Perambur", "existing_locality": "Kilpauk"}]
    assert dict(session.execute(text("SELECT pincode, locality_id FROM postcodes")).fetchall()) == mapping


def test_agent_rules_unchanged_after_loading_the_picker_catalog(client):
    c, session = client
    names = _picker_names()
    _n["i"] += 1
    tok = register_phone(c, f"+916773{_n['i']:06d}")
    h = auth_header(tok["access_token"])
    uid = tok["user_id"]
    csv_text, _, _ = _fixture(session, names[:10])
    loc_svc.load_master(session, loc_svc.rows_for_names(csv_text, names)[0])
    assert c.post("/broker/plans/agent-starter/subscribe", json={"localities": names[:3]}, headers=h).status_code == 200
    payments = session.execute(text("SELECT COUNT(*) FROM payments WHERE user_id=:u"), {"u": uid}).scalar()
    end = session.execute(text("SELECT end_date FROM subscriptions WHERE user_id=:u AND status='ACTIVE'"),
                          {"u": uid}).scalar()

    # Loading more catalog data touches no plan, payment or expiry.
    more, _, _ = _fixture(session, names[10:20])
    loc_svc.load_master(session, loc_svc.rows_for_names(more, names)[0])
    assert c.get("/broker/plan", headers=h).json()["localities_remaining"] == 2

    # Unknown names: still 422 / listed as unmatched.
    r = c.post("/broker/plan/localities", json={"localities": ["Atlantis Nagar"]}, headers=h)
    assert r.status_code == 422 and r.json()["detail"] == plan_svc.NOT_SERVED
    # Picker names (any spelling) add within the allowance; the 6th is refused.
    r = c.post("/broker/plan/localities", json={"localities": ["t nagar", names[4], "Atlantis Nagar"]}, headers=h)
    assert r.status_code == 200 and r.json()["unmatched_localities"] == ["Atlantis Nagar"]
    assert len(r.json()["added_localities"]) == 2 and r.json()["localities_remaining"] == 0
    r = c.post("/broker/plan/localities", json={"localities": [names[12]]}, headers=h)
    assert r.status_code == 422 and r.json()["detail"] == "Agent Starter Plan covers up to 5 localities."
    assert session.execute(text("SELECT COUNT(*) FROM payments WHERE user_id=:u"), {"u": uid}).scalar() == payments
    assert session.execute(text("SELECT end_date FROM subscriptions WHERE user_id=:u AND status='ACTIVE'"),
                           {"u": uid}).scalar() == end


# --- reviewed mapping artifact + review_status gate --------------------------------

REVIEWED = os.path.join(HERE, "..", "scripts", "chennai_picker_localities_reviewed.csv")


def test_review_status_gates_curated_rows():
    text_ = ("locality,pincode,aliases,review_status\n"
             "Adyar,600020,,VERIFIED\n"
             "Kilpauk,600010,,verified\n"
             "Tambaram,600045,,NEEDS_REVIEW\n"
             "Alwarpet,,,NEEDS_REVIEW\n")
    assert loc_svc.parse_master_csv(text_) == [loc_svc.MasterRow("Adyar", "600020"), loc_svc.MasterRow("Kilpauk", "600010")]
    assert [r["locality"] for r in loc_svc.pending_review(text_)] == ["Tambaram", "Alwarpet"]
    rows, missing = loc_svc.rows_for_names(text_, ["Adyar", "Tambaram", "Alwarpet"])
    assert rows == [loc_svc.MasterRow("Adyar", "600020")] and missing == ["Tambaram", "Alwarpet"]
    # Without the column the curated format behaves exactly as before.
    assert loc_svc.parse_master_csv("locality,pincode\nTambaram,600045\n") == [loc_svc.MasterRow("Tambaram", "600045")]


@pytest.mark.skip(reason="TEMPORARILY DISABLED — Chennai area seeding paused: the 600119 approval is commented out in app/schemas.py")
def test_reviewed_artifact_covers_all_picker_names_and_loads_only_verified_rows():
    import csv as _csv

    with open(REVIEWED, encoding="utf-8", newline="") as fh:
        text_ = fh.read()
    recs = list(_csv.DictReader(text_.splitlines()))
    assert list(recs[0]) == ["locality", "pincode", "aliases", "source", "source_date", "review_status", "notes"]
    assert [r["locality"] for r in recs] == _picker_names()  # exact spelling and order
    assert {r["review_status"] for r in recs} <= {"VERIFIED", "NEEDS_REVIEW"}
    verified = [r for r in recs if r["review_status"] == "VERIFIED"]
    from app.schemas import is_chennai_service_pincode

    for r in verified:
        assert is_chennai_service_pincode(r["pincode"]), r  # same policy as owners and the loader
        assert r["source"] and r["source_date"] and r["notes"], r
    assert len({r["pincode"] for r in verified}) == len(verified)  # a pincode belongs to one locality
    for r in recs:
        if r["review_status"] == "NEEDS_REVIEW":
            assert r["notes"], r  # every open question is documented
    loadable = loc_svc.parse_master_csv(text_)
    assert [m.locality for m in loadable] == [r["locality"] for r in verified]
    assert {p["locality"] for p in loc_svc.pending_review(text_)} == {"Alwarpet", "ECR", "OMR"}
    by = {r["locality"]: r for r in recs}
    # Approved decisions.
    assert (by["Guindy"]["pincode"], by["Tambaram"]["pincode"], by["Medavakkam"]["pincode"],
            by["Sholinganallur"]["pincode"]) == ("600032", "600045", "600100", "600119")
    assert "600015" not in {r["pincode"] for r in recs}  # Saidapet's pincode is not merged into Guindy
    assert "600125" not in {r["pincode"] for r in recs}
    for name, alias in (("Velachery", "Velacheri"), ("T. Nagar", "Thygarayanagar"),
                        ("Thiruvanmiyur", "Tiruvanmiyur"), ("Nungambakkam", "Nungambakkam High Road")):
        assert alias in by[name]["aliases"].split("|"), name
    for name in ("Alwarpet", "ECR", "OMR"):
        assert by[name]["pincode"] == "" and by[name]["review_status"] == "NEEDS_REVIEW"


def test_curated_aliases_survive_the_names_filter():
    text_ = ("locality,pincode,aliases,review_status\n"
             "Velachery,600042,Velacheri,VERIFIED\n"
             "T. Nagar,600017,Thygarayanagar,VERIFIED\n"
             "Sembiam,600011,Perambur,VERIFIED\n")
    rows, missing = loc_svc.rows_for_names(text_, ["Velachery", "T. Nagar", "Perambur"])
    assert rows == [
        loc_svc.MasterRow("Perambur", "600011", ("Sembiam",)),
        loc_svc.MasterRow("T. Nagar", "600017", ("Thygarayanagar",)),
        loc_svc.MasterRow("Velachery", "600042", ("Velacheri",)),
    ] and missing == []


@pytest.mark.skip(reason="TEMPORARILY DISABLED — Chennai area seeding paused: the 600119 approval is commented out in app/schemas.py")
def test_reviewed_catalog_gives_agents_the_approved_localities_only(client):
    c, session = client
    with open(REVIEWED, encoding="utf-8", newline="") as fh:
        text_ = fh.read()
    rows = loc_svc.parse_master_csv(text_)
    report = loc_svc.load_master(session, rows)
    blocked = {x["requested"] for x in report["conflicts"]}  # pincodes already owned in this shared test DB
    resolved = {r.requested: r for r in loc_svc.resolve(session, [m.locality for m in rows])}
    for m in rows:
        assert m.locality in blocked or resolved[m.locality].status == "ok", m
    for name in ("Alwarpet", "ECR", "OMR"):
        assert loc_svc.resolve(session, [name])[0].status == "unknown"
    # Aliases resolve to the canonical picker name.
    for alias, name in (("Velacheri", "Velachery"), ("Tiruvanmiyur", "Thiruvanmiyur")):
        if name not in blocked:
            assert loc_svc.resolve(session, [alias])[0].name == name
    wanted = [n for n in ("Sholinganallur", "Tambaram", "Medavakkam") if n not in blocked]
    if not wanted:
        pytest.skip("approved pincodes already owned by other localities in this test DB")
    _n["i"] += 1
    tok = register_phone(c, f"+916773{_n['i']:06d}")
    h = auth_header(tok["access_token"])
    r = c.post("/broker/plans/agent-starter/subscribe", json={"localities": wanted + ["ECR"]}, headers=h)
    assert r.status_code == 200, r.text
    assert sorted(r.json()["localities"]) == sorted(wanted) and r.json()["unmatched_localities"] == ["ECR"]
