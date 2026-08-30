#!/usr/bin/env python3
"""Save a real API response to tests/fixtures/, for eyeballing or as a fixture.

Run this from a machine with normal internet access (your laptop, or the
Lightsail box) to confirm the live response shape:

    python3 scripts/capture_sample.py weequahic 2026-09-05
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, foreup_client  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def main() -> int:
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <{'|'.join(config.COURSE_KEYS)}> <YYYY-MM-DD>")
        return 2

    course_key, date_str = sys.argv[1], sys.argv[2]
    course = config.COURSES.get(course_key)
    if course is None:
        print(f"unknown course {course_key!r}; pick one of {config.COURSE_KEYS}")
        return 2
    date = dt.date.fromisoformat(date_str)

    params = foreup_client.build_params(course, date)
    url = f"{config.BASE_API_URL}?{urllib.parse.urlencode(params, doseq=True)}"
    print(f"GET {url}\n")

    request = urllib.request.Request(url, headers=foreup_client.DEFAULT_HEADERS)
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read().decode("utf-8")

    payload = json.loads(body)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"live_{course_key}_{date_str}.json"
    out.write_text(json.dumps(payload, indent=2))
    print(f"Wrote {out}")

    slots = foreup_client.parse_response(payload, course)
    print(f"Parsed {len(slots)} slot(s) for {course.name}:\n")
    for slot in slots[:15]:
        print(
            f"  {slot.date_str} {slot.display_time:>9}  "
            f"{slot.available_spots} spots  {slot.holes} holes  "
            f"green ${slot.green_fee}"
        )
    if len(slots) > 15:
        print(f"  ... and {len(slots) - 15} more")

    if payload and isinstance(payload, list) and isinstance(payload[0], dict):
        print("\nFields present on the first entry:")
        print("  " + ", ".join(sorted(payload[0])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
