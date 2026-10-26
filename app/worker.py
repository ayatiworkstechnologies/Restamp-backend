"""Background worker for automatic property verification.

This module runs a periodic task that processes pending listings which have
been in PENDING state for more than 2 minutes, automatically transitioning
them to VERIFIED + AVAILABLE.

The worker is designed to:
- Run periodically (configurable interval, default 30 seconds)
- Process only eligible pending listings (submitted_at + 2 minutes in the past)
- Be idempotent and safe to run concurrently
- Survive restarts (deadline derived from persisted submitted_at)
- Use the existing verification audit trail with actor="auto"

In production (Render), this runs as part of the FastAPI app lifespan.
For local development, it runs in the same process.

The worker can be disabled by setting the environment variable
`RESTAMP_DISABLE_AUTO_VERIFY=1` (used in tests).
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from .db import SessionLocal, engine
from .models.properties import PropertyListing
from .services import owner_listings as owner_svc

log = logging.getLogger("restamp.worker")

# Worker configuration
WORKER_INTERVAL_SECONDS = 30  # How often to check for due listings
BATCH_SIZE = 100  # Max listings to process per iteration

def _worker_disabled() -> bool:
    """Check if worker should be disabled (at runtime, not import time)."""
    return os.environ.get("RESTAMP_DISABLE_AUTO_VERIFY", "0") == "1"


async def _process_due_listings() -> int:
    """Process all pending listings that are due for automatic verification.

    Returns the number of listings that were auto-verified.
    """
    db: Session = SessionLocal()
    try:
        from datetime import timedelta
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        # Find listings where submitted_at + 2 minutes <= now
        cutoff = now - timedelta(minutes=owner_svc._AUTO_VERIFY_MINUTES)

        due_listings = (
            db.query(PropertyListing.id)
            .filter(
                PropertyListing.verification_status == "PENDING",
                PropertyListing.listing_status == "AVAILABLE",
                PropertyListing.deleted_at.is_(None),
                PropertyListing.submitted_at.is_not(None),
                PropertyListing.submitted_at <= cutoff,
            )
            .limit(BATCH_SIZE)
            .all()
        )

        if not due_listings:
            return 0

        listing_ids = [row[0] for row in due_listings]
        log.info("Auto-verifying %d due listings: %s", len(listing_ids), listing_ids)

        verified_count = 0
        for listing_id in listing_ids:
            try:
                listing = owner_svc.transition_verification_automatic(db, listing_id)
                if listing is not None:
                    verified_count += 1
            except Exception as exc:
                log.exception("Failed to auto-verify listing %d: %s", listing_id, exc)
                # Continue with other listings

        if verified_count:
            db.commit()
            log.info("Auto-verified %d listings", verified_count)
        else:
            db.rollback()

        return verified_count
    except Exception as exc:
        log.exception("Error in auto-verification worker: %s", exc)
        db.rollback()
        return 0
    finally:
        db.close()


async def _worker_loop(stop_event: asyncio.Event) -> None:
    """Main worker loop that runs periodically."""
    log.info("Auto-verification worker started (interval=%ds)", WORKER_INTERVAL_SECONDS)
    while not stop_event.is_set():
        try:
            await _process_due_listings()
        except Exception as exc:
            log.exception("Worker iteration failed: %s", exc)
        try:
            # Wait for the interval or until stop_event is set
            await asyncio.wait_for(stop_event.wait(), timeout=WORKER_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            # Normal timeout, continue loop
            pass
    log.info("Auto-verification worker stopped")


@asynccontextmanager
async def worker_lifespan():
    """Lifespan context manager for the background worker.

    Usage in main.py:
        @asynccontextmanager
        async def lifespan(app):
            async with worker_lifespan():
                yield
    """
    if _worker_disabled():
        log.info("Auto-verification worker disabled (RESTAMP_DISABLE_AUTO_VERIFY=1)")
        yield
        return

    stop_event = asyncio.Event()
    task = asyncio.create_task(_worker_loop(stop_event))
    try:
        yield
    finally:
        stop_event.set()
        await task


def run_worker_once() -> int:
    """Synchronous helper for testing or manual invocation.

    Runs one iteration of the worker and returns the number of listings verified.
    """
    import asyncio
    return asyncio.run(_process_due_listings())