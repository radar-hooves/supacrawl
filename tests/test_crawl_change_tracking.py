"""Tests for crawl service change tracking integration."""

from collections.abc import AsyncGenerator
from typing import Literal
from unittest.mock import AsyncMock, MagicMock

import pytest

from supacrawl.models import (
    ChangeTrackingData,
    CrawlEvent,
    MapEvent,
    MapLink,
    MapResult,
    ScrapeData,
    ScrapeMetadata,
    ScrapeResult,
)
from supacrawl.services.crawl import CrawlService


def _make_scrape_result(
    url: str, change_status: Literal["new", "same", "changed", "removed"] | None = None
) -> ScrapeResult:
    """Helper to build a ScrapeResult with optional change tracking."""
    change_tracking = None
    if change_status:
        change_tracking = ChangeTrackingData(
            change_status=change_status,
            content_hash="abc123",
        )
    return ScrapeResult(
        success=True,
        data=ScrapeData(
            markdown=f"# {url}",
            metadata=ScrapeMetadata(source_url=url),
            change_tracking=change_tracking,
        ),
    )


def _make_map_events(urls: list[str]) -> list[MapEvent]:
    """Helper to build map events ending with a complete event."""
    links = [MapLink(url=u) for u in urls]
    return [
        MapEvent(type="discovery", discovered=len(urls), message="Found URLs"),
        MapEvent(
            type="complete",
            discovered=len(urls),
            result=MapResult(success=True, links=links),
        ),
    ]


class TestCrawlChangeTracking:
    """Tests for change tracking in CrawlService."""

    @pytest.mark.asyncio
    async def test_change_summary_in_complete_event(self) -> None:
        """Change summary is emitted in the complete event when changeTracking format is used."""
        urls = ["https://example.com/a", "https://example.com/b", "https://example.com/c"]
        map_events = _make_map_events(urls)

        mock_browser = MagicMock()
        mock_map = AsyncMock()
        mock_scrape = AsyncMock()

        # Map service returns URLs
        async def fake_map(**kwargs: object) -> AsyncGenerator[MapEvent]:
            for e in map_events:
                yield e

        mock_map.map = fake_map

        # Scrape service returns pages with different change statuses
        statuses: list[Literal["new", "same", "changed", "removed"]] = ["new", "changed", "same"]
        call_count = 0

        async def fake_scrape(url: str, **kwargs: object) -> ScrapeResult:
            nonlocal call_count
            result = _make_scrape_result(url, statuses[call_count])
            call_count += 1
            return result

        mock_scrape.scrape = fake_scrape

        service = CrawlService(
            browser=mock_browser,
            map_service=mock_map,
            scrape_service=mock_scrape,
        )

        events = []
        async for event in service.crawl(
            url="https://example.com",
            limit=10,
            formats=["markdown", "changeTracking"],
        ):
            events.append(event)

        # Find the complete event
        complete = next(e for e in events if e.type == "complete")
        assert complete.change_summary is not None
        assert complete.change_summary == {"new": 1, "changed": 1, "same": 1}

    @pytest.mark.asyncio
    async def test_no_change_summary_without_change_tracking_format(self) -> None:
        """No change summary when changeTracking format is not requested."""
        urls = ["https://example.com/a"]
        map_events = _make_map_events(urls)

        mock_browser = MagicMock()
        mock_map = AsyncMock()
        mock_scrape = AsyncMock()

        async def fake_map(**kwargs: object) -> AsyncGenerator[MapEvent]:
            for e in map_events:
                yield e

        mock_map.map = fake_map

        async def fake_scrape(url: str, **kwargs: object) -> ScrapeResult:
            return _make_scrape_result(url)

        mock_scrape.scrape = fake_scrape

        service = CrawlService(
            browser=mock_browser,
            map_service=mock_map,
            scrape_service=mock_scrape,
        )

        events = []
        async for event in service.crawl(
            url="https://example.com",
            limit=10,
            formats=["markdown"],
        ):
            events.append(event)

        complete = next(e for e in events if e.type == "complete")
        assert complete.change_summary is None

    @pytest.mark.asyncio
    async def test_change_tracking_modes_passed_to_scrape(self) -> None:
        """change_tracking_modes is threaded through to scrape calls."""
        urls = ["https://example.com/a"]
        map_events = _make_map_events(urls)

        mock_browser = MagicMock()
        mock_map = AsyncMock()
        mock_scrape = AsyncMock()

        async def fake_map(**kwargs: object) -> AsyncGenerator[MapEvent]:
            for e in map_events:
                yield e

        mock_map.map = fake_map

        captured_kwargs: list[dict] = []

        async def fake_scrape(url: str, **kwargs: object) -> ScrapeResult:
            captured_kwargs.append(kwargs)
            return _make_scrape_result(url, "new")

        mock_scrape.scrape = fake_scrape

        service = CrawlService(
            browser=mock_browser,
            map_service=mock_map,
            scrape_service=mock_scrape,
        )

        async for _ in service.crawl(
            url="https://example.com",
            limit=10,
            formats=["markdown", "changeTracking"],
            change_tracking_modes=["git-diff"],
        ):
            pass

        assert len(captured_kwargs) == 1
        assert captured_kwargs[0]["change_tracking_modes"] == ["git-diff"]

    @pytest.mark.asyncio
    async def test_change_tracking_adds_format_to_scrape(self) -> None:
        """changeTracking in crawl formats includes changeTracking in per-page scrape formats."""
        urls = ["https://example.com/a"]
        map_events = _make_map_events(urls)

        mock_browser = MagicMock()
        mock_map = AsyncMock()
        mock_scrape = AsyncMock()

        async def fake_map(**kwargs: object) -> AsyncGenerator[MapEvent]:
            for e in map_events:
                yield e

        mock_map.map = fake_map

        captured_kwargs: list[dict] = []

        async def fake_scrape(url: str, **kwargs: object) -> ScrapeResult:
            captured_kwargs.append(kwargs)
            return _make_scrape_result(url, "new")

        mock_scrape.scrape = fake_scrape

        service = CrawlService(
            browser=mock_browser,
            map_service=mock_map,
            scrape_service=mock_scrape,
        )

        async for _ in service.crawl(
            url="https://example.com",
            limit=10,
            formats=["markdown", "changeTracking"],
        ):
            pass

        assert len(captured_kwargs) == 1
        assert "changeTracking" in captured_kwargs[0]["formats"]

    @pytest.mark.asyncio
    async def test_change_summary_excludes_zero_counts(self) -> None:
        """Change summary only includes statuses with non-zero counts."""
        urls = ["https://example.com/a", "https://example.com/b"]
        map_events = _make_map_events(urls)

        mock_browser = MagicMock()
        mock_map = AsyncMock()
        mock_scrape = AsyncMock()

        async def fake_map(**kwargs: object) -> AsyncGenerator[MapEvent]:
            for e in map_events:
                yield e

        mock_map.map = fake_map

        call_count = 0

        async def fake_scrape(url: str, **kwargs: object) -> ScrapeResult:
            nonlocal call_count
            status: Literal["new", "same", "changed", "removed"] = "changed"
            call_count += 1
            return _make_scrape_result(url, status)

        mock_scrape.scrape = fake_scrape

        service = CrawlService(
            browser=mock_browser,
            map_service=mock_map,
            scrape_service=mock_scrape,
        )

        events = []
        async for event in service.crawl(
            url="https://example.com",
            limit=10,
            formats=["markdown", "changeTracking"],
        ):
            events.append(event)

        complete = next(e for e in events if e.type == "complete")
        assert complete.change_summary == {"changed": 2}
        # "new", "same", "removed" should not be present
        assert "new" not in complete.change_summary
        assert "same" not in complete.change_summary
        assert "removed" not in complete.change_summary


class TestCrawlEventModel:
    """Tests for CrawlEvent change_summary field."""

    def test_crawl_event_change_summary_default_none(self) -> None:
        """CrawlEvent.change_summary defaults to None."""
        event = CrawlEvent(type="complete", completed=5, total=5)
        assert event.change_summary is None

    def test_crawl_event_change_summary_populated(self) -> None:
        """CrawlEvent.change_summary can be set."""
        event = CrawlEvent(
            type="complete",
            completed=5,
            total=5,
            change_summary={"new": 2, "changed": 3},
        )
        assert event.change_summary == {"new": 2, "changed": 3}
