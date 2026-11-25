"""Agent Location Master — source preservation, PIN grouping and the
workbook layer. No database is needed (runs with --noconftest too)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import location_master as lm


@pytest.fixture(scope="module")
def source():
    return lm.read_source_workbook()


def test_all_298_original_records_are_preserved(source):
    assert len(source) == 298
    assert [r.record_no for r in source] == list(range(1, 299))
    assert all(lm.PIN_RE.match(r.pincode) and isinstance(r.pincode, str) for r in source)


def test_zones_are_the_three_chennai_zones(source):
    counts = {z: sum(1 for r in source if r.zone == z) for z in lm.ZONES}
    assert counts == {"North Chennai": 108, "Central Chennai": 116, "South Chennai": 74}


def test_grouping_is_by_pin_code_only(source):
    groups, conflicts = lm.group_source(source)
    assert len(groups) == 112
    assert conflicts == {}
    # every original record is in exactly one group; totals match
    assert sum(len(v) for v in groups.values()) == len(source)
    seen = [r.record_no for v in groups.values() for r in v]
    assert sorted(seen) == sorted(r.record_no for r in source)
    for pin, recs in groups.items():
        assert {r.pincode for r in recs} == {pin}


def test_600053_includes_every_associated_locality(source):
    groups, _ = lm.group_source(source)
    names = [r.display for r in groups["600053"]]
    assert names == ["Ambattur Bazaar", "Ambattur H O", "Ambattur West", "Mednambedu", "Orgadam",
                     "Prithivipakkam", "Vijayalakshmipuram"]
    assert lm.display_name("Ambattur", names, "600053") == (
        "Ambattur (Ambattur Bazaar, Ambattur H O, Ambattur West, Mednambedu, Orgadam, "
        "Prithivipakkam, Vijayalakshmipuram) - 600053"
    )


def test_single_locality_format():
    assert lm.display_name("Kolathur", ["Kolathur"], "600099") == "Kolathur - 600099"


def test_reviewed_primary_names_cover_every_multi_locality_pin(source):
    groups, _ = lm.group_source(source)
    names = lm.read_primary_names()
    assert set(names) == set(groups)
    for pin, recs in groups.items():
        if len(recs) == 1:
            assert names[pin][0] == recs[0].display
    flagged = sorted(p for p, (_n, review) in names.items() if review)
    assert flagged == ["600001", "600013", "600057"]


def test_names_are_display_normalised_without_changing_raw():
    assert lm.normalize_name("  Ambattur   H O ") == "Ambattur H O"
    r = lm.SourceRecord(1, "  Ambattur   H O ", "600053", "North Chennai")
    assert r.raw_name == "  Ambattur   H O " and r.display == "Ambattur H O"


@pytest.mark.parametrize("pin", ["60005", "6000533", "60005a", "", " 600053x"])
def test_invalid_pin_codes_are_refused(pin):
    with pytest.raises(Exception) as e:
        lm.validate_input("Ambattur North", pin, "North Chennai")
    assert e.value.status_code == 422 and "pincode" in e.value.detail["errors"]


@pytest.mark.parametrize("zone", ["", "West Chennai", "north chennai", "Chennai"])
def test_invalid_zones_are_refused(zone):
    with pytest.raises(Exception) as e:
        lm.validate_input("Ambattur North", "600053", zone)
    assert e.value.status_code == 422 and "zone" in e.value.detail["errors"]


def test_valid_input_is_normalised():
    assert lm.validate_input("  Ambattur   North ", " 600053 ", "North Chennai") == ("Ambattur North", "600053", "North Chennai")


# ---------------------------------------------------------------- workbook ---

def _fake(source, extra=()):
    recs = [SimpleNamespace(record_no=r.record_no, raw_name=r.raw_name, display_name=r.display,
                            pincode=r.pincode, zone=r.zone) for r in source] + list(extra)
    groups_src, _ = lm.group_source(source)
    names = lm.read_primary_names()
    groups = [SimpleNamespace(id=i + 1, pincode=p, primary_name=names[p][0], zone=v[0].zone, needs_review=names[p][1])
              for i, (p, v) in enumerate(groups_src.items())]
    return recs, groups


def test_workbook_has_both_sheets_and_zone_sheets(source, tmp_path):
    from openpyxl import load_workbook

    recs, groups = _fake(source)
    tmp, sha = lm.write_temp(lm.build_workbook(recs, groups, 1), tmp_path / "m.xlsx")
    lm.swap_in(tmp, tmp_path / "m.xlsx", 1)
    wb = load_workbook(tmp_path / "m.xlsx")
    assert wb.sheetnames[:2] == [lm.SHEET_MASTER, lm.SHEET_GROUPS]
    assert set(lm.ZONES) <= set(wb.sheetnames)
    assert wb[lm.SHEET_META].sheet_state == "hidden"
    master = list(wb[lm.SHEET_MASTER].iter_rows(values_only=True))
    assert master[0] == ("Record ID", "Locality Name", "PIN Code", "Zone") and len(master) == 299
    sel = list(wb[lm.SHEET_GROUPS].iter_rows(values_only=True))
    assert sel[0] == ("Location ID", "Primary Location Name", "Associated Localities", "PIN Code", "Zone", "Display Name")
    assert len(sel) == 113
    zone_rows = sum(len(list(wb[z].iter_rows(min_row=2))) for z in lm.ZONES)
    assert zone_rows == 298
    pins = [row[3] for row in sel[1:]]
    assert len(pins) == len(set(pins)) == 112 and all(isinstance(p, str) for p in pins)
    assert (tmp_path / "backups" / "m_v000001.xlsx").exists()


def test_group_rows_cover_every_record_exactly_once(source):
    recs, groups = _fake(source)
    rows = lm._group_rows(recs, groups)
    assert sum(r["locality_count"] for r in rows) == 298
    assert len({r["pincode"] for r in rows}) == 112


def test_meta_roundtrip_and_digest_detects_changes(source, tmp_path):
    recs, groups = _fake(source)
    path = tmp_path / "m.xlsx"
    tmp, _ = lm.write_temp(lm.build_workbook(recs, groups, 7), path)
    lm.swap_in(tmp, path, 7)
    meta = lm.read_meta(path)
    assert meta == {"version": 7, "digest": lm.content_digest(recs, groups)}
    extra = SimpleNamespace(record_no=299, raw_name="Ambattur North", display_name="Ambattur North",
                            pincode="600053", zone="North Chennai")
    recs2, _ = _fake(source, [extra])
    assert lm.content_digest(recs2, groups) != meta["digest"]


def test_adding_to_existing_pin_updates_that_group_only(source):
    extra = SimpleNamespace(record_no=299, raw_name="Ambattur North", display_name="Ambattur North",
                            pincode="600053", zone="North Chennai")
    recs, groups = _fake(source, [extra])
    rows = {r["pincode"]: r for r in lm._group_rows(recs, groups)}
    assert len(rows) == 112
    assert rows["600053"]["localities"][-1] == "Ambattur North"
    assert rows["600053"]["display_name"].startswith("Ambattur (Ambattur Bazaar")


def test_unreadable_or_missing_workbook(tmp_path):
    assert lm.read_meta(tmp_path / "none.xlsx") is None
    bad = tmp_path / "bad.xlsx"
    bad.write_bytes(b"not a workbook")
    assert lm.read_meta(bad) == {"unreadable": True}


def test_failed_temp_write_leaves_live_file_untouched(tmp_path, source):
    path = tmp_path / "m.xlsx"
    path.write_bytes(b"LIVE")

    class Broken:
        def save(self, _p):
            raise OSError("disk full")

    with pytest.raises(OSError):
        lm.write_temp(Broken(), path)
    assert path.read_bytes() == b"LIVE"
    assert not list(tmp_path.glob(".lm-*"))


def test_storage_path_rules(monkeypatch, tmp_path):
    from app import config

    monkeypatch.setattr(config, "is_development", lambda: False)
    monkeypatch.delenv("RESTAMP_LOCATION_MASTER_PATH", raising=False)
    assert "not set" in lm.storage_problem()
    monkeypatch.setenv("RESTAMP_LOCATION_MASTER_PATH", "/tmp/x/master.xlsx")
    assert "temporary" in lm.storage_problem()
    monkeypatch.setenv("RESTAMP_LOCATION_MASTER_PATH", str(lm.REPO_ROOT / "var" / "m.xlsx"))
    assert "repository" in lm.storage_problem()
    monkeypatch.setenv("RESTAMP_LOCATION_MASTER_PATH", "/var/lib/restamp/agent-locations/master.csv")
    assert ".xlsx" in lm.storage_problem()
    monkeypatch.setenv("RESTAMP_LOCATION_MASTER_PATH", "/var/lib/restamp/agent-locations/master.xlsx")
    assert lm.storage_problem() is None
