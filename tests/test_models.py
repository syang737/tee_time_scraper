import datetime as dt

import pytest

from app.models import TeeTimeSlot, Watch


def slot(time="08:00", spots=4, holes=18, course="weequahic"):
    hour, minute = (int(p) for p in time.split(":"))
    return TeeTimeSlot(
        course_key=course,
        course_name=course.title(),
        start=dt.datetime(2026, 9, 5, hour, minute),
        available_spots=spots,
        holes=holes,
    )


def watch(**kwargs):
    base = dict(id=1, courses=["weequahic"], days=["sat", "sun"])
    base.update(kwargs)
    return Watch(**base)


# --------------------------------------------------------------------------
# course criterion
# --------------------------------------------------------------------------


def test_only_watched_courses_match():
    w = watch(courses=["weequahic", "byrne"])
    assert w.matches(slot(course="weequahic"))
    assert w.matches(slot(course="byrne"))
    assert not w.matches(slot(course="hendricks"))


# --------------------------------------------------------------------------
# time window criterion
# --------------------------------------------------------------------------


def test_time_window_is_inclusive_on_both_ends():
    w = watch(time_start="07:00", time_end="10:00")
    assert w.matches(slot(time="07:00"))
    assert w.matches(slot(time="10:00"))
    assert not w.matches(slot(time="06:59"))
    assert not w.matches(slot(time="10:01"))


def test_open_ended_windows():
    assert watch(time_start="12:00").matches(slot(time="18:00"))
    assert not watch(time_start="12:00").matches(slot(time="09:00"))
    assert watch(time_end="12:00").matches(slot(time="09:00"))
    assert watch().matches(slot(time="05:30"))


# --------------------------------------------------------------------------
# players criterion
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "min_players,spots,expected",
    [
        (2, 4, True),
        (2, 2, True),
        (2, 1, False),
        (4, 3, False),
        (0, 1, True),
    ],
)
def test_min_players_is_a_lower_bound(min_players, spots, expected):
    assert watch(min_players=min_players).matches(slot(spots=spots)) is expected


def test_a_slot_with_no_open_spots_never_matches():
    assert not watch(min_players=0).matches(slot(spots=0))


# --------------------------------------------------------------------------
# holes
# --------------------------------------------------------------------------


def test_holes_filter():
    assert watch(holes="18").matches(slot(holes=18))
    assert not watch(holes="18").matches(slot(holes=9))
    assert watch(holes="any").matches(slot(holes=9))


def test_unknown_hole_count_does_not_satisfy_a_specific_request():
    assert not watch(holes="18").matches(slot(holes=None))
    assert watch(holes="any").matches(slot(holes=None))


def test_all_criteria_must_hold_together():
    w = watch(courses=["weequahic"], time_start="07:00", time_end="10:00",
              min_players=2, holes="18")
    assert w.matches(slot(time="08:00", spots=2, holes=18))
    assert not w.matches(slot(time="11:00", spots=2, holes=18))
    assert not w.matches(slot(time="08:00", spots=1, holes=18))
    assert not w.matches(slot(time="08:00", spots=2, holes=9))
    assert not w.matches(slot(time="08:00", spots=2, holes=18, course="byrne"))


# --------------------------------------------------------------------------
# date resolution
# --------------------------------------------------------------------------


def test_candidate_dates_picks_the_requested_weekdays():
    # 2026-09-05 is a Saturday.
    today = dt.date(2026, 9, 2)  # Wednesday
    dates = watch(days=["sat", "sun"], horizon_days=14).candidate_dates(today)
    assert dates[:4] == [
        dt.date(2026, 9, 5),
        dt.date(2026, 9, 6),
        dt.date(2026, 9, 12),
        dt.date(2026, 9, 13),
    ]


def test_candidate_dates_includes_today_when_it_matches():
    saturday = dt.date(2026, 9, 5)
    assert saturday in watch(days=["sat"], horizon_days=7).candidate_dates(saturday)


def test_specific_date_overrides_weekdays():
    w = watch(days=["sat"], specific_date="2026-09-09")  # a Wednesday
    assert w.candidate_dates(dt.date(2026, 9, 2)) == [dt.date(2026, 9, 9)]


def test_a_past_specific_date_yields_nothing_and_expires():
    w = watch(specific_date="2026-09-01")
    today = dt.date(2026, 9, 2)
    assert w.candidate_dates(today) == []
    assert w.is_expired(today)


def test_recurring_watches_never_expire():
    assert not watch(days=["sat"]).is_expired(dt.date(2026, 9, 2))


def test_describe_mentions_every_criterion():
    text = watch(
        courses=["weequahic"], time_start="07:00", time_end="10:00", min_players=2
    ).describe()
    assert "Weequahic" in text
    assert "07:00-10:00" in text
    assert "2+ players" in text
