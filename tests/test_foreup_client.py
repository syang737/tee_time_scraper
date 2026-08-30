import datetime as dt
import json
from pathlib import Path

import pytest

from app import config, foreup_client
from app.foreup_client import ForeUpError

FIXTURE = Path(__file__).parent / "fixtures" / "sample_times_response.json"
WEEQUAHIC = config.COURSES["weequahic"]
HENDRICKS = config.COURSES["hendricks"]


@pytest.fixture
def payload():
    return json.loads(FIXTURE.read_text())


def test_build_params_matches_the_booking_page_request():
    params = foreup_client.build_params(WEEQUAHIC, dt.date(2026, 8, 30))
    assert params["date"] == "08-30-2026"
    assert params["schedule_id"] == "11077"
    assert params["booking_class"] == "49424"
    # Facility-wide list is sent regardless of which course we're asking about.
    assert params["schedule_ids[]"] == ["11078", "11075", "11077"]
    assert params["api_key"] == ""


def test_parses_only_the_requested_courses_slots(payload):
    slots = foreup_client.parse_response(payload, WEEQUAHIC)
    # The Hendricks row (schedule 11075) is in the payload but must be dropped.
    assert len(slots) == 4
    assert {s.course_key for s in slots} == {"weequahic"}


def test_slots_come_back_sorted_and_normalized(payload):
    slots = foreup_client.parse_response(payload, WEEQUAHIC)
    assert [s.time_str for s in slots] == ["07:10", "08:26", "09:42", "14:18"]

    first = slots[0]
    assert first.available_spots == 4
    assert first.holes == 18
    assert first.green_fee == 32.0
    assert first.cart_fee == 18.0
    assert first.teetime_id == "tt-0710"
    assert first.date_str == "2026-09-05"
    assert first.display_time == "7:10 AM"


def test_filtering_by_schedule_works_for_the_other_course(payload):
    slots = foreup_client.parse_response(payload, HENDRICKS)
    assert len(slots) == 1
    assert slots[0].course_key == "hendricks"
    assert slots[0].time_str == "08:02"


def test_slot_key_is_stable_and_distinct(payload):
    slots = foreup_client.parse_response(payload, WEEQUAHIC)
    keys = [s.slot_key for s in slots]
    assert len(set(keys)) == len(keys)
    # Re-parsing the same payload yields identical keys, so dedup holds
    # across polls even though teetime_id is not relied on.
    again = foreup_client.parse_response(payload, WEEQUAHIC)
    assert [s.slot_key for s in again] == keys


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-09-05 07:10:00", dt.datetime(2026, 9, 5, 7, 10)),
        ("2026-09-05 07:10", dt.datetime(2026, 9, 5, 7, 10)),
        ("2026-09-05T07:10:00", dt.datetime(2026, 9, 5, 7, 10)),
    ],
)
def test_accepts_the_time_formats_foreup_might_send(raw, expected):
    slots = foreup_client.parse_response(
        [{"time": raw, "schedule_id": 11077, "available_spots": 2}], WEEQUAHIC
    )
    assert slots[0].start == expected


def test_entries_without_a_usable_time_or_spots_are_skipped():
    slots = foreup_client.parse_response(
        [
            {"time": "nonsense", "schedule_id": 11077, "available_spots": 2},
            {"schedule_id": 11077, "available_spots": 2},
            {"time": "2026-09-05 07:10:00", "schedule_id": 11077},
            {"time": "2026-09-05 08:10:00", "schedule_id": 11077, "available_spots": 1},
        ],
        WEEQUAHIC,
    )
    assert len(slots) == 1
    assert slots[0].time_str == "08:10"


def test_optional_fields_may_be_missing():
    slots = foreup_client.parse_response(
        [{"time": "2026-09-05 07:10:00", "schedule_id": 11077, "available_spots": 3}],
        WEEQUAHIC,
    )
    slot = slots[0]
    assert slot.holes is None
    assert slot.green_fee is None
    assert slot.course_name == "Weequahic"  # falls back to our own label


def test_entries_without_a_schedule_id_are_trusted_to_the_requested_course():
    slots = foreup_client.parse_response(
        [{"time": "2026-09-05 07:10:00", "available_spots": 3}], WEEQUAHIC
    )
    assert len(slots) == 1


def test_wrapped_list_response_is_unwrapped(payload):
    slots = foreup_client.parse_response({"times": payload}, WEEQUAHIC)
    assert len(slots) == 4


def test_unusable_payload_raises():
    with pytest.raises(ForeUpError):
        foreup_client.parse_response("not json we can use", WEEQUAHIC)
