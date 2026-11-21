"""Shared fixed-window rate limiting for the public Reva AI endpoints.

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
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request, status
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from ..config import settings
from ..models import RateLimitWindow

_RATE_LIMIT_DETAIL = "Too many requests. Please try again soon."


def client_ip(request: Request) -> str:
    """The client IP for rate-limit buckets, honouring the trusted proxy only.

    RESTAMP_CHAT_TRUST_PROXY=1 means a reverse proxy we control (Render) forwarded
    the real client address in X-Forwarded-For: the RIGHTMOST entry is the
    one that proxy appended (earlier entries are client-controlled and can
    never be trusted). Without the flag the header is ignored entirely, so a
    caller cannot rotate buckets by sending spoofed header values.
    """
    if settings.CHAT_TRUST_PROXY:
        forwarded = request.headers.get("x-forwarded-for", "")
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[-1][:191]
    return (request.client.host if request.client else "unknown")[:191]


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

    Raises a static 429 (with Retry-After) when any window is already full.
    The upsert is atomic in MySQL (row-locked duplicate-key update), so
    concurrent requests can never both end up under-counted.
    """
    if not limits:
        return
    now = _now()
    widest = max(l.seconds for l in limits)
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
            # Counted even when the caller is refused, so a flood cannot reset its
    # own counter by failing: commit before refusing.
            _prune(db, bucket_key, now, widest)
            db.commit()
            retry_after = max(1, int((start + timedelta(seconds=lim.seconds) - now).total_seconds()))
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=_RATE_LIMIT_DETAIL,
                headers={"Retry-After": str(retry_after)},
            )
    _prune(db, bucket_key, now, widest)
    # The counter must survive endpoint failures and rollbacks: commit the
    # hits independently of whatever the endpoint does next.
    db.commit()


def _prune(db: Session, bucket_key: str, now: datetime, keep_seconds: int) -> None:
    """Drop the bucket's superseded windows (older than the widest one)."""
    cutoff = now - timedelta(seconds=keep_seconds)
    db.query(RateLimitWindow).filter(
        RateLimitWindow.bucket_key == bucket_key,
        RateLimitWindow.window_start < cutoff,
    ).delete(synchronize_session=False)
