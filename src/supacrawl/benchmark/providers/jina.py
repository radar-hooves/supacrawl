"""Jina Reader's hosted endpoint (``r.jina.ai``) as a benchmark reader.

Keyless use is limited to 20 requests a minute, so requests are paced; a
``JINA_API_KEY`` in the environment lifts the limit and is sent as a bearer.
"""

from __future__ import annotations

import asyncio
import os
import time

import httpx

from supacrawl.benchmark.providers.base import ProviderOutput, SearchOutput

_ENDPOINT = "https://r.jina.ai/"
_KEYLESS_INTERVAL_S = 3.1


class JinaProvider:
    """``GET r.jina.ai/<url>`` with the markdown return format."""

    name = "jina"

    def __init__(self) -> None:
        headers = {"X-Return-Format": "markdown", "Accept": "text/plain"}
        key = os.getenv("JINA_API_KEY")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        self._interval = 0.0 if key else _KEYLESS_INTERVAL_S
        self._client = httpx.AsyncClient(timeout=90.0, headers=headers)
        self._lock = asyncio.Lock()
        self._last_request = 0.0

    async def scrape(self, url: str, *, content_type: str = "html") -> ProviderOutput:
        async with self._lock:
            wait = self._last_request + self._interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()
        t0 = time.monotonic()
        try:
            response = await self._client.get(_ENDPOINT + url)
        except Exception as exc:
            return ProviderOutput(success=False, error=str(exc), latency_ms=(time.monotonic() - t0) * 1000)
        elapsed = (time.monotonic() - t0) * 1000
        if response.status_code >= 400 or not response.text.strip():
            return ProviderOutput(
                success=False,
                status_code=response.status_code,
                error=f"HTTP {response.status_code}: {response.text[:200]}",
                latency_ms=elapsed,
            )
        return ProviderOutput(
            success=True, markdown=response.text, status_code=response.status_code, latency_ms=elapsed
        )

    async def aclose(self) -> None:
        await self._client.aclose()


class JinaSearchProvider:
    """``s.jina.ai``, which needs a key: without ``JINA_API_KEY`` every query is a stated failure, not a skip."""

    name = "jina"

    def __init__(self) -> None:
        key = os.getenv("JINA_API_KEY")
        self._client = httpx.AsyncClient(
            timeout=90.0,
            headers={"Accept": "application/json", **({"Authorization": f"Bearer {key}"} if key else {})},
        )
        self._keyed = bool(key)

    async def search(self, query: str, *, limit: int = 5) -> SearchOutput:
        if not self._keyed:
            return SearchOutput(success=False, error="s.jina.ai needs JINA_API_KEY (a free key)")
        t0 = time.monotonic()
        try:
            response = await self._client.get("https://s.jina.ai/", params={"q": query})
            payload = response.json()
        except Exception as exc:
            return SearchOutput(success=False, error=str(exc), latency_ms=(time.monotonic() - t0) * 1000)
        elapsed = (time.monotonic() - t0) * 1000
        if response.status_code >= 400:
            return SearchOutput(success=False, error=f"HTTP {response.status_code}", latency_ms=elapsed)
        urls = [item.get("url", "") for item in payload.get("data") or [] if item.get("url")]
        return SearchOutput(success=True, urls=urls[:limit], latency_ms=elapsed)

    async def aclose(self) -> None:
        await self._client.aclose()
