"""Push delivery (notification Phase 4): outbox claim/lease/retry, dual
devices, delivery-time eligibility rechecks, admin visibility of outcomes.

Everything is in-memory: the MemoryProvider records messages (one per device);
no network call is ever made. The outbox is driven explicitly with
push_svc.process_outbox on the test session.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.models import EmailOutbox, PropertyVisit, PushDevice, PushOutbox
from app.services import email as email_svc
from app.services import email_providers
from app.services import notifications as notif_svc
from app.services import push as push_svc
from app.services import push_providers
from app.services.push_providers import MemoryProvider
from tests.test_admin import _admin
from tests.test_notifications_events import _pair, _rows
from tests.test_push import _register, _user


@pytest.fixture()
def pushprov():
    provider = MemoryProvider()
    push_providers.set_provider_for_tests(provider)
    yield provider
    push_providers.set_provider_for_tests(None)


@pytest.fixture()
def mailprov():
    provider = email_providers.MemoryProvider()
    email_providers.set_provider_for_tests(provider)
    yield provider
    email_providers.set_provider_for_tests(None)


class _Shared:
    """The test session, with close() disabled (the fixture owns it)."""

    def __init__(self, session):
        self._s = session

    def __getattr__(self, name):
        return getattr(self._s, name)

    def close(self):
        pass


def _run(session):
    return push_svc.process_outbox(session_factory=lambda: _Shared(session))


def _outbox(session, user_id, template=None):
    session.expire_all()
    q = session.query(PushOutbox).filter(PushOutbox.user_id == user_id)
    if template:
        q = q.filter(PushOutbox.template == template)
    return q.order_by(PushOutbox.id).all()


def _device(c, h, platform="ios"):
    device = _register(c, h, platform=platform)
    return device["id"]


def _queued(c, session, pushprov):
    """An owner (auth header returned) with one active device and one queued
    push (LISTING_VERIFIED on a real listing — the FK needs a live listing)."""
    owner_id, oh, buyer_id, bh, lid = _pair(c, "Push Q Flat")
    device_id = _register(c, oh)["id"]
    _run(session)  # flush the registration-side state
    pushprov.sent.clear()
    row = notif_svc.notify_safely(session, "LISTING_VERIFIED", recipient_id=owner_id, actor_id=None,
                                  entity_id=lid, listing_id=lid, dedupe_key=f"q:{owner_id}")
    assert row is not None
    return owner_id, oh, device_id, _outbox(session, owner_id, "LISTING_VERIFIED")[0].id


def _due(session, outbox_id):
    """Make the push due again: the outbox row AND its device deliveries
    (since revision 2 each device carries its own retry time)."""
    session.execute(text("UPDATE push_outbox SET next_attempt_at = UTC_TIMESTAMP() - INTERVAL 1 SECOND WHERE id=:i"),
                    {"i": outbox_id})
    session.execute(text("UPDATE push_deliveries SET next_attempt_at = UTC_TIMESTAMP() - INTERVAL 1 SECOND "
                         "WHERE outbox_id=:i AND status='PENDING'"), {"i": outbox_id})
    session.flush()


def _own_row(session, outbox_id):
    row = session.get(PushOutbox, outbox_id)
    session.refresh(row)
    return row


def _raw_token(session, device_id):
    enc = session.get(PushDevice, device_id).token_enc
    return push_svc._fernet().decrypt(str(enc).encode("ascii")).decode()


# --- happy path ------------------------------------------------------------------------

def test_queued_push_is_delivered_to_the_device(client, pushprov):
    c, session = client
    uid, h, device_id, oid = _queued(c, session, pushprov)
    assert _run(session)["sent"] == 1
    message, key = pushprov.sent[0]
    assert message.to == _raw_token(session, device_id)  # the decrypted token, server-side only
    assert message.title == "Listing verified" and "verified" in message.body
    assert message.payload == {"target": "OWNER_LISTING"}
    assert key == f"restamp-push-{oid}-{device_id}"  # stable idempotency key per row + device
    row = _own_row(session, oid)
    assert row.status == "SENT" and row.sent_at is not None and row.provider == "memory"
    assert row.provider_message_id == "mem-1" and row.last_error is None and row.locked_at is None


def test_every_active_device_receives_one_copy(client, pushprov):
    c, session = client
    uid, h = _user(c)
    device_a = _device(c, h, "ios")
    device_b = _device(c, h, "android")
    notif_svc.notify_safely(session, "LISTING_VERIFIED", recipient_id=uid, actor_id=None,
                            entity_id=1, dedupe_key="two:devices")
    assert _run(session)["sent"] == 1  # ONE outbox row per recipient
    assert len(pushprov.sent) == 2  # delivered to every active device
    assert {m.to for m, _ in pushprov.sent} == {_raw_token(session, device_a), _raw_token(session, device_b)}
    assert {k.rsplit("-", 1)[-1] for _, k in pushprov.sent} == {str(device_a), str(device_b)}
    row = _outbox(session, uid, "LISTING_VERIFIED")[0]
    assert row.status == "SENT" and len(_outbox(session, uid)) == 1


def test_push_never_carries_secrets_in_payload_or_logs(client, pushprov):
    c, session = client
    uid, h, device_id, oid = _queued(c, session, pushprov)
    _run(session)
    row = _own_row(session, oid)
    blob = json.dumps(row.payload or {})
    token = _raw_token(session, device_id)
    assert token not in blob and "token" not in blob  # payload holds target ids only
    assert row.title and row.body  # fixed-template content, rendered server-side


# --- eligibility rechecked at delivery ----------------------------------------------------

def test_account_closed_receives_nothing(client, pushprov):
    c, session = client
    uid, h, device_id, oid = _queued(c, session, pushprov)
    session.execute(text("UPDATE users SET status='closed' WHERE id=:u"), {"u": uid})
    session.flush()
    assert _run(session)["dropped"] == 1 and pushprov.sent == []
    assert _own_row(session, oid).last_error == "account_closed"


def test_suspended_account_drops_optional_push(client, pushprov):
    c, session = client
    uid, h, device_id, oid = _queued(c, session, pushprov)
    session.execute(text("UPDATE users SET status='suspended' WHERE id=:u"), {"u": uid})
    session.flush()
    assert _run(session)["dropped"] == 1 and pushprov.sent == []
    assert _own_row(session, oid).last_error == "account_inactive"


def test_device_deactivated_after_queuing_is_not_delivered(client, pushprov):
    c, session = client
    uid, h, device_id, oid = _queued(c, session, pushprov)
    c.delete(f"/users/me/push/devices/{device_id}", headers=h)
    assert _run(session)["dropped"] == 1 and pushprov.sent == []
    assert _own_row(session, oid).last_error == "no_device"


def test_opted_out_after_queuing_is_dropped(client, pushprov):
    c, session = client
    uid, h, device_id, oid = _queued(c, session, pushprov)
    c.put("/users/me/push/preferences", json={"enquiries": True, "visits": True, "listings": False}, headers=h)
    assert _run(session)["dropped"] == 1 and pushprov.sent == []
    assert _own_row(session, oid).last_error == "opted_out"


def test_retry_backoff_then_success(client, pushprov):
    c, session = client
    uid, h, device_id, oid = _queued(c, session, pushprov)
    pushprov.fail_with = "retry"
    before = datetime.now(timezone.utc).replace(tzinfo=None)
    assert _run(session)["retry"] == 1
    row = _own_row(session, oid)
    assert row.status == "PENDING" and row.attempts == 1 and row.last_error == "retry" and row.sent_at is None
    assert timedelta(seconds=50) <= row.next_attempt_at - before <= timedelta(seconds=75)  # first backoff = 60 s
    assert _run(session) == {"sent": 0, "retry": 0, "failed": 0, "dropped": 0, "skipped": 0}  # not due yet
    _due(session, oid)
    assert _run(session)["retry"] == 1
    row = _own_row(session, oid)
    assert row.attempts == 2 and row.next_attempt_at - datetime.now(timezone.utc).replace(tzinfo=None) > timedelta(seconds=240)
    pushprov.fail_with = None
    _due(session, oid)
    assert _run(session)["sent"] == 1
    row = _own_row(session, oid)
    assert row.status == "SENT" and row.attempts == 3 and row.last_error is None and len(pushprov.sent) == 1


@pytest.mark.parametrize("kind", ["retry", "rate_limit", "config"])
def test_retries_are_exhausted_after_max_attempts(client, pushprov, kind):
    c, session = client
    uid, h, device_id, oid = _queued(c, session, pushprov)
    pushprov.fail_with = kind
    for attempt in range(1, push_svc.MAX_ATTEMPTS + 1):
        _due(session, oid)
        counts = _run(session)
        assert counts["failed" if attempt == push_svc.MAX_ATTEMPTS else "retry"] == 1
    row = _own_row(session, oid)
    assert row.status == "FAILED" and row.attempts == push_svc.MAX_ATTEMPTS and row.failed_at is not None
    assert row.last_error == f"{kind}_exhausted" and pushprov.sent == []
    _due(session, oid)
    assert _run(session) == {"sent": 0, "retry": 0, "failed": 0, "dropped": 0, "skipped": 0}  # FAILED is final


def test_refused_token_fails_immediately_and_invalidates_the_device(client, pushprov):
    c, session = client
    uid, h, device_id, oid = _queued(c, session, pushprov)
    pushprov.fail_with = "recipient"
    assert _run(session)["failed"] == 1 and pushprov.sent == []
    row = _own_row(session, oid)
    assert row.status == "FAILED" and row.attempts == 1 and row.last_error == "recipient"
    device = session.get(PushDevice, device_id)
    session.refresh(device)
    assert device.status == "INVALID" and device.invalid_reason == "provider_refused"
    _due(session, oid)
    assert _run(session) == {"sent": 0, "retry": 0, "failed": 0, "dropped": 0, "skipped": 0}  # FAILED is final
    assert len(pushprov.sent) == 0


def test_invalid_message_fails_without_invalidating_the_device(client, pushprov):
    c, session = client
    uid, h, device_id, oid = _queued(c, session, pushprov)
    pushprov.fail_with = "invalid_message"
    assert _run(session)["failed"] == 1
    row = _own_row(session, oid)
    assert row.status == "FAILED" and row.last_error == "invalid_message"
    device = session.get(PushDevice, device_id)
    session.refresh(device)
    assert device.status == "ACTIVE"  # the device is fine; only this message was rejected


def test_provider_crash_is_contained_and_retried(client, pushprov, monkeypatch):
    c, session = client
    uid, h, device_id, oid = _queued(c, session, pushprov)

    def explode(message, key):
        raise RuntimeError("expo secret=hunter2 token=ExponentPushToken[abc]")

    monkeypatch.setattr(pushprov, "send", explode)
    assert _run(session)["retry"] == 1
    row = _own_row(session, oid)
    assert row.status == "PENDING" and row.last_error == "retry"  # only a kind is stored, never provider text


def test_worker_restart_reclaims_only_expired_leases(client, pushprov):
    c, session = client
    uid, h, device_id, oid = _queued(c, session, pushprov)
    # A worker claimed the row and died before sending.
    session.execute(text("UPDATE push_outbox SET status='PROCESSING', attempts=1, locked_at=UTC_TIMESTAMP() WHERE id=:i"),
                    {"i": oid})
    session.flush()
    assert _run(session) == {"sent": 0, "retry": 0, "failed": 0, "dropped": 0, "skipped": 0}  # lease still held
    session.execute(text("UPDATE push_outbox SET locked_at = UTC_TIMESTAMP() - INTERVAL :s SECOND WHERE id=:i"),
                    {"s": push_svc.LEASE_SECONDS + 5, "i": oid})
    session.flush()
    assert _run(session)["sent"] == 1  # lease expired: the restarted worker finishes the job
    row = _own_row(session, oid)
    assert row.status == "SENT" and row.attempts == 2 and row.locked_at is None and len(pushprov.sent) == 1


def test_a_reclaimed_row_is_not_overwritten_by_its_old_worker(client, pushprov):
    c, session = client
    uid, h, device_id, oid = _queued(c, session, pushprov)
    session.execute(text("UPDATE push_outbox SET status='PROCESSING', attempts=2, locked_at=UTC_TIMESTAMP() WHERE id=:i"),
                    {"i": oid})
    session.flush()
    # The old worker (attempt 1) reports late: its write must not apply.
    push_svc._finish(_Shared(session), oid, 1, {"status": "SENT", "sent_at": datetime(2026, 1, 1)})
    row = _own_row(session, oid)
    assert row.status == "PROCESSING" and row.sent_at is None


def test_no_provider_means_nothing_is_queued_or_sent(client):
    c, session = client
    uid, h = _user(c)
    _register(c, h)
    assert c.get("/users/me/push", headers=h).json()["available"] is False
    assert notif_svc.notify_safely(session, "LISTING_VERIFIED", recipient_id=uid, actor_id=None,
                                   entity_id=1, dedupe_key="dark:1") is not None
    assert _outbox(session, uid) == []  # never queued without a provider
    assert _run(session) == {"sent": 0, "retry": 0, "failed": 0, "dropped": 0, "skipped": 0}


# --- reminder twins: eligibility re-checked at delivery (services/reminders.py) ---------

def _confirmed_visit(c, session, offset):
    """A CONFIRMED visit `offset` (timedelta) in the future on a live listing.
    Returns (owner_id, buyer_id, visit, buyer_auth_header)."""
    owner_id, oh, buyer_id, bh, lid = _pair(c, "Reminder Flat")
    visit = PropertyVisit(property_listing_id=lid, buyer_user_id=buyer_id, owner_user_id=owner_id,
                          scheduled_at=_utcnow() + offset, status="CONFIRMED", initiator="BUYER")
    session.add(visit)
    session.flush()
    return owner_id, buyer_id, visit, bh


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def _queue_visit_reminder(session, visit, recipient_id, recipient_header, c):
    _register(c, recipient_header)
    iso = visit.scheduled_at.replace(microsecond=0).isoformat()
    row = notif_svc.notify_safely(
        session, "VISIT_REMINDER", recipient_id=recipient_id, actor_id=None,
        entity_id=visit.id, listing_id=visit.property_listing_id, audience="BUYER",
        dedupe_key=f"visit:{visit.id}:reminder:24h:{iso}", when=visit.scheduled_at,
        push_payload={"visit_id": visit.id, "scheduled_at": iso, "interval": "24h"})
    assert row is not None
    return _outbox(session, recipient_id, "VISIT_REMINDER")[0].id


def test_visit_reminder_is_delivered_when_still_valid(client, pushprov):
    c, session = client
    owner_id, buyer_id, visit, bh = _confirmed_visit(c, session, timedelta(hours=2))
    oid = _queue_visit_reminder(session, visit, buyer_id, bh, c)
    assert _run(session)["sent"] == 1
    message = pushprov.sent[0][0]
    assert message.title == "Upcoming visit" and "Reminder" in message.body
    assert message.payload == {"target": "BUYER_VISIT", "visit_id": visit.id,
                               "scheduled_at": visit.scheduled_at.replace(microsecond=0).isoformat(),
                               "interval": "24h"}
    assert _own_row(session, oid).status == "SENT"


def test_visit_reminder_dropped_after_reschedule(client, pushprov):
    c, session = client
    owner_id, buyer_id, visit, bh = _confirmed_visit(c, session, timedelta(hours=2))
    oid = _queue_visit_reminder(session, visit, buyer_id, bh, c)
    # The visit moves to a new time after the reminder was queued.
    session.execute(text("UPDATE property_visits SET scheduled_at = scheduled_at + INTERVAL 3 HOUR WHERE id=:v"),
                    {"v": visit.id})
    session.flush()
    assert _run(session)["dropped"] == 1 and pushprov.sent == []
    assert _own_row(session, oid).last_error == "superseded"  # the NEW time gets its own fresh reminder


def test_visit_reminder_dropped_when_cancelled(client, pushprov):
    c, session = client
    owner_id, buyer_id, visit, bh = _confirmed_visit(c, session, timedelta(hours=2))
    oid = _queue_visit_reminder(session, visit, buyer_id, bh, c)
    session.execute(text("UPDATE property_visits SET status='CANCELLED' WHERE id=:v"), {"v": visit.id})
    session.flush()
    assert _run(session)["dropped"] == 1 and pushprov.sent == []
    assert _own_row(session, oid).last_error == "superseded"


def test_visit_reminder_dropped_once_the_time_has_passed(client, pushprov):
    c, session = client
    owner_id, buyer_id, visit, bh = _confirmed_visit(c, session, timedelta(hours=2))
    oid = _queue_visit_reminder(session, visit, buyer_id, bh, c)
    # The visit time arrives with the status still CONFIRMED (worker was down):
    # the payload time must still MATCH the visit for the "past" rule to hit.
    past = (_utcnow() - timedelta(minutes=1)).isoformat()
    session.execute(text("UPDATE property_visits SET scheduled_at = :t WHERE id=:v"),
                    {"t": past.replace("T", " "), "v": visit.id})
    session.execute(text("UPDATE push_outbox SET payload = JSON_SET(payload, '$.scheduled_at', :t) WHERE id=:o"),
                    {"t": past, "o": oid})
    session.flush()
    assert _run(session)["dropped"] == 1 and pushprov.sent == []
    assert _own_row(session, oid).last_error == "past"


def _queue_lead_reminder(session, eid, recipient_id, recipient_header, c, offset=2):
    _register(c, recipient_header)
    row = notif_svc.notify_safely(
        session, "LEAD_REMINDER", recipient_id=recipient_id, actor_id=None,
        entity_id=eid, audience="OWNER",
        dedupe_key=f"enquiry:{eid}:lead_reminder:{offset}",
        push_payload={"enquiry_id": eid, "offset_hours": offset})
    assert row is not None
    return _outbox(session, recipient_id, "LEAD_REMINDER")[0].id


def test_lead_reminder_dropped_when_the_owner_replied(client, pushprov):
    c, session = client
    owner_id, oh, buyer_id, bh, lid = _pair(c, "Lead Reply Flat")
    assert c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": "hi"}, headers=bh).status_code == 201
    eid = session.execute(text("SELECT id FROM enquiries WHERE buyer_user_id=:b AND property_listing_id=:l"),
                          {"b": buyer_id, "l": lid}).scalar()
    oid = _queue_lead_reminder(session, eid, owner_id, oh, c)
    assert c.post(f"/owner/leads/{eid}/messages", json={"message": "coming"}, headers=oh).status_code == 201
    assert _run(session)["dropped"] == 1 and pushprov.sent == []
    assert _own_row(session, oid).last_error == "superseded"  # the lead is no longer waiting


def test_lead_reminder_dropped_when_the_enquiry_is_closed(client, pushprov):
    c, session = client
    owner_id, oh, buyer_id, bh, lid = _pair(c, "Lead Closed Flat")
    assert c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": "hi"}, headers=bh).status_code == 201
    eid = session.execute(text("SELECT id FROM enquiries WHERE buyer_user_id=:b AND property_listing_id=:l"),
                          {"b": buyer_id, "l": lid}).scalar()
    oid = _queue_lead_reminder(session, eid, owner_id, oh, c)
    assert c.patch(f"/owner/leads/{eid}", json={"status": "CONTACTED"}, headers=oh).status_code == 200
    assert _run(session)["dropped"] == 1 and pushprov.sent == []
    assert _own_row(session, oid).last_error == "superseded"


def test_lead_reminder_is_delivered_when_still_waiting(client, pushprov):
    c, session = client
    owner_id, oh, buyer_id, bh, lid = _pair(c, "Lead Waiting Flat")
    assert c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": "hi"}, headers=bh).status_code == 201
    eid = session.execute(text("SELECT id FROM enquiries WHERE buyer_user_id=:b AND property_listing_id=:l"),
                          {"b": buyer_id, "l": lid}).scalar()
    oid = _queue_lead_reminder(session, eid, owner_id, oh, c)
    assert _run(session)["sent"] == 1
    message = pushprov.sent[0][0]
    assert "reply" in message.body.lower() and message.payload["target"] == "OWNER_LEAD"
    assert message.payload["enquiry_id"] == eid and message.payload["offset_hours"] == 2
    assert _own_row(session, oid).status == "SENT"


# --- consolidated mocked end-to-end: one API event -> in-app + email twin + push
# twin -> both workers deliver through their own providers -> admin audit trail ----

def test_one_event_reaches_every_channel_and_the_admin_audit(client, pushprov, mailprov):
    c, session = client
    owner_id, oh, buyer_id, bh, lid = _pair(c, "E2E Flow Flat")
    # Set the owner's contact address: the PUT response carries the
    # development-only code (GET never does). Its VERIFY_EMAIL twin is
    # delivered while the address is still unverified, then the code confirms
    # it. The captures are cleared afterwards, so every assertion below sees
    # exactly the enquiry's traffic.
    r = c.put("/users/me/email", json={"email": f"e2e-{owner_id}@example.com"}, headers=oh)
    assert r.status_code == 200, r.text
    code = r.json()["debug_code"]
    assert code and len(code) == 6
    assert email_svc.process_outbox(session_factory=lambda: _Shared(session))["sent"] == 1
    mailprov.sent.clear()
    assert c.post("/users/me/email/verify", json={"code": code}, headers=oh).status_code == 200
    # One active device: its token is captured cleanly at registration
    # (server-side only).
    token = _raw_token(session, _register(c, oh)["id"])
    pushprov.sent.clear()

    # 1) The API event: a buyer enquiry (never the owner's own action).
    r = c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": "Interested"}, headers=bh)
    assert r.status_code == 201, r.text
    eid = session.execute(text("SELECT id FROM enquiries WHERE buyer_user_id=:b AND property_listing_id=:l"),
                          {"b": buyer_id, "l": lid}).scalar()

    # 2) One in-app notification was stored and BOTH twins were queued (the
    #    same Phase 1 dedupe rules; one row per channel, linked to it).
    session.expire_all()
    inapp = _rows(session, owner_id, "ENQUIRY_NEW")
    assert len(inapp) == 1 and inapp[0]["dedupe_key"] == f"enquiry:{eid}:new"
    email_rows = session.query(EmailOutbox).filter(
        EmailOutbox.user_id == owner_id, EmailOutbox.template == "ENQUIRY_NEW").all()
    push_rows = session.query(PushOutbox).filter(
        PushOutbox.user_id == owner_id, PushOutbox.template == "ENQUIRY_NEW").all()
    assert len(email_rows) == 1 and len(push_rows) == 1
    assert email_rows[0].notification_id == inapp[0]["id"] == push_rows[0].notification_id
    assert email_rows[0].status == "PENDING" and push_rows[0].status == "PENDING"

    # 3) Each worker delivers its own channel through its own provider only.
    assert email_svc.process_outbox(session_factory=lambda: _Shared(session))["sent"] == 1
    assert push_svc.process_outbox(session_factory=lambda: _Shared(session))["sent"] == 1
    mail_message = mailprov.sent[0][0]
    assert mail_message.to == f"e2e-{owner_id}@example.com"
    assert mail_message.subject == "New enquiry on your RESTAMP listing"
    push_message = pushprov.sent[0][0]
    assert push_message.to == token  # the decrypted token, server-side only
    assert push_message.title == "New enquiry" and push_message.payload == {"target": "OWNER_LEAD"}
    session.expire_all()
    assert session.get(EmailOutbox, email_rows[0].id).status == "SENT"
    assert session.get(PushOutbox, push_rows[0].id).status == "SENT"

    # 4) Admins can audit both delivery trails — outcomes only, never secrets.
    admin_h, _ = _admin(c, session)
    push_json = c.get("/admin/push-deliveries", params={"user_id": owner_id}, headers=admin_h).json()
    push_item = [i for i in push_json["items"] if i["template"] == "ENQUIRY_NEW"][0]
    assert push_item["status"] == "SENT" and push_item["attempts"] == 1
    assert push_item["provider"] == "memory" and push_item["failure"] is None
    email_json = c.get("/admin/email-deliveries", params={"user_id": owner_id}, headers=admin_h).json()
    email_item = [i for i in email_json["items"] if i["template"] == "ENQUIRY_NEW"][0]
    assert email_item["status"] == "SENT" and email_item["provider"] == "memory"
    blob = json.dumps(push_json) + json.dumps(email_json)
    assert token not in blob  # the device token never leaves the server
    assert "OWNER_LEAD" not in blob and "Interested" not in blob  # payload/message content stays out
