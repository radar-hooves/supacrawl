"""A Firecrawl API (the self-hosted build by default) as a benchmark reader."""

from __future__ import annotations

import os
import time

import httpx

from supacrawl.benchmark.providers.base import ProviderOutput


class FirecrawlProvider:
    """``POST /v2/scrape`` with ``onlyMainContent`` on ``FIRECRAWL_API_URL`` (default ``http://localhost:3002``).

    ``FIRECRAWL_API_KEY`` is sent as a bearer when set; the self-hosted build needs none.
    """

    name = "firecrawl"

    def __init__(self) -> None:
        self._base = os.getenv("FIRECRAWL_API_URL", "http://localhost:3002").rstrip("/")
        headers = {}
        key = os.getenv("FIRECRAWL_API_KEY")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        self._client = httpx.AsyncClient(timeout=120.0, headers=headers)

    async def scrape(self, url: str, *, content_type: str = "html") -> ProviderOutput:
        body = {"url": url, "formats": ["markdown"], "onlyMainContent": True, "timeout": 60_000}
        if content_type == "pdf":
            body["parsers"] = ["pdf"]
        t0 = time.monotonic()
        try:
            response = await self._client.post(f"{self._base}/v2/scrape", json=body)
            payload = response.json()
        except Exception as exc:
            return ProviderOutput(success=False, error=str(exc), latency_ms=(time.monotonic() - t0) * 1000)
        elapsed = (time.monotonic() - t0) * 1000
        data = payload.get("data") or {}
        markdown = data.get("markdown") or ""
        status = (data.get("metadata") or {}).get("statusCode")
        if response.status_code >= 400 or not payload.get("success") or not markdown.strip():
            return ProviderOutput(
                success=False,
                status_code=status,
                error=payload.get("error") or f"HTTP {response.status_code}: no content",
                latency_ms=elapsed,
            )
        return ProviderOutput(success=True, markdown=markdown, status_code=status, latency_ms=elapsed)

    async def aclose(self) -> None:
        await self._client.aclose()
