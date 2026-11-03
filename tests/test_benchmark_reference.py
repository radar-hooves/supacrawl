"""Tests for benchmark/reference.py — hydration-settle strategy.

Verifies that the reference renderer's content-stabilisation wait captures
content injected by inline JavaScript after the initial navigation event,
rather than returning a pre-hydration shell.

All tests are offline: content is injected via Playwright's ``set_content``
method rather than a network request, so no HTTP server is required.
"""

from __future__ import annotations

import asyncio

import pytest

from supacrawl.benchmark.reference import ReferenceRenderer, _settle_content

# Helpers


class _FakePage:
    """Fake Playwright page that simulates progressive JS content injection.

    ``evaluate`` returns the injected text after a configurable delay,
    mimicking a JS-hydrated page that populates its main container after
    the navigation event fires.

    Args:
        injected_text: The text to return once ``inject_after_s`` has elapsed.
        inject_after_s: Seconds before the injected text becomes available.
        selector_to_match: The JS selector string the ``_MAIN_TEXT_JS`` uses;
            our fake ignores it and always returns the injected text.
    """

    def __init__(self, injected_text: str, inject_after_s: float = 0.4) -> None:
        """Initialise with text and the delay before injection.

        Args:
            injected_text: Final text returned after hydration.
            inject_after_s: Simulated JS hydration delay in seconds.
        """
        self._text = injected_text
        self._inject_after_s = inject_after_s
        self._created_at = asyncio.get_event_loop().time()

    async def evaluate(self, _js: str) -> str:
        """Return an empty string until ``inject_after_s`` elapses, then the real text.

        Args:
            _js: JavaScript expression (ignored; behaviour is time-based).

        Returns:
            Empty string before hydration delay, ``injected_text`` after.
        """
        elapsed = asyncio.get_event_loop().time() - self._created_at
        if elapsed < self._inject_after_s:
            return ""
        return self._text


# Unit tests for _settle_content


@pytest.mark.unit
async def test_settle_content_waits_for_injection() -> None:
    """_settle_content must not return before JS-injected content is stable.

    The fake page returns empty for 0.4 s, then the real text.  Without the
    settle loop the content would be empty; with it the loop detects two
    consecutive non-zero equal reads and returns, leaving the real text
    available for extraction.
    """
    injected = "Australian tax rates and income brackets"
    page = _FakePage(injected_text=injected, inject_after_s=0.4)

    await _settle_content(page)

    # After settling, the page should return the injected text.
    text = await page.evaluate("")
    assert text == injected, f"Expected injected text after settle, got: {text!r}"


@pytest.mark.unit
async def test_settle_content_returns_quickly_for_static_pages() -> None:
    """_settle_content must exit quickly when content is already stable.

    A static page always returns the same text; the settler should detect
    two consecutive equal reads without waiting for the full timeout.
    """
    stable_text = "Static page content that never changes"

    class _StaticPage:
        async def evaluate(self, _js: str) -> str:
            return stable_text

    page = _StaticPage()
    start = asyncio.get_event_loop().time()
    await _settle_content(page)
    elapsed = asyncio.get_event_loop().time() - start

    # Should settle in << 5 s (two polls at 300 ms each → ~600 ms max).
    assert elapsed < 2.0, f"Settle took too long on a static page: {elapsed:.2f}s"


@pytest.mark.unit
async def test_settle_content_does_not_raise_on_evaluate_error() -> None:
    """_settle_content must be resilient to evaluate() raising exceptions.

    If the page evaluate call fails (e.g. the page is detached), the settle
    loop should silently skip the bad read and continue until timeout.
    """

    class _BrokenPage:
        async def evaluate(self, _js: str) -> str:
            raise RuntimeError("Page is detached")

    page = _BrokenPage()
    # Should complete (timeout) without raising.
    await _settle_content(page)


# Integration test using a real Playwright browser (marked e2e)


@pytest.mark.e2e
async def test_renderer_captures_js_injected_content() -> None:
    """ReferenceRenderer must capture content injected by inline JavaScript.

    An HTML page is served via set_content with a script that inserts text
    into the ``<main>`` container 200 ms after load.  Without the settle
    wait the capture returns an empty main_text; with it the text is present.

    This test launches a real headless Chromium and is therefore marked e2e.
    """
    # Build a self-contained HTML page that injects content after a short delay.
    js_hydrated_html = """
    <!DOCTYPE html>
    <html>
    <head><title>Hydration test</title></head>
    <body>
      <main id="main-content"><!-- injected by JS --></main>
      <script>
        setTimeout(function() {
          document.querySelector('main').innerText =
            'Australian resident tax rates 18200 taxable income';
        }, 200);
      </script>
    </body>
    </html>
    """
    expected_fragment = "18200"

    try:
        import playwright  # noqa: F401
    except ImportError:
        pytest.skip("Playwright not installed")

    async with ReferenceRenderer() as renderer:
        # Bypass the URL-based capture to inject content directly.
        # We need access to the underlying browser to set content.
        assert renderer._browser is not None, "Browser should be started"

        page = await renderer._browser.new_page()
        try:
            await page.set_content(js_hydrated_html)
            # Settle on the live page as the renderer would.
            await _settle_content(page)

            from supacrawl.benchmark.reference import _MAIN_TEXT_JS

            main_text: str = await page.evaluate(_MAIN_TEXT_JS)
        finally:
            await page.close()

    assert expected_fragment in main_text, (
        f"Expected {expected_fragment!r} in settled main_text, but got: {main_text!r}"
    )


class TestCaptureInstallsNavigationGuard:
    """``capture`` installs the same per-request re-validation as BrowserManager (#152).

    The pre-flight ``resolve_and_pin`` call only checks the URL the caller
    asked for; a redirect or a page-issued subresource request after
    navigation starts is only caught if the route handler is installed
    before ``page.goto``. Driven with a fake browser/page so no real
    Chromium is needed.
    """

    class _FakePage:
        def __init__(self) -> None:
            self.route_installed = False
            self.route_installed_before_goto = False

        async def route(self, _pattern: str, _handler) -> None:
            self.route_installed = True

        async def goto(self, _url: str, **_kwargs):
            self.route_installed_before_goto = self.route_installed
            raise RuntimeError("simulated navigation failure — test stops here")

    class _FakeBrowser:
        def __init__(self, page) -> None:
            self._page = page

        async def new_page(self):
            return self._page

    @pytest.mark.asyncio
    async def test_route_handler_installed_before_navigation(self, monkeypatch) -> None:
        from supacrawl.benchmark import reference as reference_mod

        monkeypatch.setattr(reference_mod, "resolve_and_pin", lambda url: ("93.184.216.34", "example.com"))

        renderer = reference_mod.ReferenceRenderer()
        page = self._FakePage()
        renderer._browser = self._FakeBrowser(page)

        capture = await renderer.capture("https://example.com/")

        assert page.route_installed is True
        assert page.route_installed_before_goto is True
        assert capture.error is not None and "Navigation failed" in capture.error
