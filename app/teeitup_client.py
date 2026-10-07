"""Client for TeeItUp (Somerset County).

The booking site (``somerset-group-v2.book.teeitup.com``) is only a front
end. The tee sheet comes from TeeItUp's backend, a separate host that serves
every TeeItUp site and tells them apart by an ``X-Be-Alias`` header naming
the site. The search is the same anonymous call the page makes before
anyone logs in:

    GET {TEEITUP_API_URL}/v2/tee-times?date=YYYY-MM-DD&facilityIds=7092,7083
    X-Be-Alias: somerset-group-v2

``facilityIds`` takes the same list as the site's ``?course=`` parameter, so
the whole county costs one request per date. That backend is not behind
Cloudflare, so plain httpx is used, as for ForeUp.

The response is a list with one entry per course and day::

    [{"courseId": "<mongo id>", "dayInfo": {...}, "teetimes": [
        {"teetime": "2026-10-10T11:20:00.000Z", "maxPlayers": 4,
         "bookedPlayers": 2, "rates": [
            {"name": "18 Holes", "holes": 18, "greenFeeWalking": 5400,
             "allowedPlayers": [1, 2], "golfnow": {"GolfFacilityId": 7093}}]}]}]

Four things to know, all handled below:

* **Times are UTC.** They are converted to the courses' own clock.
* **Prices are in cents.**
* **One start can be sold as both 9 and 18 holes**, as separate rates on the
  same tee time. That is one opening, so it becomes one slot that knows both
  lengths, rather than two alerts.
* **The course id on each entry is a Mongo id**, not the numeric one we ask
  with. The numeric id comes back inside each rate as
  ``golfnow.GolfFacilityId``, which is what entries are matched on.

Field names come from two independent public captures of this API rather
than from one of ours: this sandbox cannot reach teeitup.com. Parsing is
tolerant for that reason, and ``scripts/verify_teeitup.py`` checks the live
shape from the server.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from . import config
from .models import TeeTimeSlot

log = logging.getLogger(__name__)

TZ = ZoneInfo(config.COURSE_TIMEZONE)


class TeeItUpError(RuntimeError):
    """Raised when the API is unreachable or returns something unusable."""


def headers_for(alias: str) -> dict[str, str]:
    """What the booking page sends; the alias header is the one that matters."""
    site = f"https://{alias}.book.teeitup.com"
    return {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": site,
        "Referer": f"{site}/",
        "X-Be-Alias": alias,
    }


def build_params(courses: list[config.TeeItUpCourse], date: dt.date) -> dict[str, str]:
    return {
        "date": date.isoformat(),
        "facilityIds": ",".join(c.course_id for c in courses),
    }


def _as_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _parse_start(value: Any) -> dt.datetime | None:
    """UTC ISO timestamp -> naive local time, like every other provider."""
    if not isinstance(value, str):
        return None
    try:
        moment = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        log.warning("Unrecognized TeeItUp tee time format: %r", value)
        return None
    if moment.tzinfo is None:
        # Not seen in practice; a bare time is taken as already local.
        return moment
    return moment.astimezone(TZ).replace(tzinfo=None)


def _rates(entry: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in entry.get("rates") or [] if isinstance(r, dict)]


def _facility_id(entry: dict[str, Any], day: dict[str, Any]) -> str | None:
    """The numeric facility id an entry belongs to."""
    for rate in _rates(entry):
        golfnow = rate.get("golfnow")
        if isinstance(golfnow, dict) and golfnow.get("GolfFacilityId") is not None:
            return str(golfnow["GolfFacilityId"])
    for source in (entry, day):
        for name in ("facilityId", "golfFacilityId"):
            if source.get(name) is not None:
                return str(source[name])
    return None


def available_spots(entry: dict[str, Any]) -> int:
    """Open spots: capacity less what is already booked.

    When ``bookedPlayers`` is absent the slot is taken as empty, but never
    more open than the largest party any rate will sell to.
    """
    capacity = _as_int(entry.get("maxPlayers"))
    booked = _as_int(entry.get("bookedPlayers")) or 0
    allowed = [
        n for r in _rates(entry) for n in (r.get("allowedPlayers") or [])
        if isinstance(n, int)
    ]
    if capacity is None:
        capacity = max(allowed) if allowed else 0
    spots = capacity - booked
    if allowed:
        spots = min(spots, max(allowed))
    return max(spots, 0)


def holes_options(entry: dict[str, Any]) -> tuple[int, ...]:
    return tuple(sorted({
        h for h in (_as_int(r.get("holes")) for r in _rates(entry)) if h
    }))


def green_fee(entry: dict[str, Any]) -> float | None:
    """The lowest per-player fee quoted, in dollars (the API uses cents).

    Prefers walking fees; falls back to cart, which includes the cart.
    """
    for field in ("greenFeeWalking", "dueOnlineWalking", "greenFeeCart", "dueOnlineRiding"):
        fees = [
            v for v in (_as_int(r.get(field)) for r in _rates(entry)) if v and v > 0
        ]
        if fees:
            return min(fees) / 100
    return None


def parse_response(
    payload: Any, courses: list[config.TeeItUpCourse]
) -> dict[str, list[TeeTimeSlot]]:
    if isinstance(payload, dict):
        # Errors come back as an object; a valid answer is always a list.
        message = payload.get("message") or payload.get("error") or payload
        raise TeeItUpError(f"TeeItUp returned an error: {str(message)[:200]}")
    if not isinstance(payload, list):
        raise TeeItUpError(f"Expected a list, got {type(payload).__name__}")

    by_id = {c.course_id: c for c in courses}
    only = courses[0] if len(courses) == 1 else None
    result: dict[str, list[TeeTimeSlot]] = {c.key: [] for c in courses}
    unmatched = 0

    for day in payload:
        if not isinstance(day, dict):
            continue
        for entry in day.get("teetimes") or []:
            if not isinstance(entry, dict):
                continue
            facility = _facility_id(entry, day)
            course = by_id.get(facility) if facility else only
            if course is None:
                unmatched += 1
                continue

            start = _parse_start(entry.get("teetime"))
            if start is None:
                continue

            options = holes_options(entry)
            result[course.key].append(TeeTimeSlot(
                course_key=course.key,
                course_name=course.name,
                start=start,
                available_spots=available_spots(entry),
                holes=max(options) if options else None,
                holes_options=options,
                green_fee=green_fee(entry),
                cart_fee=None,
                teetime_id=None,
                raw=entry,
            ))

    if unmatched:
        log.warning("TeeItUp: %s tee time(s) for facilities we did not ask about", unmatched)
    for slots in result.values():
        slots.sort(key=lambda s: s.start)
    return result


async def fetch_times(
    client: httpx.AsyncClient, courses: list[config.TeeItUpCourse], date: dt.date
) -> dict[str, list[TeeTimeSlot]]:
    """One request per booking site per date, covering all its courses."""
    by_alias: dict[str, list[config.TeeItUpCourse]] = defaultdict(list)
    for course in courses:
        by_alias[course.alias].append(course)

    result: dict[str, list[TeeTimeSlot]] = {}
    for alias, group in by_alias.items():
        try:
            response = await client.get(
                f"{config.TEEITUP_API_URL}/v2/tee-times",
                params=build_params(group, date),
                headers=headers_for(alias),
            )
        except httpx.HTTPError as exc:
            raise TeeItUpError(f"request to TeeItUp failed: {exc}") from exc

        if response.status_code != 200:
            raise TeeItUpError(
                f"TeeItUp returned HTTP {response.status_code} for {alias}: "
                f"{response.text[:150]}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise TeeItUpError(f"TeeItUp returned non-JSON for {alias}: {exc}") from exc
        result.update(parse_response(payload, group))
    return result
