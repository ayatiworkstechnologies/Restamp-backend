"""Push notifications (notification Phase 4): devices, preferences, provider
adapters, admin visibility.

No real push is ever sent: delivery tests live in test_push_delivery.py (the
in-memory provider). Device tokens are synthetic Expo-shaped strings — no
endpoint is contacted. The outbox is driven explicitly with push_svc
functions on the test session (the background loops are disabled in conftest).
Rollback-isolated `client` fixture on the local test database.
"""
import json

import pytest
from sqlalchemy import text

from app.models import PushDevice, PushOutbox
from app.services import notifications as notif_svc
from app.services import push as push_svc
from app.services import push_providers
from app.services.push_providers import PushSendError
from tests.conftest import auth_header
from tests.test_admin import _admin
from tests.test_notifications_events import _rows
from tests.test_visits import _buyer

_TOKEN_SEQ = {"n": 0}


def _token():
    """A unique, Expo-shaped synthetic token (validated by push_svc's regex)."""
    _TOKEN_SEQ["n"] += 1
    return f"ExponentPushToken[t{_TOKEN_SEQ['n']:021d}]"


def _user(c):
    out = _buyer(c)
    return out["user_id"], auth_header(out["access_token"])


def _register(c, h, token=None, platform="ios"):
    r = c.post("/users/me/push/devices", json={"token": token or _token(), "platform": platform}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture()
def pushprov():
    provider = push_providers.MemoryProvider()
    push_providers.set_provider_for_tests(provider)
    yield provider
    push_providers.set_provider_for_tests(None)


# --- authentication ------------------------------------------------------------------

ENDPOINTS = (
    ("GET", "/users/me/push", None),
    ("PUT", "/users/me/push/preferences", {"enquiries": True, "visits": True, "listings": True}),
    ("POST", "/users/me/push/devices", {"token": _token(), "platform": "ios"}),
    ("DELETE", "/users/me/push/devices/1", None),
)


def test_unauthenticated_requests_are_rejected(client):
    c, _ = client
    for method, path, body in ENDPOINTS:
        assert c.request(method, path, json=body).status_code == 401, path


def test_suspended_account_cannot_use_push_endpoints(client, pushprov):
    c, session = client
    uid, h = _user(c)
    session.execute(text("UPDATE users SET status='suspended' WHERE id=:u"), {"u": uid})
    session.flush()
    assert c.get("/users/me/push", headers=h).status_code == 403


# --- status --------------------------------------------------------------------------

def test_status_without_provider_or_devices(client):
    c, session = client
    uid, h = _user(c)
    body = c.get("/users/me/push", headers=h).json()
    assert body == {"available": False, "devices": [],
                    "preferences": {"enquiries": True, "visits": True, "listings": True}}
    assert session.get(PushDevice, 0) is None  # GET never writes


def test_available_reports_the_configured_provider(client, pushprov):
    c, _ = client
    uid, h = _user(c)
    assert c.get("/users/me/push", headers=h).json()["available"] is True


# --- device registration ---------------------------------------------------------------

def test_register_device_stores_only_encrypted_and_masked_forms(client, pushprov):
    c, session = client
    uid, h = _user(c)
    token = _token()
    body = _register(c, h, token)
    assert body["status"] == "ACTIVE" and body["platform"] == "ios" and body["invalid_reason"] is None
    assert token not in json.dumps(body)  # the raw token is never returned
    row = session.query(PushDevice).filter_by(user_id=uid).one()
    assert row.token_prefix != token and token not in row.token_prefix and token not in row.token_enc
    assert len(row.token_hash) == 64 and row.token_hash != token  # keyed by a hash, not the token
    assert push_svc._fernet().decrypt(row.token_enc.encode("ascii")).decode() == token  # recoverable only server-side


def test_reregistering_a_token_refreshes_the_same_row(client, pushprov):
    c, session = client
    uid, h = _user(c)
    token = _token()
    first = _register(c, h, token)
    c.delete(f"/users/me/push/devices/{first['id']}", headers=h)
    second = _register(c, h, token)
    assert second["id"] == first["id"] and second["status"] == "ACTIVE"  # reactivated, not duplicated
    assert session.query(PushDevice).filter_by(user_id=uid).count() == 1


def test_one_token_belongs_to_exactly_one_account(client, pushprov):
    """A device that signs into another account takes over its single row:
    the old account must never receive pushes for the new account's phone."""
    c, session = client
    a, ha = _user(c)
    b, hb = _user(c)
    token = _token()
    device = _register(c, ha, token)
    raw = push_svc._fernet().decrypt(
        session.query(PushDevice).filter_by(user_id=a).one().token_enc.encode("ascii")).decode()
    assert raw == token
    r = c.post("/users/me/push/devices", json={"token": raw, "platform": "ios"}, headers=hb)
    assert r.status_code == 201, r.text
    assert r.json()["id"] == device["id"]  # takeover, not a second row
    assert session.query(PushDevice).filter_by(user_id=a).count() == 0
    assert session.query(PushDevice).filter_by(user_id=b).count() == 1


def test_invalid_tokens_and_platforms_are_rejected_with_422(client, pushprov):
    c, _ = client
    _, h = _user(c)
    # Schema-level failures (length/platform) get the app-wide static body.
    for bad, platform in (("", "ios"), ("not-a-token", "ios"), ("ExponentPushToken[]", "ios"),
                          ("FCMToken[abc]", "ios"), ("ExponentPushToken[" + "x" * 200 + "]", "ios"),
                          (_token(), "web")):
        r = c.post("/users/me/push/devices", json={"token": bad, "platform": platform}, headers=h)
        assert r.status_code == 422 and r.json() == {"detail": "Invalid request"}, bad
    # Right length, wrong shape: the service's own static message.
    for bad in ("ExponentPushToken[short]", "ExponentPushToken[../path]",
                "ExponentPushToken[with space 12345678901]", "ExponentPushToken[ünicode123456789]"):
        r = c.post("/users/me/push/devices", json={"token": bad, "platform": "ios"}, headers=h)
        assert r.status_code == 422 and r.json() == {"detail": "Invalid push token or platform"}, bad


def test_deactivate_is_idempotent_and_scoped_to_the_caller(client, pushprov):
    c, session = client
    a, ha = _user(c)
    _, hb = _user(c)
    device = _register(c, ha)
    assert c.delete(f"/users/me/push/devices/{device['id']}", headers=hb).status_code == 404
    assert c.delete(f"/users/me/push/devices/{device['id']}", headers=ha).status_code == 204
    assert c.delete(f"/users/me/push/devices/{device['id']}", headers=ha).status_code == 204  # idempotent
    row = session.get(PushDevice, device["id"])
    session.refresh(row)
    assert row.status == "INACTIVE"


def test_two_devices_are_listed_masked(client, pushprov):
    c, _ = client
    uid, h = _user(c)
    t1, t2 = _register(c, h, platform="ios"), _register(c, h, platform="android")
    body = c.get("/users/me/push", headers=h).json()
    assert [d["platform"] for d in body["devices"]] == ["android", "ios"]  # newest first
    blob = json.dumps(body)
    for t in (t1, t2):
        assert t["token_prefix"] not in blob  # masked prefixes never reveal the token


# --- preferences (independent of email) --------------------------------------------------

def test_preferences_need_no_address_or_device_and_stay_independent(client, pushprov):
    c, session = client
    uid, h = _user(c)
    # Unlike email, there is no address requirement: 200 on a bare account.
    r = c.put("/users/me/push/preferences", json={"enquiries": False, "visits": True, "listings": False}, headers=h)
    assert r.status_code == 200 and r.json()["preferences"] == {"enquiries": False, "visits": True, "listings": False}
    # Email preferences are untouched (email is even unset for this user).
    assert c.get("/users/me/email", headers=h).json()["email"] is None
    r = c.put("/users/me/push/preferences", json={"enquiries": "maybe"}, headers=h).status_code
    assert c.get("/users/me/push", headers=h).json()["preferences"] == {"enquiries": False, "visits": True, "listings": False}


def test_push_preferences_do_not_touch_email_preferences(client, pushprov):
    c, _ = client
    uid, h = _user(c)
    r = c.put("/users/me/email", json={"email": "both@example.com"}, headers=h)
    assert r.status_code == 200, r.text
    c.put("/users/me/email/verify", json={"code": r.json()["debug_code"]}, headers=h)
    c.put("/users/me/email/preferences", json={"enquiries": False, "visits": False, "listings": False}, headers=h)
    assert c.put("/users/me/push/preferences", json={"enquiries": False, "visits": False, "listings": False},
                 headers=h).status_code == 200
    email_state = c.get("/users/me/email", headers=h).json()["preferences"]
    push_state = c.get("/users/me/push", headers=h).json()["preferences"]
    assert email_state == push_state == {"enquiries": False, "visits": False, "listings": False}  # changed independently
    c.put("/users/me/push/preferences", json={"enquiries": True, "visits": True, "listings": True}, headers=h)
    assert c.get("/users/me/email", headers=h).json()["preferences"]["enquiries"] is False  # email unaffected


def test_opted_out_category_queues_no_push_but_keeps_in_app(client, pushprov):
    c, session = client
    uid, h = _user(c)
    _register(c, h)
    c.put("/users/me/push/preferences", json={"enquiries": True, "visits": False, "listings": True}, headers=h)
    mk = lambda t, k: notif_svc.notify_safely(session, t, recipient_id=uid, actor_id=None, entity_id=1,
                                              audience="BUYER", dedupe_key=k)
    assert mk("VISIT_CONFIRMED", "pp:1") is not None
    assert mk("ENQUIRY_REPLY", "pp:2") is not None
    rows = session.query(PushOutbox).filter_by(user_id=uid).order_by(PushOutbox.id).all()
    assert [(r.category, r.template) for r in rows] == [("ENQUIRY", "ENQUIRY_REPLY")]  # VISIT skipped
    assert len(_rows(session, uid)) == 2  # in-app notifications are unaffected by push preferences


def test_account_push_is_mandatory(client, pushprov):
    c, session = client
    uid, h = _user(c)
    _register(c, h)
    c.put("/users/me/push/preferences", json={"enquiries": False, "visits": False, "listings": False}, headers=h)
    assert notif_svc.notify_safely(session, "ACCOUNT_REACTIVATED", recipient_id=uid, actor_id=None,
                                   entity_id=uid, dedupe_key="acct:1") is not None
    rows = session.query(PushOutbox).filter_by(user_id=uid).all()
    assert [(r.category, r.template) for r in rows] == [("ACCOUNT", "ACCOUNT_REACTIVATED")]


def test_no_device_means_nothing_is_queued(client, pushprov):
    c, session = client
    uid, h = _user(c)
    assert notif_svc.notify_safely(session, "ENQUIRY_REPLY", recipient_id=uid, actor_id=None,
                                   entity_id=1, audience="BUYER", dedupe_key="nd:1") is not None
    assert session.query(PushOutbox).filter_by(user_id=uid).all() == []
    # A device registered AFTER the event only affects future notifications.
    _register(c, h)
    assert notif_svc.notify_safely(session, "ENQUIRY_REPLY", recipient_id=uid, actor_id=None,
                                   entity_id=1, audience="BUYER", dedupe_key="nd:2") is not None
    assert [r.template for r in session.query(PushOutbox).filter_by(user_id=uid).all()] == ["ENQUIRY_REPLY"]


# --- admin visibility ---------------------------------------------------------------------

def test_admin_sees_push_deliveries_without_secrets(client, pushprov):
    c, session = client
    admin_h, _ = _admin(c, session)
    uid, h = _user(c)
    token = _register(c, h)["token_prefix"]
    notif_svc.notify_safely(session, "LISTING_VERIFIED", recipient_id=uid, actor_id=None, entity_id=1,
                            dedupe_key="adm:1")
    r = c.get("/admin/push-deliveries", params={"user_id": uid}, headers=admin_h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1 and body["items"][0]["status"] == "PENDING" and body["items"][0]["user_id"] == uid
    blob = json.dumps(body)
    assert token not in blob and "token_enc" not in blob and "payload" not in blob


def test_push_deliveries_require_admin_permission(client, pushprov):
    c, session = client
    _, h = _user(c)
    assert c.get("/admin/push-deliveries").status_code == 401
    assert c.get("/admin/push-deliveries", headers=h).status_code == 403
    support_h, _ = _admin(c, session, admin_role="SUPPORT")
    assert c.get("/admin/push-deliveries", headers=support_h).status_code == 403  # no audit-log permission


# --- provider adapters (no network) ---------------------------------------------------------

def test_provider_selection_fails_closed(monkeypatch):
    s = push_providers.settings
    monkeypatch.setattr(s, "PUSH_PROVIDER", "disabled")
    assert push_providers.get_provider().enabled is False
    for choice in ("expo-typo", "", None):
        monkeypatch.setattr(s, "PUSH_PROVIDER", choice)
        assert push_providers.get_provider().enabled is False  # unknown -> disabled
    monkeypatch.setattr(s, "PUSH_PROVIDER", "expo")
    assert push_providers.get_provider().name == "expo"  # Expo needs no credential to queue tickets
    monkeypatch.setattr(s, "PUSH_PROVIDER", "log")
    assert push_providers.get_provider().name == "log"
    monkeypatch.setattr(push_providers, "is_production", lambda: True)
    assert push_providers.get_provider().enabled is False  # development-only adapter
    monkeypatch.setattr(push_providers, "is_production", lambda: False)


class _Resp:
    def __init__(self, code, payload=None):
        self.status_code = code
        self._payload = payload or {}

    def json(self):
        return self._payload


def test_expo_adapter_maps_outcomes_without_network(monkeypatch):
    seen = {}

    def fake_post(url, headers, json, timeout):
        seen.update(url=url, headers=headers, json=json)
        return _Resp(200, {"data": [seen["ticket"]]})

    monkeypatch.setattr(push_providers.httpx, "post", fake_post)
    provider = push_providers.ExpoProvider("exp_secret", 15)
    message = push_providers.PushMessage("ExponentPushToken[a]", "T", "B", {"target": "NONE"})
    seen["ticket"] = {"status": "ok", "id": "ticket-1"}
    assert provider.send(message, "restamp-push-1-1") == "ticket-1"
    assert seen["url"] == "https://exp.host/--/api/v2/push/send"
    assert seen["json"]["to"] == "ExponentPushToken[a]" and seen["json"]["data"] == {"target": "NONE"}
    assert seen["headers"]["Authorization"] == "Bearer exp_secret"  # credentials ride only in headers
    assert "exp_secret" not in json.dumps(seen["json"])
    for ticket, kind in (
        ({"status": "error", "details": {"error": "DeviceNotRegistered"}}, "recipient"),
        ({"status": "error", "details": {"error": "MessageRateExceeded"}}, "rate_limit"),
        ({"status": "error", "details": {"error": "MessageTooBig"}}, "invalid_message"),
        ({"status": "error", "details": {"error": "something else"}}, "retry"),
    ):
        seen["ticket"] = ticket
        with pytest.raises(PushSendError) as err:
            provider.send(message, "k")
        assert err.value.kind == kind
    for code, kind in ((429, "rate_limit"), (401, "config"), (403, "config"), (500, "retry"), (503, "retry")):
        monkeypatch.setattr(push_providers.httpx, "post", lambda *a, **k: _Resp(code))
        with pytest.raises(PushSendError) as err:
            provider.send(message, "k")
        assert err.value.kind == kind

    def timeout_post(*a, **k):
        raise push_providers.httpx.ConnectTimeout("slow")

    monkeypatch.setattr(push_providers.httpx, "post", timeout_post)
    with pytest.raises(PushSendError) as err:
        provider.send(message, "k")
    assert err.value.kind == "retry"


def test_mask_token():
    token = "ExponentPushToken[abcdefghij1234567890]"
    masked = push_svc.mask_token(token)
    assert masked != token and token[20:40] not in masked and "…" in masked
    assert push_svc.mask_token("short") == "shor…"
