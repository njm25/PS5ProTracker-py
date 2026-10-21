"""Ways to fetch a target: plain HTTP and a shared Playwright Chromium."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable

import httpx
from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from .config import Options, Target
from .proxies import Proxy
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


_HEAVY = {"image", "media", "font"}


async def _skip_heavy(route) -> None:
    if route.request.resource_type in _HEAVY:
        await route.abort()
    else:
        await route.continue_()


class HttpFetcher:
    """Plain HTTP GET with its own cookie jar, so each target looks like one steady visitor."""

    def __init__(self, options: Options, proxy: Proxy | None = None, http2: bool = False):
        self._timeout = options.request_timeout_seconds
        self._proxy = proxy
        self._client = httpx.AsyncClient(
            proxy=proxy.url if proxy else None,
            http2=http2,
            follow_redirects=True,
            timeout=None,  # the whole request is bounded below instead
            headers={
                "User-Agent": options.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

    async def fetch(self, url: str, extra_headers: dict[str, str] | None = None,
                    post_body: str | None = None) -> FetchResult:
        start = time.perf_counter()
        try:
            async with asyncio.timeout(self._timeout):
                if post_body is None:
                    resp = await self._client.get(url, headers=extra_headers)
                else:
                    resp = await self._client.post(url, headers=extra_headers, content=post_body)
        except TimeoutError:
            return FetchResult(None, _ms_since(start), "", url, timed_out=True, error="timeout")
        except httpx.HTTPError as ex:
            error = str(ex) or type(ex).__name__
            return FetchResult(None, _ms_since(start), "", url, error=self._proxy.redact(error) if self._proxy else error)
        size = len(resp.content) + sum(len(k) + len(v) + 4 for k, v in resp.headers.multi_items())
        return FetchResult(
            resp.status_code, _ms_since(start), resp.text, str(resp.url),
            headers=pick_headers(resp.headers.multi_items()), bytes=size,
        )

    async def close(self) -> None:
        await self._client.aclose()


class BrowserHost:
    """One Chromium process shared by every browser target."""

    # Page loads at once. Each one spikes a renderer by a few hundred MB, and on a small VM several
    # together pushed Chromium into swap, where it froze (2026-10-20).
    LOADS_AT_ONCE = 2

    def __init__(self, options: Options):
        self._headless = options.headless
        self._lock = asyncio.Lock()
        self.slots = asyncio.Semaphore(self.LOADS_AT_ONCE)
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    async def new_context(self, proxy: Proxy | None = None) -> BrowserContext:
        async with self._lock:
            if self._browser is not None and not self._browser.is_connected():
                self._browser = None  # crashed: launch a new one
            if self._browser is None:
                if self._playwright is None:
                    self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(headless=self._headless)

        # A clean, throwaway profile: never the user's signed-in retailer sessions.
        context = await self._browser.new_context(
            locale="en-US",
            timezone_id="America/New_York",
            viewport={"width": 1366, "height": 900},
            proxy=proxy.playwright() if proxy else None,
        )
        if proxy is not None:
            # Proxy plans meter bandwidth; the stock signal never needs images, fonts or video.
            await context.route("**/*", _skip_heavy)
        return context

    async def relaunch(self) -> None:
        """Drops a browser that stopped answering; the next new_context starts a fresh one."""
        async with self._lock:
            browser, self._browser = self._browser, None
        if browser is not None:
            try:
                async with asyncio.timeout(15):
                    await browser.close()
            except (TimeoutError, PlaywrightError):
                pass

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()


class BrowserFetcher:
    """Loads a page in a per-target browser context and returns the rendered HTML."""

    # A tab reused for hours grows to most of a GB; a fresh tab every so often (same context,
    # so cookies stay) keeps it small.
    PAGE_REUSES = 20
    DEADLINE_SLACK = 10  # seconds on top of two page loads and the settle time

    def __init__(self, host: BrowserHost, options: Options, proxy: Proxy | None = None):
        self._host = host
        self._proxy = proxy
        self._timeout_ms = options.request_timeout_seconds * 1000
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._uses = 0
        self._bytes = 0
        self._loaded = False  # page_fetch: the product page is open in this tab

    def _new_page_counted(self, page: Page) -> Page:
        def count(resp) -> None:
            try:
                self._bytes += int(resp.headers.get("content-length") or 0)
            except ValueError:
                pass
        page.on("response", count)
        return page

    async def fetch(self, target: Target) -> FetchResult:
        # Every Playwright call can hang when Chromium is starved, not just the page load, so the whole
        # check gets a deadline: two page loads, the settle time and some slack.
        deadline = self._timeout_ms * 2 / 1000 + target.settle_ms / 1000 + self.DEADLINE_SLACK
        async with self._host.slots:
            start = time.perf_counter()
            try:
                async with asyncio.timeout(deadline):
                    return await self._fetch(target, start)
            except TimeoutError:
                pass
        # The tab or the whole browser is stuck: drop both so the next check starts clean.
        try:
            async with asyncio.timeout(10):
                await self.close()
        except TimeoutError:
            self._context = self._page = None
        await self._host.relaunch()
        return FetchResult(None, _ms_since(start), "", target.url, timed_out=True, error="browser stopped responding")

    async def _fetch(self, target: Target, start: float) -> FetchResult:
        try:
            if self._page is not None and self._uses >= self.PAGE_REUSES:
                await self._page.close()
                self._page = self._new_page_counted(await self._context.new_page())
                self._uses, self._loaded = 0, False
            if self._page is None:
                self._context = await self._host.new_context(self._proxy)
                self._page = self._new_page_counted(await self._context.new_page())
                self._uses, self._loaded = 0, False
                start = time.perf_counter()
            self._bytes = 0
            if target.page_fetch:
                return await self._page_fetch(target, start)
            self._uses += 1
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

            html, final_url = await page.content(), page.url
            try:
                # Leave the page: its scripts keep running (and growing) between checks otherwise. Cookies stay.
                await page.goto("about:blank")
            except PlaywrightError:
                pass
            headers = pick_headers(resp.headers.items()) if resp is not None else {}
            return FetchResult(resp.status if resp is not None else None, nav_ms, html, final_url, headers=headers,
                               bytes=max(self._bytes, len(html)))
        except PlaywrightTimeout:
            return FetchResult(None, _ms_since(start), "", target.url, timed_out=True, error="navigation timeout")
        except PlaywrightError as ex:
            await self.close()  # the page or browser may be dead: start fresh next time
            error = str(ex).split("\n")[0]
            return FetchResult(None, _ms_since(start), "", target.url,
                               error=self._proxy.redact(error) if self._proxy else error)

    async def _page_fetch(self, target: Target, start: float) -> FetchResult:
        page = self._page
        self._uses += 1
        if not self._loaded:
            resp = await page.goto(target.url, wait_until="domcontentloaded", timeout=self._timeout_ms)
            if resp is None or resp.status >= 400:
                html = await page.content()  # the bot check, most likely: let detect() see it
                return FetchResult(resp.status if resp else None, _ms_since(start), html, page.url, bytes=self._bytes)
            await page.wait_for_timeout(target.settle_ms)
            self._loaded = True
        start = time.perf_counter()
        status, body = await page.evaluate(
            """async ([u, ms]) => { const r = await fetch(u, {headers: {'X-Requested-With': 'XMLHttpRequest'},
                                                               signal: AbortSignal.timeout(ms)});
                                     return [r.status, await r.text()]; }""", [target.page_fetch, self._timeout_ms])
        if status >= 400:
            self._loaded = False  # reload the page next time; its session may have gone stale
        return FetchResult(status, _ms_since(start), body, target.page_fetch, bytes=self._bytes + len(body))

    async def close(self) -> None:
        context, self._context, self._page = self._context, None, None
        if context is not None:
            try:
                await context.close()
            except PlaywrightError:
                pass