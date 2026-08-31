"""Tests for the real notify module.

The poller tests swap in a fake notifier, so nothing there ever executes
``_headers``/``_body``. That gap is how a ValueError in the title string
reached live testing, where it killed every alert for the watch.
"""

import datetime as dt

import httpx
import pytest

from app import config, notify
from app.models import TeeTimeSlot, Watch


def make_slot(
    hour=7, minute=10, day=5, course="weequahic", spots=2, holes=18,
    green_fee=32.0, cart_fee=18.0,
):
    return TeeTimeSlot(
        course_key=course,
        course_name=config.COURSES[course].name,
        start=dt.datetime(2026, 9, day, hour, minute),
        available_spots=spots,
        holes=holes,
        green_fee=green_fee,
        cart_fee=cart_fee,
    )


WATCH = Watch(id=1, label="Weekend mornings", courses=["weequahic"],
              min_players=2, time_start="07:00", time_end="10:00")


# --------------------------------------------------------------------------
# display formatting -- portable across Windows and Linux
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hour,minute,expected",
    [
        (7, 10, "7:10 AM"),     # single-digit hour: the %-I case that broke
        (11, 59, "11:59 AM"),
        (12, 0, "12:00 PM"),    # noon is 12 PM, not 0 PM
        (12, 30, "12:30 PM"),
        (13, 5, "1:05 PM"),     # minutes keep their leading zero
        (0, 0, "12:00 AM"),     # midnight is 12 AM, not 0 AM
        (23, 45, "11:45 PM"),
    ],
)
def test_display_time_handles_the_12_hour_boundaries(hour, minute, expected):
    assert make_slot(hour=hour, minute=minute).display_time == expected


@pytest.mark.parametrize(
    "day,expected",
    [
        (5, "Sat Sep 5"),    # single-digit day: the %-d case that broke
        (13, "Sun Sep 13"),
    ],
)
def test_display_date_has_no_leading_zero(day, expected):
    assert make_slot(day=day).display_date == expected


def test_no_source_file_uses_a_platform_specific_strftime_directive():
    """Guards the whole class of bug, from any platform.

    ``%-d``/``%-I`` are glibc extensions: they work on Linux and raise
    ValueError on Windows, where the equivalent is ``%#d``. Asserting on
    *output* cannot catch this, because on Linux the output is correct --
    which is exactly how it reached live testing. So check the source.
    """
    import re
    from pathlib import Path

    offenders = []
    for path in sorted((Path(__file__).parent.parent / "app").rglob("*.py")):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]  # prose about the bug is allowed
            if re.search(r"%[-][a-zA-Z]", code) or re.search(r'"[^"]*%#[a-zA-Z]', code):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")

    assert not offenders, "platform-specific strftime directives found:\n" + "\n".join(
        offenders
    )


# --------------------------------------------------------------------------
# headers and body
# --------------------------------------------------------------------------


def test_headers_carry_a_readable_title():
    headers = notify._headers(make_slot(), WATCH)
    assert headers["Title"] == "Weequahic - Sat Sep 5 at 7:10 AM"


def test_every_header_value_is_latin_1_encodable():
    # This is the constraint an HTTP header actually has. Asserting it here
    # catches the whole class of "the title blew up while being built",
    # whichever piece of formatting is at fault.
    headers = notify._headers(make_slot(), WATCH)
    for name, value in headers.items():
        assert isinstance(value, str), name
        value.encode("latin-1")  # raises if it ever stops being safe


def test_click_header_deep_links_to_the_right_course():
    headers = notify._headers(make_slot(course="byrne"), WATCH)
    assert headers["Click"] == config.COURSES["byrne"].booking_url
    assert "22528" in headers["Click"]


def test_a_long_watch_label_is_truncated_for_the_header():
    headers = notify._headers(make_slot(), Watch(id=1, label="x" * 200))
    assert len(headers["X-Watch"]) <= 64


def test_an_unlabelled_watch_still_produces_a_header():
    assert notify._headers(make_slot(), Watch(id=7))["X-Watch"] == "watch #7"


def test_body_reports_spots_holes_and_fees():
    body = notify._body(make_slot(), WATCH)
    assert "2 spot(s) open" in body
    assert "18 holes" in body
    assert "$32 green fee" in body
    assert "$18 cart" in body
    assert "Weekend mornings" in body


def test_body_copes_with_the_optional_fields_being_absent():
    body = notify._body(make_slot(holes=None, green_fee=None, cart_fee=None), WATCH)
    assert "2 spot(s) open" in body
    assert "holes" not in body
    assert "green fee" not in body


def test_an_unlabelled_watch_falls_back_to_describing_its_criteria():
    body = notify._body(make_slot(), Watch(id=1, courses=["weequahic"], min_players=2))
    assert "2+ players" in body


# --------------------------------------------------------------------------
# send_slot_alert
# --------------------------------------------------------------------------


def send(slot, watch=WATCH, handler=None, topic="test-topic", monkeypatch=None):
    monkeypatch.setattr(config, "NTFY_TOPIC", topic)
    monkeypatch.setattr(config, "NTFY_SERVER", "https://ntfy.example")
    handler = handler or (lambda request: httpx.Response(200))
    transport = httpx.MockTransport(handler)

    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            return await notify.send_slot_alert(client, slot, watch)

    import asyncio

    return asyncio.run(run())


def test_a_successful_push_reports_delivered(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["title"] = request.headers["title"]
        seen["body"] = request.content.decode()
        return httpx.Response(200)

    assert send(make_slot(), handler=handler, monkeypatch=monkeypatch) is True
    assert seen["url"] == "https://ntfy.example/test-topic"
    assert seen["title"] == "Weequahic - Sat Sep 5 at 7:10 AM"
    assert "2 spot(s) open" in seen["body"]


def test_a_network_error_reports_undelivered(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("no route")

    assert send(make_slot(), handler=handler, monkeypatch=monkeypatch) is False


def test_an_error_status_reports_undelivered(monkeypatch):
    assert send(
        make_slot(), handler=lambda r: httpx.Response(429), monkeypatch=monkeypatch
    ) is False


def test_an_unexpected_error_is_contained_rather_than_raised(monkeypatch):
    # A formatting bug used to escape here and abandon the whole watch.
    monkeypatch.setattr(
        notify, "_headers", lambda slot, watch: (_ for _ in ()).throw(ValueError("boom"))
    )
    assert send(make_slot(), monkeypatch=monkeypatch) is False


def test_no_topic_configured_reports_undelivered(monkeypatch):
    assert send(make_slot(), topic="", monkeypatch=monkeypatch) is False
