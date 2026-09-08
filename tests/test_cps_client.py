"""Tests for the Bergen County (CPS Golf) client.

Unlike the ForeUp fixture, `cps_bergen_2026-09-12.json` is a real captured
response, trimmed to two slots per course. So these assert against values the
API genuinely returned.
"""

import datetime as dt
import json
from pathlib import Path

import pytest

from app import config, cps_client
from app.cps_client import CpsError, available_spots

FIXTURE = Path(__file__).parent / "fixtures" / "cps_bergen_2026-09-12.json"

ROCKLEIGH_BLUE = config.COURSES["rockleigh_blue"]
SOLDIER_HILL = config.COURSES["soldier_hill"]
DARLINGTON = config.COURSES["darlington"]
ALL_BERGEN = [c for c in config.COURSES.values() if c.provider == "cps"]


@pytest.fixture
def payload():
    return json.loads(FIXTURE.read_text())


# --------------------------------------------------------------------------
# request shape
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "date,expected",
    [
        (dt.date(2026, 9, 12), "Sat Sep 12 2026"),
        (dt.date(2026, 9, 5), "Sat Sep 05 2026"),   # day is zero-padded
        (dt.date(2026, 1, 1), "Thu Jan 01 2026"),
        (dt.date(2026, 12, 31), "Thu Dec 31 2026"),
    ],
)
def test_search_date_matches_the_javascript_spelling(date, expected):
    # The site sends Date.toDateString(); %a/%b would follow the process
    # locale, so the names are fixed in code instead.
    assert cps_client.format_search_date(date) == expected


def test_one_request_carries_every_course():
    params = cps_client.build_params(ALL_BERGEN, dt.date(2026, 9, 12))
    ids = params["courseIds"].split(",")
    assert len(ids) == len(ALL_BERGEN)
    assert set(ids) == {c.course_id for c in ALL_BERGEN}


def test_request_asks_for_one_player_to_widen_the_results():
    # Asking for a foursome would hide slots with fewer spots left, which are
    # exactly the ones a "1+ players" watch wants.
    params = cps_client.build_params(ALL_BERGEN, dt.date(2026, 9, 12))
    assert params["numberOfPlayer"] == "1"


def test_each_request_gets_a_fresh_transaction_id():
    first = cps_client.build_params(ALL_BERGEN, dt.date(2026, 9, 12))
    second = cps_client.build_params(ALL_BERGEN, dt.date(2026, 9, 12))
    assert first["transactionId"] != second["transactionId"]


# --------------------------------------------------------------------------
# availability
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sizes,expected",
    [
        ([1, 2, 3, 4], 4),
        ([1, 2], 2),      # two of a foursome already booked
        ([1], 1),
        ([], 0),          # nothing bookable
    ],
)
def test_spots_come_from_the_largest_party_size_accepted(sizes, expected):
    assert available_spots({"availableParticipantNo": sizes}) == expected


def test_a_missing_availability_list_is_treated_as_no_spots():
    # maxPlayer describes the slot's capacity, not what is left, so trusting
    # it would invent availability on a fully booked tee time.
    assert available_spots({"maxPlayer": 4, "minPlayer": 1}) == 0


# --------------------------------------------------------------------------
# parsing the real response
# --------------------------------------------------------------------------


def test_one_response_is_split_across_every_course(payload):
    by_course = cps_client.parse_response(payload, ALL_BERGEN)

    assert set(by_course) == {c.key for c in ALL_BERGEN}
    # Six courses appeared in the capture, two slots each.
    assert sum(len(v) for v in by_course.values()) == 12
    assert all(len(v) == 2 for v in by_course.values())


def test_slots_carry_the_real_values(payload):
    slot = cps_client.parse_response(payload, ALL_BERGEN)["rockleigh_blue"][0]

    assert slot.course_key == "rockleigh_blue"
    assert slot.course_name == "Rockleigh Blue 9"
    assert slot.start == dt.datetime(2026, 9, 12, 6, 30)
    assert slot.available_spots == 4
    assert slot.holes == 9
    assert slot.green_fee == 25.0
    assert slot.teetime_id == "978093"
    assert slot.display_time == "6:30 AM"


def test_an_eighteen_hole_course_parses_too(payload):
    slot = cps_client.parse_response(payload, ALL_BERGEN)["soldier_hill"][0]
    assert slot.holes == 18
    assert slot.green_fee == 55.0
    assert slot.time_str == "16:20"


def test_courses_we_did_not_ask_about_are_dropped(payload):
    # Never attribute an unmapped courseId to the wrong course.
    by_course = cps_client.parse_response(payload, [ROCKLEIGH_BLUE])
    assert set(by_course) == {"rockleigh_blue"}
    assert all(s.course_key == "rockleigh_blue" for s in by_course["rockleigh_blue"])


def test_slots_come_back_sorted_per_course(payload):
    by_course = cps_client.parse_response(payload, ALL_BERGEN)
    for slots in by_course.values():
        assert [s.start for s in slots] == sorted(s.start for s in slots)


def test_a_course_with_no_slots_still_gets_an_entry(payload):
    # An empty list means "checked, nothing there" -- which the poller needs
    # in order to mark previously-open slots as taken.
    by_course = cps_client.parse_response({"isSuccess": True, "content": []}, ALL_BERGEN)
    assert set(by_course) == {c.key for c in ALL_BERGEN}
    assert all(v == [] for v in by_course.values())


def test_slot_keys_are_distinct_and_stable(payload):
    by_course = cps_client.parse_response(payload, ALL_BERGEN)
    keys = [s.slot_key for slots in by_course.values() for s in slots]
    assert len(set(keys)) == len(keys)

    again = cps_client.parse_response(payload, ALL_BERGEN)
    repeated = [s.slot_key for slots in again.values() for s in slots]
    assert repeated == keys


# --------------------------------------------------------------------------
# bad responses
# --------------------------------------------------------------------------


def test_an_unsuccessful_response_raises():
    with pytest.raises(CpsError):
        cps_client.parse_response({"isSuccess": False, "content": []}, ALL_BERGEN)


def test_a_response_without_content_raises():
    with pytest.raises(CpsError):
        cps_client.parse_response({"isSuccess": True}, ALL_BERGEN)


def test_a_non_object_payload_raises():
    with pytest.raises(CpsError):
        cps_client.parse_response([], ALL_BERGEN)


def test_unusable_entries_are_skipped_not_fatal():
    by_course = cps_client.parse_response(
        {
            "isSuccess": True,
            "content": [
                {"courseId": 12, "startTime": "not a time"},
                {"courseId": 12},
                "nonsense",
                {"courseId": 12, "startTime": "2026-09-12T07:00:00",
                 "availableParticipantNo": [1, 2]},
            ],
        },
        [ROCKLEIGH_BLUE],
    )
    slots = by_course["rockleigh_blue"]
    assert len(slots) == 1
    assert slots[0].available_spots == 2


def test_optional_fields_may_be_missing():
    slot = cps_client.parse_response(
        {
            "isSuccess": True,
            "content": [{
                "courseId": "4",
                "startTime": "2026-09-12T07:00:00",
                "availableParticipantNo": [1],
            }],
        },
        [DARLINGTON],
    )["darlington"][0]

    assert slot.holes is None
    assert slot.green_fee is None
    assert slot.course_name == "Darlington 18"  # falls back to our own label
