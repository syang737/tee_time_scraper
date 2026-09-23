#!/usr/bin/env python3
"""Why haven't I had an alert? Answers it for every watch, live.

Silence has three very different causes, and this tells them apart:

* **The poller isn't running, or every request is failing.** Shown at the
  top: when it last polled and the last error it recorded.
* **The API answers but nothing qualifies.** For each course and date: how
  many tee times came back, and which criterion turned each one away
  (time, players, holes). Weekend mornings are usually booked solid, so a
  long run of "0 match" is often just the truth.
* **Something matches but no alert arrived.** Matches are listed with their
  dedup state -- would alert now, or already alerted and when -- and the
  topic the alert goes to, which must be the one subscribed in the app.

It fetches through the poller's own code and reads the same database the
service uses, but never writes to it and never sends a push.

    cd ~/tee_time_scraper
    .venv/bin/python scripts/explain_watches.py
"""

from __future__ import annotations

import asyncio
import collections
import datetime as dt
import logging
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# DB_PATH and .env are relative to the repo, as they are for the service.
os.chdir(REPO)
sys.path.insert(0, str(REPO))

import httpx  # noqa: E402

from app import config, db, notify, poller  # noqa: E402
from app.models import Watch  # noqa: E402

REASONS = {
    "time": "outside the time window",
    "players": "too few open spots",
    "holes": "wrong number of holes",
    "past": "already teed off",
    "course": "not a watched course",
}


def ago(stamp: str | None, now: dt.datetime) -> str:
    if not stamp:
        return "never"
    try:
        then = dt.datetime.fromisoformat(stamp)
    except ValueError:
        return stamp
    if then.tzinfo is None:
        then = then.replace(tzinfo=dt.timezone.utc)
    seconds = int((now - then).total_seconds())
    if seconds < 120:
        return f"{seconds}s ago"
    if seconds < 7200:
        return f"{seconds // 60} min ago"
    if seconds < 172800:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} days ago"


def header(text: str) -> None:
    print(f"\n{'=' * 72}\n{text}\n{'=' * 72}")


def show_poller(now: dt.datetime) -> None:
    header("Poller")
    print(f"database: {Path(config.DB_PATH).resolve()}")
    status = db.get_poll_status()
    if status is None or not status["last_poll_at"]:
        print("It has never polled. Is the service running?")
        print("    systemctl status teetimes")
        return

    print(f"last poll:    {ago(status['last_poll_at'], now)} "
          f"(#{status['poll_count']})")
    print(f"last success: {ago(status['last_success_at'], now)}")
    try:
        last = dt.datetime.fromisoformat(status["last_poll_at"])
        if last.tzinfo is None:
            last = last.replace(tzinfo=dt.timezone.utc)
        stale = (now - last).total_seconds() > max(180, 4 * config.POLL_INTERVAL_SECONDS)
    except ValueError:
        stale = False
    if stale:
        print("  !! The poller has not run recently -- the service is probably")
        print("     stopped or crashed. Check:")
        print("         systemctl status teetimes")
        print("         journalctl -u teetimes -n 50")
    if status["last_error"]:
        print(f"last error:   {status['last_error']}")
    else:
        print("last error:   none")


def show_inactive(watches: list[Watch], today: dt.date) -> None:
    inactive = [w for w in watches if not w.active or w.is_expired(today)]
    if not inactive:
        return
    header("Inactive watches (not polled)")
    for w in inactive:
        why = ("its date has passed, so it switches itself off"
               if w.is_expired(today) else "paused in the GUI")
        print(f"#{w.id} {w.label or '(no label)'}: {why}")


def alerts_since(watch_id: int, since: dt.datetime) -> tuple[int, str | None]:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, MAX(last_notified_at) AS latest FROM seen_slots "
            "WHERE watch_id = ? AND last_notified_at >= ?",
            (watch_id, db.to_iso(since)),
        ).fetchone()
        latest = conn.execute(
            "SELECT MAX(last_notified_at) AS latest FROM seen_slots WHERE watch_id = ?",
            (watch_id,),
        ).fetchone()
    return row["n"], latest["latest"]


def dedup_state(watch: Watch, slot, now: dt.datetime) -> str:
    existing = db.get_seen_slot(watch.id, slot.slot_key)
    if poller.should_notify(existing, now, config.RENOTIFY_AFTER_MINUTES):
        if existing is None:
            return "new -- the service will alert on it"
        return "would alert now (reopened, or re-alert cooldown passed)"
    return (f"already alerted {ago(existing['last_notified_at'], now)}; "
            f"re-alerts every {config.RENOTIFY_AFTER_MINUTES} min while open")


def explain(watch: Watch, cycle: poller.Cycle, now: dt.datetime) -> None:
    topic = notify.topic_for(watch)
    header(f"#{watch.id} {watch.label or '(no label)'}")
    print(f"criteria: {watch.describe()}")
    print(f"alerts to ntfy topic: {topic or '!! NONE -- no alert can be sent'}")
    sent, latest = alerts_since(watch.id, now - dt.timedelta(days=3))
    print(f"alerts in the last 3 days: {sent} (most recent: {ago(latest, now)})")

    dates = watch.candidate_dates(now.date())
    if not dates:
        print("No dates to check -- nothing in the horizon falls on its days.")
        return

    matched_any = False
    answered = 0
    near_misses = []
    for date in dates:
        print(f"\n  {date.strftime('%a %b')} {date.day}")
        for course_key in watch.courses:
            course = config.COURSES.get(course_key)
            name = course.name if course else course_key
            key = (course_key, date)
            if course is None:
                print(f"    {name:<24} unknown course -- remove it from the watch")
                continue
            if key in cycle.failures:
                print(f"    {name:<24} FETCH FAILED: {cycle.failures[key][:90]}")
                continue
            if key in cycle.skipped:
                print(f"    {name:<24} skipped (provider throttled)")
                continue

            answered += 1
            slots = cycle.slots.get(key, [])
            # Counted by the first reason in this order, so the counts add up
            # to the tee times rejected; the time window is what usually bites.
            rejected: collections.Counter[str] = collections.Counter()
            matches = []
            for slot in slots:
                if slot.start.replace(tzinfo=poller.TZ) <= now:
                    rejected["past"] += 1
                    continue
                failed = watch.failed_criteria(slot)
                if not failed:
                    matches.append(slot)
                    continue
                rejected[failed[0]] += 1
                if len(failed) == 1:
                    near_misses.append((failed[0], slot))

            why = ", ".join(f"{n} {REASONS[r]}" for r, n in rejected.most_common())
            print(f"    {name:<24} {len(slots):>3} tee time(s), {len(matches)} match"
                  + (f"  ({why})" if why else ""))
            for slot in matches:
                matched_any = True
                print(f"        {slot.display_time:>8}  {slot.available_spots} open  "
                      f"-> {dedup_state(watch, slot, now)}")

    if not answered:
        print("\n  None of its courses could be checked this run, so this says")
        print("  nothing about availability. See the fetch failures below.")
    elif not matched_any:
        print("\n  Nothing matches right now, so no alert is the correct outcome.")
        if near_misses:
            print("  Closest misses (one criterion away):")
            wanted = {"players": 0, "time": 1, "holes": 2}
            near_misses.sort(key=lambda m: (wanted[m[0]], m[1].start))
            for reason, slot in near_misses[:6]:
                print(f"    {slot.display_date} {slot.display_time:>8}  "
                      f"{slot.course_name:<28} {slot.available_spots} open  "
                      f"-- {REASONS[reason]}")


async def main() -> int:
    # Failures are reported in the output itself; the poller's own log lines
    # would only repeat them out of order.
    logging.getLogger("app").setLevel(logging.CRITICAL)
    now = poller.local_now()
    utc_now = now.astimezone(dt.timezone.utc)
    print(f"Now: {now:%a %Y-%m-%d %H:%M %Z}")
    db.init_db()  # only creates tables if missing; the service already has

    show_poller(utc_now)

    watches = sorted(db.list_watches(), key=lambda w: w.id or 0)
    if not watches:
        header("Watches")
        print("There are no watches at all. Create one in the web GUI.")
        return 0
    show_inactive(watches, now.date())

    active = [w for w in watches if w.active and not w.is_expired(now.date())]
    if not active:
        print("\nNo active watches, so nothing is being polled.")
        return 0

    keys = poller.required_keys(active, now.date())
    print(f"\nFetching {len(keys)} course/date pair(s) live...")
    async with httpx.AsyncClient(timeout=20.0) as client:
        cycle = await poller.fetch_cycle(client, keys, now)

    for watch in active:
        explain(watch, cycle, now)

    failed = {k[0] for k in cycle.failures}
    if failed:
        header("Fetch failures")
        for course_key in sorted(failed):
            provider = config.COURSES[course_key].provider
            print(f"  {course_key} ({provider})")
        if any(config.COURSES[k].provider == "foreup" for k in failed):
            print("\n  Essex (ForeUp) failing is new -- it has always been open.")
            print("  Rate limiting or an IP block would look like HTTP 403/429.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
