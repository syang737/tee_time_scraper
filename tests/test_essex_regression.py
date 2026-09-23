"""Essex (ForeUp) still alerts, end to end, now that it shares the poller.

The other poller tests stub ``foreup_client.fetch_times`` and ``notify``, so
nothing else runs the real chain: ForeUp HTTP -> parse -> match -> dedup ->
ntfy POST. These drive ``poll_once`` with only the network faked, and then
check the ways Bergen and Union could plausibly have broken Essex:

* an unexpected exception from a Cloudflare-blocked provider must not abort
  the cycle for everyone;
* a watch that mixes Essex with a permanently blocked county must still
  notice an Essex slot closing and reopening.
"""

import asyncio
import datetime as dt
import json
from pathlib import Path

import httpx
import pytest

from app import config, cps_client, db, ezlinks_client, poller
from app.cps_client import CpsBlocked
from app.models import Watch
from app.poller import TZ

FIXTURE = Path(__file__).parent / "fixtures" / "sample_times_response.json"
NOW = dt.datetime(2026, 9, 2, 9, 0, tzinfo=TZ)  # Wed; fixture date is Sat 9/5
SAT = "2026-09-05"


def run(coro):
    return asyncio.run(coro)


class FakeNetwork:
    """ForeUp and ntfy behind one httpx transport, as the poller shares a client."""

    def __init__(self, foreup_payload):
        self.foreup_payload = foreup_payload
        self.foreup_requests: list[httpx.Request] = []
        self.pushes: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "foreupsoftware.com":
            self.foreup_requests.append(request)
            return httpx.Response(200, json=self.foreup_payload)
        if request.url.host == "ntfy.example":
            self.pushes.append({
                "topic": request.url.path.lstrip("/"),
                "title": request.headers["Title"],
                "click": request.headers["Click"],
                "body": request.content.decode(),
            })
            return httpx.Response(200)
        raise AssertionError(f"unexpected request to {request.url}")

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


@pytest.fixture
def network(monkeypatch):
    monkeypatch.setattr(config, "NTFY_SERVER", "https://ntfy.example")
    monkeypatch.setattr(config, "NTFY_TOPIC", "")
    # The throttle is module state; a previous test's call must not leak in.
    monkeypatch.setattr(poller, "_last_called", {})
    return FakeNetwork(json.loads(FIXTURE.read_text()))


def essex_watch(**overrides) -> Watch:
    fields = dict(label="Essex", courses=["weequahic"], specific_date=SAT,
                  ntfy_topic="simon-essex")
    fields.update(overrides)
    return Watch(**fields)


# --------------------------------------------------------------------------
# the plain path
# --------------------------------------------------------------------------


def test_an_essex_opening_reaches_ntfy(temp_db, network):
    db.save_watch(essex_watch(time_start="07:00", time_end="08:00", min_players=2))

    run(poller.poll_once(network.client(), now=NOW))

    # Real request shape: the course's own schedule, the date ForeUp expects.
    assert len(network.foreup_requests) == 1
    params = network.foreup_requests[0].url.params
    assert params["schedule_id"] == "11077"
    assert params["date"] == "09-05-2026"

    # 7:10 (4 spots) is the only Weequahic slot in 7-8am with 2+ open. The
    # 8:02 slot in the fixture is Hendricks' schedule and must be dropped.
    assert [p["title"] for p in network.pushes] == [
        "Weequahic Golf Course - Sat Sep 5 at 7:10 AM"
    ]
    push = network.pushes[0]
    assert push["topic"] == "simon-essex"
    assert push["click"] == config.COURSES["weequahic"].booking_url
    assert "4 spot(s) open" in push["body"]
    assert db.get_poll_status()["last_error"] is None


def test_each_criterion_still_filters(temp_db, network):
    db.save_watch(essex_watch(label="3+", min_players=3, ntfy_topic="a"))
    db.save_watch(essex_watch(label="9 holes", holes="9", ntfy_topic="b"))
    db.save_watch(essex_watch(label="afternoon", time_start="12:00", ntfy_topic="c"))

    run(poller.poll_once(network.client(), now=NOW))

    got = sorted((p["topic"], p["title"].rsplit(" at ", 1)[1]) for p in network.pushes)
    assert got == [
        ("a", "2:18 PM"), ("a", "7:10 AM"),   # 4 and 3 spots; not the 1 or 2
        ("b", "9:42 AM"),                     # the only 9-hole slot
        ("c", "2:18 PM"),                     # the only afternoon slot
    ]


def test_all_three_essex_courses_are_polled(temp_db, network):
    db.save_watch(essex_watch(courses=["hendricks", "weequahic", "byrne"]))

    run(poller.poll_once(network.client(), now=NOW))

    schedules = sorted(r.url.params["schedule_id"] for r in network.foreup_requests)
    assert schedules == ["11075", "11077", "11078"]
    courses = {p["title"].split(" - ")[0] for p in network.pushes}
    assert courses == {"Weequahic Golf Course", "Hendricks Field Golf Course"}


def test_no_repeat_alert_next_cycle_but_one_after_the_cooldown(temp_db, network):
    db.save_watch(essex_watch(time_start="07:00", time_end="07:30"))

    run(poller.poll_once(network.client(), now=NOW))
    run(poller.poll_once(network.client(), now=NOW + dt.timedelta(seconds=30)))
    assert len(network.pushes) == 1

    later = NOW + dt.timedelta(minutes=config.RENOTIFY_AFTER_MINUTES, seconds=1)
    run(poller.poll_once(network.client(), now=later))
    assert len(network.pushes) == 2


# --------------------------------------------------------------------------
# what Bergen and Union must not be able to do to Essex
# --------------------------------------------------------------------------


def test_an_unexpected_error_from_another_county_does_not_silence_essex(
    temp_db, network, monkeypatch
):
    """Bergen is Cloudflare-blocked on the box. Whatever it raises -- not
    only the error types it is expected to -- Essex must still alert."""
    db.save_watch(essex_watch(time_start="07:00", time_end="07:30"))
    db.save_watch(Watch(label="Bergen", courses=["soldier_hill"],
                        specific_date=SAT, ntfy_topic="bergen"))

    async def explode(client, courses, date):
        raise TypeError("something curl_cffi did that nobody anticipated")

    monkeypatch.setattr(cps_client, "fetch_times", explode)

    run(poller.poll_once(network.client(), now=NOW))

    assert [p["topic"] for p in network.pushes] == ["simon-essex"]
    # ...and the Bergen watch is reported as blind rather than looking fine.
    assert "watch 2" in (db.get_poll_status()["last_error"] or "")


@pytest.mark.parametrize("provider_module, course", [
    (cps_client, "soldier_hill"),
    (ezlinks_client, "ash_brook"),
])
def test_a_mixed_watch_alerts_on_essex_while_the_other_county_is_blocked(
    temp_db, network, monkeypatch, provider_module, course
):
    db.save_watch(essex_watch(courses=["weequahic", course],
                              time_start="07:00", time_end="07:30"))

    async def blocked(client, courses, date):
        raise CpsBlocked("HTTP 403, Cloudflare challenge")

    monkeypatch.setattr(provider_module, "fetch_times", blocked)

    run(poller.poll_once(network.client(), now=NOW))

    assert [p["title"] for p in network.pushes] == [
        "Weequahic Golf Course - Sat Sep 5 at 7:10 AM"
    ]


def test_a_mixed_watch_still_sees_an_essex_slot_close_and_reopen(
    temp_db, network, monkeypatch
):
    """A reopened slot is a fresh cancellation and alerts immediately.

    That relies on noticing the slot vanish in between. If a blocked county
    in the same watch stops the bookkeeping for the whole watch, the reopen
    goes unnoticed and the alert waits out the full re-notify cooldown --
    exactly when being first matters.
    """
    db.save_watch(essex_watch(courses=["weequahic", "soldier_hill"],
                              time_start="07:00", time_end="07:30"))

    async def blocked(client, courses, date):
        raise CpsBlocked("HTTP 403, Cloudflare challenge")

    monkeypatch.setattr(cps_client, "fetch_times", blocked)
    full = network.foreup_payload

    run(poller.poll_once(network.client(), now=NOW))           # open -> alert
    network.foreup_payload = [e for e in full if e["time"] != "2026-09-05 07:10:00"]
    run(poller.poll_once(network.client(), now=NOW + dt.timedelta(minutes=1)))  # booked
    network.foreup_payload = full
    run(poller.poll_once(network.client(), now=NOW + dt.timedelta(minutes=2)))  # cancelled

    assert len(network.pushes) == 2, "the reopened slot did not alert straight away"


def test_a_throttled_county_does_not_hold_back_essex_bookkeeping(
    temp_db, network, monkeypatch
):
    """Same as above, but Bergen skipped by its throttle rather than failing."""
    db.save_watch(essex_watch(courses=["weequahic", "soldier_hill"],
                              time_start="07:00", time_end="07:30"))

    async def empty(client, courses, date):
        return {c.key: [] for c in courses}

    monkeypatch.setattr(cps_client, "fetch_times", empty)
    full = network.foreup_payload

    run(poller.poll_once(network.client(), now=NOW))
    network.foreup_payload = [e for e in full if e["time"] != "2026-09-05 07:10:00"]
    run(poller.poll_once(network.client(), now=NOW + dt.timedelta(seconds=30)))
    network.foreup_payload = full
    run(poller.poll_once(network.client(), now=NOW + dt.timedelta(seconds=60)))

    assert len(network.pushes) == 2
