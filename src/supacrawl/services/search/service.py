"""
Web search service for supacrawl.

Provides search functionality using a configurable chain of search providers
with automatic fallback. Supports web, image, and news search.

Provider chain can be configured via:
- SUPACRAWL_SEARCH_PROVIDERS env var (comma-separated, e.g. "brave,tavily,serper")
- Constructor parameter
- Defaults to Brave Search only (backwards compatible)
"""

import asyncio
import contextlib
import logging
import os
import time
import warnings
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import httpx

from supacrawl.exceptions import ProviderError, generate_correlation_id
from supacrawl.models import LocaleConfig, SearchFilters, SearchResult, SearchResultItem, SearchSourceType
from supacrawl.services.search.providers import ProviderChain
from supacrawl.services.search.registry import build_provider_chain
from supacrawl.utils import log_with_correlation

if TYPE_CHECKING:
    from supacrawl.services.scrape import ScrapeService
    from supacrawl.telemetry import MetricsSink

LOGGER = logging.getLogger(__name__)


def _describe_error(exc: BaseException) -> str:
    """Return a non-empty human-readable description of an exception.

    str(exc) is empty for some built-in exceptions (e.g. asyncio.TimeoutError()
    with no message), so we fall back to the class name to guarantee the caller
    always gets a useful string.
    """
    return str(exc).strip() or type(exc).__name__


def _unresponsive_engines_from_error(exc: BaseException) -> list[dict[str, str]]:
    """Pull SearXNG's unresponsive-engine list out of a ProviderError, if present.

    SearXNG raises a ProviderError carrying its failed engines when a query came
    back empty because those engines were down. Surfacing them on the returned
    SearchResult is what lets a caller with no fallback read WHY the set was
    empty, rather than an inscrutable success:false (#161).
    """
    if isinstance(exc, ProviderError):
        engines = exc.context.get("unresponsive_engines")
        if isinstance(engines, list):
            return engines
    return []


# Type alias for source types
type SourceType = Literal["web", "images", "news"]

# Browser-like User-Agent to avoid bot detection on search engines
_SEARCH_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# Complete browser header profile matching a real Chrome session.
_DEFAULT_ACCEPT_LANGUAGE = "en-US,en;q=0.9"

_BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": _SEARCH_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": _DEFAULT_ACCEPT_LANGUAGE,
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Ch-Ua": '"Chromium";v="131", "Google Chrome";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Connection": "keep-alive",
}

# Default rate limits per provider (requests per second)
_PROVIDER_RATE_LIMITS: dict[str, float] = {
    "duckduckgo": 1.0,  # Aggressive bot detection — keep it slow
    "brave": 10.0,  # Well under Brave's 50 QPS limit
    "tavily": 5.0,
    "serper": 10.0,
    "serpapi": 5.0,
    "exa": 5.0,
}

# Maximum time a request will wait in the rate limit queue before failing
_RATE_LIMIT_QUEUE_TIMEOUT = 30.0  # seconds


class _RateLimiter:
    """Async rate limiter using token bucket with concurrency control."""

    def __init__(
        self,
        requests_per_second: float,
        burst: int = 3,
        queue_timeout: float = _RATE_LIMIT_QUEUE_TIMEOUT,
    ):
        self._interval = 1.0 / requests_per_second if requests_per_second > 0 else 0
        self._semaphore = asyncio.Semaphore(burst)
        self._lock = asyncio.Lock()
        self._last_request_time = 0.0
        self.requests_per_second = requests_per_second
        self.burst = burst
        self.queue_timeout = queue_timeout

    async def acquire(self, timeout: float | None = None) -> None:
        effective_timeout = timeout if timeout is not None else self.queue_timeout
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=effective_timeout)
        except TimeoutError:
            raise TimeoutError(
                f"Search rate limit queue timeout ({effective_timeout}s). Too many concurrent search requests."
            ) from None

        try:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_request_time
                if elapsed < self._interval:
                    wait_time = self._interval - elapsed
                    await asyncio.sleep(wait_time)
                self._last_request_time = time.monotonic()
        except BaseException:
            self._semaphore.release()
            raise

    def release(self) -> None:
        self._semaphore.release()

    async def __aenter__(self) -> "_RateLimiter":
        await self.acquire()
        return self

    async def __aexit__(self, exc_type: type | None, exc_val: BaseException | None, exc_tb: object) -> bool:
        self.release()
        return False


@dataclass
class ScrapeOptions:
    """Options for scraping search results."""

    formats: list[Literal["markdown", "html"]] | None = None
    only_main_content: bool = True


class SearchService:
    """Web search with multi-provider fallback and optional content scraping.

    Supports configuring multiple search providers in priority order.
    On failure (quota exhausted, rate limited, CAPTCHA), automatically
    falls back to the next provider.

    Configuration:
        # Comma-separated provider priority order
        SUPACRAWL_SEARCH_PROVIDERS=brave,tavily,serper,duckduckgo

        # Each provider's API key
        BRAVE_API_KEY=xxx
        TAVILY_API_KEY=xxx
        SERPER_API_KEY=xxx

    Backwards compatible: if only ``provider`` is set (no chain), behaves
    exactly like the old single-provider mode.
    """

    # How many recent CALLER searches feed the recent-traffic health signal. A
    # synthetic probe is deliberately excluded (record_recent=False) so this
    # reflects real traffic, not a health check answering its own question.
    RECENT_WINDOW = 5

    def __init__(
        self,
        scrape_service: "ScrapeService | None" = None,
        provider: Literal["duckduckgo", "brave"] | None = None,
        providers: str | list[str] | None = None,
        brave_api_key: str | None = None,
        searxng_username: str | None = None,
        searxng_password: str | None = None,
        rate_limit: float | None = None,
        locale_config: LocaleConfig | None = None,
        telemetry: "MetricsSink | None" = None,
    ):
        """Initialise search service.

        Args:
            scrape_service: Optional ScrapeService for scraping results.
            provider: Legacy single provider name. If set without ``providers``,
                creates a single-provider chain (backwards compatible).
            providers: Comma-separated or list of provider names for the chain.
                Takes precedence over ``provider``. Also reads from
                SUPACRAWL_SEARCH_PROVIDERS env var.
            brave_api_key: API key for Brave Search. Also read from
                BRAVE_API_KEY environment variable.
            searxng_username: SearXNG HTTP Basic username supplied by the caller
                rather than the environment — the shape a secrets broker vends
                into, so the credential never has to be an env var. Beats
                SEARXNG_USERNAME and any userinfo left in SEARXNG_URL.
            searxng_password: The matching password, same precedence.
            rate_limit: Requests per second (overrides provider default).
            locale_config: Optional locale configuration for Accept-Language.
            telemetry: Optional field telemetry sink (#137). When provided, one
                event per search (provider, result count, success, latency) is
                appended to the local metrics log. None == no telemetry.
        """
        self._scrape_service = scrape_service
        self._brave_api_key = brave_api_key or os.getenv("BRAVE_API_KEY")
        self._locale_config = locale_config
        self._telemetry = telemetry

        # Build the shared HTTP client eagerly so providers that accept one
        # (DuckDuckGo, SearXNG) receive browser-realistic headers from the start.
        self._http_client: httpx.AsyncClient = httpx.AsyncClient(
            timeout=30.0,
            headers=self._build_headers(),
        )

        # Build the provider chain, sharing the HTTP client so all providers
        # that accept it make requests with the same browser header profile.
        if providers is not None:
            # Explicit multi-provider chain
            self._chain = build_provider_chain(
                providers,
                brave_api_key=self._brave_api_key,
                searxng_username=searxng_username,
                searxng_password=searxng_password,
                http_client=self._http_client,
            )
        elif provider is not None:
            # Legacy single-provider mode
            if provider == "duckduckgo":
                warnings.warn(
                    "DuckDuckGo search is deprecated due to unreliable bot detection. "
                    "Switch to Brave Search (set BRAVE_API_KEY) for reliable results.",
                    DeprecationWarning,
                    stacklevel=2,
                )
            self._chain = build_provider_chain(
                [provider],
                brave_api_key=self._brave_api_key,
                searxng_username=searxng_username,
                searxng_password=searxng_password,
                http_client=self._http_client,
            )
        else:
            # Auto-detect from env or defaults
            self._chain = build_provider_chain(
                brave_api_key=self._brave_api_key,
                searxng_username=searxng_username,
                searxng_password=searxng_password,
                http_client=self._http_client,
            )

        # Resolve effective provider name (for backwards compat)
        active = self._chain.active_providers
        if active:
            self._provider = active[0].name
        elif self._chain.providers:
            self._provider = self._chain.providers[0].name
        else:
            self._provider = "none"

        # Resolve rate limit
        if rate_limit is not None:
            effective_rate = rate_limit
        else:
            env_rate = os.getenv("SUPACRAWL_SEARCH_RATE_LIMIT")
            if env_rate:
                try:
                    effective_rate = float(env_rate)
                except ValueError:
                    LOGGER.warning(f"Invalid SUPACRAWL_SEARCH_RATE_LIMIT={env_rate!r}, using provider default")
                    effective_rate = _PROVIDER_RATE_LIMITS.get(self._provider, 10.0)
            else:
                effective_rate = _PROVIDER_RATE_LIMITS.get(self._provider, 10.0)

        self._rate_limiter = _RateLimiter(effective_rate)

        # Rolling record of whether recent CALLER searches came back empty. A
        # long-lived server accumulates real traffic here, so health can tell
        # "the last N queries all returned nothing" — a failure a per-provider
        # consecutive_failures count misses entirely, because a zero-result
        # success was never counted as a failure anywhere (#161).
        self._recent_empty: deque[bool] = deque(maxlen=self.RECENT_WINDOW)

        LOGGER.debug(
            f"Search rate limit: {effective_rate} req/s (primary={self._provider}, burst={self._rate_limiter.burst})"
        )

    @property
    def provider_chain(self) -> ProviderChain:
        """Access the provider chain (for health reporting)."""
        return self._chain

    @property
    def recent_search_health(self) -> dict[str, int | bool]:
        """Snapshot of how recent caller searches fared, for the health surface.

        ``all_recent_empty`` is True only once a full window of caller searches
        has been seen and every one came back empty — the "callers are failing
        continuously" state that a synthetic probe and a per-provider failure
        count both miss. One non-empty result rolls out of the window and clears
        it, so a single genuine no-match query never trips it.
        """
        total = len(self._recent_empty)
        empty = sum(self._recent_empty)
        return {
            "window": self.RECENT_WINDOW,
            "recent_searches": total,
            "recent_empty": empty,
            "all_recent_empty": total >= self.RECENT_WINDOW and empty == total,
        }

    def _has_keyed_provider(self) -> bool:
        """Whether any *usable* provider is backed by an API key.

        DuckDuckGo is the only keyless provider and is used as a last-resort
        fallback; if it is the only thing available, the chain is effectively
        unconfigured and an empty result should fail loudly rather than silently.
        Uses ``active_providers`` so a keyed provider that is circuit-broken
        (cooled down after repeated failures) does not mask the keyless case — an
        empty result behind a dead key should also fail loudly.
        """
        return any(p.name != "duckduckgo" for p in self._chain.active_providers)

    def _build_headers(self) -> dict[str, str]:
        """Build browser-realistic HTTP headers.

        Called at init time (before the client is created) to populate the
        shared httpx.AsyncClient that is passed to every provider.
        """
        headers = dict(_BROWSER_HEADERS)

        locale = self._locale_config
        if locale is None:
            env_locale = os.getenv("SUPACRAWL_LOCALE")
            if env_locale:
                locale = LocaleConfig(language=env_locale)

        if locale is not None:
            headers["Accept-Language"] = locale.get_accept_language_header()

        return headers

    async def _get_client(self) -> httpx.AsyncClient:
        """Return the shared HTTP client (created at init with browser headers)."""
        return self._http_client

    async def close(self) -> None:
        """Close HTTP client and all providers."""
        await self._http_client.aclose()
        await self._chain.close()

    async def __aenter__(self) -> "SearchService":
        return self

    async def __aexit__(self, exc_type: type | None, exc_val: BaseException | None, exc_tb: object) -> bool:
        await self.close()
        return False

    async def search(
        self,
        query: str,
        limit: int = 5,
        sources: list[SourceType] | None = None,
        scrape_options: ScrapeOptions | None = None,
        progress_callback: Callable[[int, int, str], Awaitable[None]] | None = None,
        filters: SearchFilters | None = None,
        record_recent: bool = True,
    ) -> SearchResult:
        """Search the web using the provider chain with automatic fallback.

        Args:
            query: Search query string.
            limit: Maximum number of results (1-10) per source type.
            sources: List of source types to search. Defaults to ["web"].
            scrape_options: Options for scraping result pages (web only).
            filters: Optional recency/topic/domain filters, mapped onto each
                provider's native API (or synthesised as ``site:`` query
                operators where a provider has no native support).
            record_recent: Whether this search feeds the recent-traffic health
                signal (``recent_search_health``). Default True for caller
                traffic; the synthetic health probe passes False so a health
                check never answers its own question.

        Returns:
            SearchResult with search results and optional scraped content.
        """
        correlation_id = generate_correlation_id()
        limit = max(1, min(limit, 10))

        if sources is None:
            sources = ["web"]

        telemetry_started = time.monotonic() if self._telemetry is not None else None

        try:
            log_with_correlation(
                LOGGER,
                logging.DEBUG,
                f"Searching with provider chain ({len(self._chain.providers)} providers)",
                correlation_id=correlation_id,
                query=query,
                limit=limit,
                sources=sources,
            )

            all_results: list[SearchResultItem] = []
            # Capture which provider actually answered for THIS request as a local,
            # the instant the chain returns. self._chain is a per-server singleton
            # shared across concurrent searches, so reading its last_provider after
            # the scrape await below would race a concurrent request and mislabel
            # this response — even masking a real fallback leak (#161).
            served_by: str | None = None

            for source in sources:
                try:
                    async with self._rate_limiter:
                        results = await self._chain.search(
                            source=source,
                            query=query,
                            limit=limit,
                            correlation_id=correlation_id,
                            filters=filters,
                        )
                except TimeoutError as e:
                    msg = (
                        _describe_error(e)
                        if str(e).strip()
                        else "Search rate-limit queue timed out before a provider slot was free"
                    )
                    log_with_correlation(
                        LOGGER,
                        logging.WARNING,
                        f"Rate limit queue timeout for source={source}",
                        correlation_id=correlation_id,
                    )
                    return self._finalize_search(
                        SearchResult(success=False, data=[], error=msg), query, telemetry_started, record_recent
                    )

                # No await between the chain returning and this read, so the value
                # is this request's own; the last source that answered wins.
                served_by = self._chain.last_provider
                all_results.extend(results)

            # Optionally scrape results (web results only)
            if scrape_options and self._scrape_service:
                web_results = [r for r in all_results if r.source_type == SearchSourceType.WEB]
                other_results = [r for r in all_results if r.source_type != SearchSourceType.WEB]
                if web_results:
                    web_results = await self._scrape_results(
                        web_results, scrape_options, correlation_id, progress_callback
                    )
                all_results = web_results + other_results

            # Out-of-the-box honesty (#132): a search that returns nothing while no
            # API-keyed provider is available is the keyless-DuckDuckGo bot-wall
            # case, not a genuine no-match. Fail loudly with the remedy rather than
            # handing back a silent empty list that an agent cannot interpret.
            if not all_results and not self._has_keyed_provider():
                loud = SearchResult(
                    success=False,
                    data=[],
                    error=(
                        "Search returned no results and no search-provider API key is configured. "
                        "Out of the box supacrawl falls back to DuckDuckGo, which is aggressively bot-walled "
                        "and frequently returns nothing. Set BRAVE_API_KEY (free tier: "
                        "https://brave.com/search/api/) — see .env.example — for reliable search."
                    ),
                )
                return self._finalize_search(loud, query, telemetry_started, record_recent)

            log_with_correlation(
                LOGGER,
                logging.INFO,
                f"Search completed with {len(all_results)} results",
                correlation_id=correlation_id,
            )

            return self._finalize_search(
                SearchResult(
                    success=True,
                    data=all_results,
                    provider=served_by,
                    provider_fallback=bool(
                        served_by and self._chain.configured_names and served_by not in self._chain.configured_names
                    ),
                ),
                query,
                telemetry_started,
                record_recent,
            )

        except Exception as e:
            msg = _describe_error(e)
            log_with_correlation(
                LOGGER,
                logging.ERROR,
                f"Search failed: {msg}",
                correlation_id=correlation_id,
                error=msg,
            )
            return self._finalize_search(
                SearchResult(
                    success=False,
                    data=[],
                    error=msg,
                    unresponsive_engines=_unresponsive_engines_from_error(e),
                ),
                query,
                telemetry_started,
                record_recent,
            )

    def _finalize_search(
        self, result: SearchResult, query: str, started: float | None, record_recent: bool = True
    ) -> SearchResult:
        """Record a completed search (recent-traffic window + telemetry), returning it unchanged.

        The recent-traffic window feeds ``recent_search_health``; it counts only
        caller traffic (``record_recent``), never the synthetic probe. Telemetry
        is a no-op when disabled (``started`` is None) and its errors never affect
        the search result.

        The ``all_recent_empty`` flag is copied onto the result AFTER the window
        update so a caller can tell a genuine no-match from a backend that has
        stopped answering, reading it off the response instead of having to poll
        the health surface (extends #161).
        """
        if record_recent:
            self._recent_empty.append(not result.data)
        # Gate on ``not result.data`` so the flag can never contradict the result it
        # rides on: it means "this empty is part of a sustained empty run", so a
        # response that DID return data always reports False, even if a prior
        # record_recent=False call left the window stale.
        result.all_recent_empty = not result.data and bool(self.recent_search_health["all_recent_empty"])
        if started is not None and self._telemetry is not None:
            latency_ms = int((time.monotonic() - started) * 1000)
            self._telemetry.record_search(
                query=query,
                provider=self._provider,
                result_count=len(result.data),
                success=result.success,
                latency_ms=latency_ms,
            )
        return result

    async def _scrape_results(
        self,
        results: list[SearchResultItem],
        options: ScrapeOptions,
        correlation_id: str,
        progress_callback: Callable[[int, int, str], Awaitable[None]] | None = None,
    ) -> list[SearchResultItem]:
        """Scrape content from search result URLs."""
        if not self._scrape_service:
            return results

        scrape_service = self._scrape_service
        formats = options.formats or ["markdown"]

        async def scrape_one(item: SearchResultItem) -> SearchResultItem:
            try:
                result = await scrape_service.scrape(
                    url=item.url,
                    formats=list(formats),
                    only_main_content=options.only_main_content,
                )
                if result.success and result.data:
                    item.markdown = result.data.markdown
                    item.html = result.data.html
                    item.metadata = result.data.metadata
            except Exception as e:
                log_with_correlation(
                    LOGGER,
                    logging.WARNING,
                    f"Failed to scrape {item.url}: {e}",
                    correlation_id=correlation_id,
                )
            return item

        total = len(results)

        # Create indexed tasks to preserve original ordering
        async def scrape_indexed(idx: int, item: SearchResultItem) -> tuple[int, SearchResultItem]:
            return idx, await scrape_one(item)

        indexed_results: dict[int, SearchResultItem] = {}
        tasks = [scrape_indexed(i, r) for i, r in enumerate(results)]

        for completed, coro in enumerate(asyncio.as_completed(tasks), start=1):
            idx, result_item = await coro
            indexed_results[idx] = result_item
            if progress_callback:
                with contextlib.suppress(Exception):  # Don't let callback errors break scraping
                    await progress_callback(completed, total, result_item.url)

        return [indexed_results[i] for i in range(total)]
