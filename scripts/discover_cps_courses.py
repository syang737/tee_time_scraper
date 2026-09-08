#!/usr/bin/env python3
"""List the courseId -> courseName mapping Bergen's API actually returns.

app/config.py only carries the Bergen courses that appeared in a real
response. Their booking page requests several more ids that returned nothing
that day; rather than guess at those, run this from a machine with internet
access on a date with wide availability and add whatever it turns up:

    python3 scripts/discover_cps_courses.py 2026-09-12

Pass --all to sweep the full id list their site uses, rather than only the
ids already configured.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, cps_client  # noqa: E402

# Every id bergencountygolf.cps.golf asks for in its own search request.
ALL_SITE_COURSE_IDS = [
    "2", "3", "4", "5", "7", "8", "9", "10", "11", "12", "13", "14", "15", "16",
]


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    sweep_all = "--all" in sys.argv

    if len(args) != 1:
        print(f"usage: {sys.argv[0]} <YYYY-MM-DD> [--all]")
        return 2
    date = dt.date.fromisoformat(args[0])

    if sweep_all:
        course_ids = ALL_SITE_COURSE_IDS
    else:
        course_ids = [
            c.course_id for c in config.COURSES.values() if c.provider == "cps"
        ]

    params = {
        "searchDate": cps_client.format_search_date(date),
        "holes": "0",
        "numberOfPlayer": config.CPS_SEARCH_PLAYERS,
        "courseIds": ",".join(course_ids),
        "searchTimeType": "0",
        "transactionId": str(uuid.uuid4()),
        "teeOffTimeMin": "0",
        "teeOffTimeMax": "23",
        "isChangeTeeOffTime": "true",
        "teeSheetSearchView": "5",
        "classCode": config.CPS_CLASS_CODE,
        "defaultOnlineRate": "N",
        "isUseCapacityPricing": "false",
        "memberStoreId": config.CPS_MEMBER_STORE_ID,
        "searchType": "1",
    }
    url = f"{config.CPS_API_URL}?{urllib.parse.urlencode(params)}"
    print(f"GET {url}\n")

    request = urllib.request.Request(url, headers=cps_client.DEFAULT_HEADERS)
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))

    entries = payload.get("content") or []
    print(f"{len(entries)} slot(s) returned\n")

    seen: dict[str, dict[str, object]] = {}
    for entry in entries:
        cid = str(entry.get("courseId"))
        row = seen.setdefault(
            cid, {"name": entry.get("courseName"), "slots": 0, "holes": set()}
        )
        row["slots"] = int(row["slots"]) + 1
        if entry.get("holes") is not None:
            row["holes"].add(entry["holes"])  # type: ignore[union-attr]

    configured = {c.course_id: c.key for c in config.COURSES.values()
                  if c.provider == "cps"}

    print(f"{'id':>4}  {'slots':>5}  {'holes':<8} {'name':<26} configured as")
    for cid in sorted(seen, key=lambda c: int(c)):
        row = seen[cid]
        holes = ",".join(str(h) for h in sorted(row["holes"]))  # type: ignore[arg-type]
        known = configured.get(cid, "-- MISSING from app/config.py --")
        print(f"{cid:>4}  {row['slots']:>5}  {holes:<8} {str(row['name']):<26} {known}")

    silent = [c for c in course_ids if c not in seen]
    if silent:
        print(f"\nno slots for id(s): {', '.join(silent)}")
        print("(could be closed that day, or not a real course id)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
