"""Reminder eligibility shared by email AND push (notification Phase 4, revision 2).

The real scan queues the reminder (in-app + email + push twins); then the
world changes BEFORE delivery (owner replies, visit cancelled / rescheduled /
past, a party's account becomes inactive). Both delivery workers must re-check
with the same rule (services/reminders.reminder_drop_reason) and drop it.
Everything is in-memory; no email or push is really sent. Rollback-isolated.
"""
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.models import EmailOutbox, PushOutbox
from app.services import email as email_svc
from app.services import email_providers, push_providers
from app.services import notifications as notif_svc
from app.services import push as push_svc
from app.services import reminders as rem_svc
from tests.test_email import _verify
from tests.test_push import _register
from tests.test_push_delivery import _Shared
from tests.test_reminders import _age_enquiry, _confirmed_visit, _new_enquiry, _utcnow


@pytest.fixture()
def providers():
    mail = email_providers.MemoryProvider()
    push = push_providers.MemoryProvider()
    email_providers.set_provider_for_tests(mail)
    push_providers.set_provider_for_tests(push)
    yield mail, push
    email_providers.set_provider_for_tests(None)
    push_providers.set_provider_for_tests(None)


def _scan(session):
    return rem_svc.run_scan(session_factory=lambda: _Shared(session))


def _deliver_all(session):
    e = email_svc.process_outbox(session_factory=lambda: _Shared(session))
    p = push_svc.process_outbox(session_factory=lambda: _Shared(session))
    return e, p


def _rows(session, model, user_id, template):
    session.expire_all()
    return session.query(model).filter(model.user_id == user_id, model.template == template).order_by(model.id).all()


def _both_channels(c, session, mail, user_id, header, address):
    _verify(c, header, address)
    _register(c, header)
    email_svc.process_outbox(session_factory=lambda: _Shared(session))  # flush the verification email
    mail.sent.clear()


def _visit_setup(c, session, providers, hours=20):
    mail, push = providers
    owner_id, buyer_id, visit, bh, oh = _confirmed_visit(c, session, timedelta(hours=hours))
    _both_channels(c, session, mail, buyer_id, bh, "visit.buyer@example.com")
    _both_channels(c, session, mail, owner_id, oh, "visit.owner@example.com")
    assert _scan(session)["visit_reminders"] == 2
    for uid in (buyer_id, owner_id):
        assert len(_rows(session, EmailOutbox, uid, "VISIT_REMINDER")) == 1
        assert len(_rows(session, PushOutbox, uid, "VISIT_REMINDER")) == 1
    return owner_id, buyer_id, visit


def _assert_dropped(session, providers, uids, template, reason):
    mail, push = providers
    _deliver_all(session)
    for uid in uids:
        e = _rows(session, EmailOutbox, uid, template)[0]
        p = _rows(session, PushOutbox, uid, template)[0]
        assert (e.status, e.last_error) == ("FAILED", reason), (uid, e.status, e.last_error)
        assert (p.status, p.last_error) == ("FAILED", reason), (uid, p.status, p.last_error)
    assert [m for m, _ in mail.sent if "eminder" in m.subject or "waiting" in m.subject] == []
    assert push.sent == []


# --- the shared rule ------------------------------------------------------------------------

def test_shared_rule_ignores_other_templates_and_fails_closed_without_facts(client):
    c, session = client
    now = _utcnow()
    assert rem_svc.reminder_drop_reason(session, "ENQUIRY_NEW", {}, now) is None
    assert rem_svc.reminder_drop_reason(session, "VISIT_REMINDER", {}, now) == "stale"
    assert rem_svc.reminder_drop_reason(session, "LEAD_REMINDER", {"offset_hours": 2}, now) == "stale"


def test_reminder_email_carries_facts_and_other_emails_do_not(client, providers):
    c, session = client
    owner_id, buyer_id, visit = _visit_setup(c, session, providers)
    payload = _rows(session, EmailOutbox, buyer_id, "VISIT_REMINDER")[0].payload
    assert payload["facts"]["visit_id"] == visit.id and payload["facts"]["scheduled_at"]
    notif_svc.notify_safely(session, "LISTING_VERIFIED", recipient_id=owner_id, actor_id=None, entity_id=1,
                            dedupe_key="plain:1")
    assert "facts" not in _rows(session, EmailOutbox, owner_id, "LISTING_VERIFIED")[0].payload  # Phase 3 unchanged


# --- visit reminders -------------------------------------------------------------------------

def test_valid_visit_reminder_is_delivered_on_both_channels(client, providers):
    c, session = client
    mail, push = providers
    owner_id, buyer_id, visit = _visit_setup(c, session, providers)
    e, p = _deliver_all(session)
    assert e["sent"] == 2 and p["sent"] == 2 and len(push.sent) == 2
    assert {m.subject for m, _ in mail.sent} == {"Reminder: your site visit is coming up"}


def test_visit_cancelled_between_scan_and_delivery(client, providers):
    c, session = client
    owner_id, buyer_id, visit = _visit_setup(c, session, providers)
    session.execute(text("UPDATE property_visits SET status='CANCELLED' WHERE id=:v"), {"v": visit.id})
    session.flush()
    _assert_dropped(session, providers, (owner_id, buyer_id), "VISIT_REMINDER", "superseded")


def test_visit_rescheduled_between_scan_and_delivery(client, providers):
    c, session = client
    owner_id, buyer_id, visit = _visit_setup(c, session, providers)
    session.execute(text("UPDATE property_visits SET scheduled_at = scheduled_at + INTERVAL 2 HOUR WHERE id=:v"), {"v": visit.id})
    session.flush()
    _assert_dropped(session, providers, (owner_id, buyer_id), "VISIT_REMINDER", "superseded")


def test_visit_time_passed_before_delivery(client, providers):
    c, session = client
    owner_id, buyer_id, visit = _visit_setup(c, session, providers)
    # Move both the visit and the stored facts into the past (same instant).
    past = (_utcnow() - timedelta(minutes=5)).isoformat()
    session.execute(text("UPDATE property_visits SET scheduled_at = :t WHERE id=:v"), {"t": past, "v": visit.id})
    for table in ("email_outbox", "push_outbox"):
        for uid in (owner_id, buyer_id):
            row = session.query(EmailOutbox if table == "email_outbox" else PushOutbox).filter_by(
                user_id=uid, template="VISIT_REMINDER").one()
            payload = dict(row.payload)
            if table == "email_outbox":
                payload["facts"] = {**payload["facts"], "scheduled_at": past}
            else:
                payload["scheduled_at"] = past
            row.payload = payload
    session.flush()
    _assert_dropped(session, providers, (owner_id, buyer_id), "VISIT_REMINDER", "past")


def test_counterpart_suspended_before_delivery_drops_both_reminders(client, providers):
    c, session = client
    owner_id, buyer_id, visit = _visit_setup(c, session, providers)
    session.execute(text("UPDATE users SET status='suspended' WHERE id=:u"), {"u": owner_id})
    session.flush()
    _deliver_all(session)
    # The buyer's reminders: dropped by the shared rule (the owner is inactive).
    for model in (EmailOutbox, PushOutbox):
        row = _rows(session, model, buyer_id, "VISIT_REMINDER")[0]
        assert (row.status, row.last_error) == ("FAILED", "account_inactive")
    # The suspended owner's own optional reminders are dropped by each channel's recipient check.
    for model in (EmailOutbox, PushOutbox):
        assert _rows(session, model, owner_id, "VISIT_REMINDER")[0].status == "FAILED"


# --- lead reminders --------------------------------------------------------------------------

def _lead_setup(c, session, providers):
    mail, push = providers
    owner_id, buyer_id, eid, oh = _new_enquiry(c, session)
    _both_channels(c, session, mail, owner_id, oh, "lead.owner@example.com")
    _age_enquiry(session, eid, 2.5)
    assert _scan(session)["lead_reminders"] == 1
    assert len(_rows(session, EmailOutbox, owner_id, "LEAD_REMINDER")) == 1
    assert len(_rows(session, PushOutbox, owner_id, "LEAD_REMINDER")) == 1
    return owner_id, buyer_id, eid, oh


def test_waiting_lead_reminder_is_delivered_on_both_channels(client, providers):
    c, session = client
    mail, push = providers
    owner_id, buyer_id, eid, oh = _lead_setup(c, session, providers)
    e, p = _deliver_all(session)
    assert e["sent"] == 1 and p["sent"] == 1
    assert [m.subject for m, _ in mail.sent] == ["A lead is waiting for your reply"]


def test_owner_replied_between_scan_and_delivery(client, providers):
    c, session = client
    owner_id, buyer_id, eid, oh = _lead_setup(c, session, providers)
    assert c.post(f"/owner/leads/{eid}/messages", json={"message": "Yes, available"}, headers=oh).status_code == 201
    _assert_dropped(session, providers, (owner_id,), "LEAD_REMINDER", "superseded")


def test_lead_marked_contacted_between_scan_and_delivery(client, providers):
    c, session = client
    owner_id, buyer_id, eid, oh = _lead_setup(c, session, providers)
    assert c.patch(f"/owner/leads/{eid}", json={"status": "CONTACTED"}, headers=oh).status_code == 200
    _assert_dropped(session, providers, (owner_id,), "LEAD_REMINDER", "superseded")


def test_buyer_account_closed_between_scan_and_delivery(client, providers):
    c, session = client
    owner_id, buyer_id, eid, oh = _lead_setup(c, session, providers)
    session.execute(text("UPDATE users SET status='closed' WHERE id=:u"), {"u": buyer_id})
    session.flush()
    _assert_dropped(session, providers, (owner_id,), "LEAD_REMINDER", "account_inactive")


def test_reminder_email_without_facts_is_dropped(client, providers):
    c, session = client
    mail, push = providers
    owner_id, buyer_id, eid, oh = _lead_setup(c, session, providers)
    row = _rows(session, EmailOutbox, owner_id, "LEAD_REMINDER")[0]
    row.payload = {k: v for k, v in row.payload.items() if k != "facts"}
    session.flush()
    email_svc.process_outbox(session_factory=lambda: _Shared(session))
    row = _rows(session, EmailOutbox, owner_id, "LEAD_REMINDER")[0]
    assert (row.status, row.last_error) == ("FAILED", "stale") and mail.sent == []
