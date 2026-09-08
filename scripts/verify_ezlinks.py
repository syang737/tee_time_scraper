#!/usr/bin/env python3
"""Check Union County's API from a machine that can actually reach it.

Answers the two things the original capture could not:

1. **Does Cloudflare let us through?** Reports exactly what came back, and
   whether curl_cffi's browser TLS fingerprint made the difference.
2. **Which field is the number of open spots?** r11 and r14 were both 4 in
   every row of that capture, so they were indistinguishable. Run this
   against a busy date; if the two ever differ, the smaller is what remains
   and app/ezlinks_client.available_spots is reading the right one.

    python3 scripts/verify_ezlinks.py 2026-09-12
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app import config, ezlinks_client  # noqa: E402

# Every id the site's own search posts, including one we have not named.
ALL_SITE_COURSE_IDS = [4549, 4551, 4545]


async def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if len(args) != 1:
        print(f"usage: {sys.argv[0]} <YYYY-MM-DD>")
        return 2
    date = dt.date.fromisoformat(args[0])

    union = [c for c in config.COURSES.values() if c.provider == "ezlinks"]
    payload = ezlinks_client.build_payload(union, date)
    payload["p01"] = ALL_SITE_COURSE_IDS  # sweep, including unnamed ids

    print(f"POST {config.EZLINKS_API_URL}")
    print(f"  {json.dumps(payload)}")
    print(f"  impersonate: {config.EZLINKS_IMPERSONATE or '(off -- plain httpx)'}\n")

    try:
        import curl_cffi  # noqa: F401
        print("curl_cffi is installed\n")
    except ImportError:
        print("curl_cffi is NOT installed -- plain httpx will be used, which "
              "Cloudflare often challenges.\n  pip install curl_cffi\n")

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            body = await ezlinks_client._post(client, payload)
        except ezlinks_client.EzLinksBlocked as exc:
            print(f"BLOCKED: {exc}")
            print("\nTry, in order:")
            print("  1. pip install curl_cffi   (matches a real browser's TLS)")
            print("  2. EZLINKS_IMPERSONATE=chrome131 (or another build)")
            print("  3. the Playwright fallback described in README.md")
            return 1
        except ezlinks_client.EzLinksError as exc:
            print(f"FAILED: {exc}")
            return 1

    rows = body.get("r06") or []
    print(f"OK -- {len(rows)} row(s) returned\n")

    plans = ezlinks_client.rate_plan_names(body)
    print("rate plans:", ", ".join(f"{k}={v}" for k, v in plans.items()) or "(none)")

    configured = {int(c.course_id): c.key for c in union}
    by_course: Counter[tuple[int, str]] = Counter()
    for row in rows:
        by_course[(row.get("r07"), row.get("r16"))] += 1

    print(f"\n{'id':>6}  {'rows':>5}  {'name':<40} configured as")
    for (cid, name), count in sorted(by_course.items(), key=lambda kv: str(kv[0][0])):
        known = configured.get(cid, "-- MISSING from app/config.py --")
        print(f"{cid:>6}  {count:>5}  {str(name):<40} {known}")

    # The question the capture could not settle.
    pairs = {(r.get("r11"), r.get("r14")) for r in rows}
    print(f"\n(r11, r14) pairs seen: {sorted(pairs, key=str)}")
    if len(pairs) == 1 and len(next(iter(pairs))) == 2 and len(set(next(iter(pairs)))) == 1:
        print("  still identical -- inconclusive; try a busier date")
    elif any(a != b for a, b in pairs if a is not None and b is not None):
        print("  they DIFFER -- the smaller is what remains, which is r14 if")
        print("  r14 <= r11 throughout. Confirm available_spots reads that one.")

    # And what our own parser makes of it.
    parsed = ezlinks_client.parse_response(body, union)
    print(f"\nparsed into {sum(len(v) for v in parsed.values())} distinct tee time(s) "
          f"after collapsing rate plans:")
    for key, slots in parsed.items():
        if slots:
            first = slots[0]
            print(f"  {key:<20} {len(slots):>3} slots, first {first.display_time} "
                  f"{first.holes}h ${first.green_fee} {first.available_spots} spots")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
