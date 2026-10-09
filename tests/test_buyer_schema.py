"""Buyer V1 schema regression (Phase 6A): 9 columns + saved_properties + price index.

Frozen source: report/RESTAMP_BUYER_SCHEMA_DECISION_FINAL.md. Metadata-level only;
no DB writes. Also asserts the 19 frozen rejections stay absent.
"""
import ast

from sqlalchemy import Enum, UniqueConstraint

from app.models import Base

MD = Base.metadata


def test_table_count_is_29():
    # Drafts phase: 32 + listing_drafts.
    assert len(MD.tables) == 33
    assert "saved_properties" in MD.tables
    assert "rent_terms" in MD.tables
    assert "amenity_master" in MD.tables
    assert "listing_amenities" in MD.tables


def test_price_paise():
    c = MD.tables["property_listings"].columns["price_paise"]
    assert type(c.type).__name__ == "BigInteger" and c.nullable is False


def test_price_period_values():
    c = MD.tables["property_listings"].columns["price_period"]
    assert isinstance(c.type, Enum) and list(c.type.enums) == ["TOTAL", "MONTHLY"]
    assert c.nullable is False and c.server_default.arg == "TOTAL"


def test_description_nullable():
    c = MD.tables["property_listings"].columns["description"]
    assert type(c.type).__name__ == "Text" and c.nullable is True


def test_construction_status_enum():
    c = MD.tables["property_listings"].columns["construction_status"]
    assert isinstance(c.type, Enum)
    assert list(c.type.enums) == ["READY_TO_MOVE", "UNDER_CONSTRUCTION", "NEW_LAUNCH"]
    assert c.nullable is True and c.server_default is None


def test_physical_specs():
    t = MD.tables["physical_properties"].columns
    assert t["address_line"].type.length == 500 and t["address_line"].nullable is True
    assert repr(t["area_value"].type) == "INTEGER(unsigned=True)" and t["area_value"].nullable is True
    assert t["area_unit"].type.length == 20 and t["area_unit"].nullable is True
    assert repr(t["bedrooms"].type) == "TINYINT(unsigned=True)" and t["bedrooms"].nullable is True
    assert repr(t["bathrooms"].type) == "TINYINT(unsigned=True)" and t["bathrooms"].nullable is True
    assert "bhk" not in MD.tables["physical_properties"].columns
    assert "sqft" not in MD.tables["physical_properties"].columns


def test_saved_properties_shape():
    t = MD.tables["saved_properties"]
    assert [c.name for c in t.columns] == ["buyer_user_id", "property_listing_id", "saved_at"]
    assert [c.name for c in t.primary_key.columns] == ["buyer_user_id", "property_listing_id"]
    fks = {(fk.parent.name, fk.column.table.name, fk.ondelete, fk.onupdate) for c in t.columns for fk in c.foreign_keys}
    assert ("buyer_user_id", "users", "CASCADE", "CASCADE") in fks
    assert ("property_listing_id", "property_listings", "CASCADE", "CASCADE") in fks
    assert "TIMESTAMP" in repr(t.columns["saved_at"].type)
    assert t.columns["saved_at"].nullable is False
    for forbidden in ("notes", "folders", "categories", "metadata"):
        assert forbidden not in t.columns


def test_price_index_and_fk_counts():
    idx = [i.name for i in MD.tables["property_listings"].indexes]
    assert "idx_property_listings_price_paise" in idx
    total_fk = sum(len(list(c.foreign_keys)) for t in MD.tables.values() for c in t.columns)
    assert total_fk == 49  # 48 existing + listing_drafts user FK


def test_forbidden_schema_absent():
    all_cols = {f"{t}.{c.name}" for t, tbl in MD.tables.items() for c in tbl.columns}
    joined = "\n".join(all_cols)
    for bad in ("rating", "raw_price", "property_age_years", "bhk", "sqft"):
        assert bad not in joined, bad
    # "phone": per-listing contact override contact_phone/contact_email is
    # approved RENT design (Task 7); only a bare `phone` column stays forbidden.
    col_names = {c.name for tbl in MD.tables.values() for c in tbl.columns}
    assert "phone" not in col_names
    assert "saved_properties" in MD.tables and len(MD.tables) == 33
    for tbl in MD.tables.values():
        for c in tbl.columns:
            assert type(c.type).__name__ != "Numeric"  # no DECIMAL money
    enums = {c.name: list(c.type.enums) for tbl in MD.tables.values() for c in tbl.columns if isinstance(c.type, Enum)}
    assert enums["construction_status"] == ["READY_TO_MOVE", "UNDER_CONSTRUCTION", "NEW_LAUNCH"]
    assert enums["price_period"] == ["TOTAL", "MONTHLY"]
    for vals in enums.values():
        assert not (set(vals) & {"PAID", "NEGOTIABLE", "RENOVATED", "REPAIR_NEEDED", "NEW"})


def test_migration_chain_and_ops():
    src = open("alembic/versions/d4e5f6a7b8c9_buyer_v1_schema.py").read()
    tree = ast.parse(src)
    assert "1a2b3c4d5e6f" in src  # down_revision
    ops = [ast.unparse(n) for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert sum("add_column" in o for o in ops) == 9
    assert sum("create_table" in o for o in ops) == 1
    assert sum("drop_column" in o for o in ops) == 9


class _FakeScalar:
    def __init__(self, n):
        self._n = n

    def scalar(self):
        return self._n


class _FakeBind:
    """Stand-in for an online Alembic bind; records the checked SQL. No DB."""

    def __init__(self, n):
        self._n = n
        self.seen = []

    def exec_driver_sql(self, sql):
        self.seen.append(sql)
        return _FakeScalar(self._n)


def _migration_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "mig_buyer_v1", "alembic/versions/d4e5f6a7b8c9_buyer_v1_schema.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_f1_guard_allows_empty_table():
    mig = _migration_module()
    assert mig._assert_empty_property_listings(_FakeBind(0)) == 0


def test_f1_guard_blocks_populated_table_clearly():
    import pytest

    mig = _migration_module()
    with pytest.raises(mig.NotEmptyForNotNull) as e:
        mig._assert_empty_property_listings(_FakeBind(7))
    msg = str(e.value).lower()
    assert "price_paise" in msg and "backfill" in msg and "no schema change" in msg


def test_f1_guard_runs_before_any_schema_op():
    tree = ast.parse(open("alembic/versions/d4e5f6a7b8c9_buyer_v1_schema.py").read())
    up = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade")
    schema_ops = {"add_column", "create_table", "create_index", "execute"}
    first_op = next(
        i
        for i, s in enumerate(up.body)
        if isinstance(s, ast.Expr)
        and isinstance(s.value, ast.Call)
        and getattr(s.value.func, "attr", None) in schema_ops
    )
    pre = ast.dump(ast.Module(body=list(up.body[:first_op]), type_ignores=[]))
    assert "_guard_bind" in pre and "_assert_empty_property_listings" in pre


def test_f1_final_price_paise_still_not_null():
    c = MD.tables["property_listings"].columns["price_paise"]
    assert type(c.type).__name__ == "BigInteger" and c.nullable is False
