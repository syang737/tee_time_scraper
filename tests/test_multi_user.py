"""Several people sharing one instance.

Two things have to hold: each watch's alerts reach only its own topic, and
the polling load stays bounded no matter how many watches exist. The second
is the one that breaks quietly -- fetching per watch means a handful of
friends push the cycle past its own interval and send the course the same
request over and over.
"""

import asyncio
import datetime as dt

import httpx
import pytest

from app import config, db, notify, poller
from app.foreup_client import ForeUpError
from app.models import TeeTimeSlot, Watch
from app.poller import TZ

NOW = dt.datetime(2026, 9, 2, 9, 0, tzinfo=TZ)


def make_slot(time="08:00", spots=4, course="weequahic", date=(2026, 9, 5)):
    hour, minute = (int(p) for p in time.split(":"))
    return TeeTimeSlot(
        course_key=course,
        course_name=course.title(),
        start=dt.datetime(*date, hour, minute),
        available_spots=spots,
        holes=18,
    )


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def pushes(monkeypatch):
    """Capture every push with the topic it was sent to."""
    sent: list[tuple[str, str]] = []

    async def fake_post(self, url, **kwargs):
        sent.append((url.rsplit("/", 1)[-1], kwargs["headers"]["Title"]))
        return httpx.Response(200)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(config, "NTFY_SERVER", "https://ntfy.example")
    return sent


def stub_fetch(monkeypatch, slots_by_key=None, fail=()):
    """Record which (course, date) pairs actually got fetched."""
    calls: list[tuple[str, dt.date]] = []

    async def fake_fetch(client, course, date):
        calls.append((course.key, date))
        if (course.key, date) in fail:
            raise ForeUpError("boom")
        if slots_by_key is not None:
            return slots_by_key.get((course.key, date), [])
        return [make_slot(date=(date.year, date.month, date.day))]

    monkeypatch.setattr(poller.foreup_client, "fetch_times", fake_fetch)
    return calls


# --------------------------------------------------------------------------
# alerts reach the right phone
# --------------------------------------------------------------------------


def test_each_watch_alerts_its_own_topic(temp_db, monkeypatch, pushes):
    monkeypatch.setattr(config, "NTFY_TOPIC", "simon-default")
    db.save_watch(Watch(label="Simon", courses=["weequahic"], days=["sat"],
                        ntfy_topic="simon-abc123"))
    db.save_watch(Watch(label="Friend", courses=["weequahic"], days=["sat"],
                        ntfy_topic="friend-xyz789"))
    stub_fetch(monkeypatch)

    run(poller.poll_once(httpx.AsyncClient(), now=NOW))

    assert {topic for topic, _ in pushes} == {"simon-abc123", "friend-xyz789"}


def test_a_watch_without_a_topic_uses_the_server_default(temp_db, monkeypatch, pushes):
    monkeypatch.setattr(config, "NTFY_TOPIC", "simon-default")
    db.save_watch(Watch(label="Legacy", courses=["weequahic"], days=["sat"]))
    stub_fetch(monkeypatch)

    run(poller.poll_once(httpx.AsyncClient(), now=NOW))

    assert {topic for topic, _ in pushes} == {"simon-default"}


def test_one_persons_topic_never_receives_anothers_alerts(
    temp_db, monkeypatch, pushes
):
    monkeypatch.setattr(config, "NTFY_TOPIC", "")
    # Different criteria: only Simon's watch can match an early slot.
    db.save_watch(Watch(label="Simon", courses=["weequahic"], days=["sat"],
                        time_start="06:00", time_end="09:00",
                        ntfy_topic="simon-abc123"))
    db.save_watch(Watch(label="Friend", courses=["weequahic"], days=["sat"],
                        time_start="15:00", ntfy_topic="friend-xyz789"))
    stub_fetch(monkeypatch, {("weequahic", dt.date(2026, 9, 5)): [make_slot("08:00")]})

    run(poller.poll_once(httpx.AsyncClient(), now=NOW))

    assert [topic for topic, _ in pushes] == ["simon-abc123"]


def test_dedup_is_per_watch_not_global(temp_db, monkeypatch, pushes):
    """Two people watching the same slot each get their own alert."""
    monkeypatch.setattr(config, "NTFY_TOPIC", "")
    for name in ("simon", "friend"):
        db.save_watch(Watch(label=name, courses=["weequahic"], days=["sat"],
                            ntfy_topic=f"{name}-topic"))
    slots = {("weequahic", dt.date(2026, 9, 5)): [make_slot("08:00")]}
    stub_fetch(monkeypatch, slots)

    run(poller.poll_once(httpx.AsyncClient(), now=NOW))
    assert sorted(t for t, _ in pushes) == ["friend-topic", "simon-topic"]

    # ...and neither re-alerts on the next cycle.
    pushes.clear()
    stub_fetch(monkeypatch, slots)
    run(poller.poll_once(httpx.AsyncClient(), now=NOW + dt.timedelta(seconds=30)))
    assert pushes == []


def test_topic_falls_back_only_when_the_watch_has_none(monkeypatch):
    monkeypatch.setattr(config, "NTFY_TOPIC", "default-topic")
    assert notify.topic_for(Watch(id=1)) == "default-topic"
    assert notify.topic_for(Watch(id=1, ntfy_topic="mine")) == "mine"


# --------------------------------------------------------------------------
# polling load stays bounded
# --------------------------------------------------------------------------


def test_overlapping_watches_share_one_fetch(temp_db, monkeypatch, pushes):
    monkeypatch.setattr(config, "NTFY_TOPIC", "")
    # Six people all watching the same three courses on the same weekend.
    for n in range(6):
        db.save_watch(Watch(label=f"person{n}", courses=list(config.COURSES),
                            days=["sat", "sun"], ntfy_topic=f"person{n}"))
    calls = stub_fetch(monkeypatch, {})

    run(poller.poll_once(httpx.AsyncClient(), now=NOW))

    assert len(calls) == len(set(calls)), "the same course/date was fetched twice"
    # 3 courses x 4 weekend dates in the 14-day horizon -- not x6 for the
    # six watches, which is what would blow past the poll interval.
    assert len(calls) == 12


def test_request_count_does_not_grow_with_watch_count(temp_db, monkeypatch, pushes):
    monkeypatch.setattr(config, "NTFY_TOPIC", "")
    counts = []
    for total in (1, 3, 8):
        for row in db.list_watches():
            db.delete_watch(row.id)
        for n in range(total):
            db.save_watch(Watch(label=f"p{n}", courses=["weequahic"],
                                days=["sat"], ntfy_topic=f"p{n}"))
        calls = stub_fetch(monkeypatch, {})
        run(poller.poll_once(httpx.AsyncClient(), now=NOW))
        counts.append(len(calls))

    assert counts[0] == counts[1] == counts[2], f"load scaled with watches: {counts}"


def test_watches_only_fetch_what_they_actually_need(temp_db, monkeypatch, pushes):
    monkeypatch.setattr(config, "NTFY_TOPIC", "")
    db.save_watch(Watch(label="just byrne", courses=["byrne"],
                        specific_date="2026-09-05", ntfy_topic="t"))
    calls = stub_fetch(monkeypatch, {})

    run(poller.poll_once(httpx.AsyncClient(), now=NOW))

    assert calls == [("byrne", dt.date(2026, 9, 5))]


def test_a_shared_fetch_failure_is_reported_to_each_affected_watch(
    temp_db, monkeypatch, pushes
):
    monkeypatch.setattr(config, "NTFY_TOPIC", "")
    for n in range(2):
        db.save_watch(Watch(label=f"p{n}", courses=["weequahic"],
                            specific_date="2026-09-05", ntfy_topic=f"p{n}"))
    stub_fetch(monkeypatch, {}, fail={("weequahic", dt.date(2026, 9, 5))})

    run(poller.poll_once(httpx.AsyncClient(), now=NOW))

    error = db.get_poll_status()["last_error"]
    assert "watch 1" in error and "watch 2" in error


def test_a_shared_failure_does_not_mark_slots_taken(temp_db, monkeypatch, pushes):
    monkeypatch.setattr(config, "NTFY_TOPIC", "")
    watch_id = db.save_watch(Watch(label="p", courses=["weequahic"],
                                   specific_date="2026-09-05", ntfy_topic="p"))
    slot = make_slot("08:00")
    key = ("weequahic", dt.date(2026, 9, 5))

    stub_fetch(monkeypatch, {key: [slot]})
    run(poller.poll_once(httpx.AsyncClient(), now=NOW))
    assert db.get_seen_slot(watch_id, slot.slot_key)["still_open"] == 1

    stub_fetch(monkeypatch, {}, fail={key})
    run(poller.poll_once(httpx.AsyncClient(), now=NOW + dt.timedelta(minutes=1)))
    assert db.get_seen_slot(watch_id, slot.slot_key)["still_open"] == 1
