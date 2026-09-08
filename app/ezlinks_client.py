"""Client for Union County's EZLinks booking API.

A third booking system, and the awkward one. Three things to know:

* **Every field is named ``rNN``/``pNN``.** The meanings below were read off
  a real captured response; where a field could not be pinned down it is
  left alone rather than guessed at. See ``docs`` note on ``r11``/``r14``.

* **The same tee time repeats once per rate plan.** A 6:42 slot came back
  three times -- Public $24, Player Card 7-day $19, Player Card 14-day $19.
  Alerting on the raw list would fire three notifications for one opening,
  so entries are collapsed per (course, start).

* **It sits behind Cloudflare.** A plain request gets challenged. See
  ``_post`` -- browser-like headers are tried first, and ``curl_cffi``
  (which matches a real browser's TLS fingerprint) is used when available,
  because fingerprinting rather than headers is the usual trigger.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any

import httpx

from . import config
from .models import TeeTimeSlot

log = logging.getLogger(__name__)


class EzLinksError(RuntimeError):
    """Raised when the API is unreachable, challenged, or unusable."""


class EzLinksBlocked(EzLinksError):
    """Cloudflare turned us away rather than the API failing."""


# Field names in the response payload, named so the parsing reads.
R_RATE_PLAN = "r06"
R_COURSE_ID = "r07"
R_PRICE = "r08"
R_MAX_PLAYERS = "r11"
R_SPOTS = "r14"
R_START = "r15"
R_COURSE_NAME = "r16"
R_HOLES = "r28"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Origin": config.EZLINKS_BASE_URL,
    "Referer": f"{config.EZLINKS_BASE_URL}/",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}


def build_payload(courses: list[config.EzLinksCourse], date: dt.date) -> dict[str, Any]:
    """The search body, mirroring what the booking page posts."""
    return {
        "p01": [int(c.course_id) for c in courses],
        "p02": date.strftime("%m/%d/%Y"),
        "p03": config.EZLINKS_EARLIEST,
        "p04": config.EZLINKS_LATEST,
        "p05": 0,  # holes: any
        # One player returns the widest set; asking for a foursome would hide
        # the partly-open slots a "1+ players" watch wants.
        "p06": int(config.EZLINKS_SEARCH_PLAYERS),
        "p07": False,
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
        log.warning("Unrecognized EZLinks tee time format: %r", value)
        return None


def rate_plan_names(payload: dict[str, Any]) -> dict[int, str]:
    """Rate plan id -> name, from the response's own legend (``r05``)."""
    names: dict[int, str] = {}
    for plan in payload.get("r05") or []:
        if isinstance(plan, dict):
            plan_id = _as_int(plan.get("r02"))
            if plan_id is not None:
                names[plan_id] = str(plan.get("r03") or "")
    return names


def _holes(entry: dict[str, Any], course: config.EzLinksCourse) -> int | None:
    """Holes for a slot.

    ``r28`` is "9" on the nine-hole course but "1,15,18" on the eighteen, so
    it is only trusted when it reads as a single number. Otherwise fall back
    to the hole count configured for the course, which is a property of the
    course rather than of the response.
    """
    raw = entry.get(R_HOLES)
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return course.holes


def available_spots(entry: dict[str, Any]) -> int:
    """Open spots on a slot.

    ASSUMPTION, not yet verified against a partly-booked slot: ``r14`` is
    what remains and ``r11`` is the slot's capacity. Every entry in the
    capture had both at 4, so the two could not be told apart. If Union
    starts alerting for parties larger than the slot really fits, this is
    the line to revisit -- scripts/verify_ezlinks.py prints both fields
    against a busy date to settle it.
    """
    spots = _as_int(entry.get(R_SPOTS))
    if spots is None:
        spots = _as_int(entry.get(R_MAX_PLAYERS))
    return spots if spots is not None else 0


def parse_response(
    payload: Any, courses: list[config.EzLinksCourse]
) -> dict[str, list[TeeTimeSlot]]:
    """Normalize one response, collapsing the per-rate-plan duplicates."""
    if not isinstance(payload, dict):
        raise EzLinksError(f"Expected a JSON object, got {type(payload).__name__}")

    entries = payload.get("r06")
    if not isinstance(entries, list):
        raise EzLinksError("Response had no 'r06' tee time list")

    plans = rate_plan_names(payload)
    by_course_id = {str(c.course_id): c for c in courses}
    # (course_key, start) -> (slot, rate plan it came from), so one tee time
    # yields one alert however many rate plans quote it.
    collapsed: dict[tuple[str, dt.datetime], tuple[TeeTimeSlot, str]] = {}

    for entry in entries:
        if not isinstance(entry, dict):
            continue

        course = by_course_id.get(str(entry.get(R_COURSE_ID)))
        if course is None:
            continue

        start = _parse_start(entry.get(R_START))
        if start is None:
            continue

        price = _as_float(entry.get(R_PRICE))
        plan_name = plans.get(_as_int(entry.get(R_RATE_PLAN)) or -1, "")
        slot = TeeTimeSlot(
            course_key=course.key,
            course_name=entry.get(R_COURSE_NAME) or course.name,
            start=start,
            available_spots=available_spots(entry),
            holes=_holes(entry, course),
            green_fee=price,
            cart_fee=None,
            teetime_id=str(entry.get("r12") or entry.get("r01") or "") or None,
            raw=entry,
        )

        key = (course.key, start)
        existing = collapsed.get(key)
        if existing is None or _prefer(plan_name, price, *existing):
            collapsed[key] = (slot, plan_name)

    result: dict[str, list[TeeTimeSlot]] = {c.key: [] for c in courses}
    for (course_key, _), (slot, _plan) in collapsed.items():
        result[course_key].append(slot)
    for slots in result.values():
        slots.sort(key=lambda s: s.start)
    return result


def _prefer(
    plan_name: str, price: float | None, existing: TeeTimeSlot, existing_plan: str
) -> bool:
    """Should this duplicate replace the one already kept?

    The Public rate wins even though the Player Card rates are cheaper: it
    is the one anyone can actually book, and quoting $19 to someone without
    a card would be wrong.
    """
    if plan_name.lower() == "public":
        return existing_plan.lower() != "public"
    if existing_plan.lower() == "public":
        return False
    # Neither is Public: keep the cheaper, which is the friendlier default.
    if price is None:
        return False
    return existing.green_fee is None or price < existing.green_fee


async def _post(client: httpx.AsyncClient, payload: dict[str, Any]) -> Any:
    """POST the search, working around Cloudflare where we can.

    Cloudflare most often rejects on TLS fingerprint rather than headers, so
    plain httpx can be challenged no matter how browser-like the headers
    look. curl_cffi replays a real Chrome handshake and is used when it is
    installed; it is synchronous, so it runs off the event loop.
    """
    if config.EZLINKS_IMPERSONATE:
        try:
            from curl_cffi import requests as curl_requests
        except ImportError:
            log.warning(
                "EZLINKS_IMPERSONATE is set but curl_cffi is not installed; "
                "falling back to httpx, which Cloudflare may challenge"
            )
        else:
            return await asyncio.to_thread(
                _post_impersonated, curl_requests, payload
            )

    try:
        response = await client.post(
            config.EZLINKS_API_URL, json=payload, headers=DEFAULT_HEADERS
        )
    except httpx.HTTPError as exc:
        raise EzLinksError(f"request to EZLinks failed: {exc}") from exc
    return _decode(response.status_code, response.text)


def _post_impersonated(curl_requests: Any, payload: dict[str, Any]) -> Any:
    try:
        response = curl_requests.post(
            config.EZLINKS_API_URL,
            json=payload,
            headers=DEFAULT_HEADERS,
            impersonate=config.EZLINKS_IMPERSONATE,
            timeout=30,
        )
    except Exception as exc:  # curl_cffi raises its own error types
        raise EzLinksError(f"request to EZLinks failed: {exc}") from exc
    return _decode(response.status_code, response.text)


def _decode(status_code: int, body: str) -> Any:
    """Turn a raw response into JSON, naming a Cloudflare block as such."""
    if status_code in (403, 429, 503):
        raise EzLinksBlocked(
            f"EZLinks returned HTTP {status_code} -- this is usually "
            "Cloudflare. See the Union County notes in README.md."
        )
    if status_code != 200:
        raise EzLinksError(f"EZLinks returned HTTP {status_code}")

    import json

    try:
        return json.loads(body)
    except ValueError as exc:
        # A challenge page is HTML, not JSON -- say which it was.
        if "<html" in body[:200].lower():
            raise EzLinksBlocked(
                "EZLinks returned an HTML challenge page instead of JSON "
                "(Cloudflare). See the Union County notes in README.md."
            ) from exc
        raise EzLinksError(f"EZLinks returned non-JSON: {exc}") from exc


async def fetch_times(
    client: httpx.AsyncClient, courses: list[config.EzLinksCourse], date: dt.date
) -> dict[str, list[TeeTimeSlot]]:
    """Fetch one date across every given course, in a single request."""
    if not courses:
        return {}
    payload = await _post(client, build_payload(courses, date))
    return parse_response(payload, courses)
