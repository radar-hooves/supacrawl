"""Brave Search provider implementation.

Per-provider credit/quota capability:
  Brave: exposes X-RateLimit-Remaining and X-RateLimit-Limit on every response.
         We read and cache the remaining count; a low-credit warning fires when
         it drops below LOW_CREDIT_THRESHOLD (providers.py).
"""

import logging

import httpx

from supacrawl.exceptions import ProviderError
from supacrawl.models import SearchFilters, SearchResultItem, SearchSourceType
from supacrawl.services.search.filters import domain_operator_query
from supacrawl.utils import log_with_correlation

LOGGER = logging.getLogger(__name__)


class BraveProvider:
    """Brave Search API provider.

    Requires a BRAVE_API_KEY. Supports web, image, and news search.

    Credit tracking: reads X-RateLimit-Remaining from each response and stores
    it as ``remaining_credits``.  The ProviderChain picks this up after each
    successful call and surfaces it via supacrawl_health.
    """

    def __init__(self, api_key: str | None, http_client: httpx.AsyncClient | None = None) -> None:
        self._api_key = api_key
        self._owns_client = http_client is None
        self._http_client = http_client
        # Populated from X-RateLimit-Remaining on each response.
        # None until the first successful response; absent on header-less responses.
        self.remaining_credits: int | None = None

    @property
    def name(self) -> str:
        return "brave"

    def is_available(self) -> bool:
        return bool(self._api_key)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=30.0)
            self._owns_client = True
        return self._http_client

    def _require_api_key(self) -> str:
        if not self._api_key:
            raise ProviderError("Brave API key not configured", provider="brave")
        return self._api_key

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "X-Subscription-Token": self._require_api_key(),
        }

    def _update_quota(self, response: httpx.Response) -> None:
        """Cache remaining credits from X-RateLimit-Remaining response header.

        A missing header (or a response object without headers, e.g. in tests)
        leaves the cached value unchanged — it does not reset to None.
        """
        headers = getattr(response, "headers", None)
        if headers is None:
            return
        raw = headers.get("X-RateLimit-Remaining")
        if raw is not None:
            try:
                self.remaining_credits = int(raw)
            except ValueError:
                pass  # Malformed header — leave previous value intact

    def _apply_freshness(self, params: dict[str, str | int], filters: SearchFilters) -> None:
        """Add Brave freshness param for recency filtering."""
        if filters.start_date and filters.end_date:
            params["freshness"] = f"{filters.start_date}to{filters.end_date}"
        elif filters.time_range:
            params["freshness"] = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}[filters.time_range]

    async def search_web(
        self, query: str, limit: int, correlation_id: str, filters: SearchFilters | None = None
    ) -> list[SearchResultItem]:
        client = await self._get_client()
        if filters and not filters.is_empty():
            query = domain_operator_query(query, filters.include_domains, filters.exclude_domains)
        params: dict[str, str | int] = {"q": query, "count": limit}
        if filters and not filters.is_empty():
            self._apply_freshness(params, filters)
        response = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers=self._headers(),
            params=params,
        )
        response.raise_for_status()
        self._update_quota(response)

        data = response.json()
        results: list[SearchResultItem] = []
        for item in data.get("web", {}).get("results", [])[:limit]:
            results.append(
                SearchResultItem(
                    url=item.get("url", ""),
                    title=item.get("title", ""),
                    description=item.get("description", ""),
                    source_type=SearchSourceType.WEB,
                )
            )

        log_with_correlation(
            LOGGER,
            logging.DEBUG,
            f"Brave returned {len(results)} results",
            correlation_id=correlation_id,
            query=query,
        )
        return results

    async def search_images(
        self, query: str, limit: int, correlation_id: str, filters: SearchFilters | None = None
    ) -> list[SearchResultItem]:
        client = await self._get_client()
        params: dict[str, str | int] = {"q": query, "count": limit}
        response = await client.get(
            "https://api.search.brave.com/res/v1/images/search",
            headers=self._headers(),
            params=params,
        )
        response.raise_for_status()
        self._update_quota(response)

        data = response.json()
        results: list[SearchResultItem] = []
        for item in data.get("results", [])[:limit]:
            results.append(
                SearchResultItem(
                    url=item.get("url", ""),
                    title=item.get("title", ""),
                    description=item.get("source", ""),
                    source_type=SearchSourceType.IMAGES,
                    image_url=item.get("properties", {}).get("url") or None,
                    thumbnail=item.get("thumbnail", {}).get("src", ""),
                    image_width=item.get("properties", {}).get("width"),
                    image_height=item.get("properties", {}).get("height"),
                )
            )

        log_with_correlation(
            LOGGER,
            logging.DEBUG,
            f"Brave images returned {len(results)} results",
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
        params: dict[str, str | int] = {"q": query, "count": limit}
        if filters and not filters.is_empty():
            self._apply_freshness(params, filters)
        response = await client.get(
            "https://api.search.brave.com/res/v1/news/search",
            headers=self._headers(),
            params=params,
        )
        response.raise_for_status()
        self._update_quota(response)

        data = response.json()
        results: list[SearchResultItem] = []
        for item in data.get("results", [])[:limit]:
            results.append(
                SearchResultItem(
                    url=item.get("url", ""),
                    title=item.get("title", ""),
                    description=item.get("description", ""),
                    source_type=SearchSourceType.NEWS,
                    published_at=item.get("page_age"),
                    source_name=item.get("meta_url", {}).get("hostname", ""),
                )
            )

        log_with_correlation(
            LOGGER,
            logging.DEBUG,
            f"Brave news returned {len(results)} results",
            correlation_id=correlation_id,
            query=query,
        )
        return results

    async def close(self) -> None:
        if self._http_client and self._owns_client:
            await self._http_client.aclose()
            self._http_client = None
