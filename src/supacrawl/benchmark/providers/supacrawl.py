"""Supacrawl provider adapter for the benchmark.

Wraps ``ScrapeService`` to conform to the ``ScraperProvider`` Protocol so the
runner can call it uniformly alongside any future third-party adapters.
"""

from __future__ import annotations

import logging
import time
from typing import Literal

from supacrawl.benchmark.providers.base import ProviderOutput, SearchOutput
from supacrawl.services.scrape import ScrapeService

LOGGER = logging.getLogger(__name__)

type _Format = Literal[
    "markdown",
    "html",
    "rawHtml",
    "links",
    "screenshot",
    "pdf",
    "json",
    "images",
    "branding",
    "structuredData",
    "summary",
    "changeTracking",
]

# Formats requested for every HTML page scrape — markdown for scoring, links
# and images for density/count metrics, structuredData for JSON-LD detection.
_HTML_FORMATS: list[_Format] = ["markdown", "links", "images", "structuredData"]
_PDF_FORMATS: list[_Format] = ["markdown"]


class SupacrawlProvider:
    """Benchmark adapter that drives supacrawl's own scrape pipeline.

    One instance should be shared across the whole run; ``ScrapeService`` is
    stateless for most purposes but avoids repeated initialisation overhead
    when reused.
    """

    name = "supacrawl"

    def __init__(self) -> None:
        """Initialise the provider with a fresh ``ScrapeService``."""
        self._service = ScrapeService()

    async def scrape(self, url: str, *, content_type: str = "html") -> ProviderOutput:
        """Scrape ``url`` via supacrawl and return normalised output.

        Timing covers the full ``ScrapeService.scrape`` call. PDF pages use
        ``parse_pdf="auto"`` and do not request image/link formats since those
        are meaningless for binary PDFs. JSON-LD detection reads the raw list
        so that an empty list is treated as absent.

        Args:
            url: The page to scrape.
            content_type: ``"html"`` or ``"pdf"``.

        Returns:
            ``ProviderOutput`` with the result.
        """
        parse_pdf: Literal["auto"] | None = "auto" if content_type == "pdf" else None
        formats = _HTML_FORMATS if content_type != "pdf" else _PDF_FORMATS

        t0 = time.monotonic()
        try:
            result = await self._service.scrape(
                url,
                formats=formats,
                only_main_content=True,
                parse_pdf=parse_pdf,
                http_first=True,
                timeout=30000,
            )
        except Exception as exc:
            elapsed = (time.monotonic() - t0) * 1000
            LOGGER.debug("SupacrawlProvider.scrape failed for %s: %s", url, exc)
            return ProviderOutput(
                success=False,
                error=str(exc),
                latency_ms=elapsed,
            )

        elapsed = (time.monotonic() - t0) * 1000

        if not result.success or result.data is None:
            return ProviderOutput(
                success=False,
                error=result.error,
                latency_ms=elapsed,
            )

        data = result.data
        markdown = data.markdown or ""
        status_code = data.metadata.status_code if data.metadata else None
        images_count = len(data.images) if data.images else 0

        # A non-empty JSON-LD list (even a list of one item) counts as found.
        json_ld_found = bool(
            data.structured_data and data.structured_data.json_ld and len(data.structured_data.json_ld) > 0
        )

        return ProviderOutput(
            success=True,
            markdown=markdown,
            status_code=status_code,
            json_ld_found=json_ld_found,
            images_count=images_count,
            latency_ms=elapsed,
        )

    async def aclose(self) -> None:
        """Close the underlying ScrapeService.

        ``ScrapeService.close`` is a safe no-op if the service never opened a
        browser, so this is always safe to call.

        Returns:
            None
        """
        await self._service.close()


class SupacrawlSearchProvider:
    """supacrawl's own web search, configured from the environment as the CLI is."""

    name = "supacrawl"

    def __init__(self) -> None:
        from supacrawl.services.search import SearchService

        self._service = SearchService()

    async def search(self, query: str, *, limit: int = 5) -> SearchOutput:
        t0 = time.monotonic()
        try:
            result = await self._service.search(query, limit=limit)
        except Exception as exc:
            return SearchOutput(success=False, error=str(exc), latency_ms=(time.monotonic() - t0) * 1000)
        elapsed = (time.monotonic() - t0) * 1000
        if not result.success:
            return SearchOutput(success=False, error=result.error, latency_ms=elapsed)
        return SearchOutput(success=True, urls=[item.url for item in result.data], latency_ms=elapsed)

    async def aclose(self) -> None:
        await self._service.close()
