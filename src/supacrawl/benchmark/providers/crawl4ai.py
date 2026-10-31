"""Crawl4AI as a benchmark reader: its own headless Chromium, with the pruning content filter for main content."""

from __future__ import annotations

import time
from typing import Any

from supacrawl.benchmark.providers.base import ProviderOutput


class Crawl4AIProvider:
    """``AsyncWebCrawler.arun`` with ``fit_markdown`` (PruningContentFilter), caching off, one browser for the run."""

    name = "crawl4ai"

    def __init__(self) -> None:
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig, DefaultMarkdownGenerator
        from crawl4ai.content_filter_strategy import PruningContentFilter

        self._crawler: Any = AsyncWebCrawler(config=BrowserConfig(headless=True, verbose=False))
        self._config = CrawlerRunConfig(
            cache_mode=CacheMode.BYPASS,
            page_timeout=30_000,
            markdown_generator=DefaultMarkdownGenerator(content_filter=PruningContentFilter()),
            verbose=False,
        )
        self._started = False

    async def scrape(self, url: str, *, content_type: str = "html") -> ProviderOutput:
        t0 = time.monotonic()
        try:
            if not self._started:
                await self._crawler.start()
                self._started = True
            result = await self._crawler.arun(url, config=self._config)
        except Exception as exc:
            return ProviderOutput(success=False, error=str(exc), latency_ms=(time.monotonic() - t0) * 1000)
        elapsed = (time.monotonic() - t0) * 1000
        markdown = ""
        if result.markdown is not None:
            markdown = getattr(result.markdown, "fit_markdown", None) or str(result.markdown)
        if not result.success or not markdown.strip():
            return ProviderOutput(
                success=False,
                status_code=result.status_code,
                error=result.error_message or "no content extracted",
                latency_ms=elapsed,
            )
        return ProviderOutput(success=True, markdown=markdown, status_code=result.status_code, latency_ms=elapsed)

    async def aclose(self) -> None:
        if self._started:
            await self._crawler.close()
