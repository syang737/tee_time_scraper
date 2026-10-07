#!/usr/bin/env python3
"""Check the Somerset (TeeItUp) client against the live API.

The client was written from public captures of TeeItUp's API, not from a
response of Somerset's own -- the machine it was written on could not reach
teeitup.com. This confirms, from wherever you run it:

* the API answers anonymously (no login, no Cloudflare);
* the six facility ids and names match what the site itself lists;
* the response has the fields the parser reads, and parsing yields sane
  slots: local times in daylight hours, at most four open spots per slot.

    cd ~/tee_time_scraper
    .venv/bin/python scripts/verify_teeitup.py              # next Saturday
    .venv/bin/python scripts/verify_teeitup.py 2026-10-17
    .venv/bin/python scripts/verify_teeitup.py --save       # keep the response

``--save`` writes the raw response to tests/fixtures/, to replace the
synthetic fixture the tests currently use.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import httpx  # noqa: E402

from app import config, teeitup_client  # noqa: E402

COURSES = [c for c in config.COURSES.values() if c.provider == "teeitup"]


def next_saturday() -> dt.date:
    today = dt.date.today()
    return today + dt.timedelta(days=(5 - today.weekday()) % 7 or 7)


def keys_of(value) -> str:
    return ", ".join(sorted(value)) if isinstance(value, dict) else type(value).__name__


async def check_facilities(client: httpx.AsyncClient, alias: str) -> bool:
    print(f"\n-- Facilities on {alias}")
    response = await client.get(
        f"{config.TEEITUP_API_URL}/alias/{alias}/facilities",
        headers=teeitup_client.headers_for(alias),
    )
    print(f"   HTTP {response.status_code}")
    if response.status_code != 200:
        print(f"   {response.text[:300]}")
        return False
    facilities = response.json()
    if not isinstance(facilities, list):
        print(f"   unexpected shape: {keys_of(facilities)}")
        return False

    ours = {c.course_id: c for c in COURSES if c.alias == alias}
    seen = set()
    for f in facilities:
        if not isinstance(f, dict):
            continue
        fid = str(f.get("id"))
        seen.add(fid)
        mine = ours.get(fid)
        mark = f"configured as {mine.key!r}" if mine else "NOT CONFIGURED"
        print(f"   {fid:>6}  {str(f.get('name')):<40} tz={f.get('timeZone')}  {mark}")
    missing = set(ours) - seen
    for fid in sorted(missing):
        print(f"   {fid:>6}  configured as {ours[fid].key!r} but NOT on the site")
    return not missing


async def check_tee_times(client: httpx.AsyncClient, date: dt.date, save: bool) -> bool:
    print(f"\n-- Tee times for {date:%a %Y-%m-%d}, all {len(COURSES)} courses in one request")
    alias = COURSES[0].alias
    response = await client.get(
        f"{config.TEEITUP_API_URL}/v2/tee-times",
        params=teeitup_client.build_params(COURSES, date),
        headers=teeitup_client.headers_for(alias),
    )
    print(f"   HTTP {response.status_code}, {len(response.content)} bytes, "
          f"server={response.headers.get('server')!r}")
    if response.status_code != 200:
        print(f"   {response.text[:400]}")
        if response.status_code in (401, 403):
            print("   => refused. If this says 'Unauthorized' rather than looking like")
            print("      a Cloudflare page, the search may need a session after all.")
        return False

    payload = response.json()
    if save:
        out = REPO / "tests" / "fixtures" / f"teeitup_somerset_{date.isoformat()}.json"
        out.write_text(json.dumps(payload, indent=2))
        print(f"   saved raw response to {out.relative_to(REPO)}")

    if not isinstance(payload, list):
        print(f"   expected a list; got {keys_of(payload)}")
        return False
    print(f"   {len(payload)} course/day entries")

    # Field names, so a renamed field is obvious rather than silently empty.
    day = next((d for d in payload if isinstance(d, dict) and d.get("teetimes")), None)
    if day is None:
        print("   no entry has any tee times -- try a nearer date, or one")
        print("   inside the booking window")
        return True
    entry = day["teetimes"][0]
    rate = (entry.get("rates") or [{}])[0]
    print(f"   day fields:   {keys_of(day)}")
    print(f"   time fields:  {keys_of(entry)}")
    print(f"   rate fields:  {keys_of(rate)}")
    expected = {"teetime": entry, "maxPlayers": entry, "rates": entry,
                "holes": rate, "golfnow": rate}
    missing = [f for f, src in expected.items() if f not in src]
    if "bookedPlayers" not in entry:
        print("   note: no 'bookedPlayers' -- open spots fall back to the largest")
        print("         party size a rate allows")
    if missing:
        print(f"   !! fields the parser relies on are missing: {missing}")

    ok = not missing
    parsed = teeitup_client.parse_response(payload, COURSES)
    print("\n-- Parsed")
    for course in COURSES:
        slots = parsed.get(course.key, [])
        print(f"   {course.name:<30} {len(slots):>3} tee time(s)")
        for slot in slots[:3]:
            fee = f"${slot.green_fee:.0f}" if slot.green_fee else "?"
            print(f"       {slot.display_time:>8}  {slot.available_spots} open  "
                  f"{slot.holes_text or '?'} holes  {fee}")
        for slot in slots:
            if not 5 <= slot.start.hour <= 21:
                print(f"   !! {slot.display_time} is outside daylight -- time zone wrong?")
                ok = False
                break
            if slot.available_spots > 4:
                print(f"   !! {slot.available_spots} open spots -- spot math wrong?")
                ok = False
                break
    if not any(parsed.values()):
        print("   !! the API returned tee times but none parsed onto a course")
        ok = False
    return ok


async def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--save"]
    save = "--save" in sys.argv
    date = dt.date.fromisoformat(args[0]) if args else next_saturday()

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            facilities_ok = await check_facilities(client, COURSES[0].alias)
            times_ok = await check_tee_times(client, date, save)
        except httpx.HTTPError as exc:
            print(f"\nCould not reach TeeItUp: {exc}")
            return 1

    print("\n-- Verdict")
    print(f"   facilities: {'OK' if facilities_ok else 'CHECK ABOVE'}")
    print(f"   tee times:  {'OK' if times_ok else 'CHECK ABOVE'}")
    return 0 if facilities_ok and times_ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
