"""The readers the benchmark can drive, by name.

Every reader satisfies ``ScraperProvider``; a reader with a search surface also
has a ``SearchProvider``. Third-party readers import their packages lazily, so
naming one that is not installed fails at construction with the install hint,
and nothing else in the benchmark needs them.
"""

from __future__ import annotations

from collections.abc import Callable

from supacrawl.benchmark.providers.base import ProviderOutput, ScraperProvider, SearchOutput, SearchProvider


def _supacrawl() -> ScraperProvider:
    from supacrawl.benchmark.providers.supacrawl import SupacrawlProvider

    return SupacrawlProvider()


def _trafilatura() -> ScraperProvider:
    from supacrawl.benchmark.providers.trafilatura import TrafilaturaProvider

    return TrafilaturaProvider()


def _jina() -> ScraperProvider:
    from supacrawl.benchmark.providers.jina import JinaProvider

    return JinaProvider()


def _crawl4ai() -> ScraperProvider:
    from supacrawl.benchmark.providers.crawl4ai import Crawl4AIProvider

    return Crawl4AIProvider()


def _firecrawl() -> ScraperProvider:
    from supacrawl.benchmark.providers.firecrawl import FirecrawlProvider

    return FirecrawlProvider()


def _supacrawl_search() -> SearchProvider:
    from supacrawl.benchmark.providers.supacrawl import SupacrawlSearchProvider

    return SupacrawlSearchProvider()


def _jina_search() -> SearchProvider:
    from supacrawl.benchmark.providers.jina import JinaSearchProvider

    return JinaSearchProvider()


PROVIDERS: dict[str, Callable[[], ScraperProvider]] = {
    "supacrawl": _supacrawl,
    "trafilatura": _trafilatura,
    "jina": _jina,
    "crawl4ai": _crawl4ai,
    "firecrawl": _firecrawl,
}

SEARCH_PROVIDERS: dict[str, Callable[[], SearchProvider]] = {
    "supacrawl": _supacrawl_search,
    "jina": _jina_search,
}

INSTALL_HINTS = {
    "trafilatura": "uv sync --group bench  (or: pip install trafilatura)",
    "crawl4ai": "pip install crawl4ai && crawl4ai-setup",
}


def make_provider(name: str) -> ScraperProvider:
    """Construct the named reader; an uninstalled third-party reader names what to install."""
    try:
        factory = PROVIDERS[name]
    except KeyError:
        raise ValueError(f"Unknown provider {name!r}; choose from {', '.join(PROVIDERS)}") from None
    try:
        return factory()
    except ImportError as exc:
        hint = INSTALL_HINTS.get(name, f"pip install {name}")
        raise RuntimeError(f"Provider {name!r} is not installed ({exc}). Install it with: {hint}") from exc


def make_search_provider(name: str) -> SearchProvider | None:
    """The reader's search surface, or None when it has none."""
    factory = SEARCH_PROVIDERS.get(name)
    return factory() if factory else None


__all__ = [
    "PROVIDERS",
    "SEARCH_PROVIDERS",
    "ProviderOutput",
    "ScraperProvider",
    "SearchOutput",
    "SearchProvider",
    "make_provider",
    "make_search_provider",
]
