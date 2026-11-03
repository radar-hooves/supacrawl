"""An empty result is a successful answer, not a reason to stop looking.

The field incident: a self-hosted SearXNG whose upstream engines were all
CAPTCHA-blocked answered HTTP 200 with an empty result set for days. The caller
saw ``success: true``, ``data: []``, ``consecutive_failures: 0`` and a green
healthcheck — nothing said search was *dead* rather than that the query had *no
matches*. Two conditions caused it: ``ProviderChain.search`` banked an empty list
as a healthy success and never advanced to the next configured provider, and the
per-provider health record read clean because a zero-result success was counted
as a failure nowhere.

These tests pin the fix:

- an empty result from one provider advances the chain to the next CONFIGURED
  provider, and a provider with matches ends the chain;
- all-providers-empty returns success-with-no-data and never raises (a genuine
  no-match must stay a real outcome, #132/#156);
- provider health separates "answered with results" from "answered with nothing"
  and degrades on a sustained empty run;
- the response itself carries the sustained-empty signal, so a caller can tell a
  no-match from a dead backend without polling the health surface (extends #161);
- the privacy stance holds: a silent-empty self-hosted backend stays in-house
  unless the operator opted into the public fallback (#156/#158).

All services-layer so they run in CI without the mcp extra (the
test_search_fallback.py convention).
"""

from __future__ import annotations

import os
from unittest.mock import patch

import httpx
import pytest

from supacrawl.exceptions import ProviderError
from supacrawl.models import SearchResultItem, SearchSourceType
from supacrawl.services.search.providers import ProviderChain, ProviderHealth, ProviderStatus
from supacrawl.services.search.searxng import SearXNGProvider
from supacrawl.services.search.service import SearchService


class _Provider:
    """A SearchProvider double that returns a fixed list (possibly empty) or raises.

    Deterministic empty/non-empty answers are what let empty-fallthrough be
    driven without a live backend.
    """

    def __init__(
        self,
        name: str,
        *,
        results: list[SearchResultItem] | None = None,
        available: bool = True,
        raises: Exception | None = None,
    ) -> None:
        self._name = name
        self._results = results if results is not None else []
        self._available = available
        self._raises = raises
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return self._available

    async def search_web(self, *_a: object, **_k: object) -> list[SearchResultItem]:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return list(self._results)

    async def search_images(self, *_a: object, **_k: object) -> list[SearchResultItem]:
        raise NotImplementedError

    async def search_news(self, *_a: object, **_k: object) -> list[SearchResultItem]:
        raise NotImplementedError

    async def close(self) -> None:
        return None


def _item(url: str) -> SearchResultItem:
    return SearchResultItem(url=url, title="r", source_type=SearchSourceType.WEB)


# ProviderChain: empty advances the chain; all-empty is a genuine empty


class TestEmptyAdvancesTheChain:
    @pytest.mark.asyncio
    async def test_empty_result_advances_to_next_configured_provider(self) -> None:
        empty = _Provider("searxng", results=[])
        full = _Provider("brave", results=[_item("https://brave/1")])
        chain = ProviderChain(providers=[empty, full], configured_names=["searxng", "brave"])

        results = await chain.search("web", "q", 5, "corr")

        assert [r.url for r in results] == ["https://brave/1"], "the chain did not advance past an empty provider"
        assert empty.calls == 1 and full.calls == 1
        assert chain.last_provider == "brave"

    @pytest.mark.asyncio
    async def test_all_providers_empty_returns_empty_without_raising(self) -> None:
        p1 = _Provider("searxng", results=[])
        p2 = _Provider("brave", results=[])
        chain = ProviderChain(providers=[p1, p2], configured_names=["searxng", "brave"])

        results = await chain.search("web", "q", 5, "corr")

        assert results == [], "an all-empty chain must return an empty set, not raise"
        assert p1.calls == 1 and p2.calls == 1, "every configured provider should have been tried"
        # Attributed to the FIRST provider that answered, and never flagged as a
        # fallback: an all-empty result is a real no-match, not a leak.
        assert chain.last_provider == "searxng"
        assert chain.fallback_serving is False

    @pytest.mark.asyncio
    async def test_a_provider_with_matches_after_an_empty_and_a_failure_wins(self) -> None:
        raised = _Provider("serper", raises=TimeoutError("engines down"))
        empty = _Provider("searxng", results=[])
        full = _Provider("brave", results=[_item("https://brave/1")])
        chain = ProviderChain(providers=[raised, empty, full], configured_names=["serper", "searxng", "brave"])

        results = await chain.search("web", "q", 5, "corr")

        assert [r.url for r in results] == ["https://brave/1"]
        assert chain.last_provider == "brave"

    @pytest.mark.asyncio
    async def test_all_empty_cascade_attributes_to_the_consulted_public_engine(self) -> None:
        """The #158 audit trail: when an all-empty cascade actually queried an
        unconfigured provider (DuckDuckGo, appended under the public-fallback
        opt-in), the response must show a query reached it — not attribute the
        empty to the configured backend and hide the consultation."""
        searxng = _Provider("searxng", results=[])
        ddg = _Provider("duckduckgo", results=[])  # unconfigured, appended last
        chain = ProviderChain(providers=[searxng, ddg], configured_names=["searxng"])

        results = await chain.search("web", "q", 5, "corr")

        assert results == []
        assert searxng.calls == 1 and ddg.calls == 1, "the public engine WAS queried over the wire"
        assert chain.last_provider == "duckduckgo", "an all-empty cascade hid that the query reached the public engine"
        assert chain.fallback_serving is True

    @pytest.mark.asyncio
    async def test_unconfigured_provider_that_never_touched_the_wire_is_not_a_leak(self) -> None:
        """A leak report must mean a query actually REACHED a public engine. An
        unconfigured provider that raises NotImplementedError (source unsupported,
        no request made) must NOT be reported as consulted."""
        searxng = _Provider("searxng", results=[])
        ddg = _Provider("duckduckgo", raises=NotImplementedError("no web search"))
        chain = ProviderChain(providers=[searxng, ddg], configured_names=["searxng"])

        results = await chain.search("web", "q", 5, "corr")

        assert results == []
        assert chain.last_provider == "searxng", "a provider that never made a request was reported as consulted"
        assert chain.fallback_serving is False

    @pytest.mark.asyncio
    async def test_unconfigured_provider_that_captcha_d_after_calling_is_a_leak(self) -> None:
        """The mirror case: an unconfigured provider that raised AFTER making its
        request (a CAPTCHA) DID receive the query, so an all-empty cascade must
        still report the consultation."""
        searxng = _Provider("searxng", results=[])
        ddg = _Provider("duckduckgo", raises=ProviderError("CAPTCHA challenge detected", provider="duckduckgo"))
        chain = ProviderChain(providers=[searxng, ddg], configured_names=["searxng"])

        results = await chain.search("web", "q", 5, "corr")

        assert results == []
        assert chain.last_provider == "duckduckgo", (
            "a query that reached (and was CAPTCHA'd by) the public engine was hidden"
        )
        assert chain.fallback_serving is True

    @pytest.mark.asyncio
    async def test_banked_empty_survives_a_later_non_fallback_error(self) -> None:
        """An earlier empty is a valid answer; a later provider's hard error must
        not discard it (nor become an exception the caller cannot read)."""
        empty = _Provider("searxng", results=[])
        broken = _Provider("brave", raises=ValueError("malformed query"))
        chain = ProviderChain(providers=[empty, broken], configured_names=["searxng", "brave"])

        results = await chain.search("web", "q", 5, "corr")

        assert results == []
        assert chain.last_provider == "searxng"

    @pytest.mark.asyncio
    async def test_non_fallback_error_still_raises_when_nothing_answered_yet(self) -> None:
        """With no earlier answer in hand, a non-fallback error still stops the
        chain and raises — the pre-existing #132 contract is untouched."""
        broken = _Provider("brave", raises=ValueError("malformed query"))
        never = _Provider("tavily", results=[_item("https://tavily/1")])
        chain = ProviderChain(providers=[broken, never], configured_names=["brave", "tavily"])

        with pytest.raises(ValueError, match="malformed query"):
            await chain.search("web", "q", 5, "corr")
        assert never.calls == 0, "a non-fallback error must not try later providers"


# Provider health: answered-with-results vs answered-with-nothing


class TestEmptyRunIsAHealthSignal:
    def test_record_empty_success_tracks_streak_and_degrades(self) -> None:
        health = ProviderHealth()

        health.record_empty_success()
        assert health.consecutive_empty == 1
        assert health.status == ProviderStatus.HEALTHY, "a single empty is not yet a verdict"

        for _ in range(ProviderHealth.EMPTY_DEGRADED_THRESHOLD - 1):
            health.record_empty_success()
        assert health.consecutive_empty == ProviderHealth.EMPTY_DEGRADED_THRESHOLD
        assert health.status == ProviderStatus.DEGRADED, "a sustained empty run must read degraded, not clean"

    def test_a_result_with_matches_clears_the_empty_streak(self) -> None:
        health = ProviderHealth()
        for _ in range(ProviderHealth.EMPTY_DEGRADED_THRESHOLD):
            health.record_empty_success()
        assert health.status == ProviderStatus.DEGRADED

        health.record_success()
        assert health.consecutive_empty == 0
        assert health.status == ProviderStatus.HEALTHY

    def test_empty_success_never_trips_the_unavailable_circuit_breaker(self) -> None:
        """A provider that keeps answering — even with nothing — is not UNAVAILABLE.
        Dropping it (an empty may be a genuine no-match) would be wrong."""
        health = ProviderHealth()
        for _ in range(health.UNAVAILABLE_THRESHOLD + 3):
            health.record_empty_success()

        assert health.status != ProviderStatus.UNAVAILABLE
        assert health.should_skip is False

    def test_empty_answer_clears_a_stale_unavailable_from_failures(self) -> None:
        """A provider circuit-broken by real failures, then answering (even empty),
        has recovered at the transport level: its status must not stay UNAVAILABLE
        while consecutive_failures reads 0 — an internally contradictory payload."""
        health = ProviderHealth()
        for _ in range(health.UNAVAILABLE_THRESHOLD):
            health.record_failure("boom")
        assert health.status == ProviderStatus.UNAVAILABLE

        health.record_empty_success()

        assert health.consecutive_failures == 0
        assert health.status == ProviderStatus.DEGRADED, "a recovered-but-empty provider must not read unavailable"
        assert health.should_skip is False

    def test_to_dict_exposes_consecutive_empty(self) -> None:
        health = ProviderHealth()
        health.record_empty_success()
        d = health.to_dict()
        assert d["consecutive_empty"] == 1
        assert d["consecutive_failures"] == 0

    @pytest.mark.asyncio
    async def test_chain_health_reflects_a_sustained_empty_run(self) -> None:
        provider = _Provider("searxng", results=[])
        chain = ProviderChain(providers=[provider], configured_names=["searxng"])

        for _ in range(ProviderHealth.EMPTY_DEGRADED_THRESHOLD):
            assert await chain.search("web", "q", 5, "corr") == []

        health = chain.get_health()["searxng"]
        assert health["consecutive_empty"] == ProviderHealth.EMPTY_DEGRADED_THRESHOLD
        assert health["consecutive_failures"] == 0, "empties are not failures"
        assert health["status"] == "degraded"


# SearchService: the caller can read the difference off the response


def _service_with_empty_chain() -> SearchService:
    """A real SearchService whose single keyed provider always answers empty."""
    service = SearchService(providers=["brave"], brave_api_key="key", rate_limit=1000)
    service._chain = ProviderChain(
        providers=[_Provider("brave", results=[])],
        configured_names=["brave"],
    )
    return service


class TestResponseCarriesTheSignal:
    @pytest.mark.asyncio
    async def test_single_empty_is_a_clean_no_match(self) -> None:
        service = _service_with_empty_chain()
        try:
            result = await service.search("q", limit=3)
            assert result.success is True, "a genuine no-match behind a keyed provider must stay success"
            assert result.data == []
            assert result.provider == "brave"
            assert result.provider_fallback is False
            assert result.all_recent_empty is False, "a single no-match was mislabelled as a backend failure"
        finally:
            await service.close()

    @pytest.mark.asyncio
    async def test_sustained_empty_run_trips_the_response_signal(self) -> None:
        service = _service_with_empty_chain()
        try:
            last = None
            for _ in range(SearchService.RECENT_WINDOW):
                last = await service.search("q", limit=3)

            assert last is not None
            assert last.success is True and last.data == [], "a sustained empty run must NOT become an error"
            assert last.all_recent_empty is True, "the response did not carry the sustained-empty signal"
            assert service.recent_search_health["all_recent_empty"] is True
        finally:
            await service.close()

    @pytest.mark.asyncio
    async def test_a_response_with_data_never_claims_the_sustained_empty_signal(self) -> None:
        """The flag must never contradict the result it rides on: a response that
        returned data reports all_recent_empty False even when the rolling window
        was left all-empty by prior non-window (record_recent=False) traffic."""
        service = SearchService(providers=["brave"], brave_api_key="key", rate_limit=1000)
        service._chain = ProviderChain(providers=[_Provider("brave", results=[])], configured_names=["brave"])
        try:
            for _ in range(SearchService.RECENT_WINDOW):
                await service.search("q", limit=3)
            assert service.recent_search_health["all_recent_empty"] is True

            # A search that returns data, deliberately kept out of the window.
            service._chain = ProviderChain(
                providers=[_Provider("brave", results=[_item("https://brave/1")])],
                configured_names=["brave"],
            )
            result = await service.search("q", limit=3, record_recent=False)

            assert result.data, "sanity: this search returned data"
            assert result.all_recent_empty is False, "a response WITH data must never claim the sustained-empty signal"
        finally:
            await service.close()


# The privacy stance holds under empty-fallthrough (#156/#158)


# 200-with-[] and NO unresponsive engines: the CAPTCHA-walled case the incident
# hit. SearXNG answered, upstream engines returned nothing, nothing was flagged
# unresponsive — so `_guard_upstream_failure` does not raise and the empty is
# handed straight back.
_SILENT_EMPTY_BODY: dict[str, list[object]] = {"results": [], "unresponsive_engines": []}

# Minimal DuckDuckGo-lite HTML the real DuckDuckGoProvider knows how to parse.
_DDG_HTML = """
<table>
  <tr><td><a class="result-link" href="https://example.org/ddg1">DDG One</a></td></tr>
  <tr><td class="result-snippet">snippet one</td></tr>
  <tr><td><a class="result-link" href="https://example.org/ddg2">DDG Two</a></td></tr>
  <tr><td class="result-snippet">snippet two</td></tr>
</table>
"""


def _silent_empty_searxng_client() -> httpx.AsyncClient:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_SILENT_EMPTY_BODY)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _searxng_empty_ddg_answers_client() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if "duckduckgo" in request.url.host:
            return httpx.Response(200, text=_DDG_HTML, headers={"content-type": "text/html"})
        return httpx.Response(200, json=_SILENT_EMPTY_BODY)  # searxng: silent empty

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _both_empty_client() -> httpx.AsyncClient:
    """SearXNG AND DuckDuckGo both answer empty (a valid 200, zero results)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "duckduckgo" in request.url.host:
            return httpx.Response(200, text="<table></table>", headers={"content-type": "text/html"})
        return httpx.Response(200, json=_SILENT_EMPTY_BODY)  # searxng: silent empty

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _searxng_service(client: httpx.AsyncClient, **env: str) -> SearchService:
    base = {
        "SEARXNG_URL": "http://searxng.invalid",
        "SUPACRAWL_SEARCH_PROVIDERS": "searxng",
        "SUPACRAWL_SEARCH_STRICT_PROVIDERS": "",
    }
    with patch.dict(os.environ, {**base, **env}):
        service = SearchService(providers=["searxng"])
    from supacrawl.services.search.duckduckgo import DuckDuckGoProvider

    for provider in service.provider_chain.providers:
        if isinstance(provider, (SearXNGProvider, DuckDuckGoProvider)):
            provider._http_client = client
    return service


class TestPrivacyStanceHolds:
    @pytest.mark.asyncio
    async def test_silent_empty_backend_stays_in_house_and_is_honest(self) -> None:
        """The exact incident: SearXNG answers 200-with-[] and, with the public
        fallback OFF, the query stays in-house — a real empty, not an error, and
        no leak to an unconfigured engine."""
        client = _silent_empty_searxng_client()
        service = _searxng_service(client, SUPACRAWL_SEARCH_PUBLIC_FALLBACK="")
        try:
            assert [p.name for p in service.provider_chain.providers] == ["searxng"], (
                "an engine nobody configured joined the chain"
            )

            result = await service.search("open source software", limit=5)

            assert result.success is True, "a genuine empty must stay success, not become an error"
            assert result.data == []
            assert result.provider == "searxng"
            assert result.provider_fallback is False, "queries leaked to an unconfigured engine"
        finally:
            await service.close()
            await client.aclose()

    @pytest.mark.asyncio
    async def test_sustained_silent_empty_degrades_health_and_signals_the_response(self) -> None:
        client = _silent_empty_searxng_client()
        service = _searxng_service(client, SUPACRAWL_SEARCH_PUBLIC_FALLBACK="")
        try:
            last = None
            for _ in range(SearchService.RECENT_WINDOW):
                last = await service.search("open source software", limit=5)

            assert last is not None and last.success is True and last.data == []
            assert last.all_recent_empty is True
            assert service.recent_search_health["all_recent_empty"] is True

            health = service.provider_chain.get_health()["searxng"]
            assert health["consecutive_empty"] >= ProviderHealth.EMPTY_DEGRADED_THRESHOLD
            assert health["status"] == "degraded"
        finally:
            await service.close()
            await client.aclose()

    @pytest.mark.asyncio
    async def test_empty_falls_through_to_ddg_only_with_the_opt_in(self) -> None:
        """With SUPACRAWL_SEARCH_PUBLIC_FALLBACK on, an EMPTY (not merely failed)
        SearXNG may hand off to DuckDuckGo, and the caller is told a fallback
        answered — the operator opted into exactly this."""
        client = _searxng_empty_ddg_answers_client()
        service = _searxng_service(client, SUPACRAWL_SEARCH_PUBLIC_FALLBACK="1")
        try:
            assert [p.name for p in service.provider_chain.providers] == ["searxng", "duckduckgo"]

            result = await service.search("open source software", limit=5)

            assert result.success is True
            assert result.data, "the opt-in fallback did not rescue an empty configured backend"
            assert result.provider == "duckduckgo"
            assert result.provider_fallback is True, "the caller cannot tell a fallback answered"
        finally:
            await service.close()
            await client.aclose()

    @pytest.mark.asyncio
    async def test_all_empty_cascade_still_flags_that_the_public_engine_was_queried(self) -> None:
        """Opt-in on, and BOTH SearXNG and DuckDuckGo answer empty: the result is a
        genuine empty (success, no data), but the response must still tell the
        caller a query reached the public engine — not attribute the empty to the
        in-house backend and bury the leak (#158 audit trail)."""
        client = _both_empty_client()
        service = _searxng_service(client, SUPACRAWL_SEARCH_PUBLIC_FALLBACK="1")
        try:
            assert [p.name for p in service.provider_chain.providers] == ["searxng", "duckduckgo"]

            result = await service.search("open source software", limit=5)

            assert result.success is True and result.data == [], "an all-empty cascade must stay success-with-no-data"
            assert result.provider == "duckduckgo"
            assert result.provider_fallback is True, (
                "the response hid that an all-empty query reached the public engine"
            )
        finally:
            await service.close()
            await client.aclose()
