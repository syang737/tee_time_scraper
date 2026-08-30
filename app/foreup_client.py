"""Client for the public ForeUp booking-times endpoint.

The endpoint is the same one the courses' own booking pages call, and it
takes an empty api_key (their page sets ``API_KEY = ''``), so no login or
credentials are involved.

Field names below come from the booking page's own JS templates
(``template_time``, ``template_book_time``), which render ``time``,
``holes``, ``available_spots``, ``green_fee``, ``cart_fee``, ``course_name``
and ``schedule_id`` off each entry. Parsing stays tolerant anyway: an entry
missing an optional field is kept, not dropped.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import httpx

from . import config
from .models import TeeTimeSlot

log = logging.getLogger(__name__)

# Presenting as the booking page itself; this is the same request a browser
# on the course's booking site makes.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://foreupsoftware.com/index.php/booking/",
}

_TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
)


class ForeUpError(RuntimeError):
    """Raised when the API is unreachable or returns something unusable."""


def build_params(course: config.Course, date: dt.date) -> dict[str, Any]:
    """Query params for one course on one date, mirroring the booking page."""
    return {
        "time": "all",
        "date": date.strftime("%m-%d-%Y"),
        "holes": "all",
        "players": "0",
        "booking_class": course.booking_class,
        "schedule_id": course.schedule_id,
        # Facility-wide: all three courses share one booking site.
        "schedule_ids[]": config.FACILITY_SCHEDULE_IDS,
        "specials_only": "0",
        "api_key": "",
    }


def _parse_start(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    for fmt in _TIME_FORMATS:
        try:
            return dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError:
        log.warning("Unrecognized tee time format: %r", value)
        return None


def _as_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_response(payload: Any, course: config.Course) -> list[TeeTimeSlot]:
    """Normalize the raw JSON into slots belonging to ``course``."""
    if isinstance(payload, dict):
        # Some ForeUp responses wrap the list; take the first list we find.
        payload = next(
            (v for v in payload.values() if isinstance(v, list)),
            [],
        )
    if not isinstance(payload, list):
        raise ForeUpError(f"Expected a list of tee times, got {type(payload).__name__}")

    slots: list[TeeTimeSlot] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue

        # The request carries the facility-wide schedule_ids[], so responses
        # can include other courses' schedules. Keep only this course's.
        entry_schedule = entry.get("schedule_id")
        if entry_schedule is not None and str(entry_schedule) != course.schedule_id:
            continue

        start = _parse_start(entry.get("time"))
        if start is None:
            continue

        spots = _as_int(entry.get("available_spots"))
        if spots is None:
            continue

        teetime_id = entry.get("teetime_id") or entry.get("time_id")
        slots.append(
            TeeTimeSlot(
                course_key=course.key,
                course_name=entry.get("course_name") or course.name,
                start=start,
                available_spots=spots,
                holes=_as_int(entry.get("holes")),
                green_fee=_as_float(entry.get("green_fee")),
                cart_fee=_as_float(entry.get("cart_fee")),
                teetime_id=str(teetime_id) if teetime_id is not None else None,
                raw=entry,
            )
        )

    slots.sort(key=lambda s: s.start)
    return slots


async def fetch_times(
    client: httpx.AsyncClient, course: config.Course, date: dt.date
) -> list[TeeTimeSlot]:
    """Fetch and normalize one course's tee times for one date."""
    try:
        response = await client.get(
            config.BASE_API_URL,
            params=build_params(course, date),
            headers=DEFAULT_HEADERS,
        )
    except httpx.HTTPError as exc:
        raise ForeUpError(f"request to ForeUp failed: {exc}") from exc

    if response.status_code != 200:
        raise ForeUpError(f"ForeUp returned HTTP {response.status_code} for {course.key}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise ForeUpError(f"ForeUp returned non-JSON for {course.key}: {exc}") from exc

    return parse_response(payload, course)
