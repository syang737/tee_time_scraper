"""Retention must bound the database without breaking dedup.

``seen_slots`` does double duty: it is both the history shown in the GUI and
the live state that stops a still-open slot re-alerting every 30 seconds.
Purging the wrong rows would turn every surviving open slot back into a
"new opening" and spam the phone, so these tests pin that boundary.
"""

import datetime as dt

import pytest

from app import config, db
from app.models import TeeTimeSlot, Watch
from app.poller import TZ

NOW = dt.datetime(2026, 9, 20, 9, 0, tzinfo=TZ)


def make_slot(time="08:00", spots=4, course="weequahic", date=(2026, 9, 26)):
    hour, minute = (int(p) for p in time.split(":"))
    return TeeTimeSlot(
        course_key=course,
        course_name=course.title(),
        start=dt.datetime(*date, hour, minute),
        available_spots=spots,
        holes=18,
    )


@pytest.fixture
def watch_id(temp_db):
    return db.save_watch(Watch(label="w", courses=["weequahic"], days=["sat"]))


def test_a_still_open_slot_is_never_purged(watch_id):
    """The one that would cause alert spam if we got it wrong."""
    slot = make_slot()
    # Seen for the first time long ago, but still being seen right now.
    db.upsert_slot(watch_id, slot, notified=True, now=NOW - dt.timedelta(days=30))
    db.upsert_slot(watch_id, slot, notified=False, now=NOW)

    assert db.purge_old_slots(NOW, retention_days=5) == 0
    assert db.get_seen_slot(watch_id, slot.slot_key) is not None


def test_a_slot_not_seen_since_the_cutoff_is_purged(watch_id):
    slot = make_slot()
    db.upsert_slot(watch_id, slot, notified=True, now=NOW - dt.timedelta(days=6))

    assert db.purge_old_slots(NOW, retention_days=5) == 1
    assert db.get_seen_slot(watch_id, slot.slot_key) is None


def test_the_cutoff_is_honoured_exactly(watch_id):
    recent = make_slot(time="08:00")
    stale = make_slot(time="09:00")
    db.upsert_slot(watch_id, recent, notified=True, now=NOW - dt.timedelta(days=4, hours=23))
    db.upsert_slot(watch_id, stale, notified=True, now=NOW - dt.timedelta(days=5, hours=1))

    assert db.purge_old_slots(NOW, retention_days=5) == 1
    assert db.get_seen_slot(watch_id, recent.slot_key) is not None
    assert db.get_seen_slot(watch_id, stale.slot_key) is None


def test_purging_leaves_watches_alone(watch_id):
    db.upsert_slot(watch_id, make_slot(), notified=True, now=NOW - dt.timedelta(days=30))

    db.purge_old_slots(NOW, retention_days=5)

    assert db.get_watch(watch_id) is not None, "history ages out, saved searches do not"


def test_a_purged_slot_that_reappears_alerts_again(watch_id):
    # Not a regression -- if a tee time resurfaces a week later it genuinely
    # is news. This just documents the intended consequence.
    slot = make_slot()
    db.upsert_slot(watch_id, slot, notified=True, now=NOW - dt.timedelta(days=10))
    db.purge_old_slots(NOW, retention_days=5)

    from app.poller import should_notify

    assert should_notify(db.get_seen_slot(watch_id, slot.slot_key), NOW, 10)


# --------------------------------------------------------------------------
# cadence
# --------------------------------------------------------------------------


def test_a_fresh_database_is_due_for_a_purge(temp_db):
    assert db.due_for_purge(NOW)


def test_not_due_again_within_a_day(temp_db):
    db.purge_old_slots(NOW, retention_days=5)
    assert not db.due_for_purge(NOW + dt.timedelta(hours=23))


def test_due_again_after_a_day(temp_db):
    db.purge_old_slots(NOW, retention_days=5)
    assert db.due_for_purge(NOW + dt.timedelta(days=1, minutes=1))


def test_purge_records_when_it_ran(temp_db):
    db.purge_old_slots(NOW, retention_days=5)
    assert db.get_poll_status()["last_purge_at"] is not None


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------


def test_storage_stats_counts_rows_and_bytes(watch_id):
    db.upsert_slot(watch_id, make_slot(time="08:00"), notified=True, now=NOW)
    db.upsert_slot(watch_id, make_slot(time="09:00"), notified=True, now=NOW)

    stats = db.storage_stats()
    assert stats["slot_rows"] == 2
    assert stats["db_bytes"] > 0


def test_the_database_stays_bounded_over_a_long_run(watch_id):
    """A year of daily churn must not accumulate a year of rows."""
    day = NOW - dt.timedelta(days=365)
    while day <= NOW:
        # Each day brings a new tee time and re-sees nothing older.
        db.upsert_slot(
            watch_id,
            make_slot(time="08:00", date=(day.year, day.month, day.day)),
            notified=True,
            now=day,
        )
        db.purge_old_slots(day, retention_days=config.RETENTION_DAYS)
        day += dt.timedelta(days=1)

    assert db.storage_stats()["slot_rows"] <= config.RETENTION_DAYS + 1
