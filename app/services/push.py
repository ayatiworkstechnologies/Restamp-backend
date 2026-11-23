"""RESTAMP push notifications: devices, preferences, outbox (Phase 4).

DEVICES
- One row per physical Expo device (push_devices). The FULL push token is
  never stored in the clear (token_enc, Fernet with a server-derived key),
  never returned by an API, never logged: token_hash (SHA-256) identifies the
  device and token_prefix is a masked display form. token_hash is UNIQUE
  globally, so a device that signs into another account TAKES OVER its single
  row — one token can never deliver for two accounts at once.
- status ACTIVE / INACTIVE (the user logged out) / INVALID (the provider
  refused the token). Registration is idempotent: re-registering the same
  token refreshes its row.

PREFERENCES
- push_preferences: one row per account, INDEPENDENT of email and in-app
  preferences: these flags govern push delivery only. No row means every
  optional push is on (safe default). ACCOUNT pushes are mandatory (no
  opt-out).

OUTBOX
- Events never send a push inline. notify_safely inserts an in-app row and
  then queues its push twin here (after the email twin), in a savepoint
  inside the caller's transaction; UNIQUE (user_id, dedupe_key) makes that
  idempotent. A failure to queue never fails the business operation, except
  transaction-level database errors, which are re-raised exactly like in
  services/email.py.
- One row per recipient (not per device): the worker delivers to EVERY active
  device of the user at send time, so "all my devices get it" and a device
  registered after queuing still receives it.
- The worker claims due rows with SELECT ... FOR UPDATE SKIP LOCKED, marks
  them PROCESSING with a lease (locked_at) and commits BEFORE sending, so two
  workers never hold the same row; a crashed worker's rows are reclaimed
  after LEASE_SECONDS. Failures retry with exponential backoff up to
  MAX_ATTEMPTS, then FAILED. Only an error KIND is stored.
- Eligibility is re-checked at send time (account active, preference still
  on, at least one active device, reminder still valid — see
  services/reminders.py), so changes apply to pushes that are already queued.
- With no provider configured nothing is queued and nothing is sent; no push
  endpoint is contacted.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, status
from sqlalchemy import and_, func, null, or_, update
from sqlalchemy.exc import IntegrityError, OperationalError, PendingRollbackError
from sqlalchemy.orm import Session

from ..config import settings
from ..models import PushDelivery, PushDevice, PushOutbox, PushPreference, User
from . import push_providers, email_templates
from .push_providers import PushMessage, PushSendError

log = logging.getLogger("restamp.push")

MAX_ATTEMPTS = 6
# Delay before attempt N+1 after N failed attempts (exponential-style backoff).
BACKOFF_SECONDS = (60, 300, 900, 3600, 10800)
LEASE_SECONDS = 600
# Expo: check a ticket's receipt ~15 min after sending; receipts are kept ~24 h.
RECEIPT_FIRST_DELAY_SECONDS = 900
RECEIPT_BACKOFF_SECONDS = (900, 1800, 3600, 7200, 14400)
RECEIPT_MAX_AGE_SECONDS = 24 * 3600
RECEIPT_BATCH = 300
BATCH_SIZE = 20

_MYSQL_TXN_FATAL = (1213, 1205)

# Expo push tokens look like ExponentPushToken[...] / ExpoPushToken[...].
_TOKEN_RE = re.compile(r"^Expo(nent)?PushToken\[[0-9a-zA-Z_\-]{20,80}\]$")

# Categories with a user-facing preference switch (push_preferences).
OPTIONAL_CATEGORIES = ("ENQUIRY", "VISIT", "LISTING")


def _utcnow_naive() -> datetime:
    # Whole seconds: the DATETIME columns have no fractional part and MySQL
    # ROUNDS on insert, so a sub-second value could land in the future and a
    # just-queued row would not be "due" for the worker.
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def _secret(label: bytes) -> bytes:
    return hashlib.sha256(label + b":" + settings.JWT_SECRET.encode("utf-8")).digest()


def _fernet() -> Fernet:
    # A push-specific derivation: the push key is not the email-code key.
    return Fernet(base64.urlsafe_b64encode(_secret(b"restamp-push-token-enc")))


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mask_token(token: str) -> str:
    """`ExponentPushToken[a1b2…9z8]` — enough to recognise, never the token."""
    if len(token) > 24:
        return f"{token[:20]}…{token[-4:]}"
    return token[:4] + "…"


# --- status / devices / preferences --------------------------------------------------

def delivery_available() -> bool:
    return bool(push_providers.get_provider().enabled)


def _pref_row(db: Session, user_id: int, lock: bool = False) -> PushPreference | None:
    return db.get(PushPreference, int(user_id), with_for_update=lock) if lock else db.get(PushPreference, int(user_id))


def _pref_on(row: PushPreference | None, category: str) -> bool:
    if row is None:
        return True  # no row (never opted out): every optional push is on
    if category == "ENQUIRY":
        return bool(row.pref_enquiries)
    if category == "VISIT":
        return bool(row.pref_visits)
    if category == "LISTING":
        return bool(row.pref_listings)
    return True  # mandatory categories (ACCOUNT) have no preference


def get_preferences(db: Session, user_id: int) -> dict:
    row = _pref_row(db, user_id)
    return {
        "enquiries": bool(row.pref_enquiries) if row else True,
        "visits": bool(row.pref_visits) if row else True,
        "listings": bool(row.pref_listings) if row else True,
    }


def set_preferences(db: Session, user_id: int, *, enquiries: bool, visits: bool, listings: bool) -> dict:
    """Optional-push preferences (lazily creates the row). Mandatory ACCOUNT
    pushes have no switch."""
    row = _pref_row(db, user_id, lock=True)
    now = _utcnow_naive()
    if row is None:
        row = PushPreference(
            user_id=int(user_id), pref_enquiries=bool(enquiries), pref_visits=bool(visits),
            pref_listings=bool(listings), created_at=now, updated_at=now,
        )
        db.add(row)
    else:
        row.pref_enquiries = bool(enquiries)
        row.pref_visits = bool(visits)
        row.pref_listings = bool(listings)
        row.updated_at = now
    db.flush()
    return get_status(db, user_id)


def list_devices(db: Session, user_id: int) -> list[dict]:
    """The caller's own devices, masked. Never a token."""

    def _utc(value: datetime | None) -> datetime | None:
        return value.replace(tzinfo=timezone.utc) if value is not None else None

    rows = (
        db.query(PushDevice)
        .filter(PushDevice.user_id == int(user_id))
        .order_by(PushDevice.id.desc())
        .all()
    )
    return [
        {
            "id": int(r.id), "platform": r.platform, "status": r.status,
            "invalid_reason": r.invalid_reason if r.status == "INVALID" else None,
            "token_prefix": r.token_prefix, "app_version": r.app_version,
            "device_name": r.device_name, "registered_at": _utc(r.registered_at),
            "refreshed_at": _utc(r.refreshed_at),
        }
        for r in rows
    ]


def get_status(db: Session, user_id: int) -> dict:
    return {
        "available": delivery_available(),
        "devices": list_devices(db, user_id),
        "preferences": get_preferences(db, user_id),
    }


def register_device(db: Session, user_id: int, token: str, platform: str,
                    *, app_version: str | None = None, device_name: str | None = None) -> dict:
    """Register (or refresh) the caller's device. ValueError for an invalid
    Expo token or platform (the router answers 422). One token is one row:
    registering a token that belongs to another account takes the row over,
    so the device delivers only for THIS account from now on. Flushes; the
    router commits."""
    normalized = (token or "").strip()
    if platform not in ("ios", "android") or not _TOKEN_RE.match(normalized):
        raise ValueError("Invalid push token or platform")
    now = _utcnow_naive()
    token_hash = hash_token(normalized)
    encrypted = _fernet().encrypt(normalized.encode("utf-8")).decode("ascii")
    prefix = mask_token(normalized)
    version = (app_version or "").strip()[:50] or None
    name = (device_name or "").strip()[:100] or None
    row = db.query(PushDevice).filter(PushDevice.token_hash == token_hash).with_for_update().first()
    if row is None:
        row = PushDevice(
            user_id=int(user_id), platform=platform, token_hash=token_hash, token_enc=encrypted,
            token_prefix=prefix, app_version=version, device_name=name, status="ACTIVE",
            invalid_reason=None, registered_at=now, refreshed_at=now, created_at=now, updated_at=now,
        )
        db.add(row)
    else:
        # Ownership takeover + refresh: the previous account loses the device.
        row.user_id = int(user_id)
        row.platform = platform
        row.token_enc = encrypted
        row.token_prefix = prefix
        row.app_version = version
        row.device_name = name
        row.status = "ACTIVE"
        row.invalid_reason = None
        row.refreshed_at = now
        row.updated_at = now
    db.flush()
    return {
        "id": int(row.id), "platform": row.platform, "status": row.status, "invalid_reason": None,
        "token_prefix": row.token_prefix, "app_version": row.app_version,
        "device_name": row.device_name, "registered_at": row.registered_at, "refreshed_at": row.refreshed_at,
    }


def deactivate_device(db: Session, user_id: int, device_id: int) -> None:
    """The caller logs a device out (idempotent). 404 for another user's or a
    missing device — the row is kept (it is this account's history; a later
    registration of the same token reactivates it). Flushes."""
    row = db.get(PushDevice, int(device_id), with_for_update=True)
    if row is None or row.user_id != int(user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if row.status != "INACTIVE":
        row.status = "INACTIVE"
        row.updated_at = _utcnow_naive()
        db.flush()


# --- queuing (inside the caller's transaction) -------------------------------------

def _mysql_code(exc: Exception) -> int | None:
    orig = getattr(exc, "orig", None)
    args = getattr(orig, "args", None) or ()
    return args[0] if args and isinstance(args[0], int) else None


def _enqueue(db: Session, *, user_id: int, notification_id: int | None, category: str,
             template: str, title: str, body: str, payload: dict, dedupe_key: str) -> bool:
    """Insert one outbox row in a savepoint. True when a new row was queued;
    False for a duplicate key or an isolated failure. Never commits."""
    try:
        with db.begin_nested():
            db.add(PushOutbox(
                user_id=int(user_id), notification_id=notification_id, category=category,
                template=template, title=title[:160], body=body[:500], payload=payload or {},
                dedupe_key=dedupe_key[:191], status="PENDING", attempts=0,
                next_attempt_at=_utcnow_naive(), created_at=_utcnow_naive(),
            ))
            db.flush()
        return True
    except PendingRollbackError:
        raise
    except OperationalError as exc:
        if _mysql_code(exc) in _MYSQL_TXN_FATAL:
            log.error("Push %s aborted the transaction: %s", template, type(exc).__name__)
            raise
        if not db.is_active:
            raise
        log.error("Push %s not queued: %s", template, type(exc).__name__)
        return False
    except IntegrityError as exc:
        if not db.is_active:
            raise
        if _mysql_code(exc) != 1062:  # 1062 = already queued (the normal retry outcome)
            log.error("Push %s not queued: %s", template, type(exc).__name__)
        return False
    except Exception as exc:  # a savepoint-confined failure never breaks the business operation
        if not db.is_active:
            raise
        log.error("Push %s not queued: %s", template, type(exc).__name__)
        return False


def enqueue_for_notification(db: Session, notification, target_kind: str | None,
                             extra_payload: dict | None = None) -> bool:
    """Queue the push twin of a NEWLY created in-app notification.

    Called only for a row notify_safely just inserted, so never-the-actor,
    one-per-dedupe-key and follow-up coalescing apply to push exactly as they
    do in-app and in email. The recipient, its devices and preferences come
    from the database — never from a request. Nothing is queued without a
    provider, an active device, or with the category switched off.

    `extra_payload` carries reminder eligibility facts (from
    services/reminders.py): the delivery-time recheck needs the scheduled
    visit time or the lead offset, which the outbox row stores so a resend
    after a crash re-checks the same facts.
    """
    try:
        spec = email_templates.TEMPLATES.get(notification.type)
        if spec is None or not delivery_available():
            return False
        category = spec.category
        if category in OPTIONAL_CATEGORIES and not _pref_on(_pref_row(db, notification.user_id), category):
            return False
        has_device = (
            db.query(PushDevice.id)
            .filter(PushDevice.user_id == notification.user_id, PushDevice.status == "ACTIVE")
            .first()
            is not None
        )
        if not has_device:
            return False
    except (PendingRollbackError, OperationalError):
        raise
    except Exception as exc:
        if not db.is_active:
            raise
        log.error("Push %s not queued: %s", notification.type, type(exc).__name__)
        return False
    payload = {"target": target_kind}
    if extra_payload:
        payload.update(extra_payload)
    return _enqueue(
        db, user_id=notification.user_id, notification_id=int(notification.id), category=category,
        template=notification.type, title=notification.title, body=notification.body,
        payload=payload, dedupe_key=f"n:{notification.dedupe_key}",
    )


# --- delivery (worker; own sessions) ---------------------------------------------

def _claim(db: Session, limit: int) -> list[tuple[int, int]]:
    """Lock due rows, mark them PROCESSING and commit. Returns (id, attempts).
    SKIP LOCKED: a row another worker is claiming is simply not seen."""
    now = _utcnow_naive()
    rows = (
        db.query(PushOutbox)
        .filter(or_(
            and_(PushOutbox.status == "PENDING", PushOutbox.next_attempt_at <= now),
            # A worker that died mid-send: reclaim after the lease.
            and_(PushOutbox.status == "PROCESSING",
                 PushOutbox.locked_at <= now - timedelta(seconds=LEASE_SECONDS)),
        ))
        .order_by(PushOutbox.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .all()
    )
    claimed = []
    for row in rows:
        row.status = "PROCESSING"
        row.locked_at = now
        row.attempts = int(row.attempts or 0) + 1
        claimed.append((int(row.id), int(row.attempts)))
    db.commit()
    return claimed


def _finish(db: Session, outbox_id: int, attempts: int, values: dict) -> None:
    """Write the outcome only if this worker still owns the claim (same
    attempt number, still PROCESSING) — a reclaimed row is left to its new owner."""
    db.execute(
        update(PushOutbox)
        .where(PushOutbox.id == outbox_id, PushOutbox.status == "PROCESSING", PushOutbox.attempts == attempts)
        .values(locked_at=None, **values)
    )
    db.commit()


def _recipient_drop_reason(db: Session, row: PushOutbox) -> str | None:
    """Recipient-level eligibility, re-checked immediately before sending:
    account state, push preference, and reminder facts (shared with email via
    services/reminders.reminder_drop_reason). None = may be sent."""
    user = db.get(User, int(row.user_id))
    if user is None or user.status == "closed":
        return "account_closed"
    if row.category in OPTIONAL_CATEGORIES:
        if user.status != "active":
            return "account_inactive"
        if not _pref_on(_pref_row(db, row.user_id), row.category):
            return "opted_out"
    from . import reminders  # lazy: reminders imports notifications
    return reminders.reminder_drop_reason(db, row.template, row.payload or {}, _utcnow_naive())


def _fan_out(db: Session, outbox_id: int, user_id: int) -> int:
    """Create one PENDING delivery per device ACTIVE now, once per outbox row.
    Idempotent: UNIQUE (outbox_id, device_id) — a reclaimed row (worker crash)
    or a concurrent attempt never creates a second set. Returns rows created."""
    existing = {int(d) for (d,) in db.query(PushDelivery.device_id).filter(PushDelivery.outbox_id == outbox_id)}
    if existing:
        return 0  # the device set is fixed at the first fan-out
    devices = [int(d) for (d,) in db.query(PushDevice.id).filter(
        PushDevice.user_id == int(user_id), PushDevice.status == "ACTIVE").order_by(PushDevice.id)]
    now = _utcnow_naive()
    created = 0
    for device_id in devices:
        try:
            with db.begin_nested():
                db.add(PushDelivery(outbox_id=outbox_id, device_id=device_id, status="PENDING", attempts=0,
                                    next_attempt_at=now, receipt_attempts=0, created_at=now))
                db.flush()
            created += 1
        except IntegrityError as exc:
            if _mysql_code(exc) != 1062:
                raise
    db.commit()
    return created


def _fail_open_deliveries(db: Session, outbox_id: int, reason: str) -> None:
    """A recipient-level drop: every not-yet-final device delivery fails too."""
    now = _utcnow_naive()
    db.execute(
        update(PushDelivery)
        .where(PushDelivery.outbox_id == outbox_id, PushDelivery.status.in_(("PENDING", "PROCESSING")))
        .values(status="FAILED", failed_at=now, last_error=reason[:60], locked_at=None)
    )
    db.commit()


def _invalidate_device(db: Session, device_id: int, reason: str) -> None:
    """Mark one device INVALID (its own transaction; a failure is logged and
    never blocks the outbox row)."""
    try:
        db.execute(
            update(PushDevice)
            .where(PushDevice.id == int(device_id), PushDevice.status == "ACTIVE")
            .values(status="INVALID", invalid_reason=reason[:60], updated_at=_utcnow_naive())
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        log.error("Device %d not marked invalid: %s", device_id, type(exc).__name__)


def _write_delivery(db: Session, delivery_id: int, attempts: int, values: dict) -> bool:
    """Write a device outcome only if this worker still owns that delivery
    (still PROCESSING with the same attempt number). Returns whether it applied."""
    result = db.execute(
        update(PushDelivery)
        .where(PushDelivery.id == delivery_id, PushDelivery.status == "PROCESSING",
               PushDelivery.attempts == attempts)
        .values(locked_at=None, **values)
    )
    db.commit()
    return bool(result.rowcount)


def _send_one(db: Session, outbox_row: dict, delivery_id: int, provider) -> str:
    """Send ONE device's copy. Claims the delivery (PROCESSING, committed)
    before the provider call, so a device never gets two copies from two
    workers and a crash leaves a lease that expires. Returns the outcome."""
    now = _utcnow_naive()
    claimed = db.execute(
        update(PushDelivery)
        .where(PushDelivery.id == delivery_id, PushDelivery.status == "PENDING",
               PushDelivery.next_attempt_at <= now)
        .values(status="PROCESSING", locked_at=now, attempts=PushDelivery.attempts + 1)
    )
    db.commit()
    if not claimed.rowcount:
        return "skipped"
    delivery = db.get(PushDelivery, delivery_id)
    attempts = int(delivery.attempts)
    device = db.get(PushDevice, int(delivery.device_id))
    if device is None or device.status != "ACTIVE":
        db.rollback()
        _write_delivery(db, delivery_id, attempts, {"status": "FAILED", "failed_at": now, "last_error": "device_inactive"})
        return "failed"
    try:
        token = _fernet().decrypt(str(device.token_enc).encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeDecodeError):
        db.rollback()
        log.error("Push token of device %d unreadable", int(device.id))
        _invalidate_device(db, int(device.id), "unreadable")
        _write_delivery(db, delivery_id, attempts, {"status": "FAILED", "failed_at": now, "last_error": "unreadable"})
        return "failed"
    device_id = int(device.id)
    db.rollback()  # nothing is held while the provider is called
    message = PushMessage(token, outbox_row["title"], outbox_row["body"], outbox_row["payload"])
    try:
        ticket = provider.send(message, f"restamp-push-{outbox_row['id']}-{device_id}")
    except PushSendError as exc:
        kind = exc.kind
    except Exception as exc:  # an adapter bug must never crash the worker
        log.error("Push provider error: %s", type(exc).__name__)
        kind = "retry"
    else:
        # Accepted by the provider. NOT delivered yet: the receipt decides.
        _write_delivery(db, delivery_id, attempts, {
            "status": "SENT", "sent_at": _utcnow_naive(), "ticket_id": (str(ticket)[:191] if ticket else None),
            "receipt_due_at": (_utcnow_naive() + timedelta(seconds=RECEIPT_FIRST_DELAY_SECONDS)) if ticket else None,
            "last_error": None,
        })
        return "sent"
    if kind == "recipient":
        _invalidate_device(db, device_id, "provider_refused")
    if kind in ("recipient", "invalid_message") or attempts >= MAX_ATTEMPTS:
        _write_delivery(db, delivery_id, attempts, {
            "status": "FAILED", "failed_at": _utcnow_naive(),
            "last_error": kind if kind in ("recipient", "invalid_message") else f"{kind}_exhausted",
        })
        return "failed"
    delay = BACKOFF_SECONDS[min(attempts - 1, len(BACKOFF_SECONDS) - 1)]
    _write_delivery(db, delivery_id, attempts, {
        "status": "PENDING", "next_attempt_at": _utcnow_naive() + timedelta(seconds=delay), "last_error": kind,
    })
    return "retry"


def _aggregate(db: Session, outbox_id: int) -> dict:
    """Outbox status from its device deliveries:
    any PENDING/PROCESSING -> PENDING (next try = earliest device retry);
    else any SENT/DELIVERED -> SENT; else FAILED (most significant kind)."""
    rows = db.query(PushDelivery.status, PushDelivery.next_attempt_at, PushDelivery.last_error,
                    PushDelivery.ticket_id).filter(PushDelivery.outbox_id == outbox_id).all()
    db.rollback()
    now = _utcnow_naive()
    if not rows:
        return {"status": "FAILED", "failed_at": now, "last_error": "no_device"}
    open_rows = [r for r in rows if r[0] in ("PENDING", "PROCESSING")]
    if open_rows:
        due = min((r[1] for r in open_rows if r[0] == "PENDING"), default=now + timedelta(seconds=LEASE_SECONDS))
        kinds = [r[2] for r in open_rows if r[2]]
        return {"status": "PENDING", "next_attempt_at": due, "last_error": kinds[0] if kinds else None}
    accepted = [r for r in rows if r[0] in ("SENT", "DELIVERED")]
    if accepted:
        first_ticket = next((r[3] for r in accepted if r[3]), None)
        return {"status": "SENT", "sent_at": now, "provider_message_id": first_ticket, "last_error": None}
    kinds = [r[2] for r in rows if r[2]]
    for preferred in ("recipient", "invalid_message"):
        if preferred in kinds:
            return {"status": "FAILED", "failed_at": now, "last_error": preferred}
    return {"status": "FAILED", "failed_at": now, "last_error": kinds[0] if kinds else "failed"}


def _deliver(db: Session, outbox_id: int, attempts: int, provider) -> str:
    """Process one claimed outbox row: re-check the recipient, fan out to its
    devices once, send each due device, then set the aggregate status."""
    row = db.get(PushOutbox, outbox_id)
    if row is None or row.status != "PROCESSING":
        db.rollback()
        return "skipped"
    try:
        reason = _recipient_drop_reason(db, row)
    except Exception as exc:
        log.error("Push %s could not be checked: %s", row.template, type(exc).__name__)
        reason = "render_error"
    snapshot = {"id": int(row.id), "user_id": int(row.user_id), "title": row.title, "body": row.body,
                "payload": dict(row.payload or {})}
    db.rollback()
    if reason:
        _fail_open_deliveries(db, outbox_id, reason)
        _finish(db, outbox_id, attempts, {"status": "FAILED", "failed_at": _utcnow_naive(), "last_error": reason})
        return "dropped"
    _fan_out(db, outbox_id, snapshot["user_id"])
    if not db.query(PushDelivery.id).filter(PushDelivery.outbox_id == outbox_id).first():
        # The recipient has no active device (logged out everywhere since
        # queuing): nothing to send, nothing to retry.
        db.rollback()
        _finish(db, outbox_id, attempts, {"status": "FAILED", "failed_at": _utcnow_naive(), "last_error": "no_device"})
        return "dropped"
    # A device send left PROCESSING by a crashed worker: once its lease has
    # expired it is retried (we hold this outbox row's lease, so no live
    # worker owns it). At-least-once for that one device, never a silent loss.
    db.execute(
        update(PushDelivery)
        .where(PushDelivery.outbox_id == outbox_id, PushDelivery.status == "PROCESSING",
               PushDelivery.locked_at <= _utcnow_naive() - timedelta(seconds=LEASE_SECONDS))
        .values(status="PENDING", locked_at=None, next_attempt_at=_utcnow_naive(), last_error="lease_expired")
    )
    db.commit()
    due = [int(d) for (d,) in db.query(PushDelivery.id).filter(
        PushDelivery.outbox_id == outbox_id, PushDelivery.status == "PENDING",
        PushDelivery.next_attempt_at <= _utcnow_naive()).order_by(PushDelivery.id)]
    db.rollback()
    for delivery_id in due:
        try:
            _send_one(db, snapshot, delivery_id, provider)
        except Exception as exc:
            db.rollback()
            log.error("Push delivery failed: %s", type(exc).__name__)
    values = _aggregate(db, outbox_id)
    values.setdefault("provider", provider.name[:30])
    if values["status"] == "PENDING" and attempts >= MAX_ATTEMPTS * 2:
        # Safety net: an outbox row cannot cycle forever (device retries are
        # themselves bounded by MAX_ATTEMPTS).
        _fail_open_deliveries(db, outbox_id, "retry_exhausted")
        values = _aggregate(db, outbox_id)
        values.setdefault("provider", provider.name[:30])
    _finish(db, outbox_id, attempts, values)
    if values["status"] == "SENT":
        return "sent"
    if values["status"] == "PENDING":
        return "retry"
    return "failed"


def process_outbox(limit: int = BATCH_SIZE, session_factory=None) -> dict:
    """One worker pass: claim due outbox rows, send each device's copy, record
    the outcome. Safe to run from several workers/processes at once. Does
    nothing while no provider is configured (rows wait; nothing expires)."""
    provider = push_providers.get_provider()
    counts = {"sent": 0, "retry": 0, "failed": 0, "dropped": 0, "skipped": 0}
    if not provider.enabled:
        return counts
    if session_factory is None:
        from ..db import SessionLocal as session_factory  # lazy: tests swap the factory
    db: Session = session_factory()
    try:
        claimed = _claim(db, limit)
        for outbox_id, attempts in claimed:
            try:
                counts[_deliver(db, outbox_id, attempts, provider)] += 1
            except Exception as exc:
                db.rollback()
                log.error("Push outbox row failed: %s", type(exc).__name__)
        return counts
    finally:
        db.close()


# --- receipts (Expo: a ticket is NOT delivery) -----------------------------------------

def _reopen_outbox(db: Session, outbox_id: int, when: datetime) -> None:
    """A device delivery went back to PENDING (receipt said rate-limited):
    make the recipient row due again so the send worker picks it up."""
    db.execute(
        update(PushOutbox)
        .where(PushOutbox.id == outbox_id, PushOutbox.status.in_(("SENT", "FAILED")))
        .values(status="PENDING", next_attempt_at=when, failed_at=None)
    )


def poll_receipts(limit: int = RECEIPT_BATCH, session_factory=None) -> dict:
    """One receipt pass: claim SENT deliveries whose receipt is due (SKIP
    LOCKED + a lease in locked_at, committed before the provider call), ask
    the provider, and write each answer conditionally.

    ok -> DELIVERED; DeviceNotRegistered -> FAILED + device INVALID;
    MessageRateExceeded -> back to PENDING for a resend (attempt budget
    applies); other errors -> FAILED (permanent); not ready / call failed ->
    poll again later with backoff; still unknown RECEIPT_MAX_AGE after the
    send -> stop polling (`receipt_unavailable`, the delivery stays SENT)."""
    provider = push_providers.get_provider()
    counts = {"delivered": 0, "failed": 0, "resend": 0, "waiting": 0, "expired": 0}
    if not provider.enabled:
        return counts
    if session_factory is None:
        from ..db import SessionLocal as session_factory
    db: Session = session_factory()
    try:
        now = _utcnow_naive()
        rows = (
            db.query(PushDelivery)
            .filter(PushDelivery.status == "SENT", PushDelivery.ticket_id.is_not(None),
                    PushDelivery.receipt_due_at.is_not(None), PushDelivery.receipt_due_at <= now,
                    or_(PushDelivery.locked_at.is_(None),
                        PushDelivery.locked_at <= now - timedelta(seconds=LEASE_SECONDS)))
            .order_by(PushDelivery.id)
            .limit(min(int(limit), push_providers.RECEIPT_BATCH_MAX))
            .with_for_update(skip_locked=True)
            .all()
        )
        claimed = []
        for r in rows:
            r.locked_at = now
            claimed.append((int(r.id), int(r.outbox_id), int(r.device_id), str(r.ticket_id),
                            r.sent_at, int(r.receipt_attempts or 0), int(r.attempts or 0)))
        db.commit()
        if not claimed:
            return counts
        try:
            receipts = provider.get_receipts([c[3] for c in claimed])
            call_failed = False
        except PushSendError:
            receipts, call_failed = {}, True
        except Exception as exc:
            log.error("Push receipt call failed: %s", type(exc).__name__)
            receipts, call_failed = {}, True
        for delivery_id, outbox_id, device_id, ticket, sent_at, receipt_attempts, attempts in claimed:
            cond = and_(PushDelivery.id == delivery_id, PushDelivery.status == "SENT", PushDelivery.locked_at == now)
            receipt = receipts.get(ticket)
            stamp = _utcnow_naive()
            if receipt is None:
                too_old = sent_at is None or stamp - sent_at >= timedelta(seconds=RECEIPT_MAX_AGE_SECONDS)
                if too_old and not call_failed:
                    db.execute(update(PushDelivery).where(cond).values(
                        locked_at=None, receipt_due_at=None, last_error="receipt_unavailable"))
                    counts["expired"] += 1
                else:
                    delay = RECEIPT_BACKOFF_SECONDS[min(receipt_attempts, len(RECEIPT_BACKOFF_SECONDS) - 1)]
                    db.execute(update(PushDelivery).where(cond).values(
                        locked_at=None, receipt_attempts=receipt_attempts + 1,
                        receipt_due_at=stamp + timedelta(seconds=delay)))
                    counts["waiting"] += 1
            elif receipt.get("status") == "ok":
                db.execute(update(PushDelivery).where(cond).values(
                    locked_at=None, status="DELIVERED", delivered_at=stamp, receipt_due_at=None, last_error=None))
                counts["delivered"] += 1
            else:
                kind = receipt.get("error") or "retry"
                if kind == "rate_limit" and attempts < MAX_ATTEMPTS:
                    when = stamp + timedelta(seconds=BACKOFF_SECONDS[min(attempts - 1, len(BACKOFF_SECONDS) - 1)])
                    db.execute(update(PushDelivery).where(cond).values(
                        locked_at=None, status="PENDING", next_attempt_at=when, receipt_due_at=None,
                        ticket_id=None, last_error="receipt_rate_limit"))
                    _reopen_outbox(db, outbox_id, when)
                    counts["resend"] += 1
                else:
                    db.execute(update(PushDelivery).where(cond).values(
                        locked_at=None, status="FAILED", failed_at=stamp, receipt_due_at=None,
                        last_error=f"receipt_{kind}"[:60]))
                    counts["failed"] += 1
                    if kind == "recipient":
                        db.commit()
                        _invalidate_device(db, device_id, "DeviceNotRegistered")
            db.commit()
        return counts
    finally:
        db.close()


# --- admin visibility ---------------------------------------------------------------

def admin_list(db: Session, page: int, page_size: int, *, status_filter: str | None = None,
               category: str | None = None, user_id: int | None = None) -> tuple[list[dict], int]:
    """Delivery records for administrators: status, category, recipient
    ACCOUNT (id; a device token NEVER leaves the server, not even masked in
    this list), timestamps, attempts and the failure kind. Never the payload
    or provider credentials."""
    q = db.query(PushOutbox)
    if status_filter:
        q = q.filter(PushOutbox.status == status_filter)
    if category:
        q = q.filter(PushOutbox.category == category)
    if user_id is not None:
        q = q.filter(PushOutbox.user_id == int(user_id))
    total = q.count()
    rows = q.order_by(PushOutbox.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    # Per-device outcome counts in ONE grouped query (no N+1).
    counts: dict[int, dict[str, int]] = {}
    ids = [int(r.id) for r in rows]
    if ids:
        for outbox_id, dstatus, n in (
            db.query(PushDelivery.outbox_id, PushDelivery.status, func.count(PushDelivery.id))
            .filter(PushDelivery.outbox_id.in_(ids)).group_by(PushDelivery.outbox_id, PushDelivery.status)
        ):
            counts.setdefault(int(outbox_id), {})[str(dstatus)] = int(n)

    def _devices(oid: int) -> dict:
        c = counts.get(oid, {})
        return {"pending": c.get("PENDING", 0) + c.get("PROCESSING", 0), "accepted": c.get("SENT", 0),
                "delivered": c.get("DELIVERED", 0), "failed": c.get("FAILED", 0)}

    def _utc(value):
        return value.replace(tzinfo=timezone.utc) if value is not None else None

    return [
        {
            "id": int(r.id), "user_id": int(r.user_id), "category": r.category, "template": r.template,
            "status": r.status, "attempts": int(r.attempts or 0),
            "failure": r.last_error if r.status in ("FAILED", "PENDING") else None,
            "provider": r.provider, "created_at": _utc(r.created_at), "sent_at": _utc(r.sent_at),
            "failed_at": _utc(r.failed_at),
            "next_attempt_at": _utc(r.next_attempt_at) if r.status == "PENDING" else None,
            # accepted = Expo ticket ok (not yet confirmed); delivered = receipt ok.
            "devices": _devices(int(r.id)),
        }
        for r in rows
    ], int(total)
