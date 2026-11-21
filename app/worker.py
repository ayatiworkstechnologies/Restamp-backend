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


# --- Email outbox delivery (notification system Phase 3) -----------------------------
# Runs beside the verification loop in the same process (no extra
# infrastructure). services/email.process_outbox claims rows with
# FOR UPDATE SKIP LOCKED and a PROCESSING lease, so several Uvicorn
# processes may run this loop at once without ever sending a row twice, and
# rows of a worker that died are picked up again after the lease. The blocking
# provider call runs in a thread so the event loop keeps serving requests.
EMAIL_INTERVAL_SECONDS = 15


def _email_worker_disabled() -> bool:
    """Runtime check (tests set RESTAMP_DISABLE_EMAIL_WORKER=1)."""
    return os.environ.get("RESTAMP_DISABLE_EMAIL_WORKER", "0") == "1"


async def _email_loop(stop_event: asyncio.Event) -> None:
    from .services import email as email_svc

    log.info("Email outbox worker started (interval=%ds)", EMAIL_INTERVAL_SECONDS)
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(email_svc.process_outbox)
        except Exception as exc:
            log.error("Email outbox iteration failed: %s", type(exc).__name__)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=EMAIL_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
    log.info("Email outbox worker stopped")


# --- Push outbox delivery (notification system Phase 4) ------------------------------
# Same contract as the email loop: services/push.process_outbox claims rows
# with FOR UPDATE SKIP LOCKED and a PROCESSING lease, so several Uvicorn
# processes may run this loop at once without ever sending a row twice. With
# the default disabled provider the loop does nothing and no push endpoint is
# contacted.
PUSH_INTERVAL_SECONDS = 15


def _push_worker_disabled() -> bool:
    """Runtime check (tests set RESTAMP_DISABLE_PUSH_WORKER=1)."""
    return os.environ.get("RESTAMP_DISABLE_PUSH_WORKER", "0") == "1"


async def _push_loop(stop_event: asyncio.Event) -> None:
    from .services import push as push_svc

    log.info("Push outbox worker started (interval=%ds)", PUSH_INTERVAL_SECONDS)
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(push_svc.process_outbox)
            # Expo receipts: a ticket is not delivery (services/push.poll_receipts).
            await asyncio.to_thread(push_svc.poll_receipts)
        except Exception as exc:
            log.error("Push outbox iteration failed: %s", type(exc).__name__)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=PUSH_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
    log.info("Push outbox worker stopped")


# --- Smart reminders (notification system Phase 4) -----------------------------------
# services/reminders.run_scan scans due CONFIRMED visits (T-24h / T-1h) and
# unanswered enquiries (2h/24h/72h) and queues reminder notifications whose
# email/push twins are delivered by the loops above.
REMINDERS_INTERVAL_SECONDS = 60


def _reminders_worker_disabled() -> bool:
    """Runtime check (tests set RESTAMP_DISABLE_REMINDER_WORKER=1)."""
    return os.environ.get("RESTAMP_DISABLE_REMINDER_WORKER", "0") == "1"


async def _reminders_loop(stop_event: asyncio.Event) -> None:
    from .services import reminders as reminders_svc

    log.info("Reminders worker started (interval=%ds)", REMINDERS_INTERVAL_SECONDS)
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(reminders_svc.run_scan)
        except Exception as exc:
            log.error("Reminders iteration failed: %s", type(exc).__name__)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=REMINDERS_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
    log.info("Reminders worker stopped")


@asynccontextmanager
async def worker_lifespan():
    """Lifespan context manager for the background worker.

    Usage in main.py:
        @asynccontextmanager
        async def lifespan(app):
            async with worker_lifespan():
                yield
    """
    stop_event = asyncio.Event()
    tasks = []
    if _worker_disabled():
        log.info("Auto-verification worker disabled (RESTAMP_DISABLE_AUTO_VERIFY=1)")
    else:
        tasks.append(asyncio.create_task(_worker_loop(stop_event)))
    if _email_worker_disabled():
        log.info("Email outbox worker disabled (RESTAMP_DISABLE_EMAIL_WORKER=1)")
    else:
        tasks.append(asyncio.create_task(_email_loop(stop_event)))
    if _push_worker_disabled():
        log.info("Push outbox worker disabled (RESTAMP_DISABLE_PUSH_WORKER=1)")
    else:
        tasks.append(asyncio.create_task(_push_loop(stop_event)))
    if _reminders_worker_disabled():
        log.info("Reminders worker disabled (RESTAMP_DISABLE_REMINDER_WORKER=1)")
    else:
        tasks.append(asyncio.create_task(_reminders_loop(stop_event)))
    try:
        yield
    finally:
        stop_event.set()
        for task in tasks:
            await task


def run_worker_once() -> int:
    """Synchronous helper for testing or manual invocation.

    Runs one iteration of the worker and returns the number of listings verified.
    """
    import asyncio
    return asyncio.run(_process_due_listings())