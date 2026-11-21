"""Email notifications (notification Phase 3): address, verification, preferences,
event emails, outbox delivery/retries, admin visibility, provider adapters.

No real email is ever sent: every test uses the in-memory provider (or a
monkeypatched HTTP/SMTP transport). The outbox is driven explicitly with
email_svc.process_outbox on the test session (the background loop is disabled
in conftest). Rollback-isolated `client` fixture on the local test database.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.models import EmailOutbox, EmailVerificationCode, UserEmail
from app.services import email as email_svc
from app.services import email_providers, email_templates
from app.services import notifications as notif_svc
from app.services.email_providers import EmailSendError, MemoryProvider
from tests.conftest import auth_header
from tests.test_admin import _admin
from tests.test_notifications_events import SECRET, _enquiry_id, _pair, _rows
from tests.test_visits import FUTURE, _buyer

ENDPOINTS = (
    ("GET", "/users/me/email", None),
    ("PUT", "/users/me/email", {"email": "a@example.com"}),
    ("POST", "/users/me/email/verification", None),
    ("POST", "/users/me/email/verify", {"code": "123456"}),
    ("PUT", "/users/me/email/preferences", {"enquiries": True, "visits": True, "listings": True}),
    ("DELETE", "/users/me/email", None),
)


@pytest.fixture()
def mail():
    provider = MemoryProvider()
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
    return email_svc.process_outbox(session_factory=lambda: _Shared(session))


def _user(c):
    out = _buyer(c)
    return out["user_id"], auth_header(out["access_token"])


def _verify(c, h, address):
    r = c.put("/users/me/email", json={"email": address}, headers=h)
    assert r.status_code == 200, r.text
    code = r.json()["debug_code"]
    assert code and len(code) == 6
    r = c.post("/users/me/email/verify", json={"code": code}, headers=h)
    assert r.status_code == 200 and r.json()["verified"] is True, r.text
    return code


def _outbox(session, user_id, template=None):
    session.expire_all()
    q = session.query(EmailOutbox).filter(EmailOutbox.user_id == user_id)
    if template:
        q = q.filter(EmailOutbox.template == template)
    return q.order_by(EmailOutbox.id).all()


def _age_codes(session, user_id, seconds):
    session.execute(text("UPDATE email_verification_codes SET created_at = created_at - INTERVAL :s SECOND "
                         "WHERE user_id = :u"), {"s": seconds, "u": user_id})
    session.flush()


# --- validation ---------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("  User.Name+tag@Example.COM ", "user.name+tag@example.com"),
    ("a@b.co", "a@b.co"),
    ("first_last@sub.domain.in", "first_last@sub.domain.in"),
])
def test_valid_addresses_are_normalised(raw, expected):
    assert email_svc.normalize_email(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "plain", "a@b", "a@@b.com", "@b.com", "a@.com", "a..b@c.com", ".a@c.com", "a@c..com",
    "a b@c.com", "a@c.com\nBcc: x@y.com", "a@c.com\r\nX: y", "a@-c.com", "a@c.123", "ä@c.com", "a@exämple.com",
    "x" * 65 + "@c.com", "a@" + "c" * 250 + ".com", "a@c.c", "<a@c.com>", "a@c.com,b@c.com",
])
def test_invalid_addresses_are_rejected(raw):
    with pytest.raises(ValueError):
        email_svc.normalize_email(raw)


def test_api_rejects_invalid_address_with_static_message(client, mail):
    c, session = client
    uid, h = _user(c)
    r = c.put("/users/me/email", json={"email": "not-an-email"}, headers=h)
    assert r.status_code == 422 and r.json() == {"detail": "Invalid email address"}
    assert session.get(UserEmail, uid) is None


# --- authentication / isolation ----------------------------------------------------------

def test_unauthenticated_requests_are_rejected(client):
    c, _ = client
    for method, path, body in ENDPOINTS:
        assert c.request(method, path, json=body).status_code == 401, path


def test_suspended_account_cannot_use_email_endpoints(client, mail):
    c, session = client
    uid, h = _user(c)
    session.execute(text("UPDATE users SET status='suspended' WHERE id=:u"), {"u": uid})
    session.flush()
    assert c.get("/users/me/email", headers=h).status_code == 403


def test_users_never_see_or_change_each_others_email(client, mail):
    c, session = client
    a, ha = _user(c)
    b, hb = _user(c)
    _verify(c, ha, "alice@example.com")
    assert c.get("/users/me/email", headers=hb).json()["email"] is None
    assert c.put("/users/me/email/preferences", json={"enquiries": False, "visits": False, "listings": False},
                 headers=hb).status_code == 409  # B has no address; A's row is untouched
    assert c.delete("/users/me/email", headers=hb).status_code == 204
    state = c.get("/users/me/email", headers=ha).json()
    assert state["email"] == "alice@example.com" and state["verified"] is True
    assert state["preferences"] == {"enquiries": True, "visits": True, "listings": True}


def test_get_never_writes(client, mail):
    c, session = client
    uid, h = _user(c)
    assert c.get("/users/me/email", headers=h).status_code == 200
    assert session.get(UserEmail, uid) is None and _outbox(session, uid) == []


# --- verification ----------------------------------------------------------------------

def test_set_email_queues_verification_and_stores_only_a_hash(client, mail):
    c, session = client
    uid, h = _user(c)
    r = c.put("/users/me/email", json={"email": "New.User@Example.com"}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["email"] == "new.user@example.com" and body["verified"] is False and body["delivery_available"] is True
    code = body["debug_code"]  # development-only convention
    record = session.query(EmailVerificationCode).filter_by(user_id=uid).one()
    assert record.code_hash != code and code not in record.code_hash and len(record.code_hash) == 64
    rows = _outbox(session, uid)
    assert [(r.category, r.template, r.status) for r in rows] == [("VERIFICATION", "VERIFY_EMAIL", "PENDING")]
    assert code not in json.dumps(rows[0].payload)  # queued encrypted, never in clear text
    assert _run(session)["sent"] == 1
    message, key = mail.sent[0]
    assert message.to == "new.user@example.com" and code in message.text and code in message.html
    assert message.subject == "Verify your email address for RESTAMP" and key == f"restamp-outbox-{rows[0].id}"
    row = _outbox(session, uid)[0]
    assert row.status == "SENT" and row.sent_at is not None and row.provider == "memory"
    assert row.provider_message_id == "mem-1" and row.payload is None  # code wiped after sending


def test_verify_success_then_code_is_single_use(client, mail):
    c, session = client
    uid, h = _user(c)
    code = _verify(c, h, "single@example.com")
    assert session.get(UserEmail, uid).verified_at is not None
    assert session.query(EmailVerificationCode).filter_by(user_id=uid).one().used_at is not None
    # Change away and back: the old (used) code can never verify again.
    c.put("/users/me/email", json={"email": "other@example.com"}, headers=h)
    _age_codes(session, uid, 120)
    c.put("/users/me/email", json={"email": "single@example.com"}, headers=h)
    r = c.post("/users/me/email/verify", json={"code": code}, headers=h)
    assert r.status_code == 422 and r.json() == {"detail": "Invalid or expired code"}
    assert c.get("/users/me/email", headers=h).json()["verified"] is False


def test_expired_code_is_rejected(client, mail):
    c, session = client
    uid, h = _user(c)
    code = c.put("/users/me/email", json={"email": "late@example.com"}, headers=h).json()["debug_code"]
    session.execute(text("UPDATE email_verification_codes SET expires_at = UTC_TIMESTAMP() - INTERVAL 1 SECOND "
                         "WHERE user_id=:u"), {"u": uid})
    session.flush()
    r = c.post("/users/me/email/verify", json={"code": code}, headers=h)
    assert r.status_code == 422 and r.json() == {"detail": "Invalid or expired code"}
    assert session.get(UserEmail, uid).verified_at is None


def test_wrong_codes_are_counted_and_exhaust_the_code(client, mail):
    c, session = client
    uid, h = _user(c)
    code = c.put("/users/me/email", json={"email": "guess@example.com"}, headers=h).json()["debug_code"]
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(email_svc.CODE_MAX_ATTEMPTS):
        assert c.post("/users/me/email/verify", json={"code": wrong}, headers=h).status_code == 422
    session.expire_all()
    record = session.query(EmailVerificationCode).filter_by(user_id=uid).one()
    assert record.attempts == email_svc.CODE_MAX_ATTEMPTS and record.used_at is not None
    # Even the right code is dead now; a new one must be requested.
    assert c.post("/users/me/email/verify", json={"code": code}, headers=h).status_code == 422
    assert c.get("/users/me/email", headers=h).json()["verified"] is False


def test_malformed_code_is_a_generic_422(client, mail):
    c, _ = client
    _, h = _user(c)
    c.put("/users/me/email", json={"email": "shape@example.com"}, headers=h)
    for bad in ("12345", "1234567", "abcdef", "12 456"):
        assert c.post("/users/me/email/verify", json={"code": bad}, headers=h).status_code == 422


def test_code_from_another_account_never_verifies(client, mail):
    c, session = client
    a, ha = _user(c)
    b, hb = _user(c)
    code_a = c.put("/users/me/email", json={"email": "shared@example.com"}, headers=ha).json()["debug_code"]
    code_b = c.put("/users/me/email", json={"email": "shared@example.com"}, headers=hb).json()["debug_code"]
    if code_a != code_b:  # (a 1-in-a-million collision would make this check meaningless)
        r = c.post("/users/me/email/verify", json={"code": code_a}, headers=hb)
        assert r.status_code == 422 and r.json() == {"detail": "Invalid or expired code"}
        assert session.get(UserEmail, b).verified_at is None
    # The hash is bound to the account: A's stored hash differs from B's even for one code.
    assert email_svc._code_hash(a, "shared@example.com", "123456") != email_svc._code_hash(b, "shared@example.com", "123456")
    assert c.post("/users/me/email/verify", json={"code": code_a}, headers=ha).status_code == 200


def test_verification_requests_are_rate_limited(client, mail):
    c, session = client
    uid, h = _user(c)
    assert c.put("/users/me/email", json={"email": "rate@example.com"}, headers=h).status_code == 200
    r = c.post("/users/me/email/verification", headers=h)
    assert r.status_code == 429 and r.json() == {"detail": "Too many requests"}  # one per minute
    assert c.put("/users/me/email", json={"email": "rate2@example.com"}, headers=h).status_code == 429
    for _ in range(email_svc.SEND_MAX_PER_HOUR - 1):
        _age_codes(session, uid, 61)
        assert c.post("/users/me/email/verification", headers=h).status_code == 200
    _age_codes(session, uid, 61)
    assert c.post("/users/me/email/verification", headers=h).status_code == 429  # hourly cap
    assert session.query(EmailVerificationCode).filter_by(user_id=uid).count() == email_svc.SEND_MAX_PER_HOUR


def test_replaced_code_email_is_not_sent(client, mail):
    c, session = client
    uid, h = _user(c)
    c.put("/users/me/email", json={"email": "twice@example.com"}, headers=h)
    _age_codes(session, uid, 61)
    assert c.post("/users/me/email/verification", headers=h).status_code == 200
    counts = _run(session)
    assert counts["sent"] == 1 and counts["dropped"] == 1
    first, second = _outbox(session, uid)
    assert first.last_error == "superseded" and first.payload is None and second.status == "SENT"


def test_resend_invalidates_the_previous_code(client, mail):
    c, session = client
    uid, h = _user(c)
    first = c.put("/users/me/email", json={"email": "resend@example.com"}, headers=h).json()["debug_code"]
    _age_codes(session, uid, 61)
    second = c.post("/users/me/email/verification", headers=h).json()["debug_code"]
    if first != second:
        assert c.post("/users/me/email/verify", json={"code": first}, headers=h).status_code == 422
    assert c.post("/users/me/email/verify", json={"code": second}, headers=h).status_code == 200


def test_resend_without_address_and_when_already_verified(client, mail):
    c, session = client
    uid, h = _user(c)
    assert c.post("/users/me/email/verification", headers=h).status_code == 409
    _verify(c, h, "done@example.com")
    before = session.query(EmailVerificationCode).filter_by(user_id=uid).count()
    r = c.post("/users/me/email/verification", headers=h)
    assert r.status_code == 200 and r.json()["verified"] is True and r.json()["debug_code"] is None
    assert session.query(EmailVerificationCode).filter_by(user_id=uid).count() == before


def test_changing_the_address_requires_verification_again(client, mail):
    c, session = client
    owner_id, oh, buyer_id, bh, lid = _pair(c, "Change Flat")
    _verify(c, oh, "owner.old@example.com")
    _age_codes(session, owner_id, 120)
    r = c.put("/users/me/email", json={"email": "owner.new@example.com"}, headers=oh)
    assert r.status_code == 200 and r.json()["verified"] is False and r.json()["email"] == "owner.new@example.com"
    # Unverified: the event creates the in-app notification but no email.
    assert c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": "hi"}, headers=bh).status_code == 201
    assert len(_rows(session, owner_id, "ENQUIRY_NEW")) == 1
    assert _outbox(session, owner_id, "ENQUIRY_NEW") == []


def test_resubmitting_the_verified_address_is_a_noop(client, mail):
    c, session = client
    uid, h = _user(c)
    _verify(c, h, "same@example.com")
    count = session.query(EmailVerificationCode).filter_by(user_id=uid).count()
    r = c.put("/users/me/email", json={"email": " SAME@example.com "}, headers=h)
    assert r.status_code == 200 and r.json()["verified"] is True and r.json()["debug_code"] is None
    assert session.query(EmailVerificationCode).filter_by(user_id=uid).count() == count


def test_same_address_on_two_accounts_gives_identical_answers(client, mail):
    """No uniqueness -> no enumeration: nothing reveals that an address is in use."""
    c, session = client
    a, ha = _user(c)
    b, hb = _user(c)
    _verify(c, ha, "dup@example.com")
    r = c.put("/users/me/email", json={"email": "dup@example.com"}, headers=hb)
    fresh = c.put("/users/me/email", json={"email": "never.used@example.com"}, headers=_user(c)[1])
    strip = lambda j: {k: v for k, v in j.items() if k not in ("debug_code", "email")}
    assert r.status_code == fresh.status_code == 200 and strip(r.json()) == strip(fresh.json())
    assert c.post("/users/me/email/verify", json={"code": r.json()["debug_code"]}, headers=hb).status_code == 200
    assert session.get(UserEmail, a).verified_at is not None and session.get(UserEmail, b).verified_at is not None


def test_remove_email_stops_delivery(client, mail):
    c, session = client
    uid, h = _user(c)
    _verify(c, h, "bye@example.com")
    assert c.delete("/users/me/email", headers=h).status_code == 204
    assert c.delete("/users/me/email", headers=h).status_code == 204  # idempotent
    state = c.get("/users/me/email", headers=h).json()
    assert state["email"] is None and state["verified"] is False
    assert notif_svc.notify_safely(session, "LISTING_VERIFIED", recipient_id=uid, actor_id=None, entity_id=1,
                                   dedupe_key="rm:1") is not None
    assert _outbox(session, uid, "LISTING_VERIFIED") == []


def test_production_never_returns_a_code_and_disabled_provider_issues_none(client, monkeypatch):
    c, session = client
    uid, h = _user(c)
    # Not a development environment + no provider: the address is saved,
    # delivery is reported unavailable, and no code exists at all.
    monkeypatch.setattr("app.routers.email.is_development", lambda: False)
    monkeypatch.setattr(email_svc, "is_development", lambda: False)
    r = c.put("/users/me/email", json={"email": "prod@example.com"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["debug_code"] is None and r.json()["delivery_available"] is False and r.json()["verified"] is False
    assert session.query(EmailVerificationCode).filter_by(user_id=uid).count() == 0
    assert _outbox(session, uid) == []
    # With a provider, a code is issued and emailed — but still never returned.
    provider = MemoryProvider()
    email_providers.set_provider_for_tests(provider)
    try:
        r = c.post("/users/me/email/verification", headers=h)
        assert r.status_code == 200 and r.json()["debug_code"] is None and r.json()["delivery_available"] is True
        assert "debug_code" not in {k for k, v in r.json().items() if v}
        assert _run(session)["sent"] == 1 and len(provider.sent) == 1
    finally:
        email_providers.set_provider_for_tests(None)


# --- preferences ----------------------------------------------------------------------

def test_preferences_apply_to_future_events_and_not_to_mandatory_email(client, mail):
    c, session = client
    uid, h = _user(c)
    _verify(c, h, "prefs@example.com")
    r = c.put("/users/me/email/preferences", json={"enquiries": False, "visits": True, "listings": False}, headers=h)
    assert r.status_code == 200 and r.json()["preferences"] == {"enquiries": False, "visits": True, "listings": False}
    mk = lambda t, k: notif_svc.notify_safely(session, t, recipient_id=uid, actor_id=None, entity_id=1,
                                              audience="BUYER", dedupe_key=k)
    assert mk("ENQUIRY_REPLY", "p:1") is not None and mk("LISTING_VERIFIED", "p:2") is not None
    assert mk("VISIT_CONFIRMED", "p:3") is not None and mk("ACCOUNT_REACTIVATED", "p:4") is not None
    queued = sorted(r.template for r in _outbox(session, uid) if r.category != "VERIFICATION")
    assert queued == ["ACCOUNT_REACTIVATED", "VISIT_CONFIRMED"]  # opted-out categories skipped; account email is mandatory
    assert len(_rows(session, uid)) == 4  # in-app notifications are unaffected by email preferences


def test_preferences_require_an_address_and_reject_bad_bodies(client, mail):
    c, _ = client
    _, h = _user(c)
    assert c.put("/users/me/email/preferences", json={"enquiries": True, "visits": True, "listings": True},
                 headers=h).status_code == 409
    _verify(c, h, "p2@example.com")
    assert c.put("/users/me/email/preferences", json={"enquiries": "maybe"}, headers=h).status_code == 422


# --- event emails ----------------------------------------------------------------------

def test_new_enquiry_emails_the_verified_owner_only(client, mail):
    c, session = client
    owner_id, oh, buyer_id, bh, lid = _pair(c, "Lake <b>View</b> Flat")
    _verify(c, oh, "owner@example.com")
    _verify(c, bh, "buyer@example.com")
    _run(session)
    mail.sent.clear()
    r = c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": SECRET}, headers=bh)
    assert r.status_code == 201, r.text
    rows = _outbox(session, owner_id, "ENQUIRY_NEW")
    assert len(rows) == 1 and rows[0].category == "ENQUIRY" and rows[0].to_email == "owner@example.com"
    notification = _rows(session, owner_id, "ENQUIRY_NEW")
    assert len(notification) == 1 and rows[0].notification_id == notification[0]["id"]  # one in-app row, linked
    assert _outbox(session, buyer_id, "ENQUIRY_NEW") == []  # the actor is never emailed
    assert _run(session)["sent"] == 1
    message = mail.sent[0][0]
    assert message.to == "owner@example.com" and message.subject == "New enquiry on your RESTAMP listing"
    for blob in (message.text, message.html):
        assert "9876543210" not in blob and "call me" not in blob  # no message text, no phone number
    assert "<b>View</b>" not in message.html and "&lt;b&gt;View&lt;/b&gt;" in message.html  # title is escaped
    assert "Lake <b>View</b> Flat" in message.text


def test_unverified_or_absent_address_gets_no_email_but_keeps_in_app(client, mail):
    c, session = client
    owner_id, oh, buyer_id, bh, lid = _pair(c, "Quiet Flat")
    c.put("/users/me/email", json={"email": "pending@example.com"}, headers=oh)  # never verified
    assert c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": "hello"}, headers=bh).status_code == 201
    assert len(_rows(session, owner_id, "ENQUIRY_NEW")) == 1
    assert [r.template for r in _outbox(session, owner_id)] == ["VERIFY_EMAIL"]


def test_follow_up_coalescing_and_retries_never_duplicate_email(client, mail):
    c, session = client
    owner_id, oh, buyer_id, bh, lid = _pair(c, "Coalesce Flat")
    _verify(c, oh, "co@example.com")
    for msg in ("first", "second", "third"):
        assert c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": msg},
                      headers=bh).status_code in (200, 201)
    event_rows = [r for r in _outbox(session, owner_id) if r.category == "ENQUIRY"]
    assert [r.template for r in event_rows] == ["ENQUIRY_NEW"]  # follow-ups coalesced: no extra email
    assert len(_rows(session, owner_id)) == 1
    # A retried event (same dedupe key) creates neither a notification nor an email.
    for _ in range(2):
        notif_svc.notify_safely(session, "LISTING_VERIFIED", recipient_id=owner_id, actor_id=None, entity_id=lid,
                                listing_id=lid, dedupe_key="retry:listing")
    assert len(_outbox(session, owner_id, "LISTING_VERIFIED")) == 1
    assert len(_rows(session, owner_id, "LISTING_VERIFIED")) == 1


def test_reply_and_visit_flow_emails_the_other_party(client, mail):
    c, session = client
    owner_id, oh, buyer_id, bh, lid = _pair(c, "Flow Flat")
    _verify(c, oh, "o@example.com")
    _verify(c, bh, "b@example.com")
    assert c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": "hi"}, headers=bh).status_code == 201
    eid = _enquiry_id(session, buyer_id, lid)
    assert c.post(f"/owner/leads/{eid}/messages", json={"message": "hello"}, headers=oh).status_code == 201
    assert c.patch(f"/owner/leads/{eid}", json={"status": "CONTACTED"}, headers=oh).status_code == 200
    v = c.post("/buyer/visits", json={"property_listing_id": lid, "scheduled_at": FUTURE()}, headers=bh)
    assert v.status_code == 201, v.text
    assert c.patch(f"/owner/visits/{v.json()['id']}", json={"status": "CONFIRMED"}, headers=oh).status_code == 200
    owner = sorted(r.template for r in _outbox(session, owner_id) if r.category != "VERIFICATION")
    buyer = sorted(r.template for r in _outbox(session, buyer_id) if r.category != "VERIFICATION")
    assert owner == ["ENQUIRY_NEW", "VISIT_REQUESTED"]
    assert buyer == ["ENQUIRY_REPLY", "LEAD_CONTACTED", "VISIT_CONFIRMED"]
    counts = _run(session)
    # 5 event emails go out; the 2 verification emails are dropped because both
    # addresses were verified (with the development code) before the worker ran.
    assert counts["sent"] == 5 and counts["dropped"] == 2 and counts["failed"] == 0
    verify_rows = [r for r in _outbox(session, owner_id) + _outbox(session, buyer_id) if r.category == "VERIFICATION"]
    assert [r.last_error for r in verify_rows] == ["already_verified", "already_verified"]
    assert all(r.payload is None for r in verify_rows)  # encrypted codes wiped
    for message, _ in mail.sent:
        assert "hello" not in message.text and "hello" not in message.html  # the reply text never reaches an email


# The two reminder templates are excluded here since Phase 4 revision 2: a
# reminder email is only sent while its visit/enquiry is still eligible, which
# needs real facts — covered by tests/test_reminder_eligibility.py.
@pytest.mark.parametrize("template", sorted(t for t in email_templates.TEMPLATES
                                            if t not in ("VERIFY_EMAIL", "ACCOUNT_SUSPENDED", "VISIT_REMINDER", "LEAD_REMINDER")))
def test_every_notification_type_has_an_email(client, mail, template):
    c, session = client
    uid, h = _user(c)
    _verify(c, h, "all@example.com")
    _run(session)
    mail.sent.clear()
    row = notif_svc.notify_safely(session, template, recipient_id=uid, actor_id=None, entity_id=7,
                                  audience="BUYER", dedupe_key=f"type:{template}", when=datetime(2026, 12, 1, 5, 30))
    assert row is not None
    queued = _outbox(session, uid, template)
    assert len(queued) == 1 and queued[0].category == email_templates.TEMPLATES[template].category
    assert _run(session)["sent"] == 1
    message = mail.sent[0][0]
    assert message.subject == email_templates.TEMPLATES[template].subject
    assert email_templates.TEMPLATES[template].heading in message.text and "RESTAMP" in message.html


def test_email_failure_never_breaks_the_business_operation(client, mail, monkeypatch):
    c, session = client
    owner_id, oh, buyer_id, bh, lid = _pair(c, "Robust Flat")
    _verify(c, oh, "robust@example.com")

    def boom(*a, **k):
        raise RuntimeError("email is down")

    monkeypatch.setattr(email_svc, "_deliverable_address", boom)
    r = c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": "hi"}, headers=bh)
    assert r.status_code == 201, r.text
    assert len(_rows(session, owner_id, "ENQUIRY_NEW")) == 1  # the in-app notification is intact
    assert _outbox(session, owner_id, "ENQUIRY_NEW") == []


def test_no_provider_means_nothing_is_queued_or_sent(client):
    c, session = client
    owner_id, oh, buyer_id, bh, lid = _pair(c, "Dark Flat")
    session.add(UserEmail(user_id=owner_id, email="dark@example.com", verified_at=datetime(2026, 1, 1),
                          created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1)))
    session.flush()
    assert c.get("/users/me/email", headers=oh).json()["delivery_available"] is False
    assert c.post("/buyer/enquiries", json={"property_listing_id": lid, "message": "hi"}, headers=bh).status_code == 201
    assert len(_rows(session, owner_id, "ENQUIRY_NEW")) == 1 and _outbox(session, owner_id) == []
    assert _run(session) == {"sent": 0, "retry": 0, "failed": 0, "dropped": 0, "skipped": 0}


# --- account status --------------------------------------------------------------------

def test_suspension_and_reactivation_emails(client, mail):
    c, session = client
    admin_h, admin_id = _admin(c, session)
    uid, h = _user(c)
    _verify(c, h, "acct@example.com")
    c.put("/users/me/email/preferences", json={"enquiries": False, "visits": False, "listings": False}, headers=h)
    _run(session)
    mail.sent.clear()
    # An optional email is already queued when the account is suspended.
    session.execute(text("UPDATE user_emails SET pref_visits=1 WHERE user_id=:u"), {"u": uid})
    notif_svc.notify_safely(session, "VISIT_CONFIRMED", recipient_id=uid, actor_id=None, entity_id=1,
                            audience="BUYER", dedupe_key="acct:visit")
    r = c.post(f"/admin/users/{uid}/suspend", json={"reason": "PRIVATE-ADMIN-REASON"}, headers=admin_h)
    assert r.status_code == 200, r.text
    assert len(_outbox(session, uid, "ACCOUNT_SUSPENDED")) == 1
    assert _rows(session, uid, "ACCOUNT_SUSPENDED") == []  # email only: no in-app notice
    assert c.post(f"/admin/users/{uid}/suspend", json={"reason": "again"}, headers=admin_h).status_code == 200
    assert len(_outbox(session, uid, "ACCOUNT_SUSPENDED")) == 1  # same-state repeat adds nothing
    counts = _run(session)
    assert counts["sent"] == 1 and counts["dropped"] == 1  # suspension sent; the optional email is dropped
    message = mail.sent[0][0]
    assert message.subject == "Your RESTAMP account has been suspended" and "PRIVATE-ADMIN-REASON" not in message.html
    dropped = _outbox(session, uid, "VISIT_CONFIRMED")[0]
    assert dropped.status == "FAILED" and dropped.last_error == "account_inactive"
    assert c.post(f"/admin/users/{uid}/reactivate", json={"reason": "ok"}, headers=admin_h).status_code == 200
    assert len(_outbox(session, uid, "ACCOUNT_REACTIVATED")) == 1 and len(_rows(session, uid, "ACCOUNT_REACTIVATED")) == 1
    assert _run(session)["sent"] == 1
    assert mail.sent[-1][0].subject == "Your RESTAMP account has been reactivated"


def test_closed_account_receives_nothing(client, mail):
    c, session = client
    uid, h = _user(c)
    _verify(c, h, "closed@example.com")
    _run(session)
    mail.sent.clear()
    notif_svc.notify_safely(session, "ACCOUNT_REACTIVATED", recipient_id=uid, actor_id=None, entity_id=uid,
                            dedupe_key="closed:1")
    session.execute(text("UPDATE users SET status='closed' WHERE id=:u"), {"u": uid})
    session.flush()
    assert _run(session)["dropped"] == 1 and mail.sent == []
    assert _outbox(session, uid, "ACCOUNT_REACTIVATED")[0].last_error == "account_closed"


def test_eligibility_is_rechecked_when_sending(client, mail):
    c, session = client
    uid, h = _user(c)
    _verify(c, h, "first@example.com")
    _run(session)
    mail.sent.clear()
    mk = lambda t, k: notif_svc.notify_safely(session, t, recipient_id=uid, actor_id=None, entity_id=1,
                                              audience="BUYER", dedupe_key=k)
    mk("VISIT_CONFIRMED", "re:1")
    c.put("/users/me/email/preferences", json={"enquiries": True, "visits": False, "listings": True}, headers=h)
    assert _run(session)["dropped"] == 1 and _outbox(session, uid, "VISIT_CONFIRMED")[0].last_error == "opted_out"
    mk("ENQUIRY_REPLY", "re:2")
    _age_codes(session, uid, 120)
    c.put("/users/me/email", json={"email": "second@example.com"}, headers=h)  # address changed after queuing
    counts = _run(session)
    assert _outbox(session, uid, "ENQUIRY_REPLY")[0].last_error == "address_changed"
    assert [m.to for m, _ in mail.sent] == ["second@example.com"]  # only the new verification email went out
    assert counts["sent"] == 1 and counts["dropped"] == 1


def test_stale_verification_email_is_not_sent(client, mail):
    c, session = client
    uid, h = _user(c)
    c.put("/users/me/email", json={"email": "one@example.com"}, headers=h)
    _age_codes(session, uid, 120)
    c.put("/users/me/email", json={"email": "two@example.com"}, headers=h)
    counts = _run(session)
    assert counts == {"sent": 1, "retry": 0, "failed": 0, "dropped": 1, "skipped": 0}
    assert [m.to for m, _ in mail.sent] == ["two@example.com"]
    first = _outbox(session, uid)[0]
    assert first.status == "FAILED" and first.last_error == "superseded" and first.payload is None


# --- outbox: retries, failures, restarts ---------------------------------------------------

def _queued(c, session, mail, address="retry@example.com"):
    uid, h = _user(c)
    _verify(c, h, address)
    _run(session)
    mail.sent.clear()
    notif_svc.notify_safely(session, "LISTING_VERIFIED", recipient_id=uid, actor_id=None, entity_id=1,
                            dedupe_key=f"q:{uid}")
    return uid, _outbox(session, uid, "LISTING_VERIFIED")[0].id


def _due(session, outbox_id):
    session.execute(text("UPDATE email_outbox SET next_attempt_at = UTC_TIMESTAMP() - INTERVAL 1 SECOND WHERE id=:i"),
                    {"i": outbox_id})
    session.flush()


def test_temporary_failure_retries_with_backoff_then_succeeds(client, mail):
    c, session = client
    uid, oid = _queued(c, session, mail)
    mail.fail_with = "retry"
    before = datetime.now(timezone.utc).replace(tzinfo=None)
    assert _run(session)["retry"] == 1
    row = session.get(EmailOutbox, oid)
    session.refresh(row)
    assert row.status == "PENDING" and row.attempts == 1 and row.last_error == "retry" and row.sent_at is None
    assert timedelta(seconds=50) <= row.next_attempt_at - before <= timedelta(seconds=75)  # first backoff = 60 s
    assert _run(session) == {"sent": 0, "retry": 0, "failed": 0, "dropped": 0, "skipped": 0}  # not due yet
    _due(session, oid)
    assert _run(session)["retry"] == 1
    session.refresh(row)
    assert row.attempts == 2 and row.next_attempt_at - datetime.now(timezone.utc).replace(tzinfo=None) > timedelta(seconds=240)
    mail.fail_with = None
    _due(session, oid)
    assert _run(session)["sent"] == 1
    session.refresh(row)
    assert row.status == "SENT" and row.attempts == 3 and row.last_error is None and len(mail.sent) == 1


@pytest.mark.parametrize("kind", ["retry", "rate_limit", "config"])
def test_retries_are_exhausted_after_max_attempts(client, mail, kind):
    c, session = client
    uid, oid = _queued(c, session, mail, f"{kind}@example.com")
    mail.fail_with = kind
    for attempt in range(1, email_svc.MAX_ATTEMPTS + 1):
        _due(session, oid)
        counts = _run(session)
        assert counts["failed" if attempt == email_svc.MAX_ATTEMPTS else "retry"] == 1
    row = session.get(EmailOutbox, oid)
    session.refresh(row)
    assert row.status == "FAILED" and row.attempts == email_svc.MAX_ATTEMPTS and row.failed_at is not None
    assert row.last_error == f"{kind}_exhausted" and mail.sent == []
    _due(session, oid)
    assert _run(session) == {"sent": 0, "retry": 0, "failed": 0, "dropped": 0, "skipped": 0}  # FAILED is final


def test_refused_recipient_fails_immediately(client, mail):
    c, session = client
    uid, oid = _queued(c, session, mail, "refused@example.com")
    mail.fail_with = "recipient"
    assert _run(session)["failed"] == 1
    row = session.get(EmailOutbox, oid)
    session.refresh(row)
    assert row.status == "FAILED" and row.attempts == 1 and row.last_error == "recipient"


def test_provider_crash_is_contained_and_retried(client, mail, monkeypatch):
    c, session = client
    uid, oid = _queued(c, session, mail, "crash@example.com")

    def explode(message, key):
        raise RuntimeError("smtp secret=hunter2 to=crash@example.com")

    monkeypatch.setattr(mail, "send", explode)
    assert _run(session)["retry"] == 1
    row = session.get(EmailOutbox, oid)
    session.refresh(row)
    assert row.status == "PENDING" and row.last_error == "retry"  # only a kind is stored, never provider text


def test_worker_restart_reclaims_only_expired_leases(client, mail):
    c, session = client
    uid, oid = _queued(c, session, mail, "lease@example.com")
    # A worker claimed the row and died before sending.
    session.execute(text("UPDATE email_outbox SET status='PROCESSING', attempts=1, locked_at=UTC_TIMESTAMP() WHERE id=:i"),
                    {"i": oid})
    session.flush()
    assert _run(session) == {"sent": 0, "retry": 0, "failed": 0, "dropped": 0, "skipped": 0}  # lease still held
    session.execute(text("UPDATE email_outbox SET locked_at = UTC_TIMESTAMP() - INTERVAL :s SECOND WHERE id=:i"),
                    {"s": email_svc.LEASE_SECONDS + 5, "i": oid})
    session.flush()
    assert _run(session)["sent"] == 1  # lease expired: the restarted worker finishes the job
    row = session.get(EmailOutbox, oid)
    session.refresh(row)
    assert row.status == "SENT" and row.attempts == 2 and row.locked_at is None and len(mail.sent) == 1


def test_a_reclaimed_row_is_not_overwritten_by_its_old_worker(client, mail):
    c, session = client
    uid, oid = _queued(c, session, mail, "owner2@example.com")
    session.execute(text("UPDATE email_outbox SET status='PROCESSING', attempts=2, locked_at=UTC_TIMESTAMP() WHERE id=:i"),
                    {"i": oid})
    session.flush()
    # The old worker (attempt 1) reports late: its write must not apply.
    email_svc._finish(_Shared(session), oid, 1, {"status": "SENT", "sent_at": datetime(2026, 1, 1)})
    row = session.get(EmailOutbox, oid)
    session.refresh(row)
    assert row.status == "PROCESSING" and row.sent_at is None


# --- admin visibility ----------------------------------------------------------------------

def test_admin_sees_delivery_records_without_secrets(client, mail):
    c, session = client
    admin_h, admin_id = _admin(c, session)
    uid, h = _user(c)
    code = c.put("/users/me/email", json={"email": "visible@example.com"}, headers=h).json()["debug_code"]
    r = c.get("/admin/email-deliveries", params={"user_id": uid}, headers=admin_h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1 and body["page"] == 1 and body["page_size"] == 20
    item = body["items"][0]
    assert item["user_id"] == uid and item["category"] == "VERIFICATION" and item["status"] == "PENDING"
    assert item["recipient"] == "v***@example.com" and item["attempts"] == 0 and item["created_at"].endswith("Z")
    blob = json.dumps(body)
    assert code not in blob and "visible@example.com" not in blob and "code_enc" not in blob and "payload" not in blob
    _run(session)
    sent = c.get("/admin/email-deliveries", params={"status": "SENT", "category": "VERIFICATION", "user_id": uid},
                 headers=admin_h).json()
    assert sent["total"] == 1 and sent["items"][0]["sent_at"] and sent["items"][0]["provider"] == "memory"
    assert c.get("/admin/email-deliveries", params={"status": "FAILED", "user_id": uid}, headers=admin_h).json()["total"] == 0
    assert c.get("/admin/email-deliveries", params={"status": "NOPE"}, headers=admin_h).status_code == 422


def test_email_deliveries_require_admin_permission(client, mail):
    c, session = client
    _, h = _user(c)
    assert c.get("/admin/email-deliveries").status_code == 401
    assert c.get("/admin/email-deliveries", headers=h).status_code == 403
    support_h, _ = _admin(c, session, admin_role="SUPPORT")
    assert c.get("/admin/email-deliveries", headers=support_h).status_code == 403  # no audit-log permission


# --- templates -----------------------------------------------------------------------------

def test_links_are_fixed_paths_on_the_configured_https_site_only(monkeypatch):
    monkeypatch.setattr(email_templates.settings, "APP_BASE_URL", "https://restamp.in/")
    assert email_templates.link_for("OWNER_LEAD") == "https://restamp.in/owner/dashboard/leads"
    assert email_templates.link_for("BUYER_VISIT") == "https://restamp.in/activity"
    assert email_templates.link_for("NONE") is None and email_templates.link_for("javascript:alert(1)") is None
    subject, text_body, html = email_templates.render("VISIT_CONFIRMED", message="Your visit is confirmed.", target_kind="BUYER_VISIT")
    assert 'href="https://restamp.in/activity"' in html and "View visit: https://restamp.in/activity" in text_body
    for bad in ("", "http://restamp.in", "javascript:alert(1)", "https://user:pw@restamp.in", "https://restamp.in/?x=1", "ftp://restamp.in"):
        monkeypatch.setattr(email_templates.settings, "APP_BASE_URL", bad)
        assert email_templates.link_for("OWNER_LEAD") is None
        assert "href=" not in email_templates.render("VISIT_CONFIRMED", message="m", target_kind="BUYER_VISIT")[2]


def test_templates_escape_everything_and_show_configured_support(monkeypatch):
    monkeypatch.setattr(email_templates.settings, "EMAIL_SUPPORT", "help@restamp.in <script>")
    subject, text_body, html = email_templates.render("ENQUIRY_NEW", message='New enquiry for "<img src=x onerror=1>".')
    assert "<img" not in html and "<script>" not in html and "&lt;img src=x onerror=1&gt;" in html
    assert "help@restamp.in &lt;script&gt;" in html and "Need help? Contact help@restamp.in <script>." in text_body
    monkeypatch.setattr(email_templates.settings, "EMAIL_SUPPORT", "")
    assert "Need help" not in email_templates.render("ENQUIRY_NEW", message="m")[2]
    with pytest.raises(KeyError):
        email_templates.render("NOT_A_TEMPLATE", message="m")
    assert set(email_templates.OPTIONAL_CATEGORIES) & set(email_templates.MANDATORY_CATEGORIES) == set()


# --- provider adapters (no network) ----------------------------------------------------------

def test_provider_selection_fails_closed(monkeypatch):
    s = email_providers.settings
    monkeypatch.setattr(s, "EMAIL_PROVIDER", "disabled")
    assert email_providers.get_provider().enabled is False
    for choice in ("resend", "smtp", "sendgrid-typo", ""):
        monkeypatch.setattr(s, "EMAIL_PROVIDER", choice)
        monkeypatch.setattr(s, "EMAIL_FROM", "")
        assert email_providers.get_provider().enabled is False  # no sender / unknown -> disabled
    monkeypatch.setattr(s, "EMAIL_FROM", "no-reply@restamp.in")
    monkeypatch.setattr(s, "EMAIL_PROVIDER", "resend")
    monkeypatch.setattr(s, "RESEND_API_KEY", "")
    assert email_providers.get_provider().enabled is False  # missing key
    monkeypatch.setattr(s, "RESEND_API_KEY", "re_test_key")
    assert email_providers.get_provider().name == "resend"
    monkeypatch.setattr(s, "EMAIL_PROVIDER", "smtp")
    monkeypatch.setattr(s, "SMTP_HOST", "")
    assert email_providers.get_provider().enabled is False  # missing host
    monkeypatch.setattr(s, "SMTP_HOST", "smtp.example.com")
    assert email_providers.get_provider().name == "smtp"
    monkeypatch.setattr(s, "EMAIL_PROVIDER", "log")
    assert email_providers.get_provider().name == "log"
    monkeypatch.setattr(email_providers, "is_production", lambda: True)
    assert email_providers.get_provider().enabled is False  # development-only adapter


class _Resp:
    def __init__(self, code, payload=None):
        self.status_code = code
        self._payload = payload or {}

    def json(self):
        return self._payload


def test_resend_adapter_maps_outcomes_without_network(monkeypatch):
    seen = {}

    def fake_post(url, headers, json, timeout):
        seen.update(url=url, headers=headers, json=json, timeout=timeout)
        return _Resp(seen["code"], {"id": "re_123"})

    monkeypatch.setattr(email_providers.httpx, "post", fake_post)
    provider = email_providers.ResendProvider("re_secret", "RESTAMP <no-reply@restamp.in>", 15)
    message = email_providers.EmailMessage("to@example.com", "S", "T", "<p>H</p>")
    seen["code"] = 200
    assert provider.send(message, "restamp-outbox-9") == "re_123"
    assert seen["url"] == "https://api.resend.com/emails" and seen["headers"]["Idempotency-Key"] == "restamp-outbox-9"
    assert seen["json"]["to"] == ["to@example.com"] and seen["json"]["from"] == "RESTAMP <no-reply@restamp.in>"
    for code, kind in ((429, "rate_limit"), (401, "config"), (403, "config"), (422, "recipient"), (400, "recipient"), (500, "retry"), (503, "retry")):
        seen["code"] = code
        with pytest.raises(EmailSendError) as err:
            provider.send(message, "k")
        assert err.value.kind == kind and "re_secret" not in str(err.value)

    def timeout_post(*a, **k):
        raise email_providers.httpx.ConnectTimeout("slow")

    monkeypatch.setattr(email_providers.httpx, "post", timeout_post)
    with pytest.raises(EmailSendError) as err:
        provider.send(message, "k")
    assert err.value.kind == "retry"


def test_smtp_adapter_uses_tls_and_maps_outcomes_without_network(monkeypatch):
    events = []

    class FakeSMTP:
        refuse = None
        raise_on_send = None

        def __init__(self, host, port, timeout=None):
            events.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context=None):
            events.append(("starttls", context is not None))

        def login(self, user, password):
            events.append(("login", user))

        def send_message(self, mime):
            if FakeSMTP.raise_on_send:
                raise FakeSMTP.raise_on_send
            events.append(("send", mime["To"], mime["Subject"], mime["From"]))
            return FakeSMTP.refuse or {}

    monkeypatch.setattr(email_providers.smtplib, "SMTP", FakeSMTP)
    provider = email_providers.SmtpProvider("smtp.example.com", 587, "user", "pw", "no-reply@restamp.in", "RESTAMP", 15)
    message = email_providers.EmailMessage("to@example.com", "Subject", "Text", "<p>Html</p>")
    message_id = provider.send(message, "restamp-outbox-3")
    assert message_id and message_id.endswith("@restamp.in")
    assert events == [("connect", "smtp.example.com", 587), ("starttls", True), ("login", "user"),
                      ("send", "to@example.com", "Subject", "RESTAMP <no-reply@restamp.in>")]
    FakeSMTP.refuse = {"to@example.com": (550, b"no")}
    with pytest.raises(EmailSendError) as err:
        provider.send(message, "k")
    assert err.value.kind == "recipient"
    FakeSMTP.refuse = None
    import smtplib
    for exc, kind in ((smtplib.SMTPAuthenticationError(535, b"bad"), "config"),
                      (smtplib.SMTPRecipientsRefused({"to@example.com": (550, b"x")}), "recipient"),
                      (smtplib.SMTPResponseException(451, b"later"), "retry"),
                      (smtplib.SMTPResponseException(421, b"slow down"), "rate_limit"),
                      (smtplib.SMTPServerDisconnected("gone"), "retry"), (TimeoutError("t"), "retry")):
        FakeSMTP.raise_on_send = exc
        with pytest.raises(EmailSendError) as err:
            provider.send(message, "k")
        assert err.value.kind == kind


def test_mask_email():
    assert email_svc.mask_email("someone@example.com") == "s***@example.com"
    assert email_svc.mask_email(None) is None and email_svc.mask_email("broken") is None


def test_concurrent_first_save_answers_409_not_500(client, mail, monkeypatch):
    c, session = client
    _, h = _user(c)
    from sqlalchemy.exc import IntegrityError

    def racing(*a, **k):
        raise IntegrityError("INSERT INTO user_emails", {}, Exception(1062, "Duplicate entry"))

    monkeypatch.setattr(email_svc, "set_email", racing)
    r = c.put("/users/me/email", json={"email": "race@example.com"}, headers=h)
    assert r.status_code == 409 and r.json() == {"detail": "Please try again"}


def test_misconfiguration_is_logged_once_and_names_no_value(monkeypatch, caplog):
    s = email_providers.settings
    monkeypatch.setattr(email_providers, "_warned", set())
    monkeypatch.setattr(s, "EMAIL_PROVIDER", "resend")
    monkeypatch.setattr(s, "EMAIL_FROM", "no-reply@restamp.in")
    monkeypatch.setattr(s, "RESEND_API_KEY", "")
    with caplog.at_level("ERROR", logger="restamp.email"):
        for _ in range(5):
            assert email_providers.get_provider().enabled is False
    messages = [r.getMessage() for r in caplog.records if r.name == "restamp.email"]
    assert messages == ["RESTAMP_RESEND_API_KEY is not set; email stays disabled."]
