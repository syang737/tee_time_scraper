"""Settings and the fixed per-course ForeUp identifiers."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

# All three courses live on one ForeUp facility, so the API needs both the
# course-specific schedule_id/booking_class and the facility-wide list of
# schedule_ids. These values came from the courses' own booking pages.
FACILITY_SCHEDULE_IDS = ["11078", "11075", "11077"]

BASE_API_URL = "https://foreupsoftware.com/index.php/api/booking/times"
BOOKING_PAGE_URL = "https://foreupsoftware.com/index.php/booking/{course_id}/{schedule_id}#/teetimes"

# The facility's own timezone -- all dates/times from the API are local to it.
COURSE_TIMEZONE = "America/New_York"


@dataclass(frozen=True)
class Course:
    key: str
    name: str
    course_id: str
    schedule_id: str
    booking_class: str

    @property
    def booking_url(self) -> str:
        return BOOKING_PAGE_URL.format(
            course_id=self.course_id, schedule_id=self.schedule_id
        )


COURSES: dict[str, Course] = {
    "hendricks": Course(
        key="hendricks",
        name="Hendricks Field",
        course_id="22526",
        schedule_id="11075",
        booking_class="49493",
    ),
    "weequahic": Course(
        key="weequahic",
        name="Weequahic",
        course_id="22527",
        schedule_id="11077",
        booking_class="49424",
    ),
    "byrne": Course(
        key="byrne",
        name="Francis A. Byrne",
        course_id="22528",
        schedule_id="11078",
        booking_class="49771",
    ),
}

COURSE_KEYS = list(COURSES)


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


GUI_PASSWORD = os.getenv("GUI_PASSWORD", "")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-insecure-secret")
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
POLL_INTERVAL_SECONDS = _int_env("POLL_INTERVAL_SECONDS", 30)


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# Pause between individual requests inside one cycle, so a watch covering
# several courses and dates trickles rather than bursts.
REQUEST_DELAY_SECONDS = _float_env("REQUEST_DELAY_SECONDS", 0.5)
RENOTIFY_AFTER_MINUTES = _int_env("RENOTIFY_AFTER_MINUTES", 10)
DB_PATH = os.getenv("DB_PATH", "teetimes.db")

SESSION_COOKIE = "teetimes_session"
