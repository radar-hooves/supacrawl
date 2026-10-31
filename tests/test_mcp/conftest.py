"""Pytest configuration and fixtures for Supacrawl MCP server tests."""

import importlib.util
from unittest.mock import AsyncMock, MagicMock

import pytest

# Every module here imports the MCP framework, which only the mcp-dev group installs.
if importlib.util.find_spec("mcp_common") is None:
    collect_ignore_glob = ["test_*.py"]

from supacrawl.services.registry import SupacrawlServices
from supacrawl.services.search.providers import ProviderChain


@pytest.fixture
def mock_browser_manager() -> MagicMock:
    """Create mock browser manager."""
    mock = MagicMock()
    mock.start = AsyncMock()
    mock.stop = AsyncMock()
    return mock


@pytest.fixture
def mock_scrape_service() -> MagicMock:
    """Create mock scrape service."""
    mock = MagicMock()
    mock.scrape = AsyncMock(
        return_value=MagicMock(
            success=True,
            data=MagicMock(
                markdown="# Test Page\n\nTest content",
                html="<h1>Test Page</h1><p>Test content</p>",
                metadata=MagicMock(title="Test Page", description="Test description"),
            ),
            model_dump=lambda: {
                "success": True,
                "data": {
                    "markdown": "# Test Page\n\nTest content",
                    "html": "<h1>Test Page</h1><p>Test content</p>",
                    "metadata": {"title": "Test Page", "description": "Test description"},
                },
            },
        )
    )
    return mock


@pytest.fixture
def mock_search_service() -> MagicMock:
    """Create mock search service.

    Returns several results, not one: a working backend clears the health
    probe's result-count floor (#161), so a single-result stub would make the
    shared fixture read as *degraded* the moment verify_search runs.
    """
    items = [
        {
            "url": f"https://example.com/{i}",
            "title": f"Example {i}",
            "description": f"Example description {i}",
            "source_type": "web",
        }
        for i in range(5)
    ]
    mock = MagicMock()
    mock.search = AsyncMock(
        return_value=MagicMock(
            success=True,
            data=[MagicMock(**item) for item in items],
            # Real provenance: a bare MagicMock reads truthy, which the health
            # probe would misread as an active fallback (#161).
            provider="brave",
            provider_fallback=False,
            model_dump=lambda: {"success": True, "data": [dict(item) for item in items]},
        )
    )
    mock.close = AsyncMock()
    # A real chain carrying one configured, available provider. An unconstrained
    # MagicMock here would make every attribute the health surface reads truthy,
    # which is how a mock quietly stops representing the thing it stands for (#158).
    mock.provider_chain = _configured_provider_chain("brave")
    return mock


def _configured_provider_chain(name: str) -> ProviderChain:
    """Build a real ProviderChain holding one configured, available provider."""

    class _AvailableProvider:
        def __init__(self, provider_name: str) -> None:
            self._name = provider_name

        @property
        def name(self) -> str:
            return self._name

        def is_available(self) -> bool:
            return True

        async def search_web(self, *args: object, **kwargs: object) -> list:
            return []

        async def search_images(self, *args: object, **kwargs: object) -> list:
            return []

        async def search_news(self, *args: object, **kwargs: object) -> list:
            return []

        async def close(self) -> None:
            return None

    chain = ProviderChain(configured_names=[name])
    chain.add(_AvailableProvider(name))  # type: ignore[arg-type]
    return chain


@pytest.fixture
def mock_crawl_service() -> MagicMock:
    """Create mock crawl service."""
    mock = MagicMock()

    async def crawl_generator(*args, **kwargs):
        """Yield mock crawl events."""
        yield MagicMock(
            type="page",
            data=MagicMock(
                markdown="# Page 1",
                metadata={"title": "Page 1"},
                model_dump=lambda: {"markdown": "# Page 1", "metadata": {"title": "Page 1"}},
            ),
        )

    mock.crawl = crawl_generator
    return mock


@pytest.fixture
def mock_map_service() -> MagicMock:
    """Create mock map service."""
    mock = MagicMock()
    mock.map_all = AsyncMock(
        return_value=MagicMock(
            success=True,
            links=["https://example.com/page1", "https://example.com/page2"],
            model_dump=lambda: {
                "success": True,
                "links": ["https://example.com/page1", "https://example.com/page2"],
            },
        )
    )
    return mock


@pytest.fixture
def mock_api_client(
    mock_browser_manager,
    mock_scrape_service,
    mock_search_service,
    mock_crawl_service,
    mock_map_service,
) -> SupacrawlServices:
    """Create mock SupacrawlServices with all services."""
    return SupacrawlServices(
        browser_manager=mock_browser_manager,
        scrape_service=mock_scrape_service,
        crawl_service=mock_crawl_service,
        map_service=mock_map_service,
        search_service=mock_search_service,
    )
