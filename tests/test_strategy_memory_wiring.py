"""Strategy-memory and telemetry wiring for crawl and batch (#130, #137).

The single-scrape entry points already thread per-domain strategy memory and the
telemetry sink into ``ScrapeService``. These tests pin the same wiring for the
multi-page paths — a crawl and a batch that own their browser must pass the
store and telemetry into the ``ScrapeService`` they build internally, so a crawl
learns each domain on the first page and seeds the rest, and per-page quality is
recorded. The browser, map, and scrape collaborators are mocked: this is a pure
wiring assertion, not a network test.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from supacrawl.models import (
    MapEvent,
    MapResult,
    QualityAssessment,
    QualityVerdict,
    ScrapeData,
    ScrapeMetadata,
    ScrapeResult,
)
from supacrawl.services.batch import run_batch_scrape
from supacrawl.services.crawl import CrawlService
from supacrawl.services.strategy_memory import StrategyStore
from supacrawl.telemetry import MetricsSink

# Opaque sentinels: the tests assert these exact objects are threaded through to
# ScrapeService, not that they behave as a real store/sink (ScrapeService is
# mocked). The store must answer ``get`` (the crawl consults the start domain's
# champion at launch, #139); a None champion means "no seeding", so the wiring
# tests exercise the default-browser path. cast keeps the type checker honest.
_STORE = MagicMock(spec=StrategyStore)
_STORE.get.return_value = None
_TELEMETRY = cast(MetricsSink, object())


def _async_cm(instance: MagicMock) -> MagicMock:
    """Make ``instance`` usable as an async context manager returning itself."""
    instance.__aenter__ = AsyncMock(return_value=instance)
    instance.__aexit__ = AsyncMock(return_value=None)
    return instance


@pytest.mark.unit
@pytest.mark.asyncio
async def test_crawl_owns_browser_threads_store_and_telemetry() -> None:
    """A crawl that owns its browser passes the store + telemetry to ScrapeService."""

    async def fake_map(**_: object) -> AsyncGenerator[MapEvent]:
        # No URLs discovered: the crawl finishes immediately after construction,
        # which is all this wiring test needs to exercise.
        yield MapEvent(type="complete", discovered=0, result=MapResult(success=True, links=[]))

    with (
        patch("supacrawl.services.crawl.BrowserManager") as mock_bm,
        patch("supacrawl.services.crawl.MapService") as mock_map_cls,
        patch("supacrawl.services.crawl.ScrapeService") as mock_scrape_cls,
    ):
        _async_cm(mock_bm.return_value)
        mock_map_cls.return_value.map = fake_map

        service = CrawlService(strategy_store=_STORE, telemetry=_TELEMETRY)
        async for _ in service.crawl(url="https://example.com", limit=10, formats=["markdown"]):
            pass

        mock_scrape_cls.assert_called_once()
        kwargs = mock_scrape_cls.call_args.kwargs
        assert kwargs["strategy_store"] is _STORE
        assert kwargs["telemetry"] is _TELEMETRY


@pytest.mark.unit
@pytest.mark.asyncio
async def test_crawl_without_store_passes_none() -> None:
    """Default crawl construction threads None through (memory off, identical to before)."""

    async def fake_map(**_: object) -> AsyncGenerator[MapEvent]:
        yield MapEvent(type="complete", discovered=0, result=MapResult(success=True, links=[]))

    with (
        patch("supacrawl.services.crawl.BrowserManager") as mock_bm,
        patch("supacrawl.services.crawl.MapService") as mock_map_cls,
        patch("supacrawl.services.crawl.ScrapeService") as mock_scrape_cls,
    ):
        _async_cm(mock_bm.return_value)
        mock_map_cls.return_value.map = fake_map

        service = CrawlService()
        async for _ in service.crawl(url="https://example.com", limit=10, formats=["markdown"]):
            pass

        kwargs = mock_scrape_cls.call_args.kwargs
        assert kwargs["strategy_store"] is None
        assert kwargs["telemetry"] is None


def _camoufox_champion_store(tmp_path, domain: str) -> StrategyStore:
    """A real store whose champion for ``domain`` is camoufox (exploration off)."""
    store = StrategyStore(strategy_dir=tmp_path, explore_rate=0.0)
    good = ScrapeResult(
        success=True,
        quality=QualityAssessment(verdict=QualityVerdict.OK, score=90),
        data=ScrapeData(markdown="ok", metadata=ScrapeMetadata(source_url=f"https://{domain}/")),
    )
    store.record(domain, engine="camoufox", stealth=False, wait_for=5000, only_main_content=True, result=good)
    return store


@pytest.mark.unit
@pytest.mark.asyncio
async def test_crawl_seeds_shared_browser_from_champion(tmp_path) -> None:
    """A hard-site (camoufox champion) crawl builds ONE shared camoufox browser (#139)."""

    async def fake_map(**_: object) -> AsyncGenerator[MapEvent]:
        yield MapEvent(type="complete", discovered=0, result=MapResult(success=True, links=[]))

    store = _camoufox_champion_store(tmp_path, "hard.example")

    with (
        patch("supacrawl.services.crawl.BrowserManager") as mock_bm,
        patch("supacrawl.services.crawl.MapService") as mock_map_cls,
        patch("supacrawl.services.crawl.ScrapeService"),
        patch("supacrawl.services.scrape._engine_available", return_value=True),
    ):
        _async_cm(mock_bm.return_value)
        mock_map_cls.return_value.map = fake_map

        service = CrawlService(strategy_store=store)
        async for _ in service.crawl(url="https://hard.example/start", limit=10, formats=["markdown"]):
            pass

        # The shared browser is constructed once, with the champion engine, so no
        # per-page temporary camoufox launches are needed for same-domain pages.
        mock_bm.assert_called_once()
        assert mock_bm.call_args.kwargs["engine"] == "camoufox"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_crawl_user_pinned_engine_bypasses_champion_seed(tmp_path) -> None:
    """A user-pinned engine wins over a camoufox champion (#139)."""

    async def fake_map(**_: object) -> AsyncGenerator[MapEvent]:
        yield MapEvent(type="complete", discovered=0, result=MapResult(success=True, links=[]))

    store = _camoufox_champion_store(tmp_path, "hard.example")

    with (
        patch("supacrawl.services.crawl.BrowserManager") as mock_bm,
        patch("supacrawl.services.crawl.MapService") as mock_map_cls,
        patch("supacrawl.services.crawl.ScrapeService"),
        patch("supacrawl.services.scrape._engine_available", return_value=True),
    ):
        _async_cm(mock_bm.return_value)
        mock_map_cls.return_value.map = fake_map

        service = CrawlService(strategy_store=store)
        async for _ in service.crawl(
            url="https://hard.example/start", limit=10, formats=["markdown"], engine="playwright"
        ):
            pass

        mock_bm.assert_called_once()
        assert mock_bm.call_args.kwargs["engine"] == "playwright"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_crawl_soft_site_unchanged_no_champion(tmp_path) -> None:
    """A crawl with no champion for the start domain builds the default browser (#139)."""

    async def fake_map(**_: object) -> AsyncGenerator[MapEvent]:
        yield MapEvent(type="complete", discovered=0, result=MapResult(success=True, links=[]))

    store = StrategyStore(strategy_dir=tmp_path, explore_rate=0.0)  # empty: no champion

    with (
        patch("supacrawl.services.crawl.BrowserManager") as mock_bm,
        patch("supacrawl.services.crawl.MapService") as mock_map_cls,
        patch("supacrawl.services.crawl.ScrapeService"),
    ):
        _async_cm(mock_bm.return_value)
        mock_map_cls.return_value.map = fake_map

        service = CrawlService(strategy_store=store)
        async for _ in service.crawl(url="https://soft.example/start", limit=10, formats=["markdown"]):
            pass

        mock_bm.assert_called_once()
        assert mock_bm.call_args.kwargs["engine"] is None  # default, no per-page override needed


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_owns_browser_threads_store_and_telemetry() -> None:
    """A batch that owns its browser passes the store + telemetry to ScrapeService."""
    with (
        patch("supacrawl.services.batch.BrowserManager") as mock_bm,
        patch("supacrawl.services.batch.ScrapeService") as mock_scrape_cls,
    ):
        _async_cm(mock_bm.return_value)
        mock_scrape_cls.return_value.scrape = AsyncMock(
            return_value=ScrapeResult(
                success=True,
                data=ScrapeData(markdown="ok", metadata=ScrapeMetadata(source_url="https://x")),
            )
        )

        await run_batch_scrape(
            ["https://x"],
            strategy_store=_STORE,
            telemetry=_TELEMETRY,
        )

        mock_scrape_cls.assert_called_once()
        kwargs = mock_scrape_cls.call_args.kwargs
        assert kwargs["strategy_store"] is _STORE
        assert kwargs["telemetry"] is _TELEMETRY
