"""The background loop: poll, filter, dedup, notify."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import sqlite3
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

import httpx

from . import config, db, foreup_client, notify
from .foreup_client import ForeUpError
from .models import TeeTimeSlot, Watch

log = logging.getLogger(__name__)

TZ = ZoneInfo(config.COURSE_TIMEZONE)


def local_now() -> dt.datetime:
    return dt.datetime.now(TZ)


def should_notify(
    existing: sqlite3.Row | None,
    now: dt.datetime,
    renotify_after_minutes: int,
) -> bool:
    """Decide whether a matching slot earns a push right now.

    - Never seen before, or seen but since closed and now reopened -> notify.
    - Still open since the last poll -> stay quiet until the cooldown expires,
      then re-notify once in case the first alert was missed.
    """
    if existing is None:
        return True
    if not existing["still_open"]:
        # It closed (someone booked it) and is open again -- a fresh opening.
        return True

    last_notified = existing["last_notified_at"]
    if not last_notified:
        return True

    try:
        previous = dt.datetime.fromisoformat(last_notified)
    except ValueError:
        return True
    if previous.tzinfo is None:
        previous = previous.replace(tzinfo=dt.timezone.utc)

    elapsed = now.astimezone(dt.timezone.utc) - previous
    return elapsed >= dt.timedelta(minutes=renotify_after_minutes)


@dataclass
class WatchResult:
    matches: list[TeeTimeSlot] = field(default_factory=list)
    fetches: int = 0
    failures: int = 0
    last_failure: str | None = None

    @property
    def all_fetches_failed(self) -> bool:
        return self.fetches > 0 and self.failures == self.fetches


async def process_watch(
    client: httpx.AsyncClient, watch: Watch, now: dt.datetime
) -> WatchResult:
    """Poll every course/date this watch cares about and notify on matches."""
    assert watch.id is not None
    today = now.date()
    dates = watch.candidate_dates(today)
    result = WatchResult()
    matches = result.matches

    for course_key in watch.courses:
        course = config.COURSES.get(course_key)
        if course is None:
            log.warning("Watch %s references unknown course %r", watch.id, course_key)
            continue

        for date in dates:
            if config.REQUEST_DELAY_SECONDS and result.fetches:
                # Space the requests out rather than bursting the whole
                # cycle at the course's server at once.
                await asyncio.sleep(config.REQUEST_DELAY_SECONDS)
            result.fetches += 1
            try:
                slots = await foreup_client.fetch_times(client, course, date)
            except ForeUpError as exc:
                # One bad course/date must not stop the rest of the cycle.
                result.failures += 1
                result.last_failure = str(exc)
                log.warning("Fetch failed for %s on %s: %s", course_key, date, exc)
                continue

            for slot in slots:
                # Slots earlier today have already teed off.
                if slot.start.replace(tzinfo=TZ) <= now:
                    continue
                if watch.matches(slot):
                    matches.append(slot)

    for slot in matches:
        existing = db.get_seen_slot(watch.id, slot.slot_key)
        notified = False
        if should_notify(existing, now, config.RENOTIFY_AFTER_MINUTES):
            notified = await notify.send_slot_alert(client, slot, watch)
        db.upsert_slot(watch.id, slot, notified=notified, now=now)

    # Anything we had open but that is no longer in the response got booked by
    # someone else -- stop re-notifying about it. Skipped when a fetch failed,
    # so a transient API error doesn't look like every slot disappearing.
    if not result.failures:
        db.close_missing_slots(watch.id, [s.slot_key for s in matches])

    return result


async def poll_once(
    client: httpx.AsyncClient, now: dt.datetime | None = None
) -> int:
    """One full cycle across all active watches. Returns matches found."""
    now = now or local_now()
    total = 0
    errors: list[str] = []

    for watch in db.list_watches(active_only=True):
        if watch.is_expired(now.date()):
            log.info("Watch %s has passed its date; deactivating", watch.id)
            db.set_watch_active(watch.id, False)
            continue
        try:
            result = await process_watch(client, watch, now)
        except Exception as exc:  # keep the loop alive no matter what
            errors.append(f"watch {watch.id}: {exc}")
            log.exception("Watch %s failed", watch.id)
            continue

        total += len(result.matches)
        # A watch whose every request failed is watching nothing -- surface
        # that rather than letting the dashboard look healthy while blind.
        if result.all_fetches_failed:
            errors.append(
                f"watch {watch.id}: all {result.failures} request(s) failed "
                f"({result.last_failure})"
            )

    db.record_poll("; ".join(errors) if errors else None)

    # Housekeeping runs at most daily, not on every 30s cycle.
    if db.due_for_purge(now):
        deleted = db.purge_old_slots(now, config.RETENTION_DAYS)
        if deleted:
            log.info(
                "Purged %s slot row(s) not seen in %s days",
                deleted,
                config.RETENTION_DAYS,
            )

    return total


async def run_forever(stop: asyncio.Event | None = None) -> None:
    """Poll on an interval until asked to stop."""
    stop = stop or asyncio.Event()
    log.info("Poller started (every %ss)", config.POLL_INTERVAL_SECONDS)

    async with httpx.AsyncClient(timeout=20.0) as client:
        while not stop.is_set():
            try:
                await poll_once(client)
            except Exception as exc:
                log.exception("Poll cycle failed")
                db.record_poll(str(exc))

            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=config.POLL_INTERVAL_SECONDS
                )
            except asyncio.TimeoutError:
                pass

    log.info("Poller stopped")
