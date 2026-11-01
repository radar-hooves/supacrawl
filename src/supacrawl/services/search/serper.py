"""Serper.dev search provider implementation."""

import logging
import os

import httpx

from supacrawl.exceptions import ProviderError
from supacrawl.models import SearchFilters, SearchResultItem, SearchSourceType
from supacrawl.services.search.filters import domain_operator_query, iso_to_us_date
from supacrawl.utils import log_with_correlation

LOGGER = logging.getLogger(__name__)


class SerperProvider:
    """Serper.dev Google Search API provider.

    Requires SERPER_API_KEY. Supports web, image, and news search via
    Google's search index.
    """

    API_BASE = "https://google.serper.dev"

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.getenv("SERPER_API_KEY")
        self._http_client: httpx.AsyncClient | None = None

    @property
    def name(self) -> str:
        return "serper"

    def is_available(self) -> bool:
        return bool(self._api_key)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=30.0)
        return self._http_client

    def _require_api_key(self) -> str:
        if not self._api_key:
            raise ProviderError("Serper API key not configured", provider="serper")
        return self._api_key

    def _headers(self) -> dict[str, str]:
        return {
            "X-API-KEY": self._require_api_key(),
            "Content-Type": "application/json",
        }

    def _apply_filters_to_payload(self, payload: dict[str, object], filters: SearchFilters) -> None:
        """Apply recency (tbs) filter to a Serper payload dict."""
        if filters.start_date and filters.end_date:
            us_min = iso_to_us_date(filters.start_date)
            us_max = iso_to_us_date(filters.end_date)
            if us_min and us_max:
                payload["tbs"] = f"cdr:1,cd_min:{us_min},cd_max:{us_max}"
        elif filters.time_range:
            payload["tbs"] = "qdr:" + {"day": "d", "week": "w", "month": "m", "year": "y"}[filters.time_range]

    async def search_web(
        self, query: str, limit: int, correlation_id: str, filters: SearchFilters | None = None
    ) -> list[SearchResultItem]:
        client = await self._get_client()
        if filters and not filters.is_empty():
            query = domain_operator_query(query, filters.include_domains, filters.exclude_domains)
        payload: dict[str, object] = {"q": query, "num": limit}
        if filters and not filters.is_empty():
            self._apply_filters_to_payload(payload, filters)
        response = await client.post(
            f"{self.API_BASE}/search",
            headers=self._headers(),
            json=payload,
        )
        response.raise_for_status()

        data = response.json()
        results: list[SearchResultItem] = []

        for item in data.get("organic", [])[:limit]:
            results.append(
                SearchResultItem(
                    url=item.get("link", ""),
                    title=item.get("title", ""),
                    description=item.get("snippet", ""),
                    source_type=SearchSourceType.WEB,
                )
            )

        log_with_correlation(
            LOGGER,
            logging.DEBUG,
            f"Serper returned {len(results)} results",
            correlation_id=correlation_id,
            query=query,
        )
        return results

    async def search_images(
        self, query: str, limit: int, correlation_id: str, filters: SearchFilters | None = None
    ) -> list[SearchResultItem]:
        client = await self._get_client()
        payload: dict[str, object] = {"q": query, "num": limit}
        response = await client.post(
            f"{self.API_BASE}/images",
            headers=self._headers(),
            json=payload,
        )
        response.raise_for_status()

        data = response.json()
        results: list[SearchResultItem] = []

        for item in data.get("images", [])[:limit]:
            results.append(
                SearchResultItem(
                    url=item.get("link", ""),
                    image_url=item.get("imageUrl") or None,
                    title=item.get("title", ""),
                    description=item.get("source", ""),
                    source_type=SearchSourceType.IMAGES,
                    thumbnail=item.get("thumbnailUrl", ""),
                    image_width=item.get("imageWidth"),
                    image_height=item.get("imageHeight"),
                )
            )

        log_with_correlation(
            LOGGER,
            logging.DEBUG,
            f"Serper images returned {len(results)} results",
            correlation_id=correlation_id,
            query=query,
        )
        return results

    async def search_news(
        self, query: str, limit: int, correlation_id: str, filters: SearchFilters | None = None
    ) -> list[SearchResultItem]:
        client = await self._get_client()
        if filters and not filters.is_empty():
            query = domain_operator_query(query, filters.include_domains, filters.exclude_domains)
        payload: dict[str, object] = {"q": query, "num": limit}
        if filters and not filters.is_empty():
            self._apply_filters_to_payload(payload, filters)
        response = await client.post(
            f"{self.API_BASE}/news",
            headers=self._headers(),
            json=payload,
        )
        response.raise_for_status()

        data = response.json()
        results: list[SearchResultItem] = []

        for item in data.get("news", [])[:limit]:
            results.append(
                SearchResultItem(
                    url=item.get("link", ""),
                    title=item.get("title", ""),
                    description=item.get("snippet", ""),
                    source_type=SearchSourceType.NEWS,
                    published_at=item.get("date"),
                    source_name=item.get("source", ""),
                )
            )

        log_with_correlation(
            LOGGER,
            logging.DEBUG,
            f"Serper news returned {len(results)} results",
            correlation_id=correlation_id,
            query=query,
        )
        return results

    async def close(self) -> None:
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
