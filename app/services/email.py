"""RESTAMP email notifications: address, verification, preferences, outbox (Phase 3).

ADDRESS + VERIFICATION
- One contact email per user (user_emails). Only a VERIFIED address receives
  transactional email. Setting a different address clears verification and
  invalidates every earlier code, so the new address must be verified again.
- Verification is a 6-digit code emailed to the address and typed into the
  app by the SAME signed-in user (authenticated endpoint). This is NOT the
  phone OTP mechanism: codes are random per request (secrets), expire after
  CODE_TTL_MINUTES, are single-use, allow CODE_MAX_ATTEMPTS wrong guesses,
  and only a keyed hash (HMAC-SHA256 bound to user + address) is stored. A
  code issued to another account, or for a previous address, can never match.
- Requests for codes are rate-limited per user (one per
  SEND_MIN_INTERVAL_SECONDS, SEND_MAX_PER_HOUR per hour).
- No enumeration: the address is not unique across accounts and no response
  depends on whether an address is used elsewhere.
- Codes never appear in logs or production responses. In an explicit
  development environment only, the API may return `debug_code` (the same
  convention as the local login code) so the flow is testable without a
  provider; production and staging never do.

OPTIONAL vs MANDATORY EMAIL
- Optional (user preferences apply): ENQUIRY, VISIT, LISTING.
- Mandatory account email (no opt-out): VERIFICATION, ACCOUNT (suspended /
  reactivated). Mandatory email still goes only to a verified address
  (except the verification email itself, which goes to the address being
  verified).

OUTBOX
- Events never send email inline. They insert an email_outbox row in a
  savepoint inside the caller's transaction (it commits or rolls back with
  the business change); UNIQUE (user_id, dedupe_key) makes that idempotent.
  A failure to queue never fails the business operation, except errors that
  have already killed the transaction (deadlock / lock timeout), which are
  re-raised exactly like in services/notifications.py.
- The worker claims due rows with SELECT ... FOR UPDATE SKIP LOCKED, marks
  them PROCESSING with a lease (`locked_at`) and commits BEFORE sending, so
  two workers (or processes) never hold the same row. A crashed worker's
  rows are reclaimed after LEASE_SECONDS. The provider gets a stable
  idempotency key per row, so a re-send after a crash is deduplicated where
  the provider supports it.
- Eligibility is re-checked at send time (address still the verified one,
  preference still on, account still active), so changes apply to mail that
  is already queued.
- Failures retry with exponential backoff up to MAX_ATTEMPTS, then FAILED.
  A refused recipient fails immediately. Only an error KIND is stored.
- With no provider configured nothing is queued and nothing is sent.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, status
from sqlalchemy import and_, null, or_, update
from sqlalchemy.exc import IntegrityError, OperationalError, PendingRollbackError
from sqlalchemy.orm import Session

from ..config import is_development, settings
from ..models import EmailOutbox, EmailVerificationCode, Notification, User, UserEmail
from . import email_providers, email_templates
from .email_providers import EmailMessage, EmailSendError

log = logging.getLogger("restamp.email")

CODE_TTL_MINUTES = 15
CODE_MAX_ATTEMPTS = 5
SEND_MIN_INTERVAL_SECONDS = 60
SEND_MAX_PER_HOUR = 5

MAX_ATTEMPTS = 6
# Delay before attempt N+1 after N failed attempts (exponential-style backoff).
BACKOFF_SECONDS = (60, 300, 900, 3600, 10800)
LEASE_SECONDS = 600
BATCH_SIZE = 20

INVALID_CODE = "Invalid or expired code"
_REMINDER_TEMPLATES = ("VISIT_REMINDER", "LEAD_REMINDER")
_MYSQL_TXN_FATAL = (1213, 1205)

_EMAIL_MAX = 254
_LOCAL_RE = re.compile(r"^[a-z0-9!#$%&'*+/=?^_`{|}~-]+(\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*$")
_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def _utcnow_naive() -> datetime:
    # Whole seconds: the DATETIME columns have no fractional part and MySQL
    # ROUNDS on insert, so a sub-second value could land in the future and a
    # just-queued row would not be "due" for the worker.
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


# --- address validation -------------------------------------------------------

def normalize_email(raw: str) -> str:
    """Canonical form used everywhere: trimmed and lower-cased ASCII.

    Raises ValueError for anything that is not a plain, deliverable-looking
    address: wrong shape, over-long parts, non-ASCII (no IDN/homograph
    look-alikes), whitespace or control characters (no header injection).
    """
    value = (raw or "").strip().lower()
    if not value or len(value) > _EMAIL_MAX or not value.isascii():
        raise ValueError("Invalid email address")
    if any(ch.isspace() or ord(ch) < 33 or ord(ch) == 127 for ch in value):
        raise ValueError("Invalid email address")
    if value.count("@") != 1:
        raise ValueError("Invalid email address")
    local, domain = value.split("@")
    if not (1 <= len(local) <= 64) or not _LOCAL_RE.match(local):
        raise ValueError("Invalid email address")
    labels = domain.split(".")
    if len(domain) > 253 or len(labels) < 2 or not all(_LABEL_RE.match(label) for label in labels):
        raise ValueError("Invalid email address")
    if len(labels[-1]) < 2 or labels[-1].isdigit():
        raise ValueError("Invalid email address")
    return value


def mask_email(value: str | None) -> str | None:
    """`a***@example.com` — enough for an admin to recognise, not the address."""
    if not value or "@" not in value:
        return None
    local, domain = value.split("@", 1)
    return f"{local[:1]}***@{domain}"


# --- code secrecy ---------------------------------------------------------------

def _secret(label: bytes) -> bytes:
    return hashlib.sha256(label + b":" + settings.JWT_SECRET.encode("utf-8")).digest()


def _code_hash(user_id: int, email: str, code: str) -> str:
    message = f"{int(user_id)}:{email}:{code}".encode("utf-8")
    return hmac.new(_secret(b"restamp-email-code-hash"), message, hashlib.sha256).hexdigest()


def _fernet() -> Fernet:
    return Fernet(base64.urlsafe_b64encode(_secret(b"restamp-email-code-enc")))


def delivery_available() -> bool:
    return bool(email_providers.get_provider().enabled)


# --- status / address / preferences ------------------------------------------------

def _row(db: Session, user_id: int, lock: bool = False) -> UserEmail | None:
    return db.get(UserEmail, int(user_id), with_for_update=lock) if lock else db.get(UserEmail, int(user_id))


def get_status(db: Session, user_id: int) -> dict:
    row = _row(db, user_id)
    return {
        "email": row.email if row else None,
        "verified": bool(row and row.verified_at is not None),
        "verified_at": row.verified_at.replace(tzinfo=timezone.utc) if row and row.verified_at else None,
        "delivery_available": delivery_available(),
        "preferences": {
            "enquiries": bool(row.pref_enquiries) if row else True,
            "visits": bool(row.pref_visits) if row else True,
            "listings": bool(row.pref_listings) if row else True,
        },
    }


def _invalidate_codes(db: Session, user_id: int, now: datetime) -> None:
    db.execute(
        update(EmailVerificationCode)
        .where(EmailVerificationCode.user_id == int(user_id), EmailVerificationCode.used_at.is_(None))
        .values(used_at=now)
    )


def _issue_code(db: Session, user_id: int, email: str) -> str | None:
    """Create a fresh code for (user, address) and queue its email.

    Returns the code ONLY for the development `debug_code` convention; callers
    must never log it. Raises 429 when the per-user limits are exceeded. With
    no provider outside development nothing is created (no code could reach
    the user) and None is returned.
    """
    now = _utcnow_naive()
    recent = (
        db.query(EmailVerificationCode.created_at)
        .filter(EmailVerificationCode.user_id == int(user_id),
                EmailVerificationCode.created_at >= now - timedelta(hours=1))
        .order_by(EmailVerificationCode.created_at.desc())
        .all()
    )
    if len(recent) >= SEND_MAX_PER_HOUR or (
        recent and (now - recent[0][0]).total_seconds() < SEND_MIN_INTERVAL_SECONDS
    ):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many requests")
    available = delivery_available()
    if not available and not is_development():
        return None
    _invalidate_codes(db, user_id, now)
    code = f"{secrets.randbelow(10 ** 6):06d}"
    record = EmailVerificationCode(
        user_id=int(user_id), email=email, code_hash=_code_hash(user_id, email, code),
        attempts=0, expires_at=now + timedelta(minutes=CODE_TTL_MINUTES), created_at=now,
    )
    db.add(record)
    db.flush()
    if available:
        _enqueue(
            db, user_id=user_id, category="VERIFICATION", template="VERIFY_EMAIL", to_email=email,
            payload={"code_enc": _fernet().encrypt(code.encode("ascii")).decode("ascii"), "code_id": int(record.id)},
            dedupe_key=f"verify:{record.id}", notification_id=None,
        )
    return code


def set_email(db: Session, user_id: int, raw_email: str) -> tuple[dict, str | None]:
    """Set (or change) the caller's address and start verification.

    ValueError for an invalid address (router -> 422). Re-submitting the
    address that is already verified is a no-op. Returns (status, code-for-
    development-only). Flushes; the router commits.
    """
    email = normalize_email(raw_email)
    row = _row(db, user_id, lock=True)
    now = _utcnow_naive()
    if row is not None and row.email == email and row.verified_at is not None:
        return get_status(db, user_id), None
    if row is None:
        row = UserEmail(user_id=int(user_id), email=email, verified_at=None, created_at=now, updated_at=now)
        db.add(row)
    else:
        row.email = email
        row.verified_at = None
        row.updated_at = now
    db.flush()
    code = _issue_code(db, user_id, email)
    return get_status(db, user_id), code


def resend_code(db: Session, user_id: int) -> tuple[dict, str | None]:
    """Send a new code for the caller's unverified address (409 when there is
    nothing to verify). Rate-limited like set_email."""
    row = _row(db, user_id, lock=True)
    if row is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No email address to verify")
    if row.verified_at is not None:
        return get_status(db, user_id), None
    code = _issue_code(db, user_id, row.email)
    return get_status(db, user_id), code


def verify_code(db: Session, user_id: int, code: str) -> bool:
    """Check `code` for the caller's current address. True = verified.

    False for every failure (no address, already used, expired, too many
    attempts, wrong code, a code issued to another account or address) — the
    router answers one generic message. A wrong guess is counted (flushed, the
    router commits it) and the code dies after CODE_MAX_ATTEMPTS.
    """
    row = _row(db, user_id, lock=True)
    if row is None or row.verified_at is not None:
        return False
    now = _utcnow_naive()
    record = (
        db.query(EmailVerificationCode)
        .filter(EmailVerificationCode.user_id == int(user_id),
                EmailVerificationCode.email == row.email,
                EmailVerificationCode.used_at.is_(None))
        .order_by(EmailVerificationCode.id.desc())
        .with_for_update()
        .first()
    )
    if record is None or record.expires_at <= now or record.attempts >= CODE_MAX_ATTEMPTS:
        return False
    candidate = (code or "").strip()
    if not (candidate.isdigit() and len(candidate) == 6) or not hmac.compare_digest(
        record.code_hash, _code_hash(user_id, row.email, candidate)
    ):
        record.attempts = int(record.attempts or 0) + 1
        if record.attempts >= CODE_MAX_ATTEMPTS:
            record.used_at = now  # exhausted: a new code must be requested
        db.flush()
        return False
    record.used_at = now
    row.verified_at = now
    row.updated_at = now
    db.flush()
    return True


def remove_email(db: Session, user_id: int) -> None:
    """Remove the caller's address (idempotent). Queued optional email for it
    is dropped at send time (the address no longer matches)."""
    row = _row(db, user_id, lock=True)
    _invalidate_codes(db, user_id, _utcnow_naive())
    if row is not None:
        db.delete(row)
    db.flush()


def set_preferences(db: Session, user_id: int, *, enquiries: bool, visits: bool, listings: bool) -> dict:
    """Optional-email preferences. Mandatory account email has no switch."""
    row = _row(db, user_id, lock=True)
    if row is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Add an email address first")
    row.pref_enquiries = bool(enquiries)
    row.pref_visits = bool(visits)
    row.pref_listings = bool(listings)
    row.updated_at = _utcnow_naive()
    db.flush()
    return get_status(db, user_id)


# --- queuing (inside the caller's transaction) -------------------------------------

def _pref_on(row: UserEmail, category: str) -> bool:
    if category == "ENQUIRY":
        return bool(row.pref_enquiries)
    if category == "VISIT":
        return bool(row.pref_visits)
    if category == "LISTING":
        return bool(row.pref_listings)
    return True  # mandatory categories have no preference


def _deliverable_address(db: Session, user_id: int, category: str) -> str | None:
    """The verified address that may receive `category` now, else None."""
    row = _row(db, user_id)
    if row is None or row.verified_at is None or not _pref_on(row, category):
        return None
    return row.email


def _mysql_code(exc: Exception) -> int | None:
    orig = getattr(exc, "orig", None)
    args = getattr(orig, "args", None) or ()
    return args[0] if args and isinstance(args[0], int) else None


def _enqueue(db: Session, *, user_id: int, category: str, template: str, to_email: str,
             payload: dict, dedupe_key: str, notification_id: int | None) -> bool:
    """Insert one outbox row in a savepoint. True when a new row was queued;
    False for a duplicate key or an isolated failure. Never commits."""
    try:
        with db.begin_nested():
            db.add(EmailOutbox(
                user_id=int(user_id), notification_id=notification_id, category=category, template=template,
                to_email=to_email, payload=payload, dedupe_key=dedupe_key[:191], status="PENDING",
                attempts=0, next_attempt_at=_utcnow_naive(), created_at=_utcnow_naive(),
            ))
            db.flush()
        return True
    except PendingRollbackError:
        raise
    except OperationalError as exc:
        if _mysql_code(exc) in _MYSQL_TXN_FATAL:
            log.error("Email %s aborted the transaction: %s", template, type(exc).__name__)
            raise
        if not db.is_active:
            raise
        log.error("Email %s not queued: %s", template, type(exc).__name__)
        return False
    except IntegrityError as exc:
        if not db.is_active:
            raise
        if _mysql_code(exc) != 1062:  # 1062 = already queued (the normal retry outcome)
            log.error("Email %s not queued: %s", template, type(exc).__name__)
        return False
    except Exception as exc:  # a savepoint-confined failure never breaks the business operation
        if not db.is_active:
            raise
        log.error("Email %s not queued: %s", template, type(exc).__name__)
        return False


def enqueue_for_notification(db: Session, notification: Notification, target_kind: str | None,
                             reminder_facts: dict | None = None) -> bool:
    """Queue the email twin of a NEWLY created in-app notification.

    Called only for a row notify_safely just inserted, so the Phase 1 rules
    carry over unchanged: never the actor, one per dedupe key, follow-up
    coalescing. Recipient and address come from the database (the
    notification's own user and that user's verified address) — never from a
    request. Nothing is queued without a provider, a verified address, or
    with the category switched off.
    """
    try:
        spec = email_templates.TEMPLATES.get(notification.type)
        if spec is None or not delivery_available():
            return False
        address = _deliverable_address(db, notification.user_id, spec.category)
        if address is None:
            return False
    except (PendingRollbackError, OperationalError):
        raise
    except Exception as exc:
        if not db.is_active:
            raise
        log.error("Email %s not queued: %s", notification.type, type(exc).__name__)
        return False
    return _enqueue(
        db, user_id=notification.user_id, category=spec.category, template=notification.type, to_email=address,
        # Reminder facts (visit id + scheduled time / enquiry id) ride along ONLY
        # for the two reminder templates, so the send-time re-check can use them;
        # every other email payload is exactly as in Phase 3.
        payload={"message": notification.body, "target": target_kind,
                 **({"facts": dict(reminder_facts)} if reminder_facts and notification.type in _REMINDER_TEMPLATES else {})},
        dedupe_key=f"n:{notification.dedupe_key}", notification_id=int(notification.id),
    )


def enqueue_account_suspended(db: Session, user_id: int, event_key: str) -> bool:
    """Mandatory account email for a suspension (there is no in-app notice: a
    suspended account cannot open the app). The admin's reason is never
    included. Needs a verified address."""
    if not delivery_available():
        return False
    address = _deliverable_address(db, user_id, "ACCOUNT")
    if address is None:
        return False
    return _enqueue(
        db, user_id=user_id, category="ACCOUNT", template="ACCOUNT_SUSPENDED", to_email=address,
        payload={}, dedupe_key=f"account:{int(user_id)}:suspended:{event_key}", notification_id=None,
    )


# --- delivery (worker; own sessions) ---------------------------------------------

def _claim(db: Session, limit: int) -> list[tuple[int, int]]:
    """Lock due rows, mark them PROCESSING and commit. Returns (id, attempts).
    SKIP LOCKED: a row another worker is claiming is simply not seen."""
    now = _utcnow_naive()
    rows = (
        db.query(EmailOutbox)
        .filter(or_(
            and_(EmailOutbox.status == "PENDING", EmailOutbox.next_attempt_at <= now),
            # A worker that died mid-send: reclaim after the lease.
            and_(EmailOutbox.status == "PROCESSING",
                 EmailOutbox.locked_at <= now - timedelta(seconds=LEASE_SECONDS)),
        ))
        .order_by(EmailOutbox.id)
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
        update(EmailOutbox)
        .where(EmailOutbox.id == outbox_id, EmailOutbox.status == "PROCESSING", EmailOutbox.attempts == attempts)
        .values(locked_at=None, **values)
    )
    db.commit()


def _prepare(db: Session, row: EmailOutbox) -> tuple[EmailMessage | None, str | None]:
    """(message, None) when the row may be sent now, else (None, reason)."""
    user = db.get(User, int(row.user_id))
    if user is None or user.status == "closed":
        return None, "account_closed"
    payload = row.payload or {}
    current = _row(db, row.user_id)
    if row.category == "VERIFICATION":
        if current is None or current.email != row.to_email:
            return None, "superseded"  # the address was changed or removed
        if current.verified_at is not None:
            return None, "already_verified"  # nothing left to verify: the code is not sent
        record = db.get(EmailVerificationCode, int(payload.get("code_id") or 0))
        if record is None or record.used_at is not None:
            return None, "superseded"  # a newer code replaced it (or it was used)
        if record.expires_at <= _utcnow_naive():
            return None, "expired"
        try:
            code = _fernet().decrypt(str(payload.get("code_enc") or "").encode("ascii")).decode("ascii")
        except (InvalidToken, ValueError):
            return None, "unreadable"
        subject, text, html = email_templates.render("VERIFY_EMAIL", code=code, code_minutes=CODE_TTL_MINUTES)
        return EmailMessage(row.to_email, subject, text, html), None
    if current is None or current.verified_at is None or current.email != row.to_email:
        return None, "address_changed"
    if row.category in email_templates.OPTIONAL_CATEGORIES:
        if user.status != "active":
            return None, "account_inactive"
        if not _pref_on(current, row.category):
            return None, "opted_out"
    if row.template in _REMINDER_TEMPLATES:
        # Reminder eligibility is re-checked at SEND time with the same rule the
        # push worker uses (services/reminders.reminder_drop_reason): a lead the
        # owner answered, or a visit cancelled / rescheduled / past / with an
        # inactive party since the scan, is dropped. No facts -> dropped.
        from . import reminders  # lazy: reminders imports notifications
        reason = reminders.reminder_drop_reason(db, row.template, payload.get("facts") or {}, _utcnow_naive())
        if reason:
            return None, reason
    subject, text, html = email_templates.render(
        row.template, message=payload.get("message"), target_kind=payload.get("target"))
    return EmailMessage(row.to_email, subject, text, html), None


def _deliver(db: Session, outbox_id: int, attempts: int, provider) -> str:
    row = db.get(EmailOutbox, outbox_id)
    if row is None or row.status != "PROCESSING":
        db.rollback()
        return "skipped"
    wipe = {"payload": null()}  # SQL NULL: the encrypted code is gone once the row is final if row.category == "VERIFICATION" else {}
    now = _utcnow_naive()
    try:
        message, reason = _prepare(db, row)
    except Exception as exc:
        log.error("Email %s could not be prepared: %s", row.template, type(exc).__name__)
        message, reason = None, "render_error"
    category = row.category
    db.rollback()  # end the read transaction: nothing is held while the provider is called
    if message is None:
        _finish(db, outbox_id, attempts, {"status": "FAILED", "failed_at": now, "last_error": reason, **wipe})
        return "dropped"
    try:
        message_id = provider.send(message, f"restamp-outbox-{outbox_id}")
    except EmailSendError as exc:
        kind = exc.kind
    except Exception as exc:  # an adapter bug must never crash the worker
        log.error("Email provider error: %s", type(exc).__name__)
        kind = "retry"
    else:
        _finish(db, outbox_id, attempts, {
            "status": "SENT", "sent_at": _utcnow_naive(), "provider": provider.name[:30],
            "provider_message_id": message_id, "last_error": None, **wipe,
        })
        return "sent"
    if kind == "recipient" or attempts >= MAX_ATTEMPTS:
        _finish(db, outbox_id, attempts, {
            "status": "FAILED", "failed_at": _utcnow_naive(), "provider": provider.name[:30],
            "last_error": kind if kind == "recipient" else f"{kind}_exhausted", **wipe,
        })
        log.error("Email %s failed permanently (%s)", category, kind)
        return "failed"
    delay = BACKOFF_SECONDS[min(attempts - 1, len(BACKOFF_SECONDS) - 1)]
    _finish(db, outbox_id, attempts, {
        "status": "PENDING", "next_attempt_at": _utcnow_naive() + timedelta(seconds=delay),
        "provider": provider.name[:30], "last_error": kind,
    })
    return "retry"


def process_outbox(limit: int = BATCH_SIZE, session_factory=None) -> dict:
    """One worker pass: claim due rows, send each, record the outcome.
    Safe to run from several workers/processes at once. Does nothing while no
    provider is configured (rows wait; verification codes simply expire)."""
    provider = email_providers.get_provider()
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
                log.error("Email outbox row failed: %s", type(exc).__name__)
        return counts
    finally:
        db.close()


# --- admin visibility ---------------------------------------------------------------

def admin_list(db: Session, page: int, page_size: int, *, status_filter: str | None = None,
               category: str | None = None, user_id: int | None = None) -> tuple[list[dict], int]:
    """Delivery records for administrators: status, category, recipient
    ACCOUNT (id) with a masked address, timestamps, attempts and the failure
    kind. Never the payload, a code, or provider credentials."""
    q = db.query(EmailOutbox)
    if status_filter:
        q = q.filter(EmailOutbox.status == status_filter)
    if category:
        q = q.filter(EmailOutbox.category == category)
    if user_id is not None:
        q = q.filter(EmailOutbox.user_id == int(user_id))
    total = q.count()
    rows = q.order_by(EmailOutbox.id.desc()).offset((page - 1) * page_size).limit(page_size).all()

    def _utc(value):
        return value.replace(tzinfo=timezone.utc) if value is not None else None

    return [
        {
            "id": int(r.id), "user_id": int(r.user_id), "recipient": mask_email(r.to_email),
            "category": r.category, "template": r.template, "status": r.status,
            "attempts": int(r.attempts or 0), "failure": r.last_error if r.status in ("FAILED", "PENDING") else None,
            "provider": r.provider, "created_at": _utc(r.created_at), "sent_at": _utc(r.sent_at),
            "failed_at": _utc(r.failed_at), "next_attempt_at": _utc(r.next_attempt_at) if r.status == "PENDING" else None,
        }
        for r in rows
    ], int(total)
