"""
Search service with multi-provider fallback.

This package provides web search with automatic fallback across multiple
search providers. Configure via SUPACRAWL_SEARCH_PROVIDERS env var
(comma-separated, ordered by priority).

Supported providers:
- brave: Brave Search API (requires BRAVE_API_KEY)
- tavily: Tavily Search API (requires TAVILY_API_KEY)
- serper: Serper.dev Google Search API (requires SERPER_API_KEY)
- serpapi: SerpAPI Google Search (requires SERPAPI_API_KEY)
- exa: Exa.ai neural search (requires EXA_API_KEY)
- duckduckgo: DuckDuckGo HTML scraping (deprecated, no key needed)
- searxng: SearXNG self-hosted metasearch (requires SEARXNG_URL; add
  SEARXNG_USERNAME and SEARXNG_PASSWORD when the instance is behind HTTP Basic)
"""

from supacrawl.services.search.providers import (
    ProviderChain,
    ProviderHealth,
    ProviderStatus,
    SearchProvider,
)
from supacrawl.services.search.registry import SUPPORTED_PROVIDERS, create_provider
from supacrawl.services.search.service import (
    _BROWSER_HEADERS,
    _PROVIDER_RATE_LIMITS,
    _SEARCH_USER_AGENT,
    ScrapeOptions,
    SearchService,
    SourceType,
    _RateLimiter,
)

__all__ = [
    "SUPPORTED_PROVIDERS",
    "_BROWSER_HEADERS",
    "_PROVIDER_RATE_LIMITS",
    "_SEARCH_USER_AGENT",
    "ProviderChain",
    "ProviderHealth",
    "ProviderStatus",
    "ScrapeOptions",
    "SearchProvider",
    "SearchService",
    "SourceType",
    "_RateLimiter",
    "create_provider",
]
