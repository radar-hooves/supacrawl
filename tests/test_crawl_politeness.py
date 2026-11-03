"""Tests for crawl-loop politeness: robots.txt enforcement and throttling (#119)."""

from collections.abc import AsyncGenerator
from unittest.mock import MagicMock

import pytest

from supacrawl.discovery.robots import RobotsConfig
from supacrawl.models import MapEvent, MapLink, MapResult, ScrapeData, ScrapeMetadata, ScrapeResult
from supacrawl.services.crawl import CrawlService


def _map_service(links: list[str]) -> MagicMock:
    """A stand-in MapService whose map() yields a single complete event."""
    result = MapResult(success=True, links=[MapLink(url=u) for u in links])

    async def fake_map(**kwargs: object) -> AsyncGenerator[MapEvent]:
        yield MapEvent(type="complete", result=result)

    service = MagicMock()
    service.map = fake_map
    return service


def _scrape_service(scraped: list[str]) -> MagicMock:
    """A stand-in ScrapeService that records every URL it is asked to scrape."""

    async def fake_scrape(url: str, **kwargs: object) -> ScrapeResult:
        scraped.append(url)
        return ScrapeResult(
            success=True,
            data=ScrapeData(metadata=ScrapeMetadata(source_url=url), markdown="content"),  # type: ignore[call-arg]
        )

    service = MagicMock()
    service.scrape = fake_scrape
    return service


@pytest.mark.asyncio
async def test_crawl_skips_robots_disallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """URLs disallowed by robots.txt are never handed to the scrape service."""

    async def fake_fetch_robots(origin: str, timeout: float = 30.0) -> RobotsConfig:
        return RobotsConfig(disallow_patterns=["/private"])

    monkeypatch.setattr("supacrawl.services.crawl.fetch_robots", fake_fetch_robots)

    scraped: list[str] = []
    service = CrawlService(
        browser=MagicMock(),
        map_service=_map_service(["https://example.com/ok", "https://example.com/private/secret"]),
        scrape_service=_scrape_service(scraped),
    )

    events = [event async for event in service.crawl("https://example.com", respect_robots=True)]

    assert "https://example.com/ok" in scraped
    assert "https://example.com/private/secret" not in scraped
    assert any(e.type == "complete" for e in events)


@pytest.mark.asyncio
async def test_crawl_default_does_not_enforce_robots(monkeypatch: pytest.MonkeyPatch) -> None:
    """By default (respect_robots unset) robots.txt is not consulted and nothing is skipped."""
    fetched_origins: list[str] = []

    async def fake_fetch_robots(origin: str, timeout: float = 30.0) -> RobotsConfig:
        fetched_origins.append(origin)
        return RobotsConfig(disallow_patterns=["/private"])

    monkeypatch.setattr("supacrawl.services.crawl.fetch_robots", fake_fetch_robots)

    scraped: list[str] = []
    service = CrawlService(
        browser=MagicMock(),
        map_service=_map_service(["https://example.com/ok", "https://example.com/private/secret"]),
        scrape_service=_scrape_service(scraped),
    )

    # No respect_robots argument -> default behaviour.
    [event async for event in service.crawl("https://example.com")]

    assert "https://example.com/private/secret" in scraped
    assert fetched_origins == []


@pytest.mark.asyncio
async def test_crawl_ignores_robots_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """With respect_robots=False, robots.txt is never fetched and nothing is skipped."""
    fetched_origins: list[str] = []

    async def fake_fetch_robots(origin: str, timeout: float = 30.0) -> RobotsConfig:
        fetched_origins.append(origin)
        return RobotsConfig(disallow_patterns=["/private"])

    monkeypatch.setattr("supacrawl.services.crawl.fetch_robots", fake_fetch_robots)

    scraped: list[str] = []
    service = CrawlService(
        browser=MagicMock(),
        map_service=_map_service(["https://example.com/ok", "https://example.com/private/secret"]),
        scrape_service=_scrape_service(scraped),
    )

    [event async for event in service.crawl("https://example.com", respect_robots=False)]

    assert "https://example.com/private/secret" in scraped
    assert fetched_origins == []


@pytest.mark.asyncio
async def test_crawl_applies_robots_crawl_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """A robots.txt Crawl-delay raises the per-host throttle gap."""
    captured: dict[str, float] = {}

    async def fake_fetch_robots(origin: str, timeout: float = 30.0) -> RobotsConfig:
        return RobotsConfig(crawl_delay=7.5)

    # Record what host delay the crawl loop programmes into the limiter.
    from supacrawl.services import throttle as throttle_module

    original_set = throttle_module.HostRateLimiter.set_host_delay

    def spy_set(self: throttle_module.HostRateLimiter, host: str, delay: float | None) -> None:
        if delay is not None:
            captured[host] = delay
        original_set(self, host, delay)

    monkeypatch.setattr("supacrawl.services.crawl.fetch_robots", fake_fetch_robots)
    monkeypatch.setattr(throttle_module.HostRateLimiter, "set_host_delay", spy_set)

    scraped: list[str] = []
    service = CrawlService(
        browser=MagicMock(),
        map_service=_map_service(["https://example.com/a"]),
        scrape_service=_scrape_service(scraped),
    )

    [event async for event in service.crawl("https://example.com", respect_robots=True)]

    assert captured.get("example.com") == 7.5
