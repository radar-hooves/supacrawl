"""#145 — CI smoke-test that each stealth engine actually launches headless.

The deliverable is the launch check itself. Each engine (playwright / patchright
Chromium, camoufox Firefox) is launched headless against a trivial loopback page;
if an engine cannot launch — package missing, browser binary not fetched, version
skew — the test FAILS. This is what makes the #144 no-launch availability report
trustworthy rather than a confident liar: something independently proves that an
engine reported "available" can in fact start.

Marked ``stealth_smoke`` and excluded from the main test job, because it needs
every engine's browser binary fetched (``camoufox fetch`` especially, which the
main job does not run). A dedicated CI job installs the stealth+camoufox extras,
fetches all browsers, and runs ``pytest -m stealth_smoke`` — a red here gates the
merge, catching a broken stealth engine in supacrawl's own pipeline instead of
downstream at a consumer's container build.

To confirm it can go RED (not merely green today): temporarily point an engine at
a missing binary, e.g.
    PLAYWRIGHT_BROWSERS_PATH=/nonexistent uv run pytest -m stealth_smoke
and observe the Chromium engines fail to launch.
"""

from __future__ import annotations

import http.server
import threading

import pytest

from supacrawl.services.browser import BrowserManager, engine_availability

_PROBE_BODY = b"<html><body><h1>engine-smoke</h1><p>launched headless</p></body></html>"


def _serve_probe_page() -> tuple[http.server.HTTPServer, int]:
    """A loopback HTTP server so the smoke test needs no external network."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(_PROBE_BODY)))
            self.end_headers()
            self.wfile.write(_PROBE_BODY)

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


@pytest.mark.stealth_smoke
@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["playwright", "patchright", "camoufox"])
async def test_engine_launches_headless_and_serves_a_page(engine: str) -> None:
    """Each stealth engine must launch headless and return real page content.

    No skip-if-missing: in the dedicated smoke job every engine is expected to be
    fully installed, so an unavailable engine is a genuine failure to surface, not
    a reason to pass quietly. The availability check is asserted first so a
    binary-missing engine fails with a precise reason before the launch attempt.
    """
    verdict = engine_availability(engine)
    assert verdict["available"], f"{engine} not available: {verdict}"

    server, port = _serve_probe_page()
    url = f"http://127.0.0.1:{port}/"
    try:
        async with BrowserManager(engine=engine, headless=True) as browser:
            assert browser.is_alive is True, f"{engine} did not stay connected after launch"
            content = await browser.fetch_page(url, wait_for_spa=False)
            assert "engine-smoke" in content.html, f"{engine} launched but returned no content"
            assert content.status_code == 200
    finally:
        server.shutdown()
