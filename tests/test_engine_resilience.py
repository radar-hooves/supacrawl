"""Stealth-engine resilience and container-aware Chromium launch args.

Covers the engine-layer hardening lane:
- #143: a missing stealth engine degrades gracefully (lazy start; non-stealth
  tools keep working; a stealth path fails with a typed, per-engine error), and
  never cascade-fails the whole server at startup.
- #146: Chromium launch args include ``--disable-dev-shm-usage`` under a
  constrained-/dev/shm container, gated so a normal host is not regressed.

Per-engine availability reporting (#144) is in ``test_engine_availability.py``;
the real-launch backstop (#145) is in ``test_engine_smoke.py``.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from supacrawl.services import browser as browser_mod
from supacrawl.services.browser import (
    BrowserManager,
    CamoufoxNotAvailableError,
    StealthNotAvailableError,
    chromium_launch_args,
)

# #143 — graceful degradation: lazy start + typed per-engine error


class TestLazyStart:
    @pytest.mark.asyncio
    async def test_server_factory_does_not_launch_a_browser_at_startup(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """#143 criterion: no engine is launched until a tool needs one."""
        from supacrawl.services import registry

        started = {"count": 0}

        async def _record_start(self: BrowserManager) -> None:
            started["count"] += 1

        monkeypatch.setattr(BrowserManager, "start", _record_start)
        monkeypatch.setenv("SUPACRAWL_CACHE_DIR", str(tmp_path / "cache"))

        services = await registry.create_supacrawl_services()
        try:
            assert started["count"] == 0, "the server must not launch the engine at init"
            # The full non-stealth tool surface is still wired and usable.
            assert services.scrape_service is not None
            assert services.search_service is not None
            assert services.map_service is not None
        finally:
            await services.close()

    @pytest.mark.asyncio
    async def test_first_fetch_triggers_lazy_start(self, monkeypatch: pytest.MonkeyPatch) -> None:
        started = {"count": 0}

        async def _record_start(self: BrowserManager) -> None:
            started["count"] += 1
            self._browser = object()  # type: ignore[assignment]
            self._ever_started = True

        monkeypatch.setattr(BrowserManager, "start", _record_start)
        monkeypatch.setattr(BrowserManager, "is_alive", property(lambda self: self._browser is not None))

        manager = BrowserManager()
        assert started["count"] == 0  # nothing launched by construction
        await manager.ensure_started()
        assert started["count"] == 1  # launched lazily on first use

    @pytest.mark.asyncio
    async def test_concurrent_first_use_launches_exactly_one_engine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A burst of concurrent first-use must launch ONE engine, not one per caller.

        The shared server-side manager is hit by scrape/crawl/map/search at once
        on a just-booted server; an unguarded lazy start would launch a browser
        per caller and orphan every loser (a real regression from moving startup
        off the synchronous init path).
        """
        import asyncio

        started = {"count": 0}

        async def _slow_start(self: BrowserManager) -> None:
            # Yield so all callers pile up on the lazy branch before any completes.
            await asyncio.sleep(0.02)
            started["count"] += 1
            self._browser = object()  # type: ignore[assignment]
            self._ever_started = True

        monkeypatch.setattr(BrowserManager, "start", _slow_start)
        monkeypatch.setattr(BrowserManager, "is_alive", property(lambda self: self._browser is not None))

        manager = BrowserManager()
        await asyncio.gather(*(manager.ensure_started() for _ in range(8)))

        assert started["count"] == 1, "concurrent first use must launch exactly one engine"

    @pytest.mark.asyncio
    async def test_missing_stealth_engine_raises_typed_error_on_use_not_at_construction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Construction never launches, so it must not raise even for a missing engine.
        manager = BrowserManager(engine="camoufox")

        async def _missing(self: BrowserManager) -> None:
            raise CamoufoxNotAvailableError()

        monkeypatch.setattr(BrowserManager, "_start_camoufox", _missing)
        with pytest.raises(CamoufoxNotAvailableError):
            await manager.ensure_started()


class TestMissingEngineClassification:
    """A missing-engine failure is INFRASTRUCTURE (scraper fault), not a site EMPTY."""

    def _service(self) -> Any:
        from supacrawl.services.scrape import ScrapeService

        return ScrapeService()

    def test_stealth_missing_is_infrastructure_and_names_the_engine(self) -> None:
        from supacrawl.models import QualityVerdict

        result = self._service()._build_failure_result(
            url="https://x.test",
            error=StealthNotAvailableError(),
            wants_change_tracking=False,
            previous_entry=None,
        )
        assert result.success is False
        assert result.quality.verdict == QualityVerdict.INFRASTRUCTURE
        assert "patchright" in result.error  # names what to install
        assert "supacrawl[stealth]" in result.error

    def test_camoufox_missing_is_infrastructure(self) -> None:
        from supacrawl.models import QualityVerdict

        result = self._service()._build_failure_result(
            url="https://x.test",
            error=CamoufoxNotAvailableError(),
            wants_change_tracking=False,
            previous_entry=None,
        )
        assert result.quality.verdict == QualityVerdict.INFRASTRUCTURE
        assert "camoufox" in result.error.lower()


class _Platform:
    name = "foleon"
    engine = "camoufox"
    wait_for = 8000
    only_main_content = False
    expand_iframes = "all"
    actions: ClassVar[list[Any]] = []


class TestEscalationAvailabilityGating:
    def test_platform_short_circuit_skips_an_uninstalled_engine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A tuned platform engine that is missing must not be chosen (#143)."""
        from supacrawl.services import scrape as scrape_mod

        # camoufox unavailable (both the binary-aware gate and the generic-ladder
        # import checks) → the platform rung is skipped and nothing else is
        # installed, so the ladder yields None rather than a camoufox rung.
        monkeypatch.setattr(scrape_mod, "engine_availability", lambda engine: {"available": False})
        rung = scrape_mod._next_escalation(
            engine="playwright", stealth=False, prefs=None, pinned=False, http2_error=False, platform=_Platform()
        )
        assert rung is None

    def test_platform_short_circuit_used_when_engine_is_installed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from supacrawl.services import scrape as scrape_mod

        monkeypatch.setattr(scrape_mod, "engine_availability", lambda engine: {"available": True})
        rung = scrape_mod._next_escalation(
            engine="playwright", stealth=False, prefs=None, pinned=False, http2_error=False, platform=_Platform()
        )
        assert rung is not None
        assert rung.engine == "camoufox"

    def test_gate_skips_a_package_present_but_binary_missing_engine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The #144/#143 join: an installed package with no browser binary is NOT usable.

        This is the common real misconfiguration (a bare ``uv sync``, or no
        ``patchright install`` / ``camoufox fetch``). It must be skipped exactly
        like a missing package, or the launch fails with a raw "executable not
        found" that misclassifies as EMPTY instead of degrading.
        """
        from supacrawl.services import browser as browser_mod
        from supacrawl.services import scrape as scrape_mod

        # Package present, browser binary absent — the binary-aware check the gate
        # now uses must report the engine unusable.
        monkeypatch.setattr(browser_mod, "_package_installed", lambda pkg: True)
        monkeypatch.setattr(browser_mod, "_chromium_browser_installed", lambda: False)
        monkeypatch.setattr(browser_mod, "_camoufox_browser_installed", lambda: False)

        assert scrape_mod._engine_available("patchright") is False
        assert scrape_mod._engine_available("camoufox") is False
        # The base engine is never gated away.
        assert scrape_mod._engine_available("playwright") is True

        # And the platform short-circuit therefore skips the binary-missing engine.
        rung = scrape_mod._next_escalation(
            engine="playwright", stealth=False, prefs=None, pinned=False, http2_error=False, platform=_Platform()
        )
        assert rung is None

    def test_generic_ladder_skips_a_binary_missing_engine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The generic ladder — not only the platform short-circuit — must use the gate.

        Measured on a real machine: camoufox was pip-installed but ``camoufox
        fetch`` had never been run, so the import-only check reported it
        available. Two blocked pages each spent ~50s escalating into a rung that
        could never launch, and returned a raw FileNotFoundError traceback.
        """
        from supacrawl.services import browser as browser_mod
        from supacrawl.services import scrape as scrape_mod

        # Both packages import fine; neither browser binary is fetched.
        monkeypatch.setattr(browser_mod, "_package_installed", lambda pkg: True)
        monkeypatch.setattr(browser_mod, "_chromium_browser_installed", lambda: False)
        monkeypatch.setattr(browser_mod, "_camoufox_browser_installed", lambda: False)

        # playwright exhausted -> nothing stronger can launch, so the ladder ends.
        assert (
            scrape_mod._next_escalation(
                engine="playwright", stealth=False, prefs=None, pinned=False, http2_error=False, platform=None
            )
            is None
        )
        # Already on stealth Chromium -> the camoufox rung is skipped too.
        assert (
            scrape_mod._next_escalation(
                engine="playwright", stealth=True, prefs=None, pinned=False, http2_error=False, platform=None
            )
            is None
        )
        # And the HTTP/2 TLS fallback, which also jumps straight to camoufox.
        assert (
            scrape_mod._next_escalation(
                engine="playwright", stealth=False, prefs=None, pinned=False, http2_error=True, platform=None
            )
            is None
        )

    def test_generic_ladder_still_climbs_when_binaries_are_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The gate must not become a blanket refusal: a fetched engine is still chosen."""
        from supacrawl.services import browser as browser_mod
        from supacrawl.services import scrape as scrape_mod

        monkeypatch.setattr(browser_mod, "_package_installed", lambda pkg: True)
        monkeypatch.setattr(browser_mod, "_chromium_browser_installed", lambda: True)
        monkeypatch.setattr(browser_mod, "_camoufox_browser_installed", lambda: True)

        first = scrape_mod._next_escalation(
            engine="playwright", stealth=False, prefs=None, pinned=False, http2_error=False, platform=None
        )
        assert first is not None and first.label == "patchright (stealth)"
        second = scrape_mod._next_escalation(
            engine="playwright", stealth=True, prefs=None, pinned=False, http2_error=False, platform=None
        )
        assert second is not None and second.engine == "camoufox"

    def test_a_fetched_engine_is_still_offered_when_only_the_other_is_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Patchright fetched, camoufox not: the ladder climbs one rung and stops."""
        from supacrawl.services import browser as browser_mod
        from supacrawl.services import scrape as scrape_mod

        monkeypatch.setattr(browser_mod, "_package_installed", lambda pkg: True)
        monkeypatch.setattr(browser_mod, "_chromium_browser_installed", lambda: True)
        monkeypatch.setattr(browser_mod, "_camoufox_browser_installed", lambda: False)

        first = scrape_mod._next_escalation(
            engine="playwright", stealth=False, prefs=None, pinned=False, http2_error=False, platform=None
        )
        assert first is not None and first.label == "patchright (stealth)"
        assert (
            scrape_mod._next_escalation(
                engine="playwright", stealth=True, prefs=None, pinned=False, http2_error=False, platform=None
            )
            is None
        )


# #146 — container-gated Chromium launch args


class TestChromiumLaunchArgs:
    def test_explicit_env_on_adds_the_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SUPACRAWL_DISABLE_DEV_SHM", "1")
        assert "--disable-dev-shm-usage" in chromium_launch_args()

    def test_explicit_env_off_wins_even_in_a_container(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SUPACRAWL_DISABLE_DEV_SHM", "0")
        monkeypatch.setattr(browser_mod, "_is_containerised", lambda: True)
        assert chromium_launch_args() == []

    def test_container_detected_adds_the_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SUPACRAWL_DISABLE_DEV_SHM", raising=False)
        monkeypatch.setattr(browser_mod, "_is_containerised", lambda: True)
        assert chromium_launch_args() == ["--disable-dev-shm-usage"]

    def test_normal_host_is_not_regressed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SUPACRAWL_DISABLE_DEV_SHM", raising=False)
        monkeypatch.setattr(browser_mod, "_is_containerised", lambda: False)
        assert chromium_launch_args() == []

    @pytest.mark.asyncio
    async def test_flag_is_wired_into_the_chromium_launch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The configured launch options actually carry the flag (#146)."""
        monkeypatch.setenv("SUPACRAWL_DISABLE_DEV_SHM", "1")
        captured: dict[str, Any] = {}

        class _FakeChromium:
            async def launch(self, **kwargs: Any) -> Any:
                captured.update(kwargs)
                return object()

        class _FakePlaywright:
            chromium = _FakeChromium()

            async def stop(self) -> None:
                return None

        class _FakeAsyncPlaywright:
            async def start(self) -> _FakePlaywright:
                return _FakePlaywright()

        manager = BrowserManager(engine="playwright", headless=True)
        monkeypatch.setattr(manager, "_get_playwright_module", lambda: lambda: _FakeAsyncPlaywright())
        await manager._start_playwright()

        assert "--disable-dev-shm-usage" in captured.get("args", [])
