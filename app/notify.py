"""Push notifications via ntfy.sh."""

from __future__ import annotations

import logging

import httpx

from . import config
from .models import TeeTimeSlot, Watch

log = logging.getLogger(__name__)


def _headers(slot: TeeTimeSlot, watch: Watch) -> dict[str, str]:
    course = config.COURSES[slot.course_key]
    label = watch.label or f"watch #{watch.id}"
    title = f"{slot.course_name} - {slot.start.strftime('%a %b %-d')} at {slot.display_time}"
    return {
        # ntfy headers must be latin-1 safe; these are all ASCII.
        "Title": title,
        "Priority": "urgent",
        "Tags": "golf",
        # Tapping the push opens that course's booking calendar so you can
        # finish the reservation by hand.
        "Click": course.booking_url,
        "X-Watch": label[:64],
    }


def _body(slot: TeeTimeSlot, watch: Watch) -> str:
    lines = [
        f"{slot.available_spots} spot(s) open"
        + (f", {slot.holes} holes" if slot.holes else ""),
    ]
    if slot.green_fee is not None:
        fee = f"${slot.green_fee:.0f} green fee"
        if slot.cart_fee:
            fee += f" + ${slot.cart_fee:.0f} cart"
        lines.append(fee)
    lines.append(f"Matched watch: {watch.label or watch.describe()}")
    lines.append("Tap to open the booking page.")
    return "\n".join(lines)


async def send_slot_alert(
    client: httpx.AsyncClient, slot: TeeTimeSlot, watch: Watch
) -> bool:
    """Push one alert. Returns True if ntfy accepted it."""
    if not config.NTFY_TOPIC:
        log.warning("NTFY_TOPIC is not set; skipping notification for %s", slot.slot_key)
        return False

    url = f"{config.NTFY_SERVER}/{config.NTFY_TOPIC}"
    try:
        response = await client.post(
            url,
            content=_body(slot, watch).encode("utf-8"),
            headers=_headers(slot, watch),
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        log.error("ntfy push failed for %s: %s", slot.slot_key, exc)
        return False

    if response.status_code >= 300:
        log.error("ntfy returned HTTP %s for %s", response.status_code, slot.slot_key)
        return False

    log.info("Notified: %s %s (%s spots)", slot.course_key, slot.start, slot.available_spots)
    return True


async def send_test_alert(client: httpx.AsyncClient) -> bool:
    """Used by the GUI's 'send test notification' button."""
    if not config.NTFY_TOPIC:
        return False
    try:
        response = await client.post(
            f"{config.NTFY_SERVER}/{config.NTFY_TOPIC}",
            content=b"Tee time watcher is connected and watching.",
            headers={"Title": "Test notification", "Tags": "golf"},
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        log.error("ntfy test push failed: %s", exc)
        return False
    return response.status_code < 300
