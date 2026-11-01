"""DuckDuckGo search provider implementation (deprecated)."""

import logging
import re
from urllib.parse import parse_qs, urlparse

import httpx
from bs4 import BeautifulSoup

from supacrawl.exceptions import ProviderError
from supacrawl.models import SearchFilters, SearchResultItem, SearchSourceType
from supacrawl.services.search.filters import domain_operator_query
from supacrawl.utils import log_with_correlation

LOGGER = logging.getLogger(__name__)


class DuckDuckGoProvider:
    """DuckDuckGo HTML scraping provider (deprecated).

    No API key required but unreliable due to aggressive bot detection.
    """

    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        self._owns_client = http_client is None
        self._http_client = http_client

    @property
    def name(self) -> str:
        return "duckduckgo"

    def is_available(self) -> bool:
        return True  # No API key needed

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=30.0)
            self._owns_client = True
        return self._http_client

    async def search_web(
        self, query: str, limit: int, correlation_id: str, filters: SearchFilters | None = None
    ) -> list[SearchResultItem]:
        client = await self._get_client()
        if filters and not filters.is_empty():
            query = domain_operator_query(query, filters.include_domains, filters.exclude_domains)
        params: dict[str, str] = {"q": query, "kl": "au-en"}
        if filters and filters.time_range and filters.time_range in ("day", "week", "month"):
            params["df"] = {"day": "d", "week": "w", "month": "m"}[filters.time_range]
        response = await client.get("https://lite.duckduckgo.com/lite/", params=params)
        response.raise_for_status()

        html = response.text

        if response.status_code == 202 or "anomaly-modal" in html:
            raise ProviderError(
                "DuckDuckGo returned a CAPTCHA challenge (bot detection). Search results are unavailable.",
                provider="duckduckgo",
                correlation_id=correlation_id,
            )

        results = self._parse_ddg_results(html, limit)
        log_with_correlation(
            LOGGER,
            logging.DEBUG,
            f"DuckDuckGo returned {len(results)} results",
            correlation_id=correlation_id,
            query=query,
        )
        return results

    def _parse_ddg_results(self, html: str, limit: int) -> list[SearchResultItem]:
        soup = BeautifulSoup(html, "html.parser")
        results: list[SearchResultItem] = []

        for link_cell in soup.select("a.result-link"):
            if len(results) >= limit:
                break

            href_attr = link_cell.get("href", "")
            href = href_attr[0] if isinstance(href_attr, list) else href_attr
            if not href or not isinstance(href, str):
                continue

            if href.startswith("//duckduckgo.com"):
                parsed = urlparse(href)
                params = parse_qs(parsed.query)
                if "uddg" in params:
                    href = params["uddg"][0]
                else:
                    continue

            title = link_cell.get_text(strip=True)

            description = ""
            parent_tr = link_cell.find_parent("tr")
            if parent_tr:
                next_tr = parent_tr.find_next_sibling("tr")
                if next_tr:
                    snippet_td = next_tr.find("td", class_="result-snippet")
                    if snippet_td:
                        description = snippet_td.get_text(strip=True)

            if href and title:
                results.append(
                    SearchResultItem(
                        url=str(href),
                        title=title,
                        description=description,
                        source_type=SearchSourceType.WEB,
                    )
                )

        return results

    async def search_images(
        self, query: str, limit: int, correlation_id: str, filters: SearchFilters | None = None
    ) -> list[SearchResultItem]:
        client = await self._get_client()

        token_response = await client.get("https://duckduckgo.com/", params={"q": query})
        vqd_match = re.search(r'vqd=["\']([^"\']+)["\']', token_response.text)
        if not vqd_match:
            vqd_match = re.search(r"vqd=([a-zA-Z0-9_-]+)", token_response.text)

        if not vqd_match:
            log_with_correlation(
                LOGGER,
                logging.WARNING,
                "Could not extract DuckDuckGo vqd token for image search",
                correlation_id=correlation_id,
            )
            return []

        vqd = vqd_match.group(1)
        params = {"q": query, "vqd": vqd, "l": "au-en", "o": "json", "f": ",,,", "p": "1"}
        response = await client.get("https://duckduckgo.com/i.js", params=params)

        results: list[SearchResultItem] = []
        try:
            data = response.json()
            for item in data.get("results", [])[:limit]:
                image_url = item.get("image", "")
                if not image_url:
                    continue
                results.append(
                    SearchResultItem(
                        url=item.get("url", ""),
                        image_url=image_url,
                        title=item.get("title", ""),
                        description=item.get("source", ""),
                        source_type=SearchSourceType.IMAGES,
                        thumbnail=item.get("thumbnail", ""),
                        image_width=item.get("width"),
                        image_height=item.get("height"),
                    )
                )
        except Exception as e:
            log_with_correlation(
                LOGGER,
                logging.WARNING,
                f"Failed to parse DuckDuckGo image results: {e}",
                correlation_id=correlation_id,
            )

        log_with_correlation(
            LOGGER,
            logging.DEBUG,
            f"DuckDuckGo images returned {len(results)} results",
            correlation_id=correlation_id,
            query=query,
        )
        return results

    async def search_news(
        self, query: str, limit: int, correlation_id: str, filters: SearchFilters | None = None
    ) -> list[SearchResultItem]:
        client = await self._get_client()

        token_response = await client.get("https://duckduckgo.com/", params={"q": query})
        vqd_match = re.search(r'vqd=["\']([^"\']+)["\']', token_response.text)
        if not vqd_match:
            vqd_match = re.search(r"vqd=([a-zA-Z0-9_-]+)", token_response.text)

        if not vqd_match:
            log_with_correlation(
                LOGGER,
                logging.WARNING,
                "Could not extract DuckDuckGo vqd token for news search",
                correlation_id=correlation_id,
            )
            return []

        vqd = vqd_match.group(1)
        # Apply domain and recency filters consistently with search_web.
        news_query = query
        df = ""
        if filters and not filters.is_empty():
            news_query = domain_operator_query(query, filters.include_domains, filters.exclude_domains)
            if filters.time_range in ("day", "week", "month"):
                df = {"day": "d", "week": "w", "month": "m"}[filters.time_range]
        params = {"q": news_query, "vqd": vqd, "l": "au-en", "o": "json", "noamp": "1", "df": df}
        response = await client.get("https://duckduckgo.com/news.js", params=params)

        results: list[SearchResultItem] = []
        try:
            data = response.json()
            for item in data.get("results", [])[:limit]:
                url = item.get("url", "")
                if not url:
                    continue

                published_at = None
                if "date" in item:
                    try:
                        from datetime import datetime, timezone

                        timestamp = item["date"]
                        if isinstance(timestamp, int):
                            dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
                            published_at = dt.isoformat()
                    except Exception:
                        pass

                results.append(
                    SearchResultItem(
                        url=url,
                        title=item.get("title", ""),
                        description=item.get("excerpt", item.get("body", "")),
                        source_type=SearchSourceType.NEWS,
                        published_at=published_at,
                        source_name=item.get("source", ""),
                    )
                )
        except Exception as e:
            log_with_correlation(
                LOGGER,
                logging.WARNING,
                f"Failed to parse DuckDuckGo news results: {e}",
                correlation_id=correlation_id,
            )

        log_with_correlation(
            LOGGER,
            logging.DEBUG,
            f"DuckDuckGo news returned {len(results)} results",
            correlation_id=correlation_id,
            query=query,
        )
        return results

    async def close(self) -> None:
        if self._http_client and self._owns_client:
            await self._http_client.aclose()
            self._http_client = None
