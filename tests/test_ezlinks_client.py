"""Tests for the Union County (EZLinks) client.

Built from a real captured response. The values here -- 6:42 at Galloping
Hill quoted at $24/$19/$19 across three rate plans, 7:20 at Ash Brook at
$66/$35/$35 -- are what the API actually returned.
"""

import datetime as dt

import pytest

from app import config, ezlinks_client
from app.ezlinks_client import EzLinksBlocked, EzLinksError, available_spots

ASH_BROOK = config.COURSES["ash_brook"]
GALLOPING_9 = config.COURSES["galloping_hill_9"]
UNION_COURSES = [ASH_BROOK, GALLOPING_9]


def entry(course_id, start, plan, price, spots=4, holes="9"):
    """One r06 row, in the shape the API sends."""
    return {
        "r01": f"guid-{course_id}-{start}-{plan}",
        "r06": plan,
        "r07": course_id,
        "r08": price,
        "r11": 4,
        "r14": spots,
        "r12": 261491,
        "r15": start,
        "r16": "Galloping Hill GC (Learning Center 9)" if course_id == 4551 else "Ash Brook GC",
        "r24": "6:42 AM",
        "r28": holes,
    }


PLANS = [
    {"r01": 1, "r02": 4862, "r03": "Public", "r04": "5 Day Access"},
    {"r01": 2, "r02": 4863, "r03": "Player Card (7day)", "r04": "7 Day Access"},
    {"r01": 3, "r02": 4864, "r03": "Player Card (14day)", "r04": "14 Day Access"},
]


@pytest.fixture
def payload():
    """The real shape: every tee time repeated once per rate plan."""
    return {
        "r01": "f5c9263e-c5f2-4c90-81e4-89d73102d299",
        "r05": PLANS,
        "r06": [
            entry(4551, "2026-09-09T06:42:00", 4862, 24.0),
            entry(4551, "2026-09-09T06:42:00", 4863, 19.0),
            entry(4551, "2026-09-09T06:42:00", 4864, 19.0),
            entry(4545, "2026-09-09T07:20:00", 4862, 66.0, holes="1,15,18"),
            entry(4545, "2026-09-09T07:20:00", 4863, 35.0, holes="1,15,18"),
            entry(4545, "2026-09-09T07:20:00", 4864, 35.0, holes="1,15,18"),
        ],
        "r07": 0,
    }


# --------------------------------------------------------------------------
# request shape
# --------------------------------------------------------------------------


def test_payload_matches_what_the_booking_page_posts():
    body = ezlinks_client.build_payload(UNION_COURSES, dt.date(2026, 9, 9))
    assert body["p01"] == [4545, 4551]
    assert body["p02"] == "09/09/2026"
    assert body["p06"] == 1  # widest search; per-slot spots filter later
    assert body["p07"] is False


def test_one_request_covers_every_union_course():
    body = ezlinks_client.build_payload(UNION_COURSES, dt.date(2026, 9, 9))
    assert len(body["p01"]) == len(UNION_COURSES)


# --------------------------------------------------------------------------
# the rate-plan duplication -- the bug this provider would otherwise cause
# --------------------------------------------------------------------------


def test_one_tee_time_yields_one_slot_not_one_per_rate_plan(payload):
    by_course = ezlinks_client.parse_response(payload, UNION_COURSES)

    # Six raw rows, but only two real tee times.
    assert len(by_course["galloping_hill_9"]) == 1
    assert len(by_course["ash_brook"]) == 1


def test_the_public_rate_is_the_one_quoted(payload):
    # Player Card rates are cheaper but need a card, so quoting $19 to
    # someone without one would be wrong.
    by_course = ezlinks_client.parse_response(payload, UNION_COURSES)
    assert by_course["galloping_hill_9"][0].green_fee == 24.0
    assert by_course["ash_brook"][0].green_fee == 66.0


def test_without_a_public_rate_the_cheapest_wins(payload):
    payload["r06"] = [
        entry(4551, "2026-09-09T06:42:00", 4864, 31.0),
        entry(4551, "2026-09-09T06:42:00", 4863, 19.0),
    ]
    slot = ezlinks_client.parse_response(payload, UNION_COURSES)["galloping_hill_9"][0]
    assert slot.green_fee == 19.0


def test_rate_plan_names_are_read_from_the_response(payload):
    assert ezlinks_client.rate_plan_names(payload)[4862] == "Public"


def test_different_times_stay_separate(payload):
    payload["r06"].append(entry(4551, "2026-09-09T07:18:00", 4862, 24.0))
    by_course = ezlinks_client.parse_response(payload, UNION_COURSES)
    assert len(by_course["galloping_hill_9"]) == 2


# --------------------------------------------------------------------------
# holes
# --------------------------------------------------------------------------


def test_holes_come_from_the_field_when_it_is_a_plain_number(payload):
    slot = ezlinks_client.parse_response(payload, UNION_COURSES)["galloping_hill_9"][0]
    assert slot.holes == 9


def test_holes_fall_back_to_the_course_when_the_field_is_a_list(payload):
    # Ash Brook's r28 is "1,15,18", which is not a hole count.
    slot = ezlinks_client.parse_response(payload, UNION_COURSES)["ash_brook"][0]
    assert slot.holes == 18  # configured on the course, not parsed


# --------------------------------------------------------------------------
# availability (documented assumption)
# --------------------------------------------------------------------------


def test_spots_read_from_the_remaining_players_field():
    assert available_spots({"r14": 2, "r11": 4}) == 2


def test_spots_fall_back_to_capacity_when_absent():
    assert available_spots({"r11": 3}) == 3


def test_an_entry_with_neither_reports_none_free():
    assert available_spots({}) == 0


# --------------------------------------------------------------------------
# parsing details
# --------------------------------------------------------------------------


def test_slots_carry_the_real_values(payload):
    slot = ezlinks_client.parse_response(payload, UNION_COURSES)["galloping_hill_9"][0]
    assert slot.course_key == "galloping_hill_9"
    assert slot.start == dt.datetime(2026, 9, 9, 6, 42)
    assert slot.display_time == "6:42 AM"
    assert slot.available_spots == 4


def test_courses_we_did_not_ask_about_are_dropped(payload):
    by_course = ezlinks_client.parse_response(payload, [GALLOPING_9])
    assert set(by_course) == {"galloping_hill_9"}


def test_a_course_with_no_slots_still_gets_an_entry():
    by_course = ezlinks_client.parse_response(
        {"r05": PLANS, "r06": []}, UNION_COURSES
    )
    assert all(v == [] for v in by_course.values())


def test_unusable_rows_are_skipped(payload):
    payload["r06"] = [
        {"r07": 4551, "r15": "nonsense"},
        "not a dict",
        entry(4551, "2026-09-09T06:42:00", 4862, 24.0),
    ]
    assert len(ezlinks_client.parse_response(payload, UNION_COURSES)["galloping_hill_9"]) == 1


def test_a_response_without_tee_times_raises():
    with pytest.raises(EzLinksError):
        ezlinks_client.parse_response({"r05": PLANS}, UNION_COURSES)


# --------------------------------------------------------------------------
# Cloudflare
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", [403, 429, 503])
def test_a_challenge_status_is_named_as_cloudflare(status):
    with pytest.raises(EzLinksBlocked) as exc:
        ezlinks_client._decode(status, "")
    assert "Cloudflare" in str(exc.value)


def test_an_html_challenge_body_is_named_as_cloudflare():
    # Cloudflare answers 200 with an interstitial in some configurations, so
    # status alone is not enough to tell a block from a real failure.
    with pytest.raises(EzLinksBlocked) as exc:
        ezlinks_client._decode(200, "<html><head><title>Just a moment...</title>")
    assert "Cloudflare" in str(exc.value)


def test_a_genuine_json_error_is_not_blamed_on_cloudflare():
    with pytest.raises(EzLinksError) as exc:
        ezlinks_client._decode(200, "{broken json")
    assert not isinstance(exc.value, EzLinksBlocked)


def test_a_good_response_decodes():
    assert ezlinks_client._decode(200, '{"r06": []}') == {"r06": []}
