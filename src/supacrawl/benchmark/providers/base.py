"""Base types for benchmark scraper providers.

``ProviderOutput`` is the normalised result that every provider returns.
``ScraperProvider`` is the Protocol that every provider must implement so the
runner can swap them without changing any scoring code.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel


class ProviderOutput(BaseModel):
    """Normalised output from any scraper provider.

    Attributes:
        success: Whether the scrape returned usable content.
        markdown: The markdown produced; empty string on failure.
        status_code: HTTP status code when available.
        error: Error description when ``success`` is False.
        json_ld_found: Whether at least one JSON-LD object was extracted.
        images_count: Number of image URLs collected.
        latency_ms: Wall-clock time from request start to content received.
    """

    success: bool
    markdown: str = ""
    status_code: int | None = None
    error: str | None = None
    json_ld_found: bool = False
    images_count: int = 0
    latency_ms: float = 0.0


class ScraperProvider(Protocol):
    """Protocol for scraper provider adapters.

    Implement this to add a new provider (Firecrawl, Crawl4AI, etc.) to the
    benchmark without changing the runner.
    """

    name: str

    async def scrape(self, url: str, *, content_type: str = "html") -> ProviderOutput:
        """Scrape ``url`` and return normalised output.

        Args:
            url: The page to scrape.
            content_type: ``"html"`` for standard web pages, ``"pdf"`` to
                request PDF-specific extraction.

        Returns:
            ``ProviderOutput`` with the result.
        """
        ...

    async def aclose(self) -> None:
        """Release any resources held by the provider.

        Returns:
            None
        """
        ...


class SearchOutput(BaseModel):
    """Normalised output from a reader's web search: the result URLs in rank order."""

    success: bool
    urls: list[str] = []
    error: str | None = None
    latency_ms: float = 0.0


class SearchProvider(Protocol):
    """A reader's web-search surface, for the suite's queries."""

    name: str

    async def search(self, query: str, *, limit: int = 5) -> SearchOutput: ...

    async def aclose(self) -> None: ...
