import asyncio
import datetime as dt

import pytest

from app import config, db, poller
from app.foreup_client import ForeUpError
from app.models import TeeTimeSlot, Watch
from app.poller import TZ, should_notify

NOW = dt.datetime(2026, 9, 2, 9, 0, tzinfo=TZ)


def make_slot(time="08:00", spots=4, holes=18, course="weequahic", date=(2026, 9, 5)):
    hour, minute = (int(p) for p in time.split(":"))
    return TeeTimeSlot(
        course_key=course,
        course_name=course.title(),
        start=dt.datetime(*date, hour, minute),
        available_spots=spots,
        holes=holes,
    )


@pytest.fixture
def saved_watch(temp_db):
    watch_id = db.save_watch(
        Watch(
            label="Weekend mornings",
            courses=["weequahic"],
            days=["sat", "sun"],
            horizon_days=14,
            time_start="07:00",
            time_end="10:00",
            min_players=2,
            holes="18",
        )
    )
    return db.get_watch(watch_id)


class FakeNotifier:
    """Stands in for ntfy so tests never touch the network."""

    def __init__(self, succeed=True):
        self.sent: list[TeeTimeSlot] = []
        self.succeed = succeed

    async def __call__(self, client, slot, watch):
        self.sent.append(slot)
        return self.succeed


def install(monkeypatch, slots_by_call, notifier):
    """Patch the network edges: fetch returns canned slots, notify is fake."""
    calls = iter(slots_by_call)

    async def fake_fetch(client, course, date):
        try:
            result = next(calls)
        except StopIteration:
            return []
        if isinstance(result, Exception):
            raise result
        return [s for s in result if s.course_key == course.key]

    monkeypatch.setattr(poller.foreup_client, "fetch_times", fake_fetch)
    monkeypatch.setattr(poller.notify, "send_slot_alert", notifier)


# --------------------------------------------------------------------------
# should_notify
# --------------------------------------------------------------------------


def test_a_slot_never_seen_before_notifies():
    assert should_notify(None, NOW, 10)


def test_a_still_open_slot_stays_quiet_inside_the_cooldown():
    row = {
        "still_open": 1,
        "last_notified_at": (NOW - dt.timedelta(minutes=3)).astimezone(dt.timezone.utc).isoformat(),
    }
    assert not should_notify(row, NOW, 10)


def test_a_still_open_slot_renotifies_once_the_cooldown_passes():
    row = {
        "still_open": 1,
        "last_notified_at": (NOW - dt.timedelta(minutes=11)).astimezone(dt.timezone.utc).isoformat(),
    }
    assert should_notify(row, NOW, 10)


def test_a_slot_that_closed_and_reopened_notifies_immediately():
    row = {
        "still_open": 0,
        "last_notified_at": NOW.astimezone(dt.timezone.utc).isoformat(),
    }
    assert should_notify(row, NOW, 10)


def test_a_slot_recorded_without_a_notification_still_notifies():
    assert should_notify({"still_open": 1, "last_notified_at": None}, NOW, 10)


# --------------------------------------------------------------------------
# process_watch
# --------------------------------------------------------------------------


def run(coro):
    return asyncio.run(coro)


def test_only_matching_slots_are_notified(monkeypatch, saved_watch):
    notifier = FakeNotifier()
    available = [
        make_slot(time="08:00", spots=2, holes=18),   # matches
        make_slot(time="06:30", spots=4, holes=18),   # too early
        make_slot(time="09:00", spots=1, holes=18),   # not enough spots
        make_slot(time="09:30", spots=4, holes=9),    # wrong holes
        make_slot(time="09:45", spots=4, holes=18, course="byrne"),  # wrong course
    ]
    # Four candidate dates over the 14-day horizon; only the first returns data.
    install(monkeypatch, [available], notifier)

    result = run(poller.process_watch(None, saved_watch, NOW))

    assert [s.time_str for s in result.matches] == ["08:00"]
    assert [s.time_str for s in notifier.sent] == ["08:00"]


def test_the_same_open_slot_is_not_notified_twice_in_a_row(monkeypatch, saved_watch):
    notifier = FakeNotifier()
    slot = make_slot(time="08:00", spots=2)

    install(monkeypatch, [[slot]], notifier)
    run(poller.process_watch(None, saved_watch, NOW))

    install(monkeypatch, [[slot]], notifier)
    run(poller.process_watch(None, saved_watch, NOW + dt.timedelta(seconds=30)))

    assert len(notifier.sent) == 1


def test_a_slot_that_reopens_after_being_taken_notifies_again(monkeypatch, saved_watch):
    notifier = FakeNotifier()
    slot = make_slot(time="08:00", spots=2)

    install(monkeypatch, [[slot]], notifier)
    run(poller.process_watch(None, saved_watch, NOW))

    # Someone books it: it vanishes from the response.
    install(monkeypatch, [[]], notifier)
    run(poller.process_watch(None, saved_watch, NOW + dt.timedelta(minutes=1)))
    assert db.get_seen_slot(saved_watch.id, slot.slot_key)["still_open"] == 0

    # They cancel: it comes back, and that is a fresh opening worth an alert.
    install(monkeypatch, [[slot]], notifier)
    run(poller.process_watch(None, saved_watch, NOW + dt.timedelta(minutes=2)))

    assert len(notifier.sent) == 2


def test_a_failed_fetch_does_not_mark_everything_as_taken(monkeypatch, saved_watch):
    notifier = FakeNotifier()
    slot = make_slot(time="08:00", spots=2)

    install(monkeypatch, [[slot]], notifier)
    run(poller.process_watch(None, saved_watch, NOW))

    # The API blips on the very first date fetched.
    install(monkeypatch, [ForeUpError("boom")], notifier)
    run(poller.process_watch(None, saved_watch, NOW + dt.timedelta(minutes=1)))

    row = db.get_seen_slot(saved_watch.id, slot.slot_key)
    assert row["still_open"] == 1, "a transient error must not look like a booking"


def test_slots_already_underway_today_are_ignored(monkeypatch, saved_watch):
    notifier = FakeNotifier()
    today = NOW.date()
    watch = db.get_watch(saved_watch.id)
    watch.days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    watch.time_start = None
    watch.time_end = None
    db.save_watch(watch)

    past = make_slot(time="08:00", spots=4, date=(today.year, today.month, today.day))
    future = make_slot(time="17:00", spots=4, date=(today.year, today.month, today.day))
    install(monkeypatch, [[past, future]], notifier)

    result = run(poller.process_watch(None, db.get_watch(saved_watch.id), NOW))

    assert [s.time_str for s in result.matches] == ["17:00"]


def test_a_failed_push_is_retried_on_the_next_poll(monkeypatch, saved_watch):
    failing = FakeNotifier(succeed=False)
    slot = make_slot(time="08:00", spots=2)

    install(monkeypatch, [[slot]], failing)
    run(poller.process_watch(None, saved_watch, NOW))
    assert db.get_seen_slot(saved_watch.id, slot.slot_key)["notify_count"] == 0

    working = FakeNotifier()
    install(monkeypatch, [[slot]], working)
    run(poller.process_watch(None, saved_watch, NOW + dt.timedelta(seconds=30)))

    assert len(working.sent) == 1, "an unsent alert must not be treated as delivered"


# --------------------------------------------------------------------------
# poll_once
# --------------------------------------------------------------------------


def test_expired_watches_get_deactivated(monkeypatch, temp_db):
    notifier = FakeNotifier()
    install(monkeypatch, [], notifier)
    watch_id = db.save_watch(
        Watch(label="Old", courses=["weequahic"], specific_date="2026-09-01")
    )

    run(poller.poll_once(None, now=NOW))

    assert db.get_watch(watch_id).active is False


def test_poll_records_health(monkeypatch, saved_watch):
    install(monkeypatch, [[make_slot(spots=2)]], FakeNotifier())
    run(poller.poll_once(None))

    status = db.get_poll_status()
    assert status["poll_count"] == 1
    assert status["last_error"] is None
    assert status["last_success_at"] is not None


def test_a_watch_whose_every_request_failed_is_reported_as_an_error(
    monkeypatch, saved_watch
):
    # A blanket failure (network down, API blocking us) must not leave the
    # dashboard looking healthy while nothing is actually being watched.
    install(monkeypatch, [ForeUpError("403 Forbidden")] * 20, FakeNotifier())

    run(poller.poll_once(None, now=NOW))

    last_error = db.get_poll_status()["last_error"]
    assert last_error is not None
    assert "403 Forbidden" in last_error


def test_partial_failures_are_not_reported_as_an_outage(monkeypatch, saved_watch):
    # Some dates succeeded, so the watch is still doing its job.
    install(
        monkeypatch,
        [ForeUpError("blip"), [make_slot(time="08:00", spots=2)]],
        FakeNotifier(),
    )

    run(poller.poll_once(None, now=NOW))

    assert db.get_poll_status()["last_error"] is None


def test_paused_watches_are_skipped(monkeypatch, saved_watch):
    notifier = FakeNotifier()
    db.set_watch_active(saved_watch.id, False)
    install(monkeypatch, [[make_slot(spots=2)]], notifier)

    run(poller.poll_once(None))

    assert notifier.sent == []
