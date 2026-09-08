"""The background loop: poll, filter, dedup, notify."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import sqlite3
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

import httpx

from . import config, cps_client, db, ezlinks_client, foreup_client, notify
from .cps_client import CpsError
from .ezlinks_client import EzLinksError
from .foreup_client import ForeUpError
from .models import TeeTimeSlot, Watch

# Either provider failing to reach its API is the same thing to the poller.
FetchError = (ForeUpError, CpsError, EzLinksError)

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
    # A throttled provider left us without data for some course/date. Not an
    # error, but it does mean we cannot conclude anything about what vanished.
    incomplete: bool = False

    @property
    def all_fetches_failed(self) -> bool:
        return self.fetches > 0 and self.failures == self.fetches


# One cycle's worth of tee times, keyed by (course_key, date).
Key = tuple[str, dt.date]

# Providers whose API answers for several courses in a single request.
BATCHING_PROVIDERS = {"cps", "ezlinks"}

# When each provider was last called, so a throttled one can be skipped.
_last_called: dict[str, dt.datetime] = {}


@dataclass
class Cycle:
    """What a single poll cycle fetched, shared across every watch.

    Watches overlap heavily -- friends want the same courses on the same
    weekend -- so fetching per watch would send the same request many times
    and grow linearly with the number of people using this. Fetching each
    course/date once per cycle keeps the request count bounded by what is
    actually being watched, not by how many watches there are.
    """

    slots: dict[Key, list[TeeTimeSlot]] = field(default_factory=dict)
    failures: dict[Key, str] = field(default_factory=dict)
    # Not fetched this cycle because the provider is throttled. Distinct from
    # a failure: nothing is wrong, we simply have no data -- which must not
    # be read as "every slot got booked".
    skipped: set[Key] = field(default_factory=set)


def required_keys(watches: list[Watch], today: dt.date) -> set[Key]:
    """The distinct course/date pairs this set of watches needs."""
    keys: set[Key] = set()
    for watch in watches:
        for date in watch.candidate_dates(today):
            for course_key in watch.courses:
                if course_key in config.COURSES:
                    keys.add((course_key, date))
    return keys


def plan_requests(keys: set[Key]) -> list[tuple[str, dt.date, list[str]]]:
    """Group the needed course/date pairs into the fewest requests.

    ForeUp wants one request per course, but CPS takes a list of courseIds
    and answers for all of them at once -- so a whole Bergen weekend costs
    one request per date rather than one per course per date.
    """
    grouped: dict[tuple[str, dt.date], list[str]] = {}
    for course_key, date in keys:
        course = config.COURSES.get(course_key)
        if course is None:
            continue
        grouped.setdefault((course.provider, date), []).append(course_key)

    plan: list[tuple[str, dt.date, list[str]]] = []
    for (provider, date), course_keys in grouped.items():
        course_keys.sort()
        if provider in BATCHING_PROVIDERS:
            plan.append((provider, date, course_keys))  # one request, all courses
        else:
            plan.extend((provider, date, [key]) for key in course_keys)
    plan.sort(key=lambda item: (item[1], item[0], item[2]))
    return plan


async def _fetch_one(
    client: httpx.AsyncClient, provider: str, date: dt.date, course_keys: list[str]
) -> dict[str, list[TeeTimeSlot]]:
    courses = [config.COURSES[k] for k in course_keys]
    if provider == "cps":
        return await cps_client.fetch_times(client, courses, date)
    if provider == "ezlinks":
        return await ezlinks_client.fetch_times(client, courses, date)
    slots = await foreup_client.fetch_times(client, courses[0], date)
    return {courses[0].key: slots}


def is_throttled(provider: str, now: dt.datetime) -> bool:
    """True while a provider is inside its minimum gap between requests.

    Union is behind Cloudflare, and hitting it as hard as the open APIs is
    the fastest way to get the instance blocked, so it gets a longer leash
    than the poll interval.
    """
    minimum = config.PROVIDER_MIN_INTERVAL_SECONDS.get(provider, 0)
    if not minimum:
        return False
    last = _last_called.get(provider)
    if last is None:
        return False
    return (now - last).total_seconds() < minimum


async def fetch_cycle(
    client: httpx.AsyncClient, keys: set[Key], now: dt.datetime | None = None
) -> Cycle:
    """Fetch every needed course/date exactly once."""
    now = now or local_now()
    cycle = Cycle()
    for index, (provider, date, course_keys) in enumerate(plan_requests(keys)):
        if is_throttled(provider, now):
            for course_key in course_keys:
                cycle.skipped.add((course_key, date))
            continue
        _last_called[provider] = now
        if config.REQUEST_DELAY_SECONDS and index:
            # Space the requests out rather than bursting the whole cycle at
            # the course's server at once.
            await asyncio.sleep(config.REQUEST_DELAY_SECONDS)
        try:
            for course_key, slots in (
                await _fetch_one(client, provider, date, course_keys)
            ).items():
                cycle.slots[(course_key, date)] = slots
        except FetchError as exc:
            # One bad request must not stop the rest of the cycle. A batched
            # request covers several courses, so all of them fail together.
            for course_key in course_keys:
                cycle.failures[(course_key, date)] = str(exc)
            log.warning(
                "Fetch failed for %s on %s: %s", ", ".join(course_keys), date, exc
            )
    return cycle


async def process_watch(
    client: httpx.AsyncClient, watch: Watch, now: dt.datetime, cycle: Cycle
) -> WatchResult:
    """Match one watch against the cycle's slots and notify on what it wants."""
    assert watch.id is not None
    result = WatchResult()
    matches = result.matches

    for date in watch.candidate_dates(now.date()):
        for course_key in watch.courses:
            if course_key not in config.COURSES:
                log.warning("Watch %s references unknown course %r", watch.id, course_key)
                continue

            key = (course_key, date)
            if key in cycle.skipped:
                result.incomplete = True
                continue

            result.fetches += 1
            if key in cycle.failures:
                result.failures += 1
                result.last_failure = cycle.failures[key]
                continue

            for slot in cycle.slots.get(key, []):
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
    if not result.failures and not result.incomplete:
        db.close_missing_slots(watch.id, [s.slot_key for s in matches])

    return result


async def poll_once(
    client: httpx.AsyncClient, now: dt.datetime | None = None
) -> int:
    """One full cycle across all active watches. Returns matches found."""
    now = now or local_now()
    total = 0
    errors: list[str] = []

    active: list[Watch] = []
    for watch in db.list_watches(active_only=True):
        if watch.is_expired(now.date()):
            log.info("Watch %s has passed its date; deactivating", watch.id)
            db.set_watch_active(watch.id, False)
            continue
        active.append(watch)

    # Fetch once for everyone, then let each watch pick from the same data.
    cycle = await fetch_cycle(client, required_keys(active, now.date()), now)

    for watch in active:
        try:
            result = await process_watch(client, watch, now, cycle)
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
