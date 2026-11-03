"""Provider fallback must be visible, not silent (#158).

These tests deliberately avoid mocking the thing under test. The defect in #158
was that a *control* (the health surface) reported ready while the service was
answering from a provider nobody configured — so asserting that health "checks
the provider" would reproduce the defect rather than catch it. Each test here
misconfigures the provider for real, drives the real code path, and asserts on
the observable outcome: the emitted log record, the returned health verdict, or
the provider named on the returned SearchResult.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from supacrawl.mcp.tools.health import _get_search_config, supacrawl_health
from supacrawl.models import SearchFilters, SearchResultItem
from supacrawl.services.search.providers import ProviderChain
from supacrawl.services.search.registry import build_provider_chain
from supacrawl.services.search.service import SearchService

pytestmark = pytest.mark.mcp

_SEARCH_KEY_ENVS = (
    "BRAVE_API_KEY",
    "TAVILY_API_KEY",
    "SERPER_API_KEY",
    "SERPAPI_API_KEY",
    "EXA_API_KEY",
    "SEARXNG_URL",
    "SEARXNG_USERNAME",
    "SEARXNG_PASSWORD",
)


@pytest.fixture
def no_search_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every search credential so the fallback path is the real path taken."""
    for env in _SEARCH_KEY_ENVS:
        monkeypatch.delenv(env, raising=False)
    monkeypatch.delenv("SUPACRAWL_SEARCH_PROVIDERS", raising=False)
    monkeypatch.delenv("SUPACRAWL_SEARCH_STRICT_PROVIDERS", raising=False)


class _StubProvider:
    """Minimal provider that answers successfully, so a chain can actually serve."""

    def __init__(self, name: str, *, available: bool = True) -> None:
        self._name = name
        self._available = available

    @property
    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return self._available

    async def search_web(
        self, query: str, limit: int, correlation_id: str, filters: SearchFilters | None = None
    ) -> list[SearchResultItem]:
        return [SearchResultItem(url="https://example.com/", title=f"{self._name} result")]

    async def search_images(
        self, query: str, limit: int, correlation_id: str, filters: SearchFilters | None = None
    ) -> list[SearchResultItem]:
        raise NotImplementedError

    async def search_news(
        self, query: str, limit: int, correlation_id: str, filters: SearchFilters | None = None
    ) -> list[SearchResultItem]:
        raise NotImplementedError

    async def close(self) -> None:
        return None


class _StubServices:
    """Stand-in for SupacrawlServices carrying a real SearchService."""

    def __init__(self, search_service: SearchService) -> None:
        self.search_service = search_service
        # No shared browser engine in these tests, so browser liveness cannot move
        # the top-line status either — the search verdict is the only variable.
        self.browser_manager = None

    def get_service_status(self) -> dict[str, bool]:
        # Every other component healthy: the search verdict is the only thing
        # that can move the top-line status in these tests.
        return {"scrape": True, "crawl": True, "map": True, "search": True}


# The fallback is announced when it is applied


class TestFallbackWarning:
    def test_brave_only_without_key_warns_naming_configured_and_effective(
        self, no_search_credentials: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="supacrawl.services.search.registry"):
            chain = build_provider_chain("brave")

        assert [p.name for p in chain.providers] == ["brave", "duckduckgo"]
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "falling back to an unconfigured provider emitted no warning at all"
        joined = " ".join(warnings)
        assert "brave" in joined, f"warning does not name the configured provider: {joined}"
        assert "duckduckgo" in joined, f"warning does not name the effective provider: {joined}"

    def test_configured_chain_emits_no_fallback_warning(
        self, no_search_credentials: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A correctly configured chain must not emit the new signal — else it is noise."""
        with caplog.at_level(logging.WARNING, logger="supacrawl.services.search.registry"):
            chain = build_provider_chain("brave", brave_api_key="test-key")

        assert [p.name for p in chain.providers] == ["brave"]
        assert chain.unconfigured_fallback_active is False
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_strict_mode_refuses_the_fallback_and_search_fails_loudly(self, no_search_credentials: None) -> None:
        """Opt-in strict mode: fail rather than answer from somewhere nobody chose."""
        chain = build_provider_chain("brave", strict=True)

        assert [p.name for p in chain.providers] == ["brave"]
        with pytest.raises(RuntimeError) as excinfo:
            import asyncio

            asyncio.run(chain.search("web", "anything", 1, "corr-1"))
        assert "BRAVE_API_KEY" in str(excinfo.value)


# The health verdict itself, driven rather than asserted about


class TestHealthVerdictUnderFallback:
    def test_health_reports_degraded_when_serving_from_unconfigured_provider(self, no_search_credentials: None) -> None:
        """Misconfigure for real, call health, observe the verdict it returns."""
        service = SearchService(providers=["brave"])
        try:
            # The service is genuinely serving from DuckDuckGo, not Brave.
            assert service.provider_chain.effective_provider == "duckduckgo"

            config = _get_search_config(service)

            assert config["configured_providers"] == ["brave"]
            assert config["effective_provider"] == "duckduckgo"
            assert config["provider_fallback_active"] is True
            assert config["status"] == "degraded", (
                "health reported an unqualified verdict while serving from an unconfigured provider"
            )
            warning = config.get("warning", "")
            assert "brave" in warning and "duckduckgo" in warning, (
                f"warning must name both configured and effective provider: {warning!r}"
            )
        finally:
            import asyncio

            asyncio.run(service.close())

    @pytest.mark.asyncio
    async def test_top_level_health_status_is_degraded_under_fallback(self, no_search_credentials: None) -> None:
        """The top line, not just the detail — the field an operator's monitor reads."""
        service = SearchService(providers=["brave"])
        try:
            result = await supacrawl_health(_StubServices(service), verify_search=False)  # type: ignore[arg-type]

            assert result["status"] == "degraded", (
                f"top-level health said {result['status']!r} while serving from an unconfigured provider"
            )
            assert result["components"]["search"]["provider_fallback_active"] is True
        finally:
            await service.close()

    @pytest.mark.asyncio
    async def test_correctly_configured_provider_reports_healthy(
        self, no_search_credentials: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The inverse: the new signal must stay quiet when nothing is wrong."""
        monkeypatch.setenv("BRAVE_API_KEY", "test-key")
        service = SearchService(providers=["brave"])
        try:
            result = await supacrawl_health(_StubServices(service), verify_search=False)  # type: ignore[arg-type]

            assert result["status"] == "healthy"
            search = result["components"]["search"]
            assert search["provider_fallback_active"] is False
            assert search["status"] == "ready"
            assert "warning" not in search
        finally:
            await service.close()


# A caller can tell which provider actually served the request


class TestServedProviderIsReported:
    @pytest.mark.asyncio
    async def test_chain_records_the_provider_that_answered(self) -> None:
        chain = ProviderChain(configured_names=["brave"])
        chain.add(_StubProvider("brave", available=False))
        chain.add(_StubProvider("duckduckgo"))

        results = await chain.search("web", "anything", 1, "corr-2")

        assert results
        assert chain.last_provider == "duckduckgo"
        assert chain.unconfigured_fallback_active is True

    @pytest.mark.asyncio
    async def test_search_result_names_the_serving_provider_and_flags_fallback(
        self, no_search_credentials: None
    ) -> None:
        service = SearchService(providers=["brave"])
        try:
            # Replace the chain's providers with stubs so no network is touched;
            # the configured intent recorded at build time is preserved.
            chain = service.provider_chain
            chain.providers.clear()
            chain.add(_StubProvider("brave", available=False))
            chain.add(_StubProvider("duckduckgo"))

            result = await service.search("anything", limit=1)

            assert result.success is True
            assert result.provider == "duckduckgo", "caller cannot tell which provider served the request"
            assert result.provider_fallback is True
        finally:
            await service.close()

    @pytest.mark.asyncio
    async def test_configured_provider_is_not_flagged_as_fallback(
        self, no_search_credentials: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BRAVE_API_KEY", "test-key")
        service = SearchService(providers=["brave"])
        try:
            chain = service.provider_chain
            chain.providers.clear()
            chain.add(_StubProvider("brave"))

            result: Any = await service.search("anything", limit=1)

            assert result.provider == "brave"
            assert result.provider_fallback is False
        finally:
            await service.close()
