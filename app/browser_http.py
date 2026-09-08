"""A shared HTTP path that looks like a browser, for Cloudflare-fronted APIs.

Two of the three booking systems sit behind Cloudflare. What usually gets a
plain Python client turned away is not the headers but the **TLS
handshake**: httpx's ClientHello is recognisably not Chrome's, whatever the
User-Agent says. ``curl_cffi`` replays a real Chrome handshake, so it is
used whenever it is installed.

The second thing that matters is **arriving like a browser does**. A browser
loads the booking page first and picks up Cloudflare's ``__cf_bm`` cookie,
then calls the API with it. Jumping straight to the API with no cookies is
itself a signal, so a session warms itself once and reuses the jar.

Sessions are kept per host so the cookies persist across poll cycles rather
than being thrown away every 30 seconds.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from . import config

log = logging.getLogger(__name__)


class BlockedError(RuntimeError):
    """Cloudflare turned us away, as opposed to the API failing."""


class TransportError(RuntimeError):
    """The request could not be completed at all."""


BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

# Markers Cloudflare leaves on a challenge, so a block is reported as a block
# rather than as a confusing parse error.
_CHALLENGE_MARKERS = (
    "just a moment",
    "__cf_chl",
    "challenge-platform",
    "cf-browser-verification",
    "attention required",
)


def available() -> bool:
    """Whether browser TLS impersonation can be used at all."""
    if not config.IMPERSONATE:
        return False
    try:
        import curl_cffi  # noqa: F401
    except ImportError:
        return False
    return True


class BrowserSession:
    """One warmed, cookie-carrying session per host."""

    def __init__(self, base_url: str, warm_path: str = "/") -> None:
        self.base_url = base_url.rstrip("/")
        self.warm_path = warm_path
        self._session: Any = None
        self._warmed = False
        self._lock = asyncio.Lock()

    def _ensure_session(self) -> Any:
        if self._session is None:
            from curl_cffi import requests as curl_requests

            self._session = curl_requests.Session(
                impersonate=config.IMPERSONATE, timeout=30
            )
        return self._session

    def _warm_sync(self) -> None:
        """Load the site the way a browser would, to pick up its cookies."""
        session = self._ensure_session()
        headers = dict(BROWSER_HEADERS)
        headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Upgrade-Insecure-Requests": "1",
        })
        response = session.get(f"{self.base_url}{self.warm_path}", headers=headers)
        log.info(
            "Warmed %s: HTTP %s, cookies: %s",
            self.base_url,
            response.status_code,
            ",".join(sorted(session.cookies.keys())) or "none",
        )

    async def warm(self) -> None:
        if self._warmed or not available():
            return
        async with self._lock:
            if self._warmed:
                return
            try:
                await asyncio.to_thread(self._warm_sync)
            except Exception as exc:
                # Warming is best effort: the API call may still work.
                log.warning("Could not warm %s: %s", self.base_url, exc)
            self._warmed = True

    def reset(self) -> None:
        """Drop the session so the next call warms again from scratch."""
        self._session = None
        self._warmed = False

    # ----------------------------------------------------------------

    def _request_sync(
        self, method: str, url: str, headers: dict[str, str], **kwargs: Any
    ) -> tuple[int, str]:
        session = self._ensure_session()
        merged = dict(BROWSER_HEADERS)
        merged.update(headers)
        try:
            response = session.request(method, url, headers=merged, **kwargs)
        except Exception as exc:  # curl_cffi raises its own error types
            raise TransportError(str(exc)) from exc
        return response.status_code, response.text

    async def request_json(
        self, method: str, url: str, headers: dict[str, str], **kwargs: Any
    ) -> Any:
        """Make a request and decode JSON, naming a Cloudflare block as such."""
        await self.warm()
        if available():
            status, body = await asyncio.to_thread(
                self._request_sync, method, url, headers, **kwargs
            )
        else:
            status, body = await _plain_request(method, url, headers, **kwargs)
        return decode(status, body, self.base_url)


async def _plain_request(
    method: str, url: str, headers: dict[str, str], **kwargs: Any
) -> tuple[int, str]:
    """Fallback when curl_cffi is unavailable: httpx, which may be challenged."""
    import httpx

    merged = dict(BROWSER_HEADERS)
    merged.update(headers)
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.request(method, url, headers=merged, **kwargs)
    except httpx.HTTPError as exc:
        raise TransportError(str(exc)) from exc
    return response.status_code, response.text


def looks_like_a_challenge(status: int, body: str) -> bool:
    if status in (403, 429, 503):
        return True
    head = body[:2000].lower()
    return any(marker in head for marker in _CHALLENGE_MARKERS)


def decode(status: int, body: str, who: str) -> Any:
    """Raw response -> JSON, distinguishing a block from a real failure."""
    if looks_like_a_challenge(status, body):
        hint = "" if available() else " (curl_cffi is not installed)"
        raise BlockedError(
            f"{who} returned HTTP {status} and looks like a Cloudflare "
            f"challenge{hint}. Run scripts/diagnose_cloudflare.py."
        )
    if status != 200:
        raise TransportError(f"{who} returned HTTP {status}")
    try:
        return json.loads(body)
    except ValueError as exc:
        raise TransportError(f"{who} returned non-JSON: {exc}") from exc
