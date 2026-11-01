"""The readers the benchmark can drive, by name.

Every reader satisfies ``ScraperProvider``; a reader with a search surface also
has a ``SearchProvider``. The rival readers the first figures were taken with
live in git history.
"""

from __future__ import annotations

from collections.abc import Callable

from supacrawl.benchmark.providers.base import ProviderOutput, ScraperProvider, SearchOutput, SearchProvider


def _supacrawl() -> ScraperProvider:
    from supacrawl.benchmark.providers.supacrawl import SupacrawlProvider

    return SupacrawlProvider()


def _supacrawl_search() -> SearchProvider:
    from supacrawl.benchmark.providers.supacrawl import SupacrawlSearchProvider

    return SupacrawlSearchProvider()


PROVIDERS: dict[str, Callable[[], ScraperProvider]] = {
    "supacrawl": _supacrawl,
}

SEARCH_PROVIDERS: dict[str, Callable[[], SearchProvider]] = {
    "supacrawl": _supacrawl_search,
}


def make_provider(name: str) -> ScraperProvider:
    """Construct the named reader."""
    try:
        factory = PROVIDERS[name]
    except KeyError:
        raise ValueError(f"Unknown provider {name!r}; choose from {', '.join(PROVIDERS)}") from None
    return factory()


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
