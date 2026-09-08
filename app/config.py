"""Settings, and the fixed identifiers for every course we can watch.

Two booking systems are covered, and they work differently enough that each
gets its own course type and client:

* **ForeUp** (Essex County) -- one request per course per date.
* **CPS Golf** (Bergen County) -- one request covers *all* courses for a
  date, since ``courseIds`` takes a list. The poller batches accordingly.
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

# Both facilities are in New Jersey; all API dates/times are local to it.
COURSE_TIMEZONE = "America/New_York"

# --------------------------------------------------------------------------
# ForeUp (Essex County)
# --------------------------------------------------------------------------

# The three Essex courses live on one ForeUp facility, so the API needs both
# the course-specific schedule_id/booking_class and the facility-wide list of
# schedule_ids. These values came from the courses' own booking pages.
FACILITY_SCHEDULE_IDS = ["11078", "11075", "11077"]

BASE_API_URL = "https://foreupsoftware.com/index.php/api/booking/times"
BOOKING_PAGE_URL = "https://foreupsoftware.com/index.php/booking/{course_id}/{schedule_id}#/teetimes"


@dataclass(frozen=True)
class ForeUpCourse:
    key: str
    name: str
    facility: str
    course_id: str
    schedule_id: str
    booking_class: str
    provider: str = "foreup"

    @property
    def booking_url(self) -> str:
        return BOOKING_PAGE_URL.format(
            course_id=self.course_id, schedule_id=self.schedule_id
        )


# --------------------------------------------------------------------------
# CPS Golf (Bergen County)
# --------------------------------------------------------------------------

CPS_BASE_URL = "https://bergencountygolf.cps.golf"
CPS_API_URL = f"{CPS_BASE_URL}/onlineres/onlineapi/api/v1/onlinereservation/TeeTimes"
CPS_BOOKING_PAGE_URL = f"{CPS_BASE_URL}/onlineres/"

# Pricing/booking class the search runs under. "NON" is the non-resident
# rate seen in the browser. County cardholders may see different times and
# prices, so this is overridable.
CPS_CLASS_CODE = os.getenv("CPS_CLASS_CODE", "NON")

# The API filters by party size, so asking for 1 returns the widest set of
# slots; how many spots each actually has comes back per slot.
CPS_SEARCH_PLAYERS = os.getenv("CPS_SEARCH_PLAYERS", "1")

CPS_MEMBER_STORE_ID = os.getenv("CPS_MEMBER_STORE_ID", "12")


@dataclass(frozen=True)
class CpsCourse:
    key: str
    name: str
    facility: str
    course_id: str  # the numeric courseId in the courseIds list
    provider: str = "cps"

    @property
    def booking_url(self) -> str:
        # CPS has no per-course deep link we can rely on, so send people to
        # the reservation front page.
        return CPS_BOOKING_PAGE_URL


# --------------------------------------------------------------------------
# EZLinks (Union County)
# --------------------------------------------------------------------------

EZLINKS_BASE_URL = "https://unioncountygolf.ezlinksgolf.com"
EZLINKS_API_URL = f"{EZLINKS_BASE_URL}/api/search/search"

# The search window sent with every request; slot-level filtering is ours.
EZLINKS_EARLIEST = os.getenv("EZLINKS_EARLIEST", "5:00 AM")
EZLINKS_LATEST = os.getenv("EZLINKS_LATEST", "8:00 PM")
EZLINKS_SEARCH_PLAYERS = os.getenv("EZLINKS_SEARCH_PLAYERS", "1")

# Union sits behind Cloudflare, which usually rejects on TLS fingerprint
# rather than headers. curl_cffi replays a real Chrome handshake; set this
# empty to force plain httpx instead.
EZLINKS_IMPERSONATE = os.getenv("EZLINKS_IMPERSONATE", "chrome124")


@dataclass(frozen=True)
class EzLinksCourse:
    key: str
    name: str
    facility: str
    course_id: str
    # Holes belong to the course: the API's r28 reads "9" on the nine-hole
    # course but "1,15,18" on the eighteen, so it can't be trusted alone.
    holes: int | None = None
    provider: str = "ezlinks"

    @property
    def booking_url(self) -> str:
        return f"{EZLINKS_BASE_URL}/"


AnyCourse = ForeUpCourse | CpsCourse | EzLinksCourse

ESSEX = "Essex County"
BERGEN = "Bergen County"
UNION = "Union County"

# How long to leave between requests to a provider, beyond the normal poll
# interval. Union is behind Cloudflare, and polling it as hard as the others
# is the quickest way to get the instance's IP blocked.
def _seconds(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        return default


PROVIDER_MIN_INTERVAL_SECONDS: dict[str, int] = {
    "ezlinks": _seconds("EZLINKS_MIN_INTERVAL_SECONDS", 120),
}

COURSES: dict[str, AnyCourse] = {
    # -- Essex County, on ForeUp ------------------------------------------
    "hendricks": ForeUpCourse(
        key="hendricks",
        name="Hendricks Field",
        facility=ESSEX,
        course_id="22526",
        schedule_id="11075",
        booking_class="49493",
    ),
    "weequahic": ForeUpCourse(
        key="weequahic",
        name="Weequahic",
        facility=ESSEX,
        course_id="22527",
        schedule_id="11077",
        booking_class="49424",
    ),
    "byrne": ForeUpCourse(
        key="byrne",
        name="Francis A. Byrne",
        facility=ESSEX,
        course_id="22528",
        schedule_id="11078",
        booking_class="49771",
    ),
    # -- Bergen County, on CPS Golf ----------------------------------------
    # Every id here was read off a real API response. Bergen's own booking
    # page requests several more ids that returned no slots in that capture,
    # so they are deliberately not guessed at -- run
    # scripts/discover_cps_courses.py to see the full id/name mapping and add
    # any that are missing.
    "soldier_hill": CpsCourse(
        key="soldier_hill", name="Soldier Hill 18", facility=BERGEN, course_id="2"
    ),
    "darlington": CpsCourse(
        key="darlington", name="Darlington 18", facility=BERGEN, course_id="4"
    ),
    "orchard_hills": CpsCourse(
        key="orchard_hills", name="Orchard Hills", facility=BERGEN, course_id="9"
    ),
    "rockleigh_rw": CpsCourse(
        key="rockleigh_rw", name="Rockleigh R/W 18", facility=BERGEN, course_id="10"
    ),
    "rockleigh_blue": CpsCourse(
        key="rockleigh_blue", name="Rockleigh Blue 9", facility=BERGEN, course_id="12"
    ),
    "valley_brook": CpsCourse(
        key="valley_brook", name="Valley Brook 18", facility=BERGEN, course_id="13"
    ),
    # -- Union County, on EZLinks -------------------------------------------
    # Ids 4545 and 4551 returned slots in a real capture. 4549 is also in the
    # site's own search but returned nothing that day, so it is left out
    # rather than named on a guess -- scripts/verify_ezlinks.py will show it.
    "ash_brook": EzLinksCourse(
        key="ash_brook", name="Ash Brook GC", facility=UNION,
        course_id="4545", holes=18,
    ),
    "galloping_hill_9": EzLinksCourse(
        key="galloping_hill_9", name="Galloping Hill (Learning Center 9)",
        facility=UNION, course_id="4551", holes=9,
    ),
}

COURSE_KEYS = list(COURSES)


def courses_by_facility() -> dict[str, list[AnyCourse]]:
    """Courses grouped for the GUI, so the list stays readable as it grows."""
    grouped: dict[str, list[AnyCourse]] = {}
    for course in COURSES.values():
        grouped.setdefault(course.facility, []).append(course)
    return grouped


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


GUI_PASSWORD = os.getenv("GUI_PASSWORD", "")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-insecure-secret")
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
POLL_INTERVAL_SECONDS = _int_env("POLL_INTERVAL_SECONDS", 30)

# Pause between individual requests inside one cycle, so a watch covering
# several courses and dates trickles rather than bursts.
REQUEST_DELAY_SECONDS = _float_env("REQUEST_DELAY_SECONDS", 0.5)

RENOTIFY_AFTER_MINUTES = _int_env("RENOTIFY_AFTER_MINUTES", 10)
DB_PATH = os.getenv("DB_PATH", "teetimes.db")

# Slot history older than this is dropped once a day. Measured from when a
# slot was last seen, so anything still open is never purged.
RETENTION_DAYS = _int_env("RETENTION_DAYS", 5)

# Set true when the GUI is served over HTTPS (behind Caddy), so the session
# cookie is never sent over a plain connection.
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "").lower() in ("1", "true", "yes")

SESSION_COOKIE = "teetimes_session"
