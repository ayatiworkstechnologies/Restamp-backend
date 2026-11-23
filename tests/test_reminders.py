"""Smart reminders (notification Phase 4): visit reminders (T-24h / T-1h),
lead reminders (2h/24h/72h), dedupe, fan-out to email + push.

The scans are driven explicitly with reminders.run_* / reminders.run_scan on
the test session (the background loop is disabled in conftest). Everything is
in-memory: no email or push is really sent.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.models import EmailOutbox, PropertyVisit, PushOutbox
from app.services import email_providers, push_providers
from app.services import notifications as notif_svc
from app.services import reminders as rem_svc
from tests.test_notifications_events import _pair, _rows, _enquiry_id
from tests.test_push import _register, _user
from tests.test_push_delivery import _Shared


@pytest.fixture()
def pushprov():
    provider = push_providers.MemoryProvider()
    push_providers.set_provider_for_tests(provider)
    yield provider
    push_providers.set_provider_for_tests(None)


@pytest.fixture()
def mailprov():
    provider = email_providers.MemoryProvider()
    email_providers.set_provider_for_tests(provider)
    yield provider
    email_providers.set_provider_for_tests(None)


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def _confirmed_visit(c, session, offset):
    """A CONFIRMED visit `offset` (timedelta) in the future. Returns
    (owner_id, buyer_id, visit, buyer_auth, owner_auth)."""
    owner_id, oh, buyer_id, bh, lid = _pair(c, "Reminder Scan Flat")
    visit = PropertyVisit(property_listing_id=lid, buyer_user_id=buyer_id, owner_user_id=owner_id,
                          scheduled_at=_utcnow() + offset, status="CONFIRMED", initiator="BUYER")
    session.add(visit)
    session.flush()
    return owner_id, buyer_id, visit, bh, oh


def _new_enquiry(c, session):
    """A fresh NEW enquiry (no owner reply). Returns (owner_id, buyer_id, eid, owner_auth)."""
    owner_id, oh, buyer_id, bh, lid = _pair(c, "Lead Scan Flat")
    assert c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": "hi"}, headers=bh).status_code == 201
    eid = _enquiry_id(session, buyer_id, lid)
    return owner_id, buyer_id, eid, oh


def _age_enquiry(session, eid, hours):
    session.execute(text("UPDATE enquiries SET created_at = created_at - INTERVAL :m MINUTE WHERE id=:e"),
                    {"m": int(hours * 60), "e": eid})
    session.flush()


def _scan(session):
    return rem_svc.run_scan(session_factory=lambda: _Shared(session))


# --- visit reminders -------------------------------------------------------------------

def test_24h_window_queues_for_buyer_and_owner(client, pushprov):
    c, session = client
    owner_id, buyer_id, visit, bh, oh = _confirmed_visit(c, session, timedelta(hours=23))
def test_24h_window_queues_for_buyer_and_owner(client, pushprov):
    c, session = client
    owner_id, buyer_id, visit, bh, oh = _confirmed_visit(c, session, timedelta(hours=23))
    _scan(session)
    # Per-user assertions: the shared dev database holds stale committed
    # entities from earlier runs, which the scan legitimately also covers.
    assert len(_rows(session, buyer_id, "VISIT_REMINDER")) == 1
    assert len(_rows(session, owner_id, "VISIT_REMINDER")) == 1
    owner_row = _rows(session, owner_id, "VISIT_REMINDER")[0]
    assert owner_row["audience"] == "OWNER" and owner_row["category"] == "VISITS"
    assert owner_row["dedupe_key"].startswith(f"visit:{visit.id}:reminder:24h:")
    buyer_row = _rows(session, buyer_id, "VISIT_REMINDER")[0]
    assert buyer_row["audience"] == "BUYER" and "scheduled for" in buyer_row["body"]


def test_1h_window_queues_only_the_1h_reminder(client, pushprov):
    c, session = client
    owner_id, buyer_id, visit, bh, oh = _confirmed_visit(c, session, timedelta(minutes=30))
    _scan(session)
    keys = [r["dedupe_key"] for r in _rows(session, buyer_id, "VISIT_REMINDER")]
    assert len(keys) == 1 and ":reminder:1h:" in keys[0] and ":reminder:24h:" not in keys[0]


def test_mid_window_queues_the_24h_reminder_only(client, pushprov):
    c, session = client
    owner_id, buyer_id, visit, bh, oh = _confirmed_visit(c, session, timedelta(hours=2))
    _scan(session)
    keys = [r["dedupe_key"] for r in _rows(session, buyer_id, "VISIT_REMINDER")]
    assert len(keys) == 1 and ":reminder:24h:" in keys[0]


def test_visit_outside_the_windows_queues_nothing(client, pushprov):
    c, session = client
    owner_id, buyer_id, visit, bh, oh = _confirmed_visit(c, session, timedelta(hours=25))
    _scan(session)
    assert _rows(session, buyer_id) == []
    owner_id, buyer_id, visit, bh, oh = _confirmed_visit(c, session, timedelta(hours=-1))  # already past
    _scan(session)
    assert _rows(session, buyer_id) == []


def test_non_confirmed_visits_are_never_reminded(client, pushprov):
    c, session = client
    owner_id, oh, buyer_id, bh, lid = _pair(c, "Requested Flat")
    visit = PropertyVisit(property_listing_id=lid, buyer_user_id=buyer_id, owner_user_id=owner_id,
                          scheduled_at=_utcnow() + timedelta(hours=2), status="REQUESTED", initiator="BUYER")
    session.add(visit)
    session.flush()
    _scan(session)
    assert _rows(session, buyer_id) == []


def test_scan_is_idempotent_per_interval_and_time(client, pushprov):
    c, session = client
    owner_id, buyer_id, visit, bh, oh = _confirmed_visit(c, session, timedelta(hours=23))
    _scan(session)
    assert _scan(session)["visit_reminders"] == 0  # second pass: every due key was already queued
    assert len(_rows(session, buyer_id, "VISIT_REMINDER")) == 1


def test_rescheduled_visit_gets_a_fresh_reminder_for_the_new_time(client, pushprov):
    c, session = client
    owner_id, buyer_id, visit, bh, oh = _confirmed_visit(c, session, timedelta(hours=23))
    _scan(session)
    session.execute(text("UPDATE property_visits SET scheduled_at = scheduled_at + INTERVAL 1 HOUR WHERE id=:v"),
                    {"v": visit.id})
    session.flush()
    session.expire_all()
    visit = session.get(PropertyVisit, visit.id)
    _scan(session)
    buyer_keys = [r["dedupe_key"] for r in _rows(session, buyer_id, "VISIT_REMINDER")]
    assert len(buyer_keys) == 2 and len(set(buyer_keys)) == 2  # two different times (label is the same)
    owner_keys = [r["dedupe_key"] for r in _rows(session, owner_id, "VISIT_REMINDER")]
    assert len(owner_keys) == 2


def test_reminders_fan_out_to_email_and_push(client, pushprov, mailprov):
    c, session = client
    owner_id, buyer_id, visit, bh, oh = _confirmed_visit(c, session, timedelta(hours=23))
    _register(c, bh)
    _scan(session)
    # In-app rows for both parties + push twin for the buyer (the only
    # registered device) + email twin for nobody (no address set).
    assert len(session.query(PushOutbox).filter_by(user_id=buyer_id, template="VISIT_REMINDER").all()) == 1
    assert session.query(EmailOutbox).filter_by(user_id=buyer_id).all() == []
    assert len(_rows(session, buyer_id, "VISIT_REMINDER")) == 1
    assert len(_rows(session, owner_id, "VISIT_REMINDER")) == 1


# --- lead reminders ----------------------------------------------------------------------

def test_lead_reminder_fires_at_the_2h_offset_only(client, pushprov):
    c, session = client
    owner_id, buyer_id, eid, oh = _new_enquiry(c, session)
    _age_enquiry(session, eid, 3)
    _scan(session)
    row = _rows(session, owner_id, "LEAD_REMINDER")[0]
    assert row["audience"] == "OWNER" and row["dedupe_key"] == f"enquiry:{eid}:lead_reminder:2"
    assert "reply" in row["body"].lower()


def test_lead_reminders_fire_one_per_offset(client, pushprov):
    c, session = client
    owner_id, buyer_id, eid, oh = _new_enquiry(c, session)
    _age_enquiry(session, eid, 25)
    _scan(session)
    assert len(_rows(session, owner_id, "LEAD_REMINDER")) == 2  # 2h + 24h both due now, queued once each
    _age_enquiry(session, eid, 50)
    _scan(session)
    keys = [r["dedupe_key"] for r in _rows(session, owner_id, "LEAD_REMINDER")]
    assert keys == [f"enquiry:{eid}:lead_reminder:2", f"enquiry:{eid}:lead_reminder:24", f"enquiry:{eid}:lead_reminder:72"]


def test_lead_reminders_stop_after_max_age(client, pushprov):
    c, session = client
    owner_id, buyer_id, eid, oh = _new_enquiry(c, session)
    _age_enquiry(session, eid, 80)  # past 72h + 6h grace: nothing, ever
    _scan(session)
    assert _rows(session, owner_id, "LEAD_REMINDER") == []


def test_replied_or_contacted_leads_are_not_reminded(client, pushprov):
    c, session = client
    owner_id, buyer_id, eid, oh = _new_enquiry(c, session)
    _age_enquiry(session, eid, 3)
    # Owner replied in the meantime: the scan adds nothing.
    assert c.post(f"/owner/leads/{eid}/messages", json={"message": "on my way"}, headers=oh).status_code == 201
    _scan(session)
    assert _rows(session, owner_id, "LEAD_REMINDER") == []
    # A contacted enquiry is out of scope too.
    owner_id2, buyer_id2, eid2, oh2 = _new_enquiry(c, session)
    _age_enquiry(session, eid2, 3)
    assert c.patch(f"/owner/leads/{eid2}", json={"status": "CONTACTED"}, headers=oh2).status_code == 200
    _scan(session)
    assert _rows(session, owner_id, "LEAD_REMINDER") == [] and _rows(session, owner_id2, "LEAD_REMINDER") == []


def test_lead_reminders_fan_out_to_email_and_push(client, pushprov, mailprov):
    c, session = client
    owner_id, buyer_id, eid, oh = _new_enquiry(c, session)
    _register(c, oh)
    _age_enquiry(session, eid, 3)
    _scan(session)
    assert len(session.query(PushOutbox).filter_by(user_id=owner_id, template="LEAD_REMINDER").all()) == 1
    assert session.query(EmailOutbox).filter_by(user_id=owner_id, template="LEAD_REMINDER").all() == []  # no address
    assert len(_rows(session, owner_id, "LEAD_REMINDER")) == 1


def test_lead_reminder_push_payload_carries_the_offset(client, pushprov):
    c, session = client
    owner_id, buyer_id, eid, oh = _new_enquiry(c, session)
    _register(c, oh)
    _age_enquiry(session, eid, 30)  # age 30h: the 2h AND 24h offsets are both due
    _scan(session)
    rows = session.query(PushOutbox).filter_by(user_id=owner_id, template="LEAD_REMINDER").all()
    assert len(rows) == 2
    row = next(r for r in rows if (r.payload or {}).get("offset_hours") == 2)
    assert row.payload == {"target": "OWNER_LEAD", "enquiry_id": eid, "offset_hours": 2}
    assert row.category == "ENQUIRY" and row.dedupe_key == f"n:enquiry:{eid}:lead_reminder:2"


def test_visit_reminder_push_payload_carries_eligibility_facts(client, pushprov):
    c, session = client
    owner_id, buyer_id, visit, bh, oh = _confirmed_visit(c, session, timedelta(hours=23))
    _register(c, bh)
    _scan(session)
    row = session.query(PushOutbox).filter_by(user_id=buyer_id, template="VISIT_REMINDER").one()
    assert row.payload["visit_id"] == visit.id and row.payload["interval"] == "24h"
    assert row.payload["scheduled_at"] == visit.scheduled_at.replace(microsecond=0).isoformat()
    assert row.payload["target"] == "BUYER_VISIT"


def test_scan_never_notifies_the_actor_or_third_parties(client, pushprov):
    """Reminder notifications exist only for the visit's/enquiry's own parties."""
    c, session = client
    owner_id, buyer_id, visit, bh, oh = _confirmed_visit(c, session, timedelta(hours=23))
    stranger, sh = _user(c)
    _scan(session)
    assert len(_rows(session, stranger)) == 0  # only buyer + owner got the reminder
