"""Ways to fetch a target: plain HTTP, a shared Playwright Chromium, and Walmart.io request signing."""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import Iterable

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from .config import Options, Target
from .models import FetchResult

_CACHE_HEADERS = {
    "age", "cache-control", "cf-cache-status", "retry-after", "server", "last-modified",
    "date", "expires", "etag", "via", "x-served-by",
}
_CACHE_PREFIXES = ("x-cache", "x-check-cacheable", "x-akamai", "akamai", "x-ratelimit", "ratelimit")


def pick_headers(headers: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Headers that say something about caching, freshness or rate limiting."""
    picked: dict[str, str] = {}
    for k, v in headers:
        key = k.lower()
        if key in _CACHE_HEADERS or key.startswith(_CACHE_PREFIXES):
            picked[key] = f"{picked[key]},{v}" if key in picked else v
    return picked


def _ms_since(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


class HttpFetcher:
    """Plain HTTP GET with its own cookie jar, so each target looks like one steady visitor."""

    def __init__(self, options: Options):
        self._timeout = options.request_timeout_seconds
        self._client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=None,  # the whole request is bounded below instead
            headers={
                "User-Agent": options.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

    async def fetch(self, url: str, extra_headers: dict[str, str] | None = None) -> FetchResult:
        start = time.perf_counter()
        try:
            async with asyncio.timeout(self._timeout):
                resp = await self._client.get(url, headers=extra_headers)
        except TimeoutError:
            return FetchResult(None, _ms_since(start), "", url, timed_out=True, error="timeout")
        except httpx.HTTPError as ex:
            return FetchResult(None, _ms_since(start), "", url, error=str(ex) or type(ex).__name__)
        return FetchResult(
            resp.status_code, _ms_since(start), resp.text, str(resp.url),
            headers=pick_headers(resp.headers.multi_items()),
        )

    async def close(self) -> None:
        await self._client.aclose()


class BrowserHost:
    """One Chromium process shared by every browser target."""

    def __init__(self, options: Options):
        self._headless = options.headless
        self._lock = asyncio.Lock()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    async def new_context(self) -> BrowserContext:
        async with self._lock:
            if self._browser is None:
                if self._playwright is None:
                    self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(headless=self._headless)

        # A clean, throwaway profile: never the user's signed-in retailer sessions.
        return await self._browser.new_context(
            locale="en-US",
            timezone_id="America/New_York",
            viewport={"width": 1366, "height": 900},
        )

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()


class BrowserFetcher:
    """Loads a page in a per-target browser context and returns the rendered HTML."""

    def __init__(self, host: BrowserHost, options: Options):
        self._host = host
        self._timeout_ms = options.request_timeout_seconds * 1000
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    async def fetch(self, target: Target) -> FetchResult:
        start = time.perf_counter()
        try:
            if self._page is None:
                self._context = await self._host.new_context()
                self._page = await self._context.new_page()
                start = time.perf_counter()
            page = self._page

            resp = await page.goto(target.url, wait_until="domcontentloaded", timeout=self._timeout_ms)
            nav_ms = _ms_since(start)

            if target.wait_for_selector:
                try:
                    await page.wait_for_selector(target.wait_for_selector, state="attached", timeout=target.settle_ms)
                except PlaywrightTimeout:
                    pass  # Expected when the item is out of stock and the element never renders.
            else:
                await page.wait_for_timeout(target.settle_ms)

            html = await page.content()
            headers = pick_headers(resp.headers.items()) if resp is not None else {}
            return FetchResult(resp.status if resp is not None else None, nav_ms, html, page.url, headers=headers)
        except PlaywrightTimeout:
            return FetchResult(None, _ms_since(start), "", target.url, timed_out=True, error="navigation timeout")
        except PlaywrightError as ex:
            return FetchResult(None, _ms_since(start), "", target.url, error=str(ex).split("\n")[0])

    async def close(self) -> None:
        if self._context is not None:
            await self._context.close()


class WalmartSigner:
    """Walmart.io affiliate auth headers: an RSA-SHA256 signature over "consumerId\\ntimestampMs\\nkeyVersion\\n"."""

    def __init__(self, consumer_id: str, key_version: str, private_key_pem: bytes):
        self._consumer_id = consumer_id
        self._key_version = key_version
        self._key = serialization.load_pem_private_key(private_key_pem, password=None)

    def headers(self, now_ms: int | None = None) -> dict[str, str]:
        ts = str(now_ms if now_ms is not None else int(time.time() * 1000))
        payload = f"{self._consumer_id}\n{ts}\n{self._key_version}\n".encode()
        signature = self._key.sign(payload, padding.PKCS1v15(), hashes.SHA256())
        return {
            "WM_CONSUMER.ID": self._consumer_id,
            "WM_CONSUMER.INTIMESTAMP": ts,
            "WM_SEC.KEY_VERSION": self._key_version,
            "WM_SEC.AUTH_SIGNATURE": base64.b64encode(signature).decode(),
            "Accept": "application/json",
        }
