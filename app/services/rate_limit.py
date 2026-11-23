"""Shared fixed-window rate limiting (Reva AI endpoints + M8 communication).

The counters live in the `rate_limit_windows` table (see
models/rate_limit.py) so a limit is enforced across backend instances and
survives restarts: MySQL is the shared store every instance already has
(there is no Redis in this stack). Each bucket is keyed by the caller's
client IP or authenticated user id — the same low-sensitivity identity the
contact-audit trail already uses — never by phone number or token.

Every hit is COMMITTED inside the caller's request session before the
endpoint does its own work, so rejected requests still count (a flood cannot
reset its own counter by failing) and an endpoint failure can never roll the
count back. In the test suite the same commit only releases the conftest
savepoint, so buckets stay isolated per test and the teardown discards them.

Buckets are pruned lazily: each hit deletes the bucket's windows that are
older than the widest window in use, keeping at most one row per limit.
Buckets that are never hit again are removed by the bounded background
cleanup (cleanup_expired_windows, run by app.worker).

M8 communication limits (comm_limit): one policy table (COMM_POLICY) for the
eight write endpoints, applied per user AND per client network, keyed
`comm:<endpoint>:user:<id>` and `comm:<endpoint>:ip:<hmac>`. The network is
the IPv4 address, or the IPv6 /64, and is stored only as HMAC-SHA256 with the
dedicated RESTAMP_RATE_LIMIT_KEY secret (never plaintext, never an unkeyed
hash, never the JWT secret). Reva keeps its existing `chat:ip:<ip>` keys so
its active counters are not reset (see client_ip).

When several windows are exceeded, the 429's Retry-After is the LONGEST
remaining wait, so a client that waits it out is not refused again.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request, status
from sqlalchemy import Connection, text
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from ..config import is_production, settings
from ..models import RateLimitWindow

log = logging.getLogger("restamp.rate_limit")

_RATE_LIMIT_DETAIL = "Too many requests. Please try again soon."


def client_ip(request: Request) -> str:
    """The client IP for rate-limit buckets, honouring the trusted proxy only.

    RESTAMP_TRUST_PROXY=1 (alias RESTAMP_CHAT_TRUST_PROXY) means a reverse
    proxy we control (Render) forwarded the real client address in
    X-Forwarded-For: the RIGHTMOST entry is the one that proxy appended
    (earlier entries are client-controlled and can never be trusted). Without
    the flag the header is ignored entirely, so a caller cannot rotate buckets
    by sending spoofed header values. Reva uses this raw value for its
    existing keys; M8 keys go through ip_bucket_id (normalized + HMAC).
    """
    if settings.FORWARDED_IP_TRUSTED:
        forwarded = request.headers.get("x-forwarded-for", "")
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[-1][:191]
    return (request.client.host if request.client else "unknown")[:191]


# Local development / tests only (refused in production by
# validate_security_config, which requires a strong dedicated key).
_DEV_RATE_LIMIT_KEY = "restamp-local-only-rate-limit-key-not-for-production-use"


def _hmac_key() -> bytes:
    key = (settings.RATE_LIMIT_KEY or "").strip()
    if key:
        return key.encode()
    if is_production():
        # Startup refuses this; never fall back to plaintext keys.
        raise RuntimeError("RESTAMP_RATE_LIMIT_KEY is not configured")
    return _DEV_RATE_LIMIT_KEY.encode()


def normalize_network(raw_ip: str) -> str:
    """The rate-limit identity for a client address.

    IPv4: the address itself. IPv6: its /64 network (a single subscriber is
    usually allocated a whole /64, so rotating the interface id must not
    yield new buckets). IPv4-mapped IPv6 is treated as IPv4. Anything that
    is not an IP address (e.g. the test client's "testclient") is kept as an
    opaque value, prefixed so it can never collide with an address.
    """
    value = (raw_ip or "").strip()
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return f"raw:{value[:191]}"
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if isinstance(addr, ipaddress.IPv4Address):
        return f"v4:{addr}"
    net = ipaddress.IPv6Network(f"{addr}/64", strict=False)
    return f"v6:{net.network_address}/64"


def ip_bucket_id(request: Request) -> str:
    """HMAC-SHA256 (hex) of the normalized client network: stable across
    workers and instances (same key), never reversible to the IP without the
    secret. The IP itself is never logged or stored by M8."""
    network = normalize_network(client_ip(request))
    return hmac.new(_hmac_key(), network.encode(), hashlib.sha256).hexdigest()


def _now() -> datetime:
    # Naive UTC like every other timestamp in this codebase.
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


@dataclass(frozen=True)
class Limit:
    """One fixed window: at most `count` hits per `seconds` for the bucket."""

    name: str
    count: int
    seconds: int


def _window_start(now: datetime, seconds: int) -> datetime:
    """Fixed window aligned to the epoch (stable across instances)."""
    epoch = int(now.replace(tzinfo=timezone.utc).timestamp())
    aligned = epoch - (epoch % seconds)
    return datetime.fromtimestamp(aligned, tz=timezone.utc).replace(tzinfo=None)


def check_and_hit(db: Session, bucket_key: str, limits: tuple[Limit, ...]) -> None:
    """Record one hit for the bucket against every limit, or raise 429.

    Every configured window is counted for every request (refused requests
    included). When any window is over its limit, raises a static 429 whose
    Retry-After is the LONGEST remaining wait among the exceeded windows.
    The upsert is atomic in MySQL (row-locked duplicate-key update), so
    concurrent requests can never both end up under-counted.
    """
    if not limits:
        return
    now = _now()
    widest = max(l.seconds for l in limits)
    waits: list[int] = []
    for lim in limits:
        if lim.count <= 0:
            continue  # disabled limit (explicit configuration, local tests only)
        start = _window_start(now, lim.seconds)
        stmt = (
            mysql_insert(RateLimitWindow)
            .values(bucket_key=bucket_key, window_start=start, hit_count=1)
            .on_duplicate_key_update(hit_count=RateLimitWindow.hit_count + 1)
        )
        db.execute(stmt)
        row = (
            db.query(RateLimitWindow)
            .filter(RateLimitWindow.bucket_key == bucket_key, RateLimitWindow.window_start == start)
            .one()
        )
        if row.hit_count > lim.count:
            waits.append(max(1, int((start + timedelta(seconds=lim.seconds) - now).total_seconds())))
    _prune(db, bucket_key, now, widest)
    # The counter must survive endpoint failures and rollbacks (and a refusal
    # must still count): commit the hits independently of what follows.
    db.commit()
    if waits:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=_RATE_LIMIT_DETAIL,
            headers={"Retry-After": str(max(waits))},
        )


def _prune(db: Session, bucket_key: str, now: datetime, keep_seconds: int) -> None:
    """Drop the bucket's superseded windows (older than the widest one)."""
    cutoff = now - timedelta(seconds=keep_seconds)
    db.query(RateLimitWindow).filter(
        RateLimitWindow.bucket_key == bucket_key,
        RateLimitWindow.window_start < cutoff,
    ).delete(synchronize_session=False)


# --- M8: communication limits ---------------------------------------------------

_TEN_MIN = 600
_DAY = 86_400

# THE M8 policy (approved numbers; do not change without approval):
# endpoint -> (per-user limits, per-IP limits).
COMM_POLICY: dict[str, tuple[tuple[Limit, ...], tuple[Limit, ...]]] = {
    "enquiry_create": (
        (Limit("user:10m", 20, _TEN_MIN), Limit("user:day", 100, _DAY)),
        (Limit("ip:10m", 60, _TEN_MIN),),
    ),
    "lead_message": (
        (Limit("user:10m", 60, _TEN_MIN), Limit("user:day", 300, _DAY)),
        (Limit("ip:10m", 120, _TEN_MIN),),
    ),
    "buyer_visit_create": (
        (Limit("user:10m", 5, _TEN_MIN), Limit("user:day", 20, _DAY)),
        (Limit("ip:10m", 30, _TEN_MIN),),
    ),
    "owner_visit_create": (
        (Limit("user:10m", 20, _TEN_MIN), Limit("user:day", 100, _DAY)),
        (Limit("ip:10m", 60, _TEN_MIN),),
    ),
    # PATCH /buyer/visits/{id} and PATCH /owner/visits/{id} (one group each).
    "buyer_visit_status": ((Limit("user:10m", 60, _TEN_MIN),), (Limit("ip:10m", 120, _TEN_MIN),)),
    "owner_visit_status": ((Limit("user:10m", 60, _TEN_MIN),), (Limit("ip:10m", 120, _TEN_MIN),)),
    "listing_contact": ((Limit("user:10m", 30, _TEN_MIN),), (Limit("ip:10m", 60, _TEN_MIN),)),
    "lead_contact": ((Limit("user:10m", 60, _TEN_MIN),), (Limit("ip:10m", 120, _TEN_MIN),)),
}


def comm_limit(db: Session, endpoint: str, user_id: int, request: Request) -> None:
    """Apply the M8 per-user AND per-IP limits for one communication endpoint.

    MUST be the first statement of the route (after authentication, before
    any business read-modify-write): check_and_hit commits the request
    session, so nothing of the endpoint's own work may be pending. Both
    buckets are always counted; a 429 carries the longest Retry-After.
    Keys never contain a phone number, token or request-body value.
    """
    if settings.COMM_RATE_LIMITS_DISABLED and not is_production():
        return  # local test suite only; production always enforces the limits
    user_limits, ip_limits = COMM_POLICY[endpoint]
    blocked: list[HTTPException] = []
    for key, limits in (
        (f"comm:{endpoint}:user:{int(user_id)}", user_limits),
        (f"comm:{endpoint}:ip:{ip_bucket_id(request)}", ip_limits),
    ):
        try:
            check_and_hit(db, key, limits)
        except HTTPException as exc:
            if exc.status_code != status.HTTP_429_TOO_MANY_REQUESTS:
                raise
            blocked.append(exc)  # keep counting the other bucket
    if blocked:
        wait = max(int(e.headers["Retry-After"]) for e in blocked)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=_RATE_LIMIT_DETAIL,
            headers={"Retry-After": str(wait)},
        )


# --- bounded cleanup -------------------------------------------------------------

CLEANUP_RETENTION_SECONDS = _DAY  # approved retention: windows older than one day
CLEANUP_BATCH_SIZE = 1000
CLEANUP_MAX_BATCHES = 50
_CLEANUP_LOCK = "restamp_rate_limit_cleanup"


def cleanup_expired_windows(
    conn: Connection,
    *,
    batch_size: int = CLEANUP_BATCH_SIZE,
    max_batches: int = CLEANUP_MAX_BATCHES,
    now: datetime | None = None,
) -> int:
    """Delete windows that STARTED more than one day ago, in bounded batches.

    Runs on ONE dedicated connection (`conn`, e.g. `engine.connect()`): the
    MySQL named lock is per connection, so lock, deletes and release must
    share it. The widest window in use is one day, so such a row can never
    count again. Each batch is `DELETE ... ORDER BY id LIMIT n` (never a
    table-wide delete), committed on its own; ordering by the primary key
    walks the oldest rows first without a window_start index. At most
    `max_batches` per run. GET_LOCK (non-blocking) prevents overlapping runs
    across workers and instances: if another run holds it, this returns 0.
    Returns the number of rows deleted (logged as a count only).
    """
    cutoff = (now or _now()) - timedelta(seconds=CLEANUP_RETENTION_SECONDS)
    got = conn.execute(text("SELECT GET_LOCK(:n, 0)"), {"n": _CLEANUP_LOCK}).scalar()
    conn.commit()
    if got != 1:
        return 0
    deleted = 0
    try:
        for _ in range(max(0, int(max_batches))):
            result = conn.execute(
                text("DELETE FROM rate_limit_windows WHERE window_start < :cutoff ORDER BY id LIMIT :n"),
                {"cutoff": cutoff, "n": max(1, int(batch_size))},
            )
            conn.commit()
            count = result.rowcount or 0
            deleted += count
            if count < batch_size:
                break
    finally:
        conn.execute(text("SELECT RELEASE_LOCK(:n)"), {"n": _CLEANUP_LOCK})
        conn.commit()
    if deleted:
        log.info("Rate-limit cleanup removed %d expired windows", deleted)
    return deleted
