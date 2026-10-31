"""Trafilatura as a benchmark reader: a plain HTTP fetch, then its main-content extraction to markdown."""

from __future__ import annotations

import time

import httpx

from supacrawl.benchmark.providers.base import ProviderOutput

_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"


class TrafilaturaProvider:
    """``trafilatura.extract`` over the page's HTML; PDFs are out of its scope and fail."""

    name = "trafilatura"

    def __init__(self) -> None:
        import trafilatura

        self._extract = trafilatura.extract
        self._client = httpx.AsyncClient(timeout=30.0, follow_redirects=True, headers={"User-Agent": _USER_AGENT})

    async def scrape(self, url: str, *, content_type: str = "html") -> ProviderOutput:
        t0 = time.monotonic()
        if content_type == "pdf":
            return ProviderOutput(success=False, error="trafilatura reads HTML only", latency_ms=0.0)
        try:
            response = await self._client.get(url)
            markdown = self._extract(
                response.text, url=url, output_format="markdown", include_tables=True, include_links=False
            )
        except Exception as exc:
            return ProviderOutput(success=False, error=str(exc), latency_ms=(time.monotonic() - t0) * 1000)
        elapsed = (time.monotonic() - t0) * 1000
        if response.status_code >= 400 or not markdown:
            return ProviderOutput(
                success=False,
                status_code=response.status_code,
                error=f"HTTP {response.status_code}" if response.status_code >= 400 else "no content extracted",
                latency_ms=elapsed,
            )
        return ProviderOutput(success=True, markdown=markdown, status_code=response.status_code, latency_ms=elapsed)

    async def aclose(self) -> None:
        await self._client.aclose()
