"""Push delivery per device + Expo tickets/receipts (notification Phase 4, revision 2).

- One recipient, several devices: each device is tracked in push_deliveries
  (UNIQUE outbox+device); a device that got its ticket is never sent again
  when a sibling fails and is retried.
- A ticket is NOT delivery: SENT (accepted) -> DELIVERED only on an ok receipt.
- Receipts: ok / not ready / DeviceNotRegistered / MessageRateExceeded /
  MessageTooBig / transport failure / too old to ever arrive.
- Crash recovery and late writes from a reclaimed worker.
Everything is in-memory (MemoryProvider) or a mocked HTTP transport; no real
push is ever sent. Rollback-isolated `client` fixture.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.models import PushDelivery, PushDevice, PushOutbox
from app.services import notifications as notif_svc
from app.services import push as push_svc
from app.services import push_providers
from app.services.push_providers import MemoryProvider, PushMessage, PushSendError
from tests.test_admin import _admin
from tests.test_notifications_events import _pair
from tests.test_push import _register
from tests.test_push_delivery import _Shared, _due, _own_row, _raw_token


@pytest.fixture()
def pushprov():
    provider = MemoryProvider()
    push_providers.set_provider_for_tests(provider)
    yield provider
    push_providers.set_provider_for_tests(None)


def _run(session):
    return push_svc.process_outbox(session_factory=lambda: _Shared(session))


def _poll(session):
    return push_svc.poll_receipts(session_factory=lambda: _Shared(session))


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def _two_devices(c, session, pushprov, n=2):
    """Owner with `n` active devices and one queued push. Returns
    (owner_id, header, [device ids], outbox id)."""
    owner_id, oh, buyer_id, bh, lid = _pair(c, "Multi Device Flat")
    devices = [_register(c, oh, platform="ios" if i % 2 == 0 else "android")["id"] for i in range(n)]
    pushprov.sent.clear()
    assert notif_svc.notify_safely(session, "LISTING_VERIFIED", recipient_id=owner_id, actor_id=None,
                                   entity_id=lid, listing_id=lid, dedupe_key=f"md:{owner_id}") is not None
    session.expire_all()
    oid = session.query(PushOutbox).filter(PushOutbox.user_id == owner_id).one().id
    return owner_id, oh, devices, oid


def _deliveries(session, oid):
    session.expire_all()
    return {d.device_id: d for d in session.query(PushDelivery).filter(PushDelivery.outbox_id == oid)}


def _sent_tokens(pushprov):
    return [m.to for m, _ in pushprov.sent]


def _receipt_due_now(session, oid):
    session.execute(text("UPDATE push_deliveries SET receipt_due_at = UTC_TIMESTAMP() - INTERVAL 1 SECOND "
                         "WHERE outbox_id=:i AND status='SENT'"), {"i": oid})
    session.flush()


# --- per-device fan-out ------------------------------------------------------------------

def test_every_device_gets_its_own_delivery_row_and_one_copy(client, pushprov):
    c, session = client
    uid, h, devices, oid = _two_devices(c, session, pushprov, n=3)
    assert _run(session)["sent"] == 1
    rows = _deliveries(session, oid)
    assert set(rows) == set(devices) and all(r.status == "SENT" and r.ticket_id for r in rows.values())
    assert sorted(_sent_tokens(pushprov)) == sorted(_raw_token(session, d) for d in devices)
    assert _own_row(session, oid).status == "SENT"


def test_a_sent_device_is_never_resent_when_its_sibling_is_retried(client, pushprov):
    c, session = client
    uid, h, (a, b), oid = _two_devices(c, session, pushprov)
    pushprov.fail_tokens[_raw_token(session, b)] = "retry"
    assert _run(session)["retry"] == 1
    rows = _deliveries(session, oid)
    assert rows[a].status == "SENT" and rows[b].status == "PENDING" and rows[b].last_error == "retry"
    outbox = _own_row(session, oid)
    assert outbox.status == "PENDING" and outbox.next_attempt_at == rows[b].next_attempt_at
    del pushprov.fail_tokens[_raw_token(session, b)]
    _due(session, oid)
    assert _run(session)["sent"] == 1
    tokens = _sent_tokens(pushprov)
    assert tokens.count(_raw_token(session, a)) == 1 and tokens.count(_raw_token(session, b)) == 1
    rows = _deliveries(session, oid)
    assert rows[a].attempts == 1 and rows[b].attempts == 2 and _own_row(session, oid).status == "SENT"


def test_an_invalid_token_deactivates_only_that_device(client, pushprov):
    c, session = client
    uid, h, (a, b), oid = _two_devices(c, session, pushprov)
    pushprov.fail_tokens[_raw_token(session, b)] = "recipient"  # DeviceNotRegistered ticket
    assert _run(session)["sent"] == 1
    rows = _deliveries(session, oid)
    assert rows[a].status == "SENT" and rows[b].status == "FAILED" and rows[b].last_error == "recipient"
    session.expire_all()
    assert session.get(PushDevice, a).status == "ACTIVE" and session.get(PushDevice, b).status == "INVALID"
    _due(session, oid)
    assert _run(session) == {"sent": 0, "retry": 0, "failed": 0, "dropped": 0, "skipped": 0}  # final


def test_all_devices_refused_fails_the_push(client, pushprov):
    c, session = client
    uid, h, (a, b), oid = _two_devices(c, session, pushprov)
    pushprov.fail_with = "recipient"
    assert _run(session)["failed"] == 1
    outbox = _own_row(session, oid)
    assert outbox.status == "FAILED" and outbox.last_error == "recipient"
    session.expire_all()
    assert {session.get(PushDevice, d).status for d in (a, b)} == {"INVALID"}


def test_each_device_has_its_own_retry_budget(client, pushprov):
    c, session = client
    uid, h, (a, b), oid = _two_devices(c, session, pushprov)
    pushprov.fail_tokens[_raw_token(session, b)] = "rate_limit"
    for _ in range(push_svc.MAX_ATTEMPTS):
        _due(session, oid)
        _run(session)
    rows = _deliveries(session, oid)
    assert rows[a].status == "SENT" and rows[a].attempts == 1
    assert rows[b].status == "FAILED" and rows[b].attempts == push_svc.MAX_ATTEMPTS and rows[b].last_error == "rate_limit_exhausted"
    assert _own_row(session, oid).status == "SENT" and _sent_tokens(pushprov).count(_raw_token(session, a)) == 1


def test_fan_out_is_idempotent_and_the_device_set_is_fixed(client, pushprov):
    c, session = client
    uid, h, (a, b), oid = _two_devices(c, session, pushprov)
    assert push_svc._fan_out(_Shared(session), oid, uid) == 2
    assert push_svc._fan_out(_Shared(session), oid, uid) == 0
    late = _register(c, h)["id"]  # registered after the push was fanned out
    assert push_svc._fan_out(_Shared(session), oid, uid) == 0
    _run(session)
    assert set(_deliveries(session, oid)) == {a, b} and _raw_token(session, late) not in _sent_tokens(pushprov)


def test_a_device_logged_out_after_fan_out_is_skipped(client, pushprov):
    c, session = client
    uid, h, (a, b), oid = _two_devices(c, session, pushprov)
    push_svc._fan_out(_Shared(session), oid, uid)
    assert c.delete(f"/users/me/push/devices/{b}", headers=h).status_code == 204
    assert _run(session)["sent"] == 1
    rows = _deliveries(session, oid)
    assert rows[b].status == "FAILED" and rows[b].last_error == "device_inactive"
    assert _sent_tokens(pushprov) == [_raw_token(session, a)]


def test_recipient_drop_fails_every_open_device_delivery(client, pushprov):
    c, session = client
    uid, h, (a, b), oid = _two_devices(c, session, pushprov)
    push_svc._fan_out(_Shared(session), oid, uid)
    assert c.put("/users/me/push/preferences", json={"enquiries": True, "visits": True, "listings": False},
                 headers=h).status_code == 200
    assert _run(session)["dropped"] == 1 and pushprov.sent == []
    assert {r.status for r in _deliveries(session, oid).values()} == {"FAILED"}
    assert {r.last_error for r in _deliveries(session, oid).values()} == {"opted_out"}


# --- crash recovery / stale writes ------------------------------------------------------

def test_worker_crash_mid_send_is_recovered_after_the_lease(client, pushprov):
    c, session = client
    uid, h, (a, b), oid = _two_devices(c, session, pushprov)
    push_svc._fan_out(_Shared(session), oid, uid)
    # A worker claimed the outbox row and device A, then died before the provider answered.
    session.execute(text("UPDATE push_outbox SET status='PROCESSING', attempts=1, locked_at=UTC_TIMESTAMP() WHERE id=:i"), {"i": oid})
    session.execute(text("UPDATE push_deliveries SET status='PROCESSING', attempts=1, locked_at=UTC_TIMESTAMP() "
                         "WHERE outbox_id=:i AND device_id=:d"), {"i": oid, "d": a})
    session.flush()
    assert _run(session) == {"sent": 0, "retry": 0, "failed": 0, "dropped": 0, "skipped": 0}  # leases still held
    session.execute(text("UPDATE push_outbox SET locked_at = UTC_TIMESTAMP() - INTERVAL :s SECOND WHERE id=:i"),
                    {"s": push_svc.LEASE_SECONDS + 5, "i": oid})
    session.execute(text("UPDATE push_deliveries SET locked_at = UTC_TIMESTAMP() - INTERVAL :s SECOND WHERE outbox_id=:i AND device_id=:d"),
                    {"s": push_svc.LEASE_SECONDS + 5, "i": oid, "d": a})
    session.flush()
    assert _run(session)["sent"] == 1
    rows = _deliveries(session, oid)
    assert rows[a].status == "SENT" and rows[a].attempts == 2 and rows[b].status == "SENT" and rows[b].attempts == 1
    assert sorted(_sent_tokens(pushprov)) == sorted([_raw_token(session, a), _raw_token(session, b)])


def test_a_late_write_from_a_reclaimed_worker_is_ignored(client, pushprov):
    c, session = client
    uid, h, (a, b), oid = _two_devices(c, session, pushprov)
    push_svc._fan_out(_Shared(session), oid, uid)
    did = _deliveries(session, oid)[a].id
    session.execute(text("UPDATE push_deliveries SET status='PROCESSING', attempts=2, locked_at=UTC_TIMESTAMP() WHERE id=:i"), {"i": did})
    session.flush()
    assert push_svc._write_delivery(_Shared(session), did, 1, {"status": "SENT", "ticket_id": "late"}) is False
    session.expire_all()
    row = session.get(PushDelivery, did)
    assert row.status == "PROCESSING" and row.ticket_id is None


# --- tickets vs receipts -------------------------------------------------------------------

def test_a_ticket_is_not_delivery(client, pushprov):
    c, session = client
    admin_h, _ = _admin(c, session)
    uid, h, (a, b), oid = _two_devices(c, session, pushprov)
    before = _now()
    _run(session)
    rows = _deliveries(session, oid)
    for r in rows.values():
        assert r.status == "SENT" and r.delivered_at is None and r.ticket_id
        assert timedelta(minutes=14) <= r.receipt_due_at - before <= timedelta(minutes=16)  # Expo: wait ~15 min
    assert _poll(session) == {"delivered": 0, "failed": 0, "resend": 0, "waiting": 0, "expired": 0}  # not due yet
    item = next(i for i in c.get("/admin/push-deliveries", params={"user_id": uid}, headers=admin_h).json()["items"])
    assert item["status"] == "SENT" and item["devices"] == {"pending": 0, "accepted": 2, "delivered": 0, "failed": 0}


def test_ok_receipt_confirms_delivery(client, pushprov):
    c, session = client
    admin_h, _ = _admin(c, session)
    uid, h, (a, b), oid = _two_devices(c, session, pushprov)
    _run(session)
    _receipt_due_now(session, oid)
    assert _poll(session)["delivered"] == 2
    rows = _deliveries(session, oid)
    assert all(r.status == "DELIVERED" and r.delivered_at and r.receipt_due_at is None for r in rows.values())
    assert sorted(pushprov.receipt_calls[0]) == sorted(r.ticket_id for r in rows.values())  # one batched call
    item = c.get("/admin/push-deliveries", params={"user_id": uid}, headers=admin_h).json()["items"][0]
    assert item["devices"]["delivered"] == 2


def test_receipt_not_ready_is_polled_again_later(client, pushprov):
    c, session = client
    uid, h, (a, b), oid = _two_devices(c, session, pushprov)
    _run(session)
    pushprov.receipt_default = None  # Expo has no receipt yet
    _receipt_due_now(session, oid)
    before = _now()
    assert _poll(session)["waiting"] == 2
    for r in _deliveries(session, oid).values():
        assert r.status == "SENT" and r.receipt_attempts == 1 and r.receipt_due_at - before >= timedelta(minutes=14)
        assert r.locked_at is None


def test_device_not_registered_receipt_deactivates_the_device(client, pushprov):
    c, session = client
    uid, h, (a, b), oid = _two_devices(c, session, pushprov)
    _run(session)
    rows = _deliveries(session, oid)
    pushprov.receipts[rows[b].ticket_id] = "DeviceNotRegistered"
    _receipt_due_now(session, oid)
    counts = _poll(session)
    assert counts["delivered"] == 1 and counts["failed"] == 1
    rows = _deliveries(session, oid)
    assert rows[a].status == "DELIVERED" and rows[b].status == "FAILED" and rows[b].last_error == "receipt_recipient"
    session.expire_all()
    assert session.get(PushDevice, b).status == "INVALID" and session.get(PushDevice, b).invalid_reason == "DeviceNotRegistered"
    assert session.get(PushDevice, a).status == "ACTIVE"


def test_rate_limited_receipt_resends_only_that_device(client, pushprov):
    c, session = client
    uid, h, (a, b), oid = _two_devices(c, session, pushprov)
    _run(session)
    rows = _deliveries(session, oid)
    pushprov.receipts[rows[b].ticket_id] = "MessageRateExceeded"
    _receipt_due_now(session, oid)
    assert _poll(session)["resend"] == 1
    rows = _deliveries(session, oid)
    assert rows[b].status == "PENDING" and rows[b].ticket_id is None and rows[b].last_error == "receipt_rate_limit"
    assert _own_row(session, oid).status == "PENDING"  # the recipient row was reopened
    _due(session, oid)
    assert _run(session)["sent"] == 1
    tokens = _sent_tokens(pushprov)
    assert tokens.count(_raw_token(session, a)) == 1 and tokens.count(_raw_token(session, b)) == 2
    assert _deliveries(session, oid)[b].status == "SENT"


def test_permanent_receipt_errors_fail_without_deactivating(client, pushprov):
    c, session = client
    uid, h, (a, b), oid = _two_devices(c, session, pushprov)
    _run(session)
    rows = _deliveries(session, oid)
    pushprov.receipts[rows[a].ticket_id] = "MessageTooBig"
    pushprov.receipts[rows[b].ticket_id] = "InvalidCredentials"
    _receipt_due_now(session, oid)
    assert _poll(session)["failed"] == 2
    rows = _deliveries(session, oid)
    assert rows[a].last_error == "receipt_invalid_message" and rows[b].last_error == "receipt_config"
    session.expire_all()
    assert {session.get(PushDevice, d).status for d in (a, b)} == {"ACTIVE"}


def test_receipt_call_failure_changes_nothing_but_the_next_poll_time(client, pushprov):
    c, session = client
    uid, h, (a, b), oid = _two_devices(c, session, pushprov)
    _run(session)
    pushprov.receipt_fail_with = "retry"
    _receipt_due_now(session, oid)
    assert _poll(session)["waiting"] == 2
    assert {r.status for r in _deliveries(session, oid).values()} == {"SENT"}
    pushprov.receipt_fail_with = None
    _receipt_due_now(session, oid)
    assert _poll(session)["delivered"] == 2


def test_receipts_are_given_up_after_expo_retention(client, pushprov):
    c, session = client
    uid, h, (a, b), oid = _two_devices(c, session, pushprov)
    _run(session)
    pushprov.receipt_default = None
    session.execute(text("UPDATE push_deliveries SET sent_at = UTC_TIMESTAMP() - INTERVAL 25 HOUR, "
                         "receipt_due_at = UTC_TIMESTAMP() - INTERVAL 1 SECOND WHERE outbox_id=:i"), {"i": oid})
    session.flush()
    assert _poll(session)["expired"] == 2
    rows = _deliveries(session, oid)
    assert all(r.status == "SENT" and r.receipt_due_at is None and r.last_error == "receipt_unavailable" for r in rows.values())
    assert _poll(session) == {"delivered": 0, "failed": 0, "resend": 0, "waiting": 0, "expired": 0}  # polling stopped


def test_no_receipts_polled_while_the_provider_is_disabled(client, pushprov):
    c, session = client
    uid, h, (a, b), oid = _two_devices(c, session, pushprov)
    _run(session)
    _receipt_due_now(session, oid)
    push_providers.set_provider_for_tests(push_providers.DisabledProvider())
    assert _poll(session) == {"delivered": 0, "failed": 0, "resend": 0, "waiting": 0, "expired": 0}


# --- Expo adapter (mocked HTTP transport; no network) -------------------------------------

class _Resp:
    def __init__(self, code, payload):
        self.status_code = code
        self._payload = payload

    def json(self):
        return self._payload


def test_expo_adapter_tickets_and_receipts_are_mapped(monkeypatch):
    calls = []
    answers = []

    def fake_post(url, headers, json, timeout):
        calls.append((url, json))
        return answers.pop(0)

    monkeypatch.setattr(push_providers.httpx, "post", fake_post)
    provider = push_providers.ExpoProvider("", 10)
    msg = PushMessage("ExponentPushToken[abcdefghijklmnopqrstuv]", "T", "B", {"target": "NONE"})
    answers.append(_Resp(200, {"data": [{"status": "ok", "id": "ticket-1"}]}))
    assert provider.send(msg, "k") == "ticket-1" and calls[-1][0].endswith("/push/send")
    for name, kind in (("DeviceNotRegistered", "recipient"), ("MessageTooBig", "invalid_message"),
                       ("MessageRateExceeded", "rate_limit"), ("InvalidCredentials", "config"), ("Weird", "retry")):
        answers.append(_Resp(200, {"data": [{"status": "error", "details": {"error": name}}]}))
        with pytest.raises(PushSendError) as err:
            provider.send(msg, "k")
        assert err.value.kind == kind
    answers.append(_Resp(200, {"data": {
        "t-ok": {"status": "ok"},
        "t-dead": {"status": "error", "message": "x", "details": {"error": "DeviceNotRegistered"}},
    }}))
    receipts = provider.get_receipts(["t-ok", "t-dead", "t-missing"])
    assert calls[-1][0].endswith("/push/getReceipts") and calls[-1][1] == {"ids": ["t-ok", "t-dead", "t-missing"]}
    assert receipts == {"t-ok": {"status": "ok"},
                        "t-dead": {"status": "error", "error": "recipient", "name": "DeviceNotRegistered"}}
    for code, kind in ((429, "rate_limit"), (401, "config"), (503, "retry")):
        answers.append(_Resp(code, {}))
        with pytest.raises(PushSendError) as err:
            provider.get_receipts(["t"])
        assert err.value.kind == kind


def test_log_provider_has_no_tickets_so_nothing_is_polled():
    assert push_providers.LogProvider().get_receipts(["x"]) == {}
