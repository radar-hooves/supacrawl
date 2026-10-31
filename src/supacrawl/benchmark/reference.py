"""Browser ground-truth renderer for the benchmark.

Deliberately independent of supacrawl's pipeline so it provides a clean
reference rather than a self-comparison. Uses vanilla Playwright to render
a page and extract the main text content, structural counts, and an optional
screenshot.

Typical usage::

    async with ReferenceRenderer() as renderer:
        capture = await renderer.capture("https://example.com")
        print(capture.main_text[:500])
"""

from __future__ import annotations

import asyncio
import base64
import logging
from types import TracebackType
from typing import Any

from pydantic import BaseModel

from supacrawl.exceptions import ValidationError
from supacrawl.services.browser import _install_navigation_guard
from supacrawl.services.renderers import page_closed, page_opened, schedule_reap
from supacrawl.services.url_guard import resolve_and_pin

LOGGER = logging.getLogger(__name__)

# Hydration-settle constants.
# After initial navigation, the renderer polls the main-content container's
# text length until two consecutive reads agree (content has stopped growing)
# or the settle window expires.  This ensures JS-hydrated pages (e.g. the ATO
# CMS, Next.js shell renders) are captured after their content is injected.
_SETTLE_POLL_MS = 300
_SETTLE_TIMEOUT_S = 5.0

# JS that collects structural element counts in a single evaluate call to
# avoid the per-call overhead of repeated page.evaluate round trips.
_COUNT_JS = """
() => ({
    headings: document.querySelectorAll('h1,h2,h3,h4,h5,h6').length,
    tables:   document.querySelectorAll('table').length,
    code:     document.querySelectorAll('pre,code').length,
    images:   document.querySelectorAll('img').length,
    links:    document.querySelectorAll('a').length,
})
"""

# JS that extracts innerText from the best available content container.
_MAIN_TEXT_JS = """
() => {
    const selectors = [
        'main',
        'article',
        "[role='main']",
        '.mw-parser-output',
        '#content',
    ];
    for (const sel of selectors) {
        const el = document.querySelector(sel);
        if (el) return el.innerText || '';
    }
    return document.body.innerText || '';
}
"""

_FULL_TEXT_JS = "() => document.body.innerText || ''"


async def _settle_content(page: Any) -> None:
    """Wait until the main-content container's text length stops growing.

    Polls the best available content container (same selector cascade as
    ``_MAIN_TEXT_JS``) every ``_SETTLE_POLL_MS`` milliseconds and returns
    when two consecutive reads return the same length, or when
    ``_SETTLE_TIMEOUT_S`` elapses — whichever comes first.  Never raises;
    a poll failure is silently skipped so one bad evaluate call cannot abort
    the whole capture.

    Args:
        page: A Playwright ``Page`` instance to poll.
    """
    deadline = asyncio.get_event_loop().time() + _SETTLE_TIMEOUT_S
    # Use -1 as "no reading yet"; 0 is a valid (empty) reading that should NOT
    # trigger early exit — an empty container means hydration has not fired yet.
    prev_len = -1

    while asyncio.get_event_loop().time() < deadline:
        try:
            text: str = await page.evaluate(_MAIN_TEXT_JS)
            current_len = len(text)
        except Exception:
            current_len = -1

        if current_len > 0 and current_len == prev_len:
            # Two consecutive non-empty reads agree: content has stabilised.
            return

        prev_len = current_len
        await asyncio.sleep(_SETTLE_POLL_MS / 1000.0)


class ReferenceCapture(BaseModel):
    """Ground-truth data captured by the reference browser renderer.

    Attributes:
        main_text: Inner text of the best main-content container.
        full_text: Inner text of the entire body (for full-doc coverage).
        dom_counts: Structural element counts (headings, tables, code,
            images, links).
        status: HTTP status code from navigation, when available.
        error: Error message when capture failed; ``None`` on success.
        screenshot_b64: Base64-encoded JPEG screenshot, best-effort.
    """

    main_text: str = ""
    full_text: str = ""
    dom_counts: dict[str, int] = {}
    status: int | None = None
    error: str | None = None
    screenshot_b64: str | None = None


class ReferenceRenderer:
    """Async context manager that wraps a headless Chromium for benchmarking.

    One browser instance is shared across all ``capture`` calls for the
    lifetime of the context manager, so the per-URL overhead is only a new
    page rather than a new browser.

    Usage::

        async with ReferenceRenderer() as renderer:
            for url in urls:
                cap = await renderer.capture(url)
    """

    def __init__(self) -> None:
        """Initialise the renderer; the browser is not launched until ``__aenter__``."""
        self._playwright: Any = None
        self._browser: Any = None

    async def __aenter__(self) -> "ReferenceRenderer":
        """Launch the headless Chromium browser.

        Returns:
            Self, ready for ``capture`` calls.
        """
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Close the browser and Playwright instance.

        Args:
            exc_type: Exception type, if any.
            exc_val: Exception value, if any.
            exc_tb: Exception traceback, if any.
        """
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    async def capture(self, url: str, *, timeout_ms: int = 30000) -> ReferenceCapture:
        """Render ``url`` and return ground-truth content.

        Navigation uses ``networkidle`` and falls back to ``domcontentloaded``
        on timeout. A screenshot is attempted but its failure never aborts the
        capture. All browser errors are caught and returned as an error field
        rather than raised, so one bad page does not kill a whole benchmark run.

        Args:
            url: The page to render.
            timeout_ms: Navigation timeout in milliseconds.

        Returns:
            ``ReferenceCapture`` populated with content and counts, or with
            ``error`` set when something went wrong.
        """
        if self._browser is None:
            return ReferenceCapture(error="ReferenceRenderer not started — use as async context manager")

        # Pre-flight SSRF check (#152), matching the other browser-navigation
        # sites: refuse a URL that resolves to a blocked address before driving
        # Playwright. Off the event loop — resolve_and_pin blocks on getaddrinfo.
        try:
            await asyncio.to_thread(resolve_and_pin, url)
        except ValidationError as exc:
            return ReferenceCapture(error=f"Refused by SSRF guard: {exc}")

        page = None
        try:
            page = await self._browser.new_page()
            page_opened()
            response = None

            # The pre-flight above only checks the original URL; a redirect or
            # a page-issued subresource request after navigation starts is not
            # covered by it, so install the same per-request re-validation the
            # rest of the browser paths get (#152) before navigating.
            await _install_navigation_guard(page)

            try:
                response = await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            except Exception:
                # networkidle timed out; try a faster condition before giving up
                try:
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                except Exception as nav_err:
                    return ReferenceCapture(error=f"Navigation failed: {nav_err}")

            status = response.status if response else None

            # Hydration-settle: poll the main-content container until its text
            # length stabilises across two consecutive reads, or the settle
            # window expires.  This captures JS-hydrated shells that inject
            # content after the navigation event fires.
            await _settle_content(page)

            # Extract text and structural counts
            main_text: str = await page.evaluate(_MAIN_TEXT_JS)
            full_text: str = await page.evaluate(_FULL_TEXT_JS)
            dom_counts: dict[str, int] = await page.evaluate(_COUNT_JS)

            # Screenshot is best-effort; a failure here must not abort the run
            screenshot_b64: str | None = None
            try:
                raw_bytes: bytes = await page.screenshot(type="jpeg", quality=70, full_page=False)
                screenshot_b64 = base64.b64encode(raw_bytes).decode("ascii")
            except Exception as ss_err:
                LOGGER.debug("Screenshot failed for %s: %s", url, ss_err)

            return ReferenceCapture(
                main_text=main_text or "",
                full_text=full_text or "",
                dom_counts=dom_counts,
                status=status,
                screenshot_b64=screenshot_b64,
            )

        except Exception as exc:
            return ReferenceCapture(error=f"Capture error: {exc}")
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass
                page_closed()
                schedule_reap(url)
