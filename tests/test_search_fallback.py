"""The opt-in DuckDuckGo fallback behind a failing configured backend (#161).

A self-hosted SearXNG is chosen to keep queries in-house, so this fallback is
deliberately OFF by default — a SearXNG failure surfaces as a loud typed error
(see test_search_upstream_failure.py), never a silent leak to a public engine.
With SUPACRAWL_SEARCH_PUBLIC_FALLBACK on, an operator who would rather have
degraded-but-answering search gets DuckDuckGo behind the configured backend.

These drive the real registry, service, and health surfaces: the fallback
engages on a SearXNG whose engines are all down, the caller is told a fallback
answered, and health reports degraded rather than pretending the configured
backend is fine. Everything except the one health-tool assertion is a
services-layer test that runs in CI without the mcp extra; that assertion defers
its ``supacrawl.mcp`` import and is marked ``mcp`` (the test_search_quota.py
convention) so a CI job without the extra deselects it rather than crashing on
collection.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from supacrawl.models import SearchResultItem, SearchSourceType
from supacrawl.services.search.duckduckgo import DuckDuckGoProvider
from supacrawl.services.search.providers import ProviderChain
from supacrawl.services.search.registry import build_provider_chain
from supacrawl.services.search.searxng import SearXNGProvider
from supacrawl.services.search.service import ScrapeOptions, SearchService

_BROKEN_SEARXNG = {
    "results": [],
    "unresponsive_engines": [["brave", "timeout"], ["wikibooks", "error"], ["wikinews", "error"]],
}

# Minimal DuckDuckGo-lite HTML the DuckDuckGoProvider knows how to parse.
_DDG_HTML = """
<table>
  <tr><td><a class="result-link" href="https://example.org/ddg1">DDG One</a></td></tr>
  <tr><td class="result-snippet">snippet one</td></tr>
  <tr><td><a class="result-link" href="https://example.org/ddg2">DDG Two</a></td></tr>
  <tr><td class="result-snippet">snippet two</td></tr>
  <tr><td><a class="result-link" href="https://example.org/ddg3">DDG Three</a></td></tr>
  <tr><td class="result-snippet">snippet three</td></tr>
</table>
"""


def _routing_client() -> httpx.AsyncClient:
    """One client: SearXNG's host is down, DuckDuckGo's answers."""

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if "searxng" in host:
            return httpx.Response(200, json=_BROKEN_SEARXNG)
        if "duckduckgo" in host:
            return httpx.Response(200, text=_DDG_HTML, headers={"content-type": "text/html"})
        return httpx.Response(500, text="unexpected host")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _searxng_service_with_public_fallback() -> tuple[SearchService, httpx.AsyncClient]:
    with patch.dict(
        os.environ,
        {
            "SEARXNG_URL": "http://searxng.invalid",
            "SUPACRAWL_SEARCH_PROVIDERS": "searxng",
            "SUPACRAWL_SEARCH_PUBLIC_FALLBACK": "1",
            "SUPACRAWL_SEARCH_STRICT_PROVIDERS": "",
        },
    ):
        service = SearchService(providers=["searxng"])

    client = _routing_client()
    for provider in service.provider_chain.providers:
        if isinstance(provider, (SearXNGProvider, DuckDuckGoProvider)):
            provider._http_client = client
    return service, client


class _StubServices:
    def __init__(self, search_service: SearchService) -> None:
        self.search_service = search_service
        self.browser_manager = None

    def get_service_status(self) -> dict[str, bool]:
        return {"scrape": True, "crawl": True, "map": True, "search": True}


# ---------------------------------------------------------------------------
# Registry: when the fallback is appended, and when it is refused
# ---------------------------------------------------------------------------


class TestPublicFallbackAppend:
    def _names(self, **env: str) -> list[str]:
        with patch.dict(os.environ, {"SEARXNG_URL": "http://searxng.invalid", **env}, clear=False):
            chain = build_provider_chain("searxng")
        return [p.name for p in chain.providers]

    def test_off_by_default_searxng_serves_alone(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SUPACRAWL_SEARCH_PUBLIC_FALLBACK", None)
            assert self._names() == ["searxng"], "a self-hosted backend must not gain a silent public fallback"

    def test_opt_in_appends_duckduckgo_behind_searxng(self) -> None:
        assert self._names(SUPACRAWL_SEARCH_PUBLIC_FALLBACK="1") == ["searxng", "duckduckgo"]

    def test_strict_overrides_the_opt_in(self) -> None:
        names = self._names(SUPACRAWL_SEARCH_PUBLIC_FALLBACK="1", SUPACRAWL_SEARCH_STRICT_PROVIDERS="1")
        assert names == ["searxng"], "strict mode must refuse the fallback even when opt-in is on"

    def test_opt_in_does_not_double_add_configured_duckduckgo(self) -> None:
        with patch.dict(
            os.environ,
            {"SEARXNG_URL": "http://searxng.invalid", "SUPACRAWL_SEARCH_PUBLIC_FALLBACK": "1"},
            clear=False,
        ):
            chain = build_provider_chain("searxng,duckduckgo")
        assert [p.name for p in chain.providers] == ["searxng", "duckduckgo"]


# ---------------------------------------------------------------------------
# The fallback actually engages, end to end
# ---------------------------------------------------------------------------


class TestFallbackEngages:
    @pytest.mark.asyncio
    async def test_search_falls_back_to_duckduckgo_and_flags_it(self) -> None:
        service, client = _searxng_service_with_public_fallback()
        try:
            result = await service.search("open source software", limit=5)

            assert result.success is True, "the fallback did not rescue a failed configured backend"
            assert result.data, "fallback returned no results"
            assert result.provider == "duckduckgo"
            assert result.provider_fallback is True, "the caller cannot tell a fallback answered"
            assert service.provider_chain.fallback_serving is True
        finally:
            await service.close()
            await client.aclose()


@pytest.mark.mcp
class TestFallbackHealthSurface:
    @pytest.mark.asyncio
    async def test_health_reports_fallback_active_and_degraded(self) -> None:
        from supacrawl.mcp.tools.health import supacrawl_health

        service, client = _searxng_service_with_public_fallback()
        try:
            with patch.dict(os.environ, {"SEARXNG_URL": "http://searxng.invalid"}):
                result: dict[str, Any] = await supacrawl_health(_StubServices(service))  # type: ignore[arg-type]

            search = result["components"]["search"]
            # The probe is answered by DDG, so it returns results — but serving
            # from an unconfigured fallback is still degraded, not healthy.
            assert search["provider_fallback_active"] is True, "health hid an active fallback"
            assert result["status"] == "degraded"
            assert "duckduckgo" in (search.get("warning", "").lower() + search.get("effective_provider", ""))
        finally:
            await service.close()
            await client.aclose()


# ---------------------------------------------------------------------------
# fallback_serving vs unconfigured_fallback_active
# ---------------------------------------------------------------------------


class _StubProvider:
    def __init__(self, name: str, *, available: bool = True, results: bool = True) -> None:
        self._name = name
        self._available = available
        self._results = results

    @property
    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return self._available

    async def search_web(self, *_a: object, **_k: object) -> list:
        from supacrawl.models import SearchResultItem

        if not self._results:
            # A fallback-eligible failure, so the chain moves to the next provider.
            raise TimeoutError("engines down")
        return [SearchResultItem(url="https://x/", title=f"{self._name}")]

    async def search_images(self, *_a: object, **_k: object) -> list:
        raise NotImplementedError

    async def search_news(self, *_a: object, **_k: object) -> list:
        raise NotImplementedError

    async def close(self) -> None:
        return None


class TestProvenanceUnderConcurrentScrape:
    """A response must report ITS OWN serving provider, not a concurrent one's.

    self._chain is one server-wide singleton; scrape_results=True adds a
    multi-second await between the chain answering and the provider being read
    back. Reading the shared last_provider after that await let a concurrent
    request overwrite it — mislabeling the response, even masking a real leak.
    """

    @pytest.mark.asyncio
    async def test_served_by_is_captured_before_the_scrape_await(self) -> None:
        service = SearchService(providers=["searxng"], rate_limit=1000)

        async def fake_chain_search(**_kwargs: object) -> list[SearchResultItem]:
            service._chain.last_provider = "searxng"  # this request's real provider
            return [SearchResultItem(url="https://x/", title="r", source_type=SearchSourceType.WEB)]

        service._chain.search = AsyncMock(side_effect=fake_chain_search)  # type: ignore[method-assign]

        async def fake_scrape(**_kwargs: object) -> MagicMock:
            # Mid-scrape, a concurrent request's fallback overwrites the shared field.
            service._chain.last_provider = "duckduckgo"
            return MagicMock(success=True, data=MagicMock(markdown="m", html=None, metadata=None))

        scrape_service = MagicMock()
        scrape_service.scrape = fake_scrape
        service._scrape_service = scrape_service

        try:
            result = await service.search("q", scrape_options=ScrapeOptions(formats=["markdown"]))

            assert result.provider == "searxng", "a concurrent request's provider was mislabeled onto this response"
            assert result.provider_fallback is False, "a genuine non-fallback response was flagged as a leak"
        finally:
            await service.close()


class TestFallbackFailureAttribution:
    """When the fallback ALSO fails, the caller reads the configured backend's
    error, never the fallback's (radar-hooves/cadmus #Nightjar, 01/10/2026).

    A domain-restricted query raised SearXNG's own "N upstream engines
    unresponsive" ProviderError — naming exactly which engines were down — but
    the chain's unqualified ``last_error = e`` reassignment let the DuckDuckGo
    fallback's later, opaque "CAPTCHA challenge" overwrite it. The caller then
    saw a failure that read as "the DuckDuckGo fallback is broken" when the
    real, actionable story was "SearXNG's own engines are down" — exactly the
    attribution the #161 ``unresponsive_engines`` surfacing exists to give.
    """

    def _searxng_and_broken_ddg_client(self) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            host = request.url.host
            if "searxng" in host:
                return httpx.Response(200, json=_BROKEN_SEARXNG)
            if "duckduckgo" in host:
                return httpx.Response(200, text="<div class='anomaly-modal'>captcha</div>")
            return httpx.Response(500, text="unexpected host")

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def _service_with_both_down(self) -> tuple[SearchService, httpx.AsyncClient]:
        with patch.dict(
            os.environ,
            {
                "SEARXNG_URL": "http://searxng.invalid",
                "SUPACRAWL_SEARCH_PROVIDERS": "searxng",
                "SUPACRAWL_SEARCH_PUBLIC_FALLBACK": "1",
                "SUPACRAWL_SEARCH_STRICT_PROVIDERS": "",
            },
        ):
            service = SearchService(providers=["searxng"])

        client = self._searxng_and_broken_ddg_client()
        for provider in service.provider_chain.providers:
            if isinstance(provider, (SearXNGProvider, DuckDuckGoProvider)):
                provider._http_client = client
        return service, client

    @pytest.mark.asyncio
    async def test_configured_backend_error_survives_a_failed_fallback(self) -> None:
        service, client = self._service_with_both_down()
        try:
            result = await service.search("site:aph.gov.au Ghost Bat", limit=5)

            assert result.success is False
            assert "duckduckgo" not in (result.error or "").lower(), (
                f"the caller must never be told DuckDuckGo failed when SearXNG failed first, got: {result.error!r}"
            )
            assert "unresponsive" in (result.error or "").lower()
            engines = {e.engine for e in result.unresponsive_engines}
            assert {"brave", "wikibooks", "wikinews"} <= engines, (
                f"SearXNG's own dead-engine list must survive the fallback also failing, got: {engines}"
            )
        finally:
            await service.close()
            await client.aclose()

    @pytest.mark.asyncio
    async def test_provider_chain_raises_the_configured_providers_error(self) -> None:
        """Unit-level: the chain itself, not just the service wrapper, prefers it."""

        class _FailingProvider(_StubProvider):
            async def search_web(self, *_a: object, **_k: object) -> list:
                raise TimeoutError(f"{self.name} down")

        chain = ProviderChain(configured_names=["searxng"])
        chain.add(_FailingProvider("searxng"))
        chain.add(_FailingProvider("duckduckgo"))

        with pytest.raises(TimeoutError, match="searxng down") as exc_info:
            await chain.search("web", "q", 1, "corr")

        assert "duckduckgo" not in str(exc_info.value)
        # The fallback was genuinely consulted (not skipped) — it just must not
        # win attribution for the final raised error.
        assert chain._health["searxng"].consecutive_failures == 1
        assert chain._health["duckduckgo"].consecutive_failures == 1


class TestFallbackServingSignal:
    def test_false_before_any_search(self) -> None:
        chain = ProviderChain(configured_names=["searxng"])
        chain.add(_StubProvider("searxng"))
        chain.add(_StubProvider("duckduckgo"))
        assert chain.fallback_serving is False

    @pytest.mark.asyncio
    async def test_true_once_an_unconfigured_provider_answers(self) -> None:
        chain = ProviderChain(configured_names=["searxng"])
        chain.add(_StubProvider("searxng", available=True, results=False))  # raises, forcing fallback
        chain.add(_StubProvider("duckduckgo"))

        await chain.search("web", "q", 1, "corr")

        assert chain.last_provider == "duckduckgo"
        assert chain.fallback_serving is True

    @pytest.mark.asyncio
    async def test_false_when_the_configured_provider_answers(self) -> None:
        chain = ProviderChain(configured_names=["searxng"])
        chain.add(_StubProvider("searxng"))
        chain.add(_StubProvider("duckduckgo"))

        await chain.search("web", "q", 1, "corr")

        assert chain.last_provider == "searxng"
        assert chain.fallback_serving is False
