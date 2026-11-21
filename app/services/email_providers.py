"""Email provider adapters (notification system Phase 3).

One small interface, selected by configuration (RESTAMP_EMAIL_PROVIDER), so
the company can pick or change the provider without touching the outbox:

    provider.send(EmailMessage, idempotency_key) -> provider message id | None

Adapters:
- DisabledProvider (default)  — never sends. `enabled` is False, and the
  email service then queues nothing and reports delivery as unavailable.
- ResendProvider              — Resend HTTPS API over httpx (already a
  dependency). Passes the outbox idempotency key so a retried request cannot
  deliver twice.
- SmtpProvider                — standard-library smtplib over STARTTLS/TLS;
  works with Amazon SES, SendGrid, Postmark, Mailgun or any SMTP relay.
- LogProvider                 — DEVELOPMENT ONLY: sends nothing and logs
  only non-sensitive metadata (template name, never the recipient or body).
  Treated as disabled in production.
- MemoryProvider              — tests only: keeps messages in a list.

Failure contract — every adapter raises EmailSendError with a `kind`:
    "retry"      temporary (timeout, connection, 5xx)        -> retried with backoff
    "rate_limit" provider throttling (429)                   -> retried with backoff
    "recipient"  the address was refused (permanent)         -> FAILED, no retry
    "config"     credentials/sender rejected (401/403/5xx auth) -> retried (an
                 operator can fix configuration), never loops fast
Error text from a provider is never stored or logged (it can contain the
recipient or account details): only the kind is kept.

Credentials come only from the server environment (app/config.py). A provider
whose required settings are missing is reported as disabled — delivery is
never half-configured. Nothing here is imported by, or sent to, any client.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage as _MimeMessage
from email.utils import formataddr, make_msgid

import httpx

from ..config import is_production, settings

log = logging.getLogger("restamp.email")


@dataclass(frozen=True)
class EmailMessage:
    to: str
    subject: str
    text: str
    html: str


class EmailSendError(Exception):
    """A delivery failure. `kind` is one of retry | rate_limit | recipient | config."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


class DisabledProvider:
    name = "disabled"
    enabled = False

    def send(self, message: EmailMessage, idempotency_key: str) -> str | None:
        raise EmailSendError("config")


class LogProvider:
    """Development only: nothing leaves the process."""

    name = "log"
    enabled = True

    def send(self, message: EmailMessage, idempotency_key: str) -> str | None:
        log.info("Email not sent (log provider): subject_len=%d", len(message.subject))
        return None


class MemoryProvider:
    """Tests only: captures messages; `fail_with` makes the next sends fail."""

    name = "memory"
    enabled = True

    def __init__(self) -> None:
        self.sent: list[tuple[EmailMessage, str]] = []
        self.fail_with: str | None = None

    def send(self, message: EmailMessage, idempotency_key: str) -> str | None:
        if self.fail_with:
            raise EmailSendError(self.fail_with)
        self.sent.append((message, idempotency_key))
        return f"mem-{len(self.sent)}"


class ResendProvider:
    name = "resend"
    enabled = True
    _URL = "https://api.resend.com/emails"

    def __init__(self, api_key: str, sender: str, timeout: float) -> None:
        self._api_key = api_key
        self._sender = sender
        self._timeout = timeout

    def send(self, message: EmailMessage, idempotency_key: str) -> str | None:
        try:
            response = httpx.post(
                self._URL,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    # Provider-side dedupe: a retry of the same outbox row
                    # (e.g. after a crash between send and commit) is not
                    # delivered a second time.
                    "Idempotency-Key": idempotency_key,
                },
                json={
                    "from": self._sender,
                    "to": [message.to],
                    "subject": message.subject,
                    "text": message.text,
                    "html": message.html,
                },
                timeout=self._timeout,
            )
        except httpx.HTTPError:
            raise EmailSendError("retry") from None
        code = response.status_code
        if 200 <= code < 300:
            try:
                value = response.json().get("id")
            except Exception:
                value = None
            return str(value)[:191] if value else None
        if code == 429:
            raise EmailSendError("rate_limit")
        if code in (401, 403):
            raise EmailSendError("config")
        if code in (400, 422):
            # Validation error: for a fixed, server-rendered template this
            # means the recipient address was refused.
            raise EmailSendError("recipient")
        raise EmailSendError("retry")


class SmtpProvider:
    name = "smtp"
    enabled = True

    def __init__(self, host: str, port: int, username: str, password: str,
                 sender_email: str, sender_name: str, timeout: float) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._sender_email = sender_email
        self._sender_name = sender_name
        self._timeout = timeout

    def send(self, message: EmailMessage, idempotency_key: str) -> str | None:
        mime = _MimeMessage()
        mime["From"] = formataddr((self._sender_name, self._sender_email))
        mime["To"] = message.to
        mime["Subject"] = message.subject
        message_id = make_msgid(idstring=idempotency_key[:40], domain=self._sender_email.rsplit("@", 1)[-1])
        mime["Message-ID"] = message_id
        mime.set_content(message.text)
        mime.add_alternative(message.html, subtype="html")
        context = ssl.create_default_context()  # certificate + hostname verification on
        try:
            if self._port == 465:
                client = smtplib.SMTP_SSL(self._host, self._port, timeout=self._timeout, context=context)
            else:
                client = smtplib.SMTP(self._host, self._port, timeout=self._timeout)
            with client:
                if self._port != 465:
                    client.starttls(context=context)  # never send credentials in clear text
                if self._username:
                    client.login(self._username, self._password)
                refused = client.send_message(mime)
            if refused:
                raise EmailSendError("recipient")
        except EmailSendError:
            raise
        except smtplib.SMTPRecipientsRefused:
            raise EmailSendError("recipient") from None
        except smtplib.SMTPAuthenticationError:
            raise EmailSendError("config") from None
        except smtplib.SMTPResponseException as exc:
            code = int(getattr(exc, "smtp_code", 0) or 0)
            if code in (421, 450, 451, 452):
                raise EmailSendError("rate_limit" if code in (421, 452) else "retry") from None
            if 500 <= code < 600:
                raise EmailSendError("recipient" if code in (550, 551, 553) else "config") from None
            raise EmailSendError("retry") from None
        except (OSError, smtplib.SMTPException):
            raise EmailSendError("retry") from None
        return message_id.strip("<>")[:191]


_override = None  # tests only (set_provider_for_tests)
_warned: set[str] = set()


def _warn_once(message: str) -> None:
    """Configuration problems are reported once per process, not on every
    worker tick or notification (the message names settings, never values)."""
    if message not in _warned:
        _warned.add(message)
        log.error(message)


def set_provider_for_tests(provider) -> None:
    """Tests only: force a provider (None restores configuration)."""
    global _override
    _override = provider


def _sender() -> str:
    address = (settings.EMAIL_FROM or "").strip()
    name = (settings.EMAIL_FROM_NAME or "").strip()
    return formataddr((name, address)) if name else address


def get_provider():
    """The configured provider, or DisabledProvider when the selection is
    unknown, incomplete, or a development-only adapter in production."""
    if _override is not None:
        return _override
    choice = (settings.EMAIL_PROVIDER or "disabled").strip().lower()
    sender_email = (settings.EMAIL_FROM or "").strip()
    timeout = float(max(3, min(int(settings.EMAIL_TIMEOUT_SECONDS or 15), 60)))
    if choice == "log":
        if is_production():
            _warn_once("RESTAMP_EMAIL_PROVIDER=log is development-only; email stays disabled.")
            return DisabledProvider()
        return LogProvider()
    if choice in ("resend", "smtp") and "@" not in sender_email:
        _warn_once("Email provider selected but RESTAMP_EMAIL_FROM is not set; email stays disabled.")
        return DisabledProvider()
    if choice == "resend":
        if not settings.RESEND_API_KEY:
            _warn_once("RESTAMP_RESEND_API_KEY is not set; email stays disabled.")
            return DisabledProvider()
        return ResendProvider(settings.RESEND_API_KEY, _sender(), timeout)
    if choice == "smtp":
        if not settings.SMTP_HOST:
            _warn_once("RESTAMP_SMTP_HOST is not set; email stays disabled.")
            return DisabledProvider()
        return SmtpProvider(settings.SMTP_HOST, int(settings.SMTP_PORT or 587), settings.SMTP_USERNAME,
                            settings.SMTP_PASSWORD, sender_email, (settings.EMAIL_FROM_NAME or "").strip(), timeout)
    return DisabledProvider()
