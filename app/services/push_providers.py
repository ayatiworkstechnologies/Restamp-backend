"""Push provider adapters (notification system Phase 4).

One small interface, selected by configuration (RESTAMP_PUSH_PROVIDER), so the
company can pick or change the provider without touching the outbox:

    provider.send(PushMessage, idempotency_key) -> provider message id | None

Adapters:
- DisabledProvider (default)  — never sends. `enabled` is False, and the push
  service then queues nothing and reports delivery as unavailable. Nothing
  device-related is contacted, stored or sent until an operator explicitly
  selects a provider.
- ExpoProvider                — Expo push HTTPS API over httpx (already a
  dependency). One message per device token; the provider ticket id is the
  idempotency result. The company's own Expo project / access token is set
  later via RESTAMP_EXPO_ACCESS_TOKEN (optional header, never logged).
- LogProvider                 — DEVELOPMENT ONLY: sends nothing and logs only
  non-sensitive metadata (title length, never a token or body). Treated as
  disabled in production.
- MemoryProvider              — tests only: keeps messages in a list.

Failure contract — every adapter raises PushSendError with a `kind`:
    "retry"           temporary (timeout, connection, 5xx)   -> retried with backoff
    "rate_limit"      provider throttling (429 / MessageRateExceeded)
                      -> retried with backoff
    "recipient"       the token was refused, e.g. DeviceNotRegistered
                      (permanent for that DEVICE)             -> row FAILED,
                      the device is marked INVALID
    "invalid_message" the message itself was rejected, e.g. MessageTooBig
                      (permanent for that MESSAGE, the device may be fine)
                      -> row FAILED, the device is NOT invalidated
    "config"          credentials rejected (401/403)          -> retried (an
                      operator can fix configuration), never loops fast
Error text from a provider is never stored or logged (it can echo token or
account details): only the kind is kept.

Credentials come only from the server environment (app/config.py). A provider
whose required settings are missing is reported as disabled — delivery is
never half-configured. Nothing here is imported by, or sent to, any client;
the full device token exists only inside the server (decrypted in this file's
call frame, never logged, never returned by an API).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from ..config import is_production, settings

log = logging.getLogger("restamp.push")

# Expo's public push endpoint. Requests carry ONLY the token and the fixed
# message fields built server-side from the outbox row.
_EXPO_URL = "https://exp.host/--/api/v2/push/send"
_EXPO_RECEIPTS_URL = "https://exp.host/--/api/v2/push/getReceipts"
# Expo answers getReceipts for at most 1000 ids per request.
RECEIPT_BATCH_MAX = 1000

# Expo error names (tickets and receipts) -> our failure kinds.
_EXPO_ERROR_KINDS = {
    "DeviceNotRegistered": "recipient",       # token dead: deactivate the device
    "MessageTooBig": "invalid_message",       # this message is bad, the device is fine
    "MessageRateExceeded": "rate_limit",      # retry later
    "InvalidCredentials": "config",           # operator must fix push credentials
    "MismatchSenderId": "config",
}


def expo_error_kind(name) -> str:
    """Map an Expo error name to a failure kind (unknown -> retry)."""
    return _EXPO_ERROR_KINDS.get(str(name or ""), "retry")


@dataclass(frozen=True)
class PushMessage:
    """One push for one device. `token` is the decrypted Expo push token;
    `payload` holds only target/notification ids (never tokens or text)."""

    to: str
    title: str
    body: str
    payload: dict


class PushSendError(Exception):
    """A delivery failure. `kind` is one of
    retry | rate_limit | recipient | invalid_message | config."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


class DisabledProvider:
    name = "disabled"
    enabled = False

    def send(self, message: PushMessage, idempotency_key: str) -> str | None:
        raise PushSendError("config")

    def get_receipts(self, ticket_ids: list[str]) -> dict[str, dict]:
        raise PushSendError("config")


class LogProvider:
    """Development only: nothing leaves the process."""

    name = "log"
    enabled = True

    def send(self, message: PushMessage, idempotency_key: str) -> str | None:
        log.info("Push not sent (log provider): title_len=%d", len(message.title))
        return None

    def get_receipts(self, ticket_ids: list[str]) -> dict[str, dict]:
        return {}  # nothing was sent, so there is never a ticket to look up


class MemoryProvider:
    """Tests only: captures messages; `fail_with` makes the next sends fail."""

    name = "memory"
    enabled = True

    def __init__(self) -> None:
        self.sent: list[tuple[PushMessage, str]] = []
        self.fail_with: str | None = None
        # Per-token scripted send failures: {token: kind}.
        self.fail_tokens: dict[str, str] = {}
        # Receipts: ticket id -> "ok" | an Expo error name. A ticket missing
        # here is "not ready yet" unless receipt_default is set.
        self.receipts: dict[str, str] = {}
        self.receipt_default: str | None = "ok"
        self.receipt_fail_with: str | None = None
        self.receipt_calls: list[list[str]] = []

    def send(self, message: PushMessage, idempotency_key: str) -> str | None:
        if self.fail_with:
            raise PushSendError(self.fail_with)
        if message.to in self.fail_tokens:
            raise PushSendError(self.fail_tokens[message.to])
        self.sent.append((message, idempotency_key))
        return f"mem-{len(self.sent)}"

    def get_receipts(self, ticket_ids: list[str]) -> dict[str, dict]:
        self.receipt_calls.append(list(ticket_ids))
        if self.receipt_fail_with:
            raise PushSendError(self.receipt_fail_with)
        out = {}
        for tid in ticket_ids:
            value = self.receipts.get(tid, self.receipt_default)
            if value is None:
                continue  # not ready yet
            out[tid] = {"status": "ok"} if value == "ok" else {"status": "error", "error": expo_error_kind(value), "name": value}
        return out


class ExpoProvider:
    name = "expo"
    enabled = True

    def __init__(self, access_token: str, timeout: float) -> None:
        self._access_token = (access_token or "").strip()
        self._timeout = timeout

    def send(self, message: PushMessage, idempotency_key: str) -> str | None:
        body = {
            "to": message.to,
            "title": message.title,
            "body": message.body,
            "data": message.payload or {},
            "sound": "default",
        }
        headers = {"Content-Type": "application/json"}
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        try:
            response = httpx.post(_EXPO_URL, headers=headers, json=body, timeout=self._timeout)
        except httpx.HTTPError:
            raise PushSendError("retry") from None
        code = response.status_code
        if 200 <= code < 300:
            # {"data":[{"status":"ok","id":"..."}]} for the single message sent.
            try:
                items = response.json().get("data") or []
                first = items[0] if items else {}
            except Exception:
                raise PushSendError("retry") from None
            status = first.get("status")
            if status == "ok":
                ticket = first.get("id")
                return str(ticket)[:191] if ticket else None
            # A ticket error: the message was NOT accepted.
            raise PushSendError(expo_error_kind((first.get("details") or {}).get("error")))
        if code == 429:
            raise PushSendError("rate_limit")
        if code in (401, 403):
            raise PushSendError("config")
        raise PushSendError("retry")

    def get_receipts(self, ticket_ids: list[str]) -> dict[str, dict]:
        """Receipts for accepted tickets: {ticket_id: {"status": "ok"} |
        {"status": "error", "error": kind, "name": expo_error_name}}. An id
        absent from the answer is not ready yet (Expo keeps receipts ~24 h).
        Raises PushSendError("retry" | "rate_limit" | "config") when the call
        itself fails; nothing else changes then."""
        headers = {"Content-Type": "application/json"}
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        try:
            response = httpx.post(_EXPO_RECEIPTS_URL, headers=headers,
                                  json={"ids": list(ticket_ids)[:RECEIPT_BATCH_MAX]}, timeout=self._timeout)
        except httpx.HTTPError:
            raise PushSendError("retry") from None
        code = response.status_code
        if code == 429:
            raise PushSendError("rate_limit")
        if code in (401, 403):
            raise PushSendError("config")
        if not 200 <= code < 300:
            raise PushSendError("retry")
        try:
            data = response.json().get("data") or {}
        except Exception:
            raise PushSendError("retry") from None
        out = {}
        for tid, receipt in (data.items() if isinstance(data, dict) else []):
            if not isinstance(receipt, dict):
                continue
            if receipt.get("status") == "ok":
                out[str(tid)] = {"status": "ok"}
            elif receipt.get("status") == "error":
                name = (receipt.get("details") or {}).get("error")
                out[str(tid)] = {"status": "error", "error": expo_error_kind(name), "name": str(name or "")[:60]}
        return out


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


def get_provider():
    """The configured provider, or DisabledProvider when the selection is
    unknown, incomplete, or a development-only adapter in production."""
    if _override is not None:
        return _override
    choice = (settings.PUSH_PROVIDER or "disabled").strip().lower()
    timeout = float(max(3, min(int(settings.PUSH_TIMEOUT_SECONDS or 15), 60)))
    if choice == "log":
        if is_production():
            _warn_once("RESTAMP_PUSH_PROVIDER=log is development-only; push stays disabled.")
            return DisabledProvider()
        return LogProvider()
    if choice == "expo":
        return ExpoProvider(settings.EXPO_ACCESS_TOKEN, timeout)
    return DisabledProvider()
