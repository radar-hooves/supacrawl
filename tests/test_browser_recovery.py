"""Browser-pool self-healing (#160).

A long-lived server hands one ``BrowserManager`` to every request, so a browser
that dies is a silent outage for every consumer of that server until a human
notices and restarts it. These tests cover the detection, the in-process
relaunch, the crash-loop guard, and — driving a real Chromium — that a killed
engine genuinely serves pages again rather than merely running the recovery code.

The precision requirement cuts both ways: a closed *page* under a healthy
browser produces the same Playwright wording as a dead engine, and must never
trigger a relaunch or a retry, or the recovery would quietly re-run genuine site
failures.
"""

from __future__ import annotations

import asyncio
import http.server
import threading
import time
from typing import Any

import pytest

from supacrawl.services.browser import (
    BrowserManager,
    BrowserUnavailableError,
    is_closed_browser_error,
)

# The verbatim message from the reported outage (correlation ids 2df84ebd,
# 8f3a0bf9): two scrapes failed instantly against a long-lived sidecar whose
# browser pool had died, and no code path brought it back.
FIELD_ERROR = "Browser.new_context: Target page, context or browser has been closed"


class _FakePage:
    async def close(self) -> None:
        pass


class _FakeContext:
    def __init__(self) -> None:
        self.closed = False

    async def add_init_script(self, script: str) -> None:
        pass

    async def new_page(self) -> _FakePage:
        return _FakePage()

    async def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    """Stand-in for a Playwright ``Browser`` whose liveness the test controls."""

    def __init__(self, *, connected: bool = True, dies_on_use: bool = False) -> None:
        self.connected = connected
        self.dies_on_use = dies_on_use
        self.contexts_opened = 0

    def is_connected(self) -> bool:
        return self.connected

    async def new_context(self, **_: Any) -> _FakeContext:
        if self.dies_on_use:
            # The race the liveness check cannot close: alive when checked, gone
            # by the time it is used.
            self.connected = False
            raise RuntimeError(FIELD_ERROR)
        if not self.connected:
            raise RuntimeError(FIELD_ERROR)
        self.contexts_opened += 1
        return _FakeContext()

    async def close(self) -> None:
        self.connected = False


def _managed(
    monkeypatch: pytest.MonkeyPatch,
    *,
    initial: _FakeBrowser | None,
    launch_fails: bool = False,
) -> tuple[BrowserManager, list[Any]]:
    """A manager whose ``start()`` installs a fresh fake browser.

    Returns the manager and the list of objects each launch produced, so a test
    can count launches (``len``) as well as observe the current engine.
    """
    manager = BrowserManager()
    manager._browser = initial  # type: ignore[assignment]
    manager._ever_started = True
    launches: list[Any] = []

    async def fake_start() -> None:
        await asyncio.sleep(0)  # a real launch yields; make interleaving possible
        if launch_fails:
            launches.append(None)
            raise RuntimeError("chromium executable not found")
        fresh = _FakeBrowser()
        launches.append(fresh)
        manager._browser = fresh  # type: ignore[assignment]

    monkeypatch.setattr(manager, "start", fake_start)
    return manager, launches


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


class TestClosedBrowserDetection:
    def test_the_reported_field_error_is_recognised(self) -> None:
        assert is_closed_browser_error(RuntimeError(FIELD_ERROR)) is True

    @pytest.mark.parametrize(
        "message",
        [
            "Timeout 30000ms exceeded.",
            "net::ERR_NAME_NOT_RESOLVED at https://example.com/",
            "net::ERR_CONNECTION_REFUSED",
            "HTTP 403 Forbidden",
        ],
    )
    def test_genuine_site_failures_are_not_mistaken_for_a_dead_engine(self, message: str) -> None:
        assert is_closed_browser_error(RuntimeError(message)) is False

    def test_is_alive_tracks_the_underlying_connection(self) -> None:
        manager = BrowserManager()
        assert manager.is_alive is False  # nothing launched yet

        live = _FakeBrowser()
        manager._browser = live  # type: ignore[assignment]
        assert manager.is_alive is True

        live.connected = False
        assert manager.is_alive is False

    def test_a_persistent_context_reports_liveness_through_its_owning_browser(self) -> None:
        """Camoufox's browser IS the context and has no ``is_connected``."""

        class _PersistentContext:
            def __init__(self, owner: _FakeBrowser) -> None:
                self.browser = owner

        owner = _FakeBrowser()
        manager = BrowserManager()
        manager._browser = _PersistentContext(owner)  # type: ignore[assignment]
        assert manager.is_alive is True

        owner.connected = False
        assert manager.is_alive is False


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


class TestRelaunch:
    @pytest.mark.asyncio
    async def test_a_dead_engine_is_relaunched_before_the_fetch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        dead = _FakeBrowser(connected=False)
        manager, launches = _managed(monkeypatch, initial=dead)

        await manager.ensure_started()

        assert len(launches) == 1
        assert manager.is_alive is True
        assert manager.relaunches == 1

    @pytest.mark.asyncio
    async def test_a_live_engine_is_left_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        manager, launches = _managed(monkeypatch, initial=_FakeBrowser())

        await manager.ensure_started()

        assert launches == []
        assert manager.relaunches == 0

    @pytest.mark.asyncio
    async def test_an_engine_dying_mid_checkout_is_relaunched_and_the_page_still_opens(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The check-then-use race: alive at the liveness check, gone at new_context."""
        manager, launches = _managed(monkeypatch, initial=_FakeBrowser(dies_on_use=True))

        await manager.ensure_started()  # passes: the browser still reports connected
        context, page = await manager._open_page(owns_context=True)

        assert page is not None
        assert context is not None
        assert len(launches) == 1
        assert manager.relaunches == 1

    @pytest.mark.asyncio
    async def test_a_healthy_browser_never_relaunches_however_the_error_reads(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A closed PAGE produces identical wording; retrying it would re-run site failures."""

        class _HealthyButFailing(_FakeBrowser):
            async def new_context(self, **_: Any) -> _FakeContext:
                raise RuntimeError(FIELD_ERROR)  # browser stays connected

        manager, launches = _managed(monkeypatch, initial=_HealthyButFailing())

        with pytest.raises(RuntimeError, match="has been closed"):
            await manager._open_page(owns_context=True)

        assert launches == []
        assert manager.relaunches == 0

    @pytest.mark.asyncio
    async def test_concurrent_callers_relaunch_the_engine_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        manager, launches = _managed(monkeypatch, initial=_FakeBrowser(connected=False))

        await asyncio.gather(*(manager.ensure_started() for _ in range(8)))

        assert len(launches) == 1
        assert manager.relaunches == 1

    @pytest.mark.asyncio
    async def test_a_concurrent_relaunch_does_not_make_a_dead_engine_look_like_a_site_fault(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The interleaving that judging liveness off the manager gets wrong.

        Caller A is mid-``new_context`` on an engine that dies. Caller B relaunches
        first, so by the time A's error surfaces the MANAGER reads healthy — and a
        check against the manager clears the fresh engine, so A's failure escapes
        raw and gets blamed on the site. A must be judged against the engine A was
        actually using, which here means A recovers onto the fresh one.
        """
        in_flight = asyncio.Event()
        release = asyncio.Event()

        class _DiesWhileInFlight(_FakeBrowser):
            async def new_context(self, **_: Any) -> _FakeContext:
                self.connected = False
                in_flight.set()
                await release.wait()  # B gets to relaunch before this error surfaces
                raise RuntimeError(FIELD_ERROR)

        manager, launches = _managed(monkeypatch, initial=_DiesWhileInFlight())

        caller_a = asyncio.create_task(manager._open_page(owns_context=True))
        await in_flight.wait()
        await manager.relaunch()  # caller B heals the pool first
        assert manager.is_alive is True
        release.set()

        context, page = await caller_a

        assert page is not None, "caller A did not recover onto the engine B relaunched"
        assert len(launches) == 1, "A relaunched again instead of using B's fresh engine"

    def test_a_mid_fetch_death_stays_an_infrastructure_fault_after_a_concurrent_relaunch(self) -> None:
        """The same race, for a browser that dies AFTER the page opened.

        That path cannot retry (a fetch may have run side-effecting actions), so
        the only thing protecting the caller is the label — and the label must be
        judged against the engine the fetch ran on, not the healthy replacement.
        """
        manager = BrowserManager()
        manager._browser = _FakeBrowser()  # type: ignore[assignment]  # B's fresh engine
        dead_engine = _FakeBrowser(connected=False)  # the one A was riding

        relabelled = manager._as_engine_failure(RuntimeError(FIELD_ERROR), dead_engine)

        assert isinstance(relabelled, BrowserUnavailableError)

    def test_a_healthy_engine_is_never_relabelled_as_infrastructure(self) -> None:
        manager = BrowserManager()
        healthy = _FakeBrowser()
        manager._browser = healthy  # type: ignore[assignment]

        error = RuntimeError(FIELD_ERROR)

        assert manager._as_engine_failure(error, healthy) is error

    @pytest.mark.asyncio
    async def test_a_deliberately_stopped_manager_is_not_resurrected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Self-healing covers an engine that DIED, never one a caller retired."""
        manager, launches = _managed(monkeypatch, initial=_FakeBrowser())

        await manager.stop()

        with pytest.raises(RuntimeError, match="not initialized"):
            await manager.ensure_started()
        assert launches == []


# ---------------------------------------------------------------------------
# Crash-loop guard
# ---------------------------------------------------------------------------


class TestCrashLoopGuard:
    @pytest.mark.asyncio
    async def test_a_broken_environment_is_not_relaunched_per_request(self, monkeypatch: pytest.MonkeyPatch) -> None:
        manager, launches = _managed(monkeypatch, initial=_FakeBrowser(connected=False), launch_fails=True)

        with pytest.raises(BrowserUnavailableError, match="relaunch failed"):
            await manager.ensure_started()
        assert len(launches) == 1

        # The immediate next request must be refused from the backoff, not turned
        # into another launch attempt against a box that cannot launch browsers.
        with pytest.raises(BrowserUnavailableError, match="before trying again"):
            await manager.ensure_started()
        assert len(launches) == 1

    @pytest.mark.asyncio
    async def test_the_backoff_expires_so_a_repaired_environment_recovers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager, launches = _managed(monkeypatch, initial=_FakeBrowser(connected=False), launch_fails=True)

        with pytest.raises(BrowserUnavailableError):
            await manager.ensure_started()
        assert manager._consecutive_relaunch_failures == 1

        # Fast-forward past the backoff and let the launch succeed this time.
        clock = {"now": manager._last_relaunch_failure_at or 0.0}
        clock["now"] += 3600.0
        monkeypatch.setattr("supacrawl.services.browser.time.monotonic", lambda: clock["now"])

        async def working_start() -> None:
            fresh = _FakeBrowser()
            launches.append(fresh)
            manager._browser = fresh  # type: ignore[assignment]

        monkeypatch.setattr(manager, "start", working_start)

        await manager.ensure_started()

        assert manager.is_alive is True
        assert manager.relaunches == 1
        assert manager._consecutive_relaunch_failures == 0

    @pytest.mark.asyncio
    async def test_an_unrevivable_engine_reports_a_scraper_fault_not_a_site_fault(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager, _ = _managed(monkeypatch, initial=_FakeBrowser(connected=False), launch_fails=True)

        with pytest.raises(BrowserUnavailableError):
            await manager._open_page(owns_context=True)

    def test_a_long_failure_streak_still_reports_a_backoff_rather_than_crashing(self) -> None:
        """An uncapped 2**failures overflows float and turns the guard into a crash."""
        manager = BrowserManager()
        manager._consecutive_relaunch_failures = 5000
        # Real monotonic, not 0.0: on a long-uptime box 0.0 reads as "ages ago"
        # and the backoff would not gate at all.
        manager._last_relaunch_failure_at = time.monotonic()

        with pytest.raises(BrowserUnavailableError, match="before trying again"):
            manager._assert_relaunch_allowed()


# ---------------------------------------------------------------------------
# The real thing: a killed Chromium serves pages again
# ---------------------------------------------------------------------------


def _serve_probe_page() -> tuple[http.server.HTTPServer, int]:
    """A loopback HTTP server so the recovery test needs no network."""
    body = b"<html><body><h1>recovery-probe</h1><p>still serving</p></body></html>"

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's contract
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


@pytest.mark.browser
@pytest.mark.asyncio
async def test_a_killed_engine_serves_pages_again_without_a_restart() -> None:
    """The outage, reproduced and recovered against a real Chromium.

    Asserting the recovery *code path* ran would prove nothing — the reported
    failure was that a dead pool kept serving errors. So this kills the browser
    process the way a crash does, then requires real page content back from the
    same manager object, with the relaunch counter proving it is a new engine
    rather than a stale success.
    """
    server, port = _serve_probe_page()
    url = f"http://127.0.0.1:{port}/"
    try:
        async with BrowserManager(headless=True) as browser:
            first = await browser.fetch_page(url, wait_for_spa=False)
            assert "recovery-probe" in first.html
            assert browser.relaunches == 0

            # Kill the engine exactly as a crash does: the manager object stays,
            # its browser process does not. No public API does this on purpose.
            await browser._browser.close()  # type: ignore[union-attr]
            assert browser.is_alive is False

            second = await browser.fetch_page(url, wait_for_spa=False)

            assert "recovery-probe" in second.html, "the relaunched engine returned no content"
            assert browser.is_alive is True
            assert browser.relaunches == 1
    finally:
        server.shutdown()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_a_scrape_through_a_shared_engine_recovers_after_the_engine_dies() -> None:
    """The reported shape: one shared engine behind a service, killed mid-life.

    ``fetch_page`` recovering is necessary but not sufficient — the outage was
    reported through ``supacrawl_scrape``, so the whole service path has to come
    back with real markdown and an ok verdict, not merely a healthier browser.
    """
    from supacrawl.services.scrape import ScrapeService

    server, port = _serve_probe_page()
    url = f"http://127.0.0.1:{port}/"
    try:
        async with BrowserManager(headless=True) as shared:
            service = ScrapeService(browser=shared)

            first = await service.scrape(url, http_first=False)
            assert first.success is True

            await shared._browser.close()  # type: ignore[union-attr]
            assert shared.is_alive is False

            second = await service.scrape(url, http_first=False)

            assert second.success is True, f"scrape did not recover: {second.error}"
            assert second.data is not None
            assert "recovery-probe" in (second.data.markdown or "")
            assert shared.relaunches == 1
    finally:
        server.shutdown()


class TestBoundedTeardown:
    """A graceful close must never hold the caller after the content is in hand.

    Chromium's own close stalls on a page with live connections and is released
    only by an internal 30s timeout. Measured end to end on this repo, same
    three URLs alternating, back to back: 33.4s mean unbounded vs 12.5s bounded,
    with byte-identical markdown in both arms.
    """

    @pytest.mark.asyncio
    async def test_a_stalled_close_does_not_hold_the_caller(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stopped: list[str] = []

        class _HangingBrowser:
            async def close(self) -> None:
                await asyncio.sleep(30)

        class _Playwright:
            async def stop(self) -> None:
                stopped.append("driver")

        monkeypatch.setenv("SUPACRAWL_BROWSER_CLOSE_TIMEOUT", "0.2")
        manager = BrowserManager()
        manager._browser = _HangingBrowser()  # type: ignore[assignment]
        manager._playwright = _Playwright()  # type: ignore[assignment]

        started = time.monotonic()
        await manager.stop()
        elapsed = time.monotonic() - started

        assert elapsed < 5, f"stop() waited {elapsed:.1f}s on a stalled close"
        # The driver stop is what actually reaps the browser process, so it must
        # still run once the graceful close is abandoned.
        assert stopped == ["driver"]
        assert manager._browser is None
        assert manager._playwright is None

    @pytest.mark.asyncio
    async def test_a_prompt_close_is_still_awaited_gracefully(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The bound is a cap on a hang, not a blanket kill."""
        closed: list[str] = []

        class _PromptBrowser:
            async def close(self) -> None:
                closed.append("browser")

        class _Playwright:
            async def stop(self) -> None:
                closed.append("driver")

        monkeypatch.setenv("SUPACRAWL_BROWSER_CLOSE_TIMEOUT", "5")
        manager = BrowserManager()
        manager._browser = _PromptBrowser()  # type: ignore[assignment]
        manager._playwright = _Playwright()  # type: ignore[assignment]
        await manager.stop()
        assert closed == ["browser", "driver"]

    @pytest.mark.asyncio
    async def test_a_close_that_raises_still_reaps_the_driver(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stopped: list[str] = []

        class _AngryBrowser:
            async def close(self) -> None:
                raise RuntimeError("Target page, context or browser has been closed")

        class _Playwright:
            async def stop(self) -> None:
                stopped.append("driver")

        manager = BrowserManager()
        manager._browser = _AngryBrowser()  # type: ignore[assignment]
        manager._playwright = _Playwright()  # type: ignore[assignment]
        await manager.stop()
        assert stopped == ["driver"]

    @pytest.mark.asyncio
    async def test_camoufox_teardown_is_not_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Camoufox must run its teardown to completion, however slow it is.

        ``AsyncCamoufox.__aexit__`` closes the browser and only THEN stops
        playwright, and that second step is what reaps the Firefox and driver
        processes. Abandoning it partway leaks them — the opposite of the
        Chromium path, where stopping the driver is a separate call that reaps
        on its own. Bounding this branch is the tempting symmetry to avoid.
        """
        steps: list[str] = []

        class _CamoufoxCM:
            async def __aexit__(self, *_: object) -> None:
                await asyncio.sleep(0.4)  # the graceful close, slower than the bound
                steps.append("reaped")  # what super().__aexit__ does

        monkeypatch.setenv("SUPACRAWL_BROWSER_CLOSE_TIMEOUT", "0.05")
        manager = BrowserManager(engine="camoufox")
        manager._camoufox_cm = _CamoufoxCM()  # type: ignore[assignment]
        manager._browser = object()  # type: ignore[assignment]

        await manager.stop()

        assert steps == ["reaped"], "camoufox teardown was cut short, leaking its browser process"
        assert manager._browser is None
        assert not hasattr(manager, "_camoufox_cm")

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(None, 3.0), ("0.5", 0.5), ("0", 0.0), ("-1", 0.0), ("not-a-number", 3.0)],
    )
    def test_the_bound_is_read_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: float
    ) -> None:
        from supacrawl.services.browser import _close_timeout_s

        monkeypatch.delenv("SUPACRAWL_BROWSER_CLOSE_TIMEOUT", raising=False)
        if raw is not None:
            monkeypatch.setenv("SUPACRAWL_BROWSER_CLOSE_TIMEOUT", raw)
        assert _close_timeout_s() == expected
