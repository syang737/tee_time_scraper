#!/usr/bin/env python3
"""Does a real browser get through where an HTTP client cannot?

The HTTP ladder is exhausted: curl_cffi with a genuine Chrome TLS handshake
and warmed cookies is still challenged on both sites. The remaining question
is whether Chromium itself passes, and it splits per site:

* **Union** already serves its homepage to us (HTTP 200, Cloudflare cookies
  issued), and only the API path is challenged. So the promising move is not
  to replay the API call at all, but to let the real page make it -- a
  ``fetch()`` from inside the page inherits its cookies, headers and origin.

* **Bergen** refuses even the booking page, with no cookies issued. That is
  the signature of IP reputation rather than fingerprinting, and a browser
  only helps if it can solve the managed challenge it is served.

This answers both before any of it gets built.

    .venv/bin/pip install playwright && .venv/bin/playwright install chromium
    .venv/bin/python scripts/probe_playwright.py
"""

from __future__ import annotations

import datetime as dt
import sys
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("Playwright is not installed. This probe needs it:\n")
    print(f"    {REPO}/.venv/bin/pip install playwright")
    print(f"    {REPO}/.venv/bin/playwright install chromium\n")
    print("Chromium is ~300 MB and needs ~300 MB RAM to run. On a 1 GB")
    print("instance add swap first:")
    print("    sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile")
    print("    sudo mkswap /swapfile && sudo swapon /swapfile")
    raise SystemExit(2)

from app import config, cps_client, ezlinks_client  # noqa: E402

CHALLENGE_HINTS = ("just a moment", "__cf_chl", "challenge-platform",
                   "attention required", "enable javascript")


def challenged(html: str) -> bool:
    low = html[:4000].lower()
    return any(h in low for h in CHALLENGE_HINTS)


def probe_bergen(page, date: dt.date) -> bool:
    """Can Chromium get past the challenge on the booking page itself?"""
    print(f"\n{'=' * 70}\nBergen -- can a browser load the page at all?\n{'=' * 70}")
    url = f"{config.CPS_BASE_URL}/onlineres/"
    response = page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    print(f"  initial: HTTP {response.status if response else '?'}")

    # A non-interactive managed challenge resolves itself in a few seconds.
    page.wait_for_timeout(12_000)
    html = page.content()
    if challenged(html):
        print("  still on the challenge page after 12s")
        print("  => Chromium did NOT get through (interactive or IP-scored)")
        return False
    print(f"  title: {page.title()!r}")

    courses = [c for c in config.COURSES.values() if c.provider == "cps"]
    params = cps_client.build_params(courses, date)
    api = config.CPS_API_URL + "?" + urllib.parse.urlencode(params, doseq=True)
    result = page.evaluate(
        """async (url) => {
            const r = await fetch(url, {credentials: 'include'});
            const t = await r.text();
            return {status: r.status, head: t.slice(0, 300), len: t.length};
        }""",
        api,
    )
    print(f"  in-page API fetch: HTTP {result['status']}, {result['len']} bytes")
    ok = result["status"] == 200 and result["head"].lstrip().startswith(("{", "["))
    print(f"  => {'WORKS from inside the browser' if ok else 'still refused'}")
    return ok


def probe_union(page, date: dt.date) -> bool:
    """Union serves its page; can the page's own fetch reach the API?"""
    print(f"\n{'=' * 70}\nUnion -- let the real page make the request\n{'=' * 70}")
    response = page.goto(f"{config.EZLINKS_BASE_URL}/",
                         wait_until="domcontentloaded", timeout=60_000)
    print(f"  homepage: HTTP {response.status if response else '?'}")
    page.wait_for_timeout(8_000)
    if challenged(page.content()):
        print("  homepage is a challenge page")
        return False
    print(f"  title: {page.title()!r}")

    courses = [c for c in config.COURSES.values() if c.provider == "ezlinks"]
    payload = ezlinks_client.build_payload(courses, date)
    result = page.evaluate(
        """async ({url, body}) => {
            const r = await fetch(url, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(body),
                credentials: 'include',
            });
            const t = await r.text();
            return {status: r.status, head: t.slice(0, 300), len: t.length};
        }""",
        {"url": config.EZLINKS_API_URL, "body": payload},
    )
    print(f"  in-page API fetch: HTTP {result['status']}, {result['len']} bytes")
    ok = result["status"] == 200 and result["head"].lstrip().startswith("{")
    print(f"  => {'WORKS from inside the browser' if ok else 'still refused'}")
    return ok


def main() -> int:
    date = dt.date.today() + dt.timedelta(days=4)
    print(f"Probing with date {date}")

    results = {}
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
        except Exception as exc:
            # pip installs the library; the browser binary is a separate step.
            if "Executable doesn" in str(exc) or "playwright install" in str(exc):
                print("\nChromium itself is not downloaded yet. Installing the")
                print("Python package does not fetch the browser:\n")
                print(f"    {REPO}/.venv/bin/playwright install chromium\n")
                print("On Ubuntu it may also want system libraries:")
                print(f"    sudo {REPO}/.venv/bin/playwright install-deps chromium")
                return 2
            raise
        context = browser.new_context(
            locale="en-US",
            timezone_id="America/New_York",
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()
        for name, fn in (("Bergen", probe_bergen), ("Union", probe_union)):
            try:
                results[name] = fn(page, date)
            except Exception as exc:
                print(f"  ERROR: {type(exc).__name__}: {str(exc)[:200]}")
                results[name] = False
        browser.close()

    print(f"\n{'=' * 70}\nVERDICT\n{'=' * 70}")
    for name, ok in results.items():
        print(f"  {'PASS' if ok else 'fail'}  {name}")
    print()
    if all(results.values()):
        print("Both work in a browser -> build the Playwright fetch path.")
    elif any(results.values()):
        passed = [n for n, ok in results.items() if ok]
        print(f"Only {', '.join(passed)} works in a browser.")
        print("Worth building the browser path for that one; the other needs")
        print("the request to come from a different IP (a relay at home).")
    else:
        print("Neither works even in a real browser on this box.")
        print("That points at the IP rather than the client: this is a")
        print("datacenter address and Cloudflare scores it before it looks at")
        print("anything else. Running the fetch from a residential connection")
        print("is the remaining option.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
