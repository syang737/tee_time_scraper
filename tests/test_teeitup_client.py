"""TeeItUp (Somerset County): request shape, parsing, and how it polls.

The fixture is synthetic -- see its _comment -- built from the response shape
in public captures of this API. What these pin is our handling of that shape:
UTC times, cents, one start sold as 9 or 18, and matching entries by numeric
facility id rather than the Mongo id they are keyed on.
"""

import asyncio
import datetime as dt
import json
from pathlib import Path

import httpx
import pytest

from app import config, db, poller, teeitup_client
from app.models import Watch
from app.poller import TZ
from app.teeitup_client import TeeItUpError

FIXTURE = Path(__file__).parent / "fixtures" / "teeitup_somerset_SYNTHETIC.json"
DATE = dt.date(2026, 10, 10)  # a Saturday, in EDT
SOMERSET = [c for c in config.COURSES.values() if c.provider == "teeitup"]


def payload():
    return json.loads(FIXTURE.read_text())


def parsed():
    return teeitup_client.parse_response(payload(), SOMERSET)


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------


def test_all_six_somerset_courses_are_configured():
    ids = sorted(c.course_id for c in SOMERSET)
    # Exactly the ?course= list in the county's own booking link.
    assert ids == sorted(["7092", "10158", "7083", "7084", "7094", "7093"])
    assert {c.facility for c in SOMERSET} == {config.SOMERSET}
    assert {c.alias for c in SOMERSET} == {"somerset-group-v2"}


def test_booking_link_opens_that_course():
    course = config.COURSES["green_knoll"]
    assert course.booking_url == (
        "https://somerset-group-v2.book.teeitup.com/?course=7092"
    )


def test_request_names_the_site_and_lists_every_course():
    params = teeitup_client.build_params(SOMERSET, DATE)
    assert params["date"] == "2026-10-10"
    assert sorted(params["facilityIds"].split(",")) == sorted(c.course_id for c in SOMERSET)
    assert teeitup_client.headers_for("somerset-group-v2")["X-Be-Alias"] == "somerset-group-v2"


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def test_times_are_converted_from_utc_to_local():
    green_knoll = parsed()["green_knoll"]
    # 11:20Z in October is 7:20 EDT.
    assert green_knoll[0].start == dt.datetime(2026, 10, 10, 7, 20)
    assert green_knoll[0].display_time == "7:20 AM"


def test_an_evening_time_stays_on_its_local_date():
    """00:10Z on the 11th is 8:10 PM on the 10th -- the date asked for."""
    evening = parsed()["green_knoll"][-1]
    assert evening.start == dt.datetime(2026, 10, 10, 20, 10)
    assert evening.date_str == "2026-10-10"


def test_open_spots_are_capacity_less_booked():
    slots = {s.time_str: s for s in parsed()["green_knoll"]}
    assert slots["07:20"].available_spots == 2   # 4 max, 2 booked
    assert slots["07:30"].available_spots == 0   # full
    assert slots["20:10"].available_spots == 4


def test_spots_never_exceed_the_largest_party_a_rate_sells_to():
    entry = {"maxPlayers": 4, "bookedPlayers": 0,
             "rates": [{"allowedPlayers": [1, 2]}]}
    assert teeitup_client.available_spots(entry) == 2


def test_spots_without_booked_players_fall_back_sensibly():
    assert teeitup_client.available_spots({"maxPlayers": 4}) == 4
    assert teeitup_client.available_spots(
        {"rates": [{"allowedPlayers": [1, 2, 3]}]}
    ) == 3
    assert teeitup_client.available_spots({}) == 0


def test_fees_are_converted_from_cents_preferring_walking():
    slot = parsed()["green_knoll"][0]
    assert slot.green_fee == 54.0   # not the $74 riding rate


def test_one_start_sold_as_9_or_18_is_one_slot_that_knows_both():
    neshanic = parsed()["neshanic_valley"]
    assert len(neshanic) == 1, "a 9 and an 18 rate on one start must not be two alerts"
    slot = neshanic[0]
    assert slot.holes_options == (9, 18)
    assert slot.holes == 18             # the dedup key stays stable
    assert slot.holes_text == "9/18"
    assert slot.green_fee == 36.0       # cheapest quoted


def test_entries_are_matched_by_numeric_id_not_mongo_id():
    result = parsed()
    assert set(result) == {c.key for c in SOMERSET}
    assert len(result["green_knoll"]) == 3
    assert len(result["neshanic_valley"]) == 1
    # Facility 9999 was not asked for and must not land on any course.
    assert all(s.green_fee != 99.0 for slots in result.values() for s in slots)


def test_a_course_with_no_times_is_present_and_empty():
    assert parsed()["spooky_brook"] == []


def test_single_course_request_tolerates_entries_without_an_id():
    bare = [{"teetimes": [{"teetime": "2026-10-10T11:20:00.000Z", "maxPlayers": 4}]}]
    result = teeitup_client.parse_response(bare, [config.COURSES["quail_brook"]])
    assert len(result["quail_brook"]) == 1


def test_an_error_object_is_reported_not_parsed_as_empty():
    with pytest.raises(TeeItUpError, match="alias"):
        teeitup_client.parse_response({"message": "Invalid alias"}, SOMERSET)


# --------------------------------------------------------------------------
# watching
# --------------------------------------------------------------------------


@pytest.mark.parametrize("holes, matches", [("9", True), ("18", True), ("any", True)])
def test_a_9_or_18_start_matches_a_watch_for_either(holes, matches):
    slot = parsed()["neshanic_valley"][0]
    watch = Watch(courses=["neshanic_valley"], holes=holes)
    assert watch.matches(slot) is matches


def test_an_18_only_start_does_not_match_a_9_hole_watch():
    slot = parsed()["green_knoll"][0]
    assert not Watch(courses=["green_knoll"], holes="9").matches(slot)


# --------------------------------------------------------------------------
# HTTP and the poller
# --------------------------------------------------------------------------


class Recorder:
    def __init__(self, status=200, body=None):
        self.status = status
        self.body = payload() if body is None else body
        self.requests: list[httpx.Request] = []

    def client(self):
        def handler(request):
            self.requests.append(request)
            return httpx.Response(self.status, json=self.body)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_fetch_sends_one_request_with_the_alias_header():
    recorder = Recorder()
    result = run(teeitup_client.fetch_times(recorder.client(), SOMERSET, DATE))

    assert len(recorder.requests) == 1
    request = recorder.requests[0]
    assert request.url.host == "phx-api-be-east-1b.kenna.io"
    assert request.url.path == "/v2/tee-times"
    assert request.headers["X-Be-Alias"] == "somerset-group-v2"
    assert request.url.params["date"] == "2026-10-10"
    assert len(result["green_knoll"]) == 3


@pytest.mark.parametrize("status", [403, 429, 500])
def test_http_errors_become_fetch_errors(status):
    recorder = Recorder(status=status, body={"message": "nope"})
    with pytest.raises(TeeItUpError, match=str(status)):
        run(teeitup_client.fetch_times(recorder.client(), SOMERSET, DATE))


def test_the_poller_batches_somerset_into_one_request_per_date():
    keys = {(c.key, d) for c in SOMERSET for d in (DATE, DATE + dt.timedelta(days=1))}
    plan = poller.plan_requests(keys)
    assert [(p, d) for p, d, _ in plan] == [
        ("teeitup", DATE), ("teeitup", DATE + dt.timedelta(days=1))
    ]
    assert all(len(course_keys) == 6 for _, _, course_keys in plan)


def test_a_somerset_opening_reaches_ntfy(temp_db, monkeypatch):
    monkeypatch.setattr(config, "NTFY_SERVER", "https://ntfy.example")
    monkeypatch.setattr(poller, "_last_called", {})
    db.save_watch(Watch(label="Neshanic", courses=["neshanic_valley", "green_knoll"],
                        specific_date=DATE.isoformat(), time_start="07:00",
                        time_end="09:00", min_players=2, ntfy_topic="simon"))
    pushes = []

    def handler(request):
        if request.url.host == "ntfy.example":
            pushes.append((request.headers["Title"], request.content.decode(),
                           request.headers["Click"]))
            return httpx.Response(200)
        return httpx.Response(200, json=payload())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    now = dt.datetime(2026, 10, 7, 9, 0, tzinfo=TZ)
    run(poller.poll_once(client, now=now))

    titles = sorted(t for t, _, _ in pushes)
    assert titles == [
        "Green Knoll - Sat Oct 10 at 7:20 AM",
        "Neshanic Valley - Sat Oct 10 at 8:00 AM",
    ]
    neshanic = next(p for p in pushes if p[0].startswith("Neshanic"))
    assert "9/18 holes" in neshanic[1]
    assert neshanic[2].endswith("?course=7083")
    assert db.get_poll_status()["last_error"] is None
