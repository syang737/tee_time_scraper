#!/usr/bin/env python3
"""Work out *which* Cloudflare behaviour is blocking us, and what fixes it.

"403" covers several different things, and they need different answers:

* **TLS fingerprinting** -- Python's handshake is not Chrome's. curl_cffi
  fixes this, and the ladder below will show plain httpx failing where
  impersonation succeeds.
* **Missing bot cookies** -- the API is called without ever loading the site,
  so there is no ``__cf_bm``. Warming the session fixes it.
* **A JS managed challenge** -- no HTTP client can pass this; it needs a real
  browser. Look for "Just a moment" / ``__cf_chl`` in the body.
* **IP reputation** -- datacenter ranges (which Lightsail is) score badly.
  Nothing client-side fixes this; the request has to come from elsewhere.
  The tell is that every strategy fails here but the same code works from
  your laptop.

    python3 scripts/diagnose_cloudflare.py

Run it on the box that is failing, and again on a machine that works.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, cps_client, ezlinks_client  # noqa: E402

CHALLENGE_HINTS = (
    "just a moment", "__cf_chl", "challenge-platform",
    "cf-browser-verification", "attention required", "enable javascript",
)


def classify(status: int, body: str, headers: dict) -> str:
    low = body[:3000].lower()
    if any(h in low for h in CHALLENGE_HINTS):
        return "JS MANAGED CHALLENGE (needs a real browser)"
    if headers.get("cf-mitigated"):
        return f"BLOCKED by Cloudflare (cf-mitigated: {headers['cf-mitigated']})"
    if status in (403, 429, 503):
        return f"REFUSED (HTTP {status}) -- fingerprint, cookies, or IP reputation"
    if status == 200:
        return "OK"
    return f"HTTP {status}"


def show_headers(headers: dict) -> str:
    keep = ("server", "cf-ray", "cf-mitigated", "cf-cache-status", "content-type")
    got = {k: headers[k] for k in keep if k in headers}
    return "  " + (json.dumps(got) if got else "(no Cloudflare headers)")


async def attempt(label: str, fn) -> bool:
    print(f"\n-- {label}")
    try:
        status, body, headers = await fn()
    except Exception as exc:
        print(f"  ERROR: {type(exc).__name__}: {str(exc)[:200]}")
        return False
    print(f"  HTTP {status}  {len(body)} bytes")
    print(show_headers(headers))
    verdict = classify(status, body, headers)
    print(f"  => {verdict}")
    return verdict == "OK"


def _lower(h) -> dict:
    return {str(k).lower(): str(v) for k, v in dict(h).items()}


async def probe(name: str, url: str, method: str, warm_url: str, **kwargs):
    print(f"\n{'=' * 70}\n{name}\n{'=' * 70}")
    results = {}

    # 1. plain httpx, honest about being a script
    import httpx

    async def plain():
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
            r = await c.request(method, url, **kwargs)
            return r.status_code, r.text, _lower(r.headers)

    results["plain httpx"] = await attempt("plain httpx (no disguise)", plain)

    # 2. httpx with browser headers -- isolates headers from fingerprint
    from app.browser_http import BROWSER_HEADERS

    async def dressed():
        headers = dict(BROWSER_HEADERS)
        headers.update(kwargs.get("headers") or {})
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
            r = await c.request(
                method, url, **{**kwargs, "headers": headers}
            )
            return r.status_code, r.text, _lower(r.headers)

    results["httpx + browser headers"] = await attempt(
        "httpx with browser headers (same TLS)", dressed
    )

    # 3. curl_cffi: a real Chrome TLS handshake
    try:
        from curl_cffi import requests as curl_requests
    except ImportError:
        print("\n-- curl_cffi impersonation\n  NOT INSTALLED: pip install curl_cffi")
        results["curl_cffi"] = False
        results["curl_cffi + warm"] = False
    else:
        def imp():
            headers = dict(BROWSER_HEADERS)
            headers.update(kwargs.get("headers") or {})
            r = curl_requests.request(
                method, url, headers=headers,
                impersonate=config.IMPERSONATE or "chrome124", timeout=30,
                **{k: v for k, v in kwargs.items() if k != "headers"},
            )
            return r.status_code, r.text, _lower(r.headers)

        results["curl_cffi"] = await attempt(
            f"curl_cffi impersonating {config.IMPERSONATE}",
            lambda: asyncio.to_thread(imp),
        )

        # 4. ...plus loading the site first, for Cloudflare's own cookies
        def warmed():
            session = curl_requests.Session(
                impersonate=config.IMPERSONATE or "chrome124", timeout=30
            )
            page = session.get(warm_url, headers={
                **BROWSER_HEADERS,
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
            })
            print(f"  warm GET {warm_url} -> HTTP {page.status_code}, "
                  f"cookies: {','.join(sorted(session.cookies.keys())) or 'none'}")
            headers = dict(BROWSER_HEADERS)
            headers.update(kwargs.get("headers") or {})
            r = session.request(
                method, url, headers=headers,
                **{k: v for k, v in kwargs.items() if k != "headers"},
            )
            return r.status_code, r.text, _lower(r.headers)

        results["curl_cffi + warm"] = await attempt(
            "curl_cffi + warmed session cookies (what the app does)",
            lambda: asyncio.to_thread(warmed),
        )

    return results


async def main() -> int:
    date = dt.date.today() + dt.timedelta(days=4)
    print(f"Probing with date {date}; impersonate={config.IMPERSONATE or '(off)'}")

    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as c:
            ip = (await c.get("https://api.ipify.org")).text
        print(f"This machine's outbound IP: {ip}")
        print("  (if this is an AWS/datacenter range, Cloudflare scores it "
              "poorly no matter what the client does)")
    except Exception:
        pass

    bergen = [c for c in config.COURSES.values() if c.provider == "cps"]
    union = [c for c in config.COURSES.values() if c.provider == "ezlinks"]

    all_results = {}
    all_results["Bergen (CPS)"] = await probe(
        "Bergen County -- bergencountygolf.cps.golf",
        config.CPS_API_URL, "GET", f"{config.CPS_BASE_URL}/onlineres/",
        params=cps_client.build_params(bergen, date),
        headers=cps_client.DEFAULT_HEADERS,
    )
    all_results["Union (EZLinks)"] = await probe(
        "Union County -- unioncountygolf.ezlinksgolf.com",
        config.EZLINKS_API_URL, "POST", f"{config.EZLINKS_BASE_URL}/",
        json=ezlinks_client.build_payload(union, date),
        headers=ezlinks_client.DEFAULT_HEADERS,
    )

    print(f"\n{'=' * 70}\nSUMMARY\n{'=' * 70}")
    for site, results in all_results.items():
        print(f"\n{site}")
        for strategy, ok in results.items():
            print(f"  {'PASS' if ok else 'fail'}  {strategy}")

    print("\nReading it:")
    print("  only 'curl_cffi' rows pass      -> TLS fingerprinting; you are done")
    print("  only the '+ warm' row passes    -> it wanted Cloudflare's cookies")
    print("  everything fails, JS challenge  -> needs a real browser (Playwright)")
    print("  everything fails, no challenge  -> likely IP reputation; compare")
    print("                                     this run against your laptop")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
