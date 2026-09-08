"""Client for Bergen County's CPS Golf booking API.

Two things differ from ForeUp in ways that matter:

* **One request covers many courses.** ``courseIds`` takes a comma-separated
  list and the response mixes them together, each entry tagged with its own
  ``courseId``. So a whole facility costs one request per date, not one per
  course per date.

* **Availability is a list, not a count.** Each slot carries
  ``availableParticipantNo`` -- the party sizes it will still accept, e.g.
  ``[1, 2]`` once two of a foursome are taken. The largest of those is the
  number of open spots, which is what a watch's "minimum players" compares
  against. An empty list means nothing is bookable.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any

import httpx

from . import config
from .models import TeeTimeSlot

log = logging.getLogger(__name__)


class CpsError(RuntimeError):
    """Raised when the API is unreachable or returns something unusable."""


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": f"{config.CPS_BASE_URL}/onlineres/",
}

# Fixed rather than locale-dependent: strftime's %a/%b follow the process
# locale, and this API wants the JavaScript Date.toDateString() spelling.
_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_MONTHS = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]


def format_search_date(date: dt.date) -> str:
    """The 'Sat Sep 12 2026' spelling the API expects, day zero-padded."""
    return (
        f"{_WEEKDAYS[date.weekday()]} {_MONTHS[date.month - 1]} "
        f"{date.day:02d} {date.year}"
    )


def build_params(courses: list[config.CpsCourse], date: dt.date) -> dict[str, Any]:
    """Query params for one date across every given course."""
    return {
        "searchDate": format_search_date(date),
        "holes": "0",  # all
        # Asking for one player returns the widest set of slots; each entry
        # still reports how many spots it actually has.
        "numberOfPlayer": config.CPS_SEARCH_PLAYERS,
        "courseIds": ",".join(c.course_id for c in courses),
        "searchTimeType": "0",
        "transactionId": str(uuid.uuid4()),
        "teeOffTimeMin": "0",
        "teeOffTimeMax": "23",
        "isChangeTeeOffTime": "true",
        "teeSheetSearchView": "5",
        "classCode": config.CPS_CLASS_CODE,
        "defaultOnlineRate": "N",
        "isUseCapacityPricing": "false",
        "memberStoreId": config.CPS_MEMBER_STORE_ID,
        "searchType": "1",
    }


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


def _parse_start(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value.strip())
    except ValueError:
        log.warning("Unrecognized CPS tee time format: %r", value)
        return None


def available_spots(entry: dict[str, Any]) -> int:
    """How many players this slot will still take.

    ``availableParticipantNo`` is the authoritative list of party sizes the
    slot accepts. Falling back to maxPlayer would overstate availability on a
    partly-booked slot, so an entry without the list is treated as unknown
    rather than optimistically full.
    """
    sizes = entry.get("availableParticipantNo")
    if isinstance(sizes, list):
        numbers = [n for n in (_as_int(s) for s in sizes) if n is not None]
        return max(numbers) if numbers else 0
    return 0


def _green_fee(entry: dict[str, Any]) -> float | None:
    prices = entry.get("shItemPrices")
    if not isinstance(prices, list) or not prices:
        return None
    first = prices[0]
    if not isinstance(first, dict):
        return None
    return _as_float(first.get("displayPrice")) or _as_float(first.get("price"))


def parse_response(
    payload: Any, courses: list[config.CpsCourse]
) -> dict[str, list[TeeTimeSlot]]:
    """Split one response into slots per course key.

    Entries for courses we did not ask about are dropped rather than guessed
    at, so an unmapped courseId can never be attributed to the wrong course.
    """
    if not isinstance(payload, dict):
        raise CpsError(f"Expected a JSON object, got {type(payload).__name__}")
    if payload.get("isSuccess") is False:
        raise CpsError("CPS reported isSuccess=false")

    entries = payload.get("content")
    if not isinstance(entries, list):
        raise CpsError("Response had no 'content' list")

    by_course_id = {c.course_id: c for c in courses}
    result: dict[str, list[TeeTimeSlot]] = {c.key: [] for c in courses}

    for entry in entries:
        if not isinstance(entry, dict):
            continue

        course = by_course_id.get(str(entry.get("courseId")))
        if course is None:
            continue

        start = _parse_start(entry.get("startTime"))
        if start is None:
            continue

        teetime_id = entry.get("teeSheetId")
        result[course.key].append(
            TeeTimeSlot(
                course_key=course.key,
                course_name=entry.get("courseName") or course.name,
                start=start,
                available_spots=available_spots(entry),
                holes=_as_int(entry.get("holes")),
                green_fee=_green_fee(entry),
                cart_fee=None,  # not itemized separately in this response
                teetime_id=str(teetime_id) if teetime_id is not None else None,
                raw=entry,
            )
        )

    for slots in result.values():
        slots.sort(key=lambda s: s.start)
    return result


async def fetch_times(
    client: httpx.AsyncClient, courses: list[config.CpsCourse], date: dt.date
) -> dict[str, list[TeeTimeSlot]]:
    """Fetch one date across every given course, in a single request."""
    if not courses:
        return {}

    try:
        response = await client.get(
            config.CPS_API_URL,
            params=build_params(courses, date),
            headers=DEFAULT_HEADERS,
        )
    except httpx.HTTPError as exc:
        raise CpsError(f"request to CPS failed: {exc}") from exc

    if response.status_code != 200:
        raise CpsError(f"CPS returned HTTP {response.status_code}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise CpsError(f"CPS returned non-JSON: {exc}") from exc

    return parse_response(payload, courses)
