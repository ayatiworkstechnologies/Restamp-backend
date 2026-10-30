"""Migration j0k1l2m3n4o5 (CONTRACT §1): additive, single head, schema = model.

Read-only checks against the migrated test database (information_schema)
plus the Alembic script graph. No DDL is executed here.
"""
import os

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from app.models import Base
from tests.conftest import _expected_heads


def _script():
    here = os.path.dirname(os.path.abspath(__file__))
    return ScriptDirectory.from_config(Config(os.path.join(here, "..", "alembic.ini")))


def test_graph_single_head_and_parent():
    script = _script()
    assert len(_expected_heads()) == 1
    rev = script.get_revision("j0k1l2m3n4o5")
    assert rev.down_revision == "i9j0k1l2m3n4"
    chain = [r.revision for r in script.walk_revisions()]
    assert chain.index("j0k1l2m3n4o5") < chain.index("i9j0k1l2m3n4") < chain.index("h8i9j0k1l2m3")


def _columns(session, table):
    return {
        r[0]: (r[1], r[2])
        for r in session.execute(text(
            "SELECT COLUMN_NAME, IS_NULLABLE, DATA_TYPE FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"), {"t": table}).fetchall()
    }


def _fks(session, table):
    return {
        (r[0], r[1], r[2])
        for r in session.execute(text(
            "SELECT kcu.COLUMN_NAME, kcu.REFERENCED_TABLE_NAME, rc.DELETE_RULE "
            "FROM information_schema.KEY_COLUMN_USAGE kcu "
            "JOIN information_schema.REFERENTIAL_CONSTRAINTS rc "
            "  ON rc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME AND rc.CONSTRAINT_SCHEMA = kcu.CONSTRAINT_SCHEMA "
            "WHERE kcu.TABLE_SCHEMA = DATABASE() AND kcu.TABLE_NAME = :t"), {"t": table}).fetchall()
    }


def _indexes(session, table):
    rows = session.execute(text(
        "SELECT INDEX_NAME, COLUMN_NAME FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t ORDER BY INDEX_NAME, SEQ_IN_INDEX"), {"t": table}).fetchall()
    out = {}
    for name, col in rows:
        out.setdefault(name, []).append(col)
    return out


def test_new_tables_match_model(client):
    _, session = client
    for table, owner_col in (("enquiry_messages", "sender_user_id"), ("lead_notes", "owner_user_id")):
        cols = _columns(session, table)
        assert set(cols) == {"id", "enquiry_id", owner_col, "body", "created_at"}, table
        assert set(cols) == set(Base.metadata.tables[table].columns.keys()), table
        assert all(cols[c][0] == "NO" for c in cols), table  # all NOT NULL; created_at app-set
        assert _fks(session, table) == {("enquiry_id", "enquiries", "CASCADE"), (owner_col, "users", "CASCADE")}
        idx_name = "idx_enquiry_messages_enquiry" if table == "enquiry_messages" else "idx_lead_notes_enquiry"
        assert _indexes(session, table)[idx_name] == ["enquiry_id", "id"]


def test_visit_rescheduled_at_and_activity_index(client):
    _, session = client
    cols = _columns(session, "property_visits")
    assert cols["rescheduled_at"] == ("YES", "datetime")
    assert "rescheduled_at" in Base.metadata.tables["property_visits"].columns
    assert _indexes(session, "activity_logs")["ix_activity_logs_action_entity"] == ["action", "entity_type", "entity_id"]
    # Existing rows were untouched: no visit is "rescheduled" without a real reschedule.
    assert session.execute(text(
        "SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
        "AND TABLE_NAME = 'property_visits' AND COLUMN_NAME = 'rescheduled_at' AND COLUMN_DEFAULT IS NOT NULL"
    )).scalar() == 0
