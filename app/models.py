"""Core data types: what we watch for, and what the API gives back."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

# Monday=0 .. Sunday=6, matching datetime.weekday().
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


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
        # Built by hand rather than with %-I/%p: the zero-stripping strftime
        # directives are glibc-only and raise ValueError on Windows, which
        # matters because this runs on Windows in dev and Linux deployed.
        hour = self.start.hour % 12 or 12
        meridiem = "AM" if self.start.hour < 12 else "PM"
        return f"{hour}:{self.start.minute:02d} {meridiem}"

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
