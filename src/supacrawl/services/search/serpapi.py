"""SerpAPI search provider implementation."""

import logging
import os

import httpx

from supacrawl.exceptions import ProviderError
from supacrawl.models import SearchFilters, SearchResultItem, SearchSourceType
from supacrawl.services.search.filters import domain_operator_query, iso_to_us_date
from supacrawl.utils import log_with_correlation

LOGGER = logging.getLogger(__name__)


class SerpAPIProvider:
    """SerpAPI Google Search provider.

    Requires SERPAPI_API_KEY. Supports web, image, and news search.
    """

    API_BASE = "https://serpapi.com"

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.getenv("SERPAPI_API_KEY")
        self._http_client: httpx.AsyncClient | None = None

    @property
    def name(self) -> str:
        return "serpapi"

    def is_available(self) -> bool:
        return bool(self._api_key)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=30.0)
        return self._http_client

    def _require_api_key(self) -> str:
        if not self._api_key:
            raise ProviderError("SerpAPI API key not configured", provider="serpapi")
        return self._api_key

    def _base_params(self, query: str) -> dict[str, str]:
        return {
            "api_key": self._require_api_key(),
            "q": query,
            "engine": "google",
        }

    def _apply_filters_to_params(self, params: dict[str, str], filters: SearchFilters) -> None:
        """Apply domain operators and tbs recency filter to SerpAPI params."""
        if filters.start_date and filters.end_date:
            us_min = iso_to_us_date(filters.start_date)
            us_max = iso_to_us_date(filters.end_date)
            if us_min and us_max:
                params["tbs"] = f"cdr:1,cd_min:{us_min},cd_max:{us_max}"
        elif filters.time_range:
            params["tbs"] = "qdr:" + {"day": "d", "week": "w", "month": "m", "year": "y"}[filters.time_range]

    async def search_web(
        self, query: str, limit: int, correlation_id: str, filters: SearchFilters | None = None
    ) -> list[SearchResultItem]:
        client = await self._get_client()
        if filters and not filters.is_empty():
            query = domain_operator_query(query, filters.include_domains, filters.exclude_domains)
        params = {**self._base_params(query), "num": str(limit)}
        if filters and not filters.is_empty():
            self._apply_filters_to_params(params, filters)
        response = await client.get(f"{self.API_BASE}/search.json", params=params)
        response.raise_for_status()

        data = response.json()
        results: list[SearchResultItem] = []

        for item in data.get("organic_results", [])[:limit]:
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
            f"SerpAPI returned {len(results)} results",
            correlation_id=correlation_id,
            query=query,
        )
        return results

    async def search_images(
        self, query: str, limit: int, correlation_id: str, filters: SearchFilters | None = None
    ) -> list[SearchResultItem]:
        client = await self._get_client()
        params = {**self._base_params(query), "engine": "google_images", "num": str(limit)}
        response = await client.get(f"{self.API_BASE}/search.json", params=params)
        response.raise_for_status()

        data = response.json()
        results: list[SearchResultItem] = []

        for item in data.get("images_results", [])[:limit]:
            results.append(
                SearchResultItem(
                    url=item.get("link", ""),
                    image_url=item.get("original") or None,
                    title=item.get("title", ""),
                    description=item.get("source", ""),
                    source_type=SearchSourceType.IMAGES,
                    thumbnail=item.get("thumbnail", ""),
                    image_width=item.get("original_width"),
                    image_height=item.get("original_height"),
                )
            )

        log_with_correlation(
            LOGGER,
            logging.DEBUG,
            f"SerpAPI images returned {len(results)} results",
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
        params = {**self._base_params(query), "engine": "google_news", "num": str(limit)}
        if filters and not filters.is_empty():
            self._apply_filters_to_params(params, filters)
        response = await client.get(f"{self.API_BASE}/search.json", params=params)
        response.raise_for_status()

        data = response.json()
        results: list[SearchResultItem] = []

        for item in data.get("news_results", [])[:limit]:
            results.append(
                SearchResultItem(
                    url=item.get("link", ""),
                    title=item.get("title", ""),
                    description=item.get("snippet", ""),
                    source_type=SearchSourceType.NEWS,
                    published_at=item.get("date"),
                    source_name=item.get("source", {}).get("name", ""),
                )
            )

        log_with_correlation(
            LOGGER,
            logging.DEBUG,
            f"SerpAPI news returned {len(results)} results",
            correlation_id=correlation_id,
            query=query,
        )
        return results

    async def close(self) -> None:
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
