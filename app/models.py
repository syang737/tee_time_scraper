"""Core data types: what we watch for, and what the API gives back."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

# config imports nothing from here, so this stays acyclic.
from . import config

# Monday=0 .. Sunday=6, matching datetime.weekday().
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

DAY_NAMES = {
    "mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu",
    "fri": "Fri", "sat": "Sat", "sun": "Sun",
}


def format_12h(hour: int, minute: int, meridiem: bool = True) -> str:
    """12-hour clock without the platform-specific strftime directives."""
    value = f"{hour % 12 or 12}:{minute:02d}"
    if not meridiem:
        return value
    return f"{value} {'AM' if hour < 12 else 'PM'}"


def _parse_hhmm(value: str) -> tuple[int, int]:
    hour, minute = value.split(":")
    return int(hour), int(minute)


@dataclass(frozen=True)
class Criterion:
    """One facet of a watch, for rendering as its own chip in the GUI."""

    kind: str  # course | when | time | players | holes
    text: str


@dataclass
class TeeTimeSlot:
    """One bookable tee time, normalized out of the ForeUp API response."""

    course_key: str
    course_name: str
    start: dt.datetime
    available_spots: int
    holes: int | None = None
    green_fee: float | None = None
    cart_fee: float | None = None
    teetime_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def date_str(self) -> str:
        return self.start.strftime("%Y-%m-%d")

    @property
    def time_str(self) -> str:
        return self.start.strftime("%H:%M")

    @property
    def display_time(self) -> str:
        return format_12h(self.start.hour, self.start.minute)

    @property
    def display_date(self) -> str:
        # %a and %b are portable; the bare day number avoids %-d.
        return f"{self.start.strftime('%a %b')} {self.start.day}"

    @property
    def slot_key(self) -> str:
        """Stable identity for dedup.

        Deliberately built from course + start + holes rather than the API's
        own id: a slot that is released and re-released is the same tee time
        to us, and teetime_id is not guaranteed to be present.
        """
        return f"{self.course_key}|{self.start.isoformat()}|{self.holes}"


@dataclass
class Watch:
    """A saved search. Its three criteria decide what earns a notification."""

    id: int | None = None
    label: str = ""
    courses: list[str] = field(default_factory=list)
    days: list[str] = field(default_factory=lambda: ["sat", "sun"])
    horizon_days: int = 14
    specific_date: str | None = None  # "YYYY-MM-DD"; overrides days/horizon
    time_start: str | None = None  # "HH:MM", inclusive; None = any
    time_end: str | None = None  # "HH:MM", inclusive; None = any
    min_players: int = 0  # slot must have >= this many open spots; 0 = any
    holes: str = "any"  # "9" | "18" | "any"
    active: bool = True
    # Where this watch's alerts go. None falls back to the server-wide topic,
    # so each person can point their own watches at their own phone without
    # seeing anyone else's alerts.
    ntfy_topic: str | None = None
    created_at: str | None = None

    def candidate_dates(self, today: dt.date) -> list[dt.date]:
        """Concrete dates this watch cares about, soonest first."""
        if self.specific_date:
            target = dt.date.fromisoformat(self.specific_date)
            return [target] if target >= today else []

        wanted = {d.lower() for d in self.days}
        return [
            day
            for day in (today + dt.timedelta(days=n) for n in range(self.horizon_days + 1))
            if WEEKDAYS[day.weekday()] in wanted
        ]

    def is_expired(self, today: dt.date) -> bool:
        """True once the watch can never match again (its date has passed)."""
        return bool(self.specific_date) and not self.candidate_dates(today)

    def matches(self, slot: TeeTimeSlot) -> bool:
        """Does this slot satisfy every criterion? Only then do we notify."""
        if slot.course_key not in self.courses:
            return False

        if self.min_players and slot.available_spots < self.min_players:
            return False
        if slot.available_spots < 1:
            return False

        if self.holes != "any":
            # Treat an unknown hole count as non-matching when the watch is
            # specific, rather than notifying about the wrong round length.
            if slot.holes is None or str(slot.holes) != self.holes:
                return False

        if self.time_start and slot.time_str < self.time_start:
            return False
        if self.time_end and slot.time_str > self.time_end:
            return False

        return True

    def when_text(self) -> str:
        if self.specific_date:
            date = dt.date.fromisoformat(self.specific_date)
            return f"{DAY_NAMES[WEEKDAYS[date.weekday()]]} {date.strftime('%b')} {date.day}"
        if not self.days:
            return "any day"
        ordered = [DAY_NAMES[d] for d in WEEKDAYS if d in self.days]
        if len(ordered) == 7:
            return "any day"
        return " & ".join(ordered) if len(ordered) <= 2 else ", ".join(ordered)

    def time_text(self) -> str:
        """The window as a reader would say it, e.g. '7:00 - 10:30 AM'."""
        if not self.time_start and not self.time_end:
            return "any time"
        if self.time_start and not self.time_end:
            return f"from {format_12h(*_parse_hhmm(self.time_start))}"
        if self.time_end and not self.time_start:
            return f"until {format_12h(*_parse_hhmm(self.time_end))}"

        start_h, start_m = _parse_hhmm(self.time_start)
        end_h, end_m = _parse_hhmm(self.time_end)
        # Drop the redundant first meridiem when both ends share one.
        same_half = (start_h < 12) == (end_h < 12)
        start = format_12h(start_h, start_m, meridiem=not same_half)
        return f"{start} - {format_12h(end_h, end_m)}"

    def criteria(self) -> list[Criterion]:
        """The watch broken into facets, so the GUI can render real chips.

        describe() flattens all of this into one pipe-separated string, which
        wraps into an unreadable stack on a phone. Keep that for notification
        text, where a single line is what's wanted, and use this for markup.
        """
        facets = [
            Criterion(
                "course",
                config.COURSES[key].name if key in config.COURSES else key.title(),
            )
            for key in self.courses
        ]
        facets.append(Criterion("when", self.when_text()))
        if self.horizon_days and not self.specific_date:
            facets.append(Criterion("when", f"next {self.horizon_days} days"))
        facets.append(Criterion("time", self.time_text()))
        facets.append(
            Criterion(
                "players",
                f"{self.min_players}+ players" if self.min_players else "any players",
            )
        )
        facets.append(
            Criterion(
                "holes", "any holes" if self.holes == "any" else f"{self.holes} holes"
            )
        )
        return facets

    def describe(self) -> str:
        """One-line summary of the criteria, for the GUI and notifications."""
        courses = ", ".join(c.title() for c in self.courses) or "no courses"
        if self.specific_date:
            when = self.specific_date
        else:
            when = "/".join(d.title() for d in self.days) or "any day"
            when += f" (next {self.horizon_days}d)"
        if self.time_start or self.time_end:
            window = f"{self.time_start or 'open'}-{self.time_end or 'close'}"
        else:
            window = "any time"
        players = f"{self.min_players}+ players" if self.min_players else "any players"
        holes = "any holes" if self.holes == "any" else f"{self.holes} holes"
        return f"{courses} | {when} | {window} | {players} | {holes}"
