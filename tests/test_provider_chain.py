"""Tests for multi-provider search chain with automatic fallback."""

import asyncio
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from supacrawl.exceptions import ProviderError
from supacrawl.models import SearchFilters, SearchResultItem, SearchSourceType
from supacrawl.services.search import SearchService
from supacrawl.services.search.providers import (
    ALERT_DEBOUNCE_SECONDS,
    ProviderChain,
    ProviderHealth,
    ProviderStatus,
    is_auth_billing_error,
    is_fallback_error,
)
from supacrawl.services.search.registry import SUPPORTED_PROVIDERS, build_provider_chain

# Helpers: mock providers


class MockProvider:
    """Test double implementing the SearchProvider protocol."""

    def __init__(self, provider_name: str, *, available: bool = True, fail_with: Exception | None = None):
        self._name = provider_name
        self._available = available
        self._fail_with = fail_with
        self._calls: list[str] = []

    @property
    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return self._available

    async def search_web(
        self, query: str, limit: int, correlation_id: str, filters: SearchFilters | None = None
    ) -> list[SearchResultItem]:
        self._calls.append(f"web:{query}")
        if self._fail_with:
            raise self._fail_with
        return [
            SearchResultItem(
                url=f"https://{self._name}.com/1", title=f"{self._name} result", source_type=SearchSourceType.WEB
            )
        ]

    async def search_images(
        self, query: str, limit: int, correlation_id: str, filters: SearchFilters | None = None
    ) -> list[SearchResultItem]:
        self._calls.append(f"images:{query}")
        if self._fail_with:
            raise self._fail_with
        return [
            SearchResultItem(
                url=f"https://{self._name}.com/img", title=f"{self._name} image", source_type=SearchSourceType.IMAGES
            )
        ]

    async def search_news(
        self, query: str, limit: int, correlation_id: str, filters: SearchFilters | None = None
    ) -> list[SearchResultItem]:
        self._calls.append(f"news:{query}")
        if self._fail_with:
            raise self._fail_with
        return [
            SearchResultItem(
                url=f"https://{self._name}.com/news", title=f"{self._name} news", source_type=SearchSourceType.NEWS
            )
        ]

    async def close(self) -> None:
        pass


# Provider health tracking


class TestProviderHealth:
    """Tests for ProviderHealth status tracking."""

    def test_initial_state_is_healthy(self):
        health = ProviderHealth()
        assert health.status == ProviderStatus.HEALTHY
        assert health.consecutive_failures == 0
        assert not health.should_skip

    def test_success_resets_failures(self):
        health = ProviderHealth()
        health.record_failure("error 1")
        health.record_failure("error 2")
        assert health.status == ProviderStatus.DEGRADED
        health.record_success()
        assert health.status == ProviderStatus.HEALTHY
        assert health.consecutive_failures == 0

    def test_three_failures_marks_unavailable(self):
        health = ProviderHealth()
        health.record_failure("error 1")
        health.record_failure("error 2")
        assert health.status == ProviderStatus.DEGRADED
        health.record_failure("error 3")
        assert health.status == ProviderStatus.UNAVAILABLE
        assert health.should_skip  # Should skip until cooldown

    def test_to_dict_serialisation(self):
        health = ProviderHealth()
        health.record_success()
        d = health.to_dict()
        assert d["status"] == "healthy"
        assert d["requests_made"] == 1
        assert d["consecutive_failures"] == 0


# Fallback error detection


class TestFallbackErrorDetection:
    """Tests for is_fallback_error()."""

    def test_http_429_is_fallback(self):
        response = httpx.Response(429, request=httpx.Request("GET", "https://api.example.com"))
        error = httpx.HTTPStatusError("rate limited", request=response.request, response=response)
        assert is_fallback_error(error)

    def test_http_402_is_fallback(self):
        response = httpx.Response(402, request=httpx.Request("GET", "https://api.example.com"))
        error = httpx.HTTPStatusError("payment required", request=response.request, response=response)
        assert is_fallback_error(error)

    def test_http_403_without_quota_body_is_not_fallback(self):
        response = httpx.Response(403, request=httpx.Request("GET", "https://api.example.com"))
        error = httpx.HTTPStatusError("forbidden", request=response.request, response=response)
        assert not is_fallback_error(error)

    def test_http_403_with_quota_body_is_fallback(self):
        response = httpx.Response(
            403,
            request=httpx.Request("GET", "https://api.example.com"),
            content=b'{"error": "Quota exceeded for this API key"}',
        )
        error = httpx.HTTPStatusError("forbidden", request=response.request, response=response)
        assert is_fallback_error(error)

    def test_http_400_is_not_fallback(self):
        response = httpx.Response(400, request=httpx.Request("GET", "https://api.example.com"))
        error = httpx.HTTPStatusError("bad request", request=response.request, response=response)
        assert not is_fallback_error(error)

    def test_http_500_is_not_fallback(self):
        response = httpx.Response(500, request=httpx.Request("GET", "https://api.example.com"))
        error = httpx.HTTPStatusError("server error", request=response.request, response=response)
        assert not is_fallback_error(error)

    def test_captcha_provider_error_is_fallback(self):
        error = ProviderError("CAPTCHA challenge detected", provider="duckduckgo")
        assert is_fallback_error(error)

    def test_quota_provider_error_is_fallback(self):
        error = ProviderError("Quota exceeded", provider="brave")
        assert is_fallback_error(error)

    def test_generic_provider_error_is_not_fallback(self):
        error = ProviderError("API key not configured", provider="brave")
        assert not is_fallback_error(error)

    def test_unresponsive_engines_provider_error_is_fallback(self):
        """SearXNG raising because every engine was down must let the chain try
        the next provider, not surface a bare empty set (#161)."""
        error = ProviderError(
            "SearXNG returned no results and 4 upstream engine(s) were unresponsive (brave (timeout))",
            provider="searxng",
        )
        assert is_fallback_error(error)

    def test_connect_timeout_is_fallback(self):
        error = httpx.ConnectTimeout("connection timed out")
        assert is_fallback_error(error)

    def test_http_401_is_fallback(self):
        response = httpx.Response(401, request=httpx.Request("GET", "https://api.example.com"))
        error = httpx.HTTPStatusError("unauthorized", request=response.request, response=response)
        assert is_fallback_error(error)

    def test_http_400_with_api_key_body_is_fallback(self):
        response = httpx.Response(
            400,
            request=httpx.Request("GET", "https://api.example.com"),
            content=b'{"error": "Invalid api key supplied"}',
        )
        error = httpx.HTTPStatusError("bad request", request=response.request, response=response)
        assert is_fallback_error(error)

    def test_http_400_plain_malformed_query_is_not_fallback(self):
        response = httpx.Response(
            400,
            request=httpx.Request("GET", "https://api.example.com"),
            content=b'{"error": "Query parameter q is required"}',
        )
        error = httpx.HTTPStatusError("bad request", request=response.request, response=response)
        assert not is_fallback_error(error)

    def test_http_403_with_expired_body_is_fallback(self):
        response = httpx.Response(
            403,
            request=httpx.Request("GET", "https://api.example.com"),
            content=b'{"message": "Your subscription has expired"}',
        )
        error = httpx.HTTPStatusError("forbidden", request=response.request, response=response)
        assert is_fallback_error(error)

    def test_value_error_is_not_fallback(self):
        error = ValueError("bad argument")
        assert not is_fallback_error(error)


# Auth/billing error classification


class TestAuthBillingErrorDetection:
    """Tests for is_auth_billing_error()."""

    def test_http_401_is_auth_billing(self):
        response = httpx.Response(401, request=httpx.Request("GET", "https://api.example.com"))
        error = httpx.HTTPStatusError("unauthorized", request=response.request, response=response)
        assert is_auth_billing_error(error)

    def test_http_402_is_auth_billing(self):
        response = httpx.Response(402, request=httpx.Request("GET", "https://api.example.com"))
        error = httpx.HTTPStatusError("payment required", request=response.request, response=response)
        assert is_auth_billing_error(error)

    def test_http_403_with_billing_body_is_auth_billing(self):
        response = httpx.Response(
            403,
            request=httpx.Request("GET", "https://api.example.com"),
            content=b'{"error": "billing account suspended"}',
        )
        error = httpx.HTTPStatusError("forbidden", request=response.request, response=response)
        assert is_auth_billing_error(error)

    def test_http_400_with_invalid_key_body_is_auth_billing(self):
        response = httpx.Response(
            400,
            request=httpx.Request("GET", "https://api.example.com"),
            content=b'{"error": "invalid key"}',
        )
        error = httpx.HTTPStatusError("bad request", request=response.request, response=response)
        assert is_auth_billing_error(error)

    def test_http_429_is_not_auth_billing(self):
        """Rate limiting is not an auth/billing issue."""
        response = httpx.Response(429, request=httpx.Request("GET", "https://api.example.com"))
        error = httpx.HTTPStatusError("too many requests", request=response.request, response=response)
        assert not is_auth_billing_error(error)

    def test_http_500_is_not_auth_billing(self):
        response = httpx.Response(500, request=httpx.Request("GET", "https://api.example.com"))
        error = httpx.HTTPStatusError("server error", request=response.request, response=response)
        assert not is_auth_billing_error(error)

    def test_generic_exception_is_not_auth_billing(self):
        assert not is_auth_billing_error(RuntimeError("something went wrong"))


# ProviderHealth alert debouncing


class TestProviderHealthAlertDebouncing:
    """Tests for ProviderHealth.should_alert() / record_alert()."""

    def test_alert_fires_on_first_call(self):
        health = ProviderHealth()
        # last_alert_time defaults to None (never alerted), so should_alert() fires
        assert health.should_alert()

    def test_alert_suppressed_immediately_after_record(self):
        health = ProviderHealth()
        health.record_alert()
        assert not health.should_alert()

    def test_alert_fires_again_after_debounce_window(self):
        health = ProviderHealth()
        # Backdate the last alert to just beyond the debounce window
        health.last_alert_time = time.monotonic() - (ALERT_DEBOUNCE_SECONDS + 1)
        assert health.should_alert()

    def test_alert_debounce_constant_is_300(self):
        assert ALERT_DEBOUNCE_SECONDS == 300.0


# Provider chain: auth/billing alert integration


class TestProviderChainAuthBillingAlert:
    """Tests that the chain emits exactly one debounced warning on auth/billing failure."""

    @pytest.mark.asyncio
    async def test_auth_billing_failure_logs_warning(self):
        """A 401 failure should emit a LOGGER.warning and still fall back."""
        response = httpx.Response(401, request=httpx.Request("GET", "https://api.example.com"))
        error = httpx.HTTPStatusError("unauthorized", request=response.request, response=response)

        p1 = MockProvider("serper", fail_with=error)
        p2 = MockProvider("tavily")
        chain = ProviderChain(providers=[p1, p2])

        with patch("supacrawl.services.search.providers.LOGGER") as mock_logger:
            results = await chain.search("web", "test", 5, "corr-1")

        # Fallback succeeded
        assert len(results) == 1
        assert results[0].url == "https://tavily.com/1"

        # Warning was emitted
        warning_calls = list(mock_logger.warning.call_args_list)
        assert any("ACTION REQUIRED" in str(call) for call in warning_calls)

    @pytest.mark.asyncio
    async def test_auth_billing_alert_debounced_on_repeated_failure(self):
        """Second failure within debounce window should NOT emit another warning."""
        response = httpx.Response(401, request=httpx.Request("GET", "https://api.example.com"))
        error = httpx.HTTPStatusError("unauthorized", request=response.request, response=response)

        p1 = MockProvider("serper", fail_with=error)
        p2 = MockProvider("tavily")
        chain = ProviderChain(providers=[p1, p2])

        # First call — alert fires
        with patch("supacrawl.services.search.providers.LOGGER") as mock_logger:
            await chain.search("web", "test1", 5, "corr-1")
            first_warning_count = sum(
                1 for call in mock_logger.warning.call_args_list if "ACTION REQUIRED" in str(call)
            )

        # Reset p2 so it can be called again, but keep p1 failing
        p2._calls.clear()

        # Second call within debounce window — alert should be suppressed
        with patch("supacrawl.services.search.providers.LOGGER") as mock_logger2:
            await chain.search("web", "test2", 5, "corr-2")
            second_warning_count = sum(
                1 for call in mock_logger2.warning.call_args_list if "ACTION REQUIRED" in str(call)
            )

        assert first_warning_count == 1
        assert second_warning_count == 0


# Provider chain: search delegation


class TestProviderChain:
    """Tests for ProviderChain fallback logic."""

    @pytest.mark.asyncio
    async def test_uses_first_available_provider(self):
        p1 = MockProvider("brave")
        p2 = MockProvider("tavily")
        chain = ProviderChain(providers=[p1, p2])

        results = await chain.search("web", "test", 5, "corr-1")
        assert len(results) == 1
        assert results[0].url == "https://brave.com/1"
        assert p1._calls == ["web:test"]
        assert p2._calls == []

    @pytest.mark.asyncio
    async def test_falls_back_on_quota_error(self):
        """HTTP 429 on first provider should try second."""
        response = httpx.Response(429, request=httpx.Request("GET", "https://api.brave.com"))
        error = httpx.HTTPStatusError("rate limited", request=response.request, response=response)

        p1 = MockProvider("brave", fail_with=error)
        p2 = MockProvider("tavily")
        chain = ProviderChain(providers=[p1, p2])

        results = await chain.search("web", "test", 5, "corr-1")
        assert len(results) == 1
        assert results[0].url == "https://tavily.com/1"
        assert len(p1._calls) == 1
        assert len(p2._calls) == 1

    @pytest.mark.asyncio
    async def test_falls_back_on_captcha(self):
        """CAPTCHA error should trigger fallback."""
        error = ProviderError("CAPTCHA challenge detected", provider="duckduckgo")
        p1 = MockProvider("duckduckgo", fail_with=error)
        p2 = MockProvider("brave")
        chain = ProviderChain(providers=[p1, p2])

        results = await chain.search("web", "test", 5, "corr-1")
        assert results[0].url == "https://brave.com/1"

    @pytest.mark.asyncio
    async def test_does_not_fallback_on_non_fallback_error(self):
        """ValueError (malformed query) should NOT trigger fallback."""
        p1 = MockProvider("brave", fail_with=ValueError("bad query"))
        p2 = MockProvider("tavily")
        chain = ProviderChain(providers=[p1, p2])

        with pytest.raises(ValueError, match="bad query"):
            await chain.search("web", "test", 5, "corr-1")

        # Second provider should not have been called
        assert p2._calls == []

    @pytest.mark.asyncio
    async def test_all_providers_fail_raises_last_error(self):
        """If all providers fail with fallback errors, raises the last one."""
        response_429 = httpx.Response(429, request=httpx.Request("GET", "https://api.brave.com"))
        error1 = httpx.HTTPStatusError("rate limited", request=response_429.request, response=response_429)
        error2 = ProviderError("CAPTCHA detected", provider="duckduckgo")

        p1 = MockProvider("brave", fail_with=error1)
        p2 = MockProvider("duckduckgo", fail_with=error2)
        chain = ProviderChain(providers=[p1, p2])

        with pytest.raises(ProviderError, match="CAPTCHA"):
            await chain.search("web", "test", 5, "corr-1")

    @pytest.mark.asyncio
    async def test_skips_unavailable_providers(self):
        p1 = MockProvider("brave", available=False)
        p2 = MockProvider("tavily")
        chain = ProviderChain(providers=[p1, p2])

        results = await chain.search("web", "test", 5, "corr-1")
        assert results[0].url == "https://tavily.com/1"
        assert p1._calls == []

    @pytest.mark.asyncio
    async def test_no_available_providers_raises(self):
        p1 = MockProvider("brave", available=False)
        chain = ProviderChain(providers=[p1])

        with pytest.raises(RuntimeError, match="No usable search provider"):
            await chain.search("web", "test", 5, "corr-1")

    @pytest.mark.asyncio
    async def test_skips_provider_not_supporting_source(self):
        """NotImplementedError should skip to next provider silently."""

        class ImagesOnlyProvider(MockProvider):
            async def search_web(self, query, limit, correlation_id, filters=None):
                raise NotImplementedError("No web search")

        p1 = ImagesOnlyProvider("images-only")
        p2 = MockProvider("brave")
        chain = ProviderChain(providers=[p1, p2])

        results = await chain.search("web", "test", 5, "corr-1")
        assert results[0].url == "https://brave.com/1"

    @pytest.mark.asyncio
    async def test_health_tracking(self):
        response = httpx.Response(429, request=httpx.Request("GET", "https://api.brave.com"))
        error = httpx.HTTPStatusError("rate limited", request=response.request, response=response)

        p1 = MockProvider("brave", fail_with=error)
        p2 = MockProvider("tavily")
        chain = ProviderChain(providers=[p1, p2])

        await chain.search("web", "test", 5, "corr-1")

        health = chain.get_health()
        assert health["brave"]["status"] == "degraded"
        assert health["brave"]["consecutive_failures"] == 1
        assert health["tavily"]["status"] == "healthy"
        assert health["tavily"]["requests_made"] == 1

    @pytest.mark.asyncio
    async def test_close_closes_all_providers(self):
        p1 = MockProvider("brave")
        p2 = MockProvider("tavily")
        p1.close = AsyncMock()  # type: ignore[method-assign]  # intentional: mock
        p2.close = AsyncMock()  # type: ignore[method-assign]  # intentional: mock
        chain = ProviderChain(providers=[p1, p2])

        await chain.close()
        p1.close.assert_called_once()
        p2.close.assert_called_once()


# Registry: provider chain builder


class TestProviderRegistry:
    """Tests for build_provider_chain() factory."""

    def test_supported_providers_list(self):
        assert "brave" in SUPPORTED_PROVIDERS
        assert "tavily" in SUPPORTED_PROVIDERS
        assert "serper" in SUPPORTED_PROVIDERS
        assert "serpapi" in SUPPORTED_PROVIDERS
        assert "exa" in SUPPORTED_PROVIDERS
        assert "duckduckgo" in SUPPORTED_PROVIDERS

    def test_default_chain_is_brave(self):
        chain = build_provider_chain(brave_api_key="test-key")
        assert len(chain.providers) == 1
        assert chain.providers[0].name == "brave"

    def test_brave_without_key_adds_ddg_fallback(self):
        with patch.dict("os.environ", {"BRAVE_API_KEY": ""}, clear=False):
            chain = build_provider_chain(brave_api_key=None)
            names = [p.name for p in chain.providers]
            assert "brave" in names
            assert "duckduckgo" in names

    def test_comma_separated_string(self):
        chain = build_provider_chain("brave,tavily,duckduckgo", brave_api_key="key")
        names = [p.name for p in chain.providers]
        assert names == ["brave", "tavily", "duckduckgo"]

    def test_list_of_providers(self):
        chain = build_provider_chain(["serper", "exa"], brave_api_key="key")
        names = [p.name for p in chain.providers]
        assert names == ["serper", "exa"]

    def test_deduplicates_providers(self):
        chain = build_provider_chain("brave,brave,tavily", brave_api_key="key")
        names = [p.name for p in chain.providers]
        assert names == ["brave", "tavily"]

    def test_unknown_provider_skipped_with_warning(self):
        chain = build_provider_chain("brave,nonexistent,tavily", brave_api_key="key")
        names = [p.name for p in chain.providers]
        assert names == ["brave", "tavily"]

    def test_env_var_config(self):
        with patch.dict("os.environ", {"SUPACRAWL_SEARCH_PROVIDERS": "tavily,serper"}):
            chain = build_provider_chain(brave_api_key="key")
            names = [p.name for p in chain.providers]
            assert names == ["tavily", "serper"]


# Provider selection when the self-hosted backend URL is absent


class TestSearXNGBackendAbsent:
    """A missing SEARXNG_URL must never route queries to a third-party engine.

    supacrawl#156 was exactly this incident: the self-hosted backend dropped out
    of the configuration and search quietly kept answering from somewhere else,
    which for a household that self-hosts search to keep queries in-house is a
    privacy failure, not a degradation. The failure mode is live again now that
    the secrets broker can refuse to render a credential-bearing SEARXNG_URL and
    silently drop the variable — so it is asserted against here rather than
    rediscovered in production.
    """

    def test_absent_url_does_not_append_a_third_party_engine(self):
        chain = build_provider_chain("searxng")

        names = [p.name for p in chain.providers]
        assert names == ["searxng"], f"an engine nobody configured joined the chain: {names}"
        assert chain.providers[0].is_available() is False
        assert chain.active_providers == []
        assert chain.effective_provider is None
        assert chain.unconfigured_fallback_active is False

    def test_absent_url_fails_loudly_naming_the_variable(self):
        chain = build_provider_chain("searxng")

        with pytest.raises(RuntimeError) as excinfo:
            asyncio.run(chain.search("web", "anything", 1, "corr-searxng"))

        message = str(excinfo.value)
        assert "searxng" in message
        assert "SEARXNG_URL" in message, f"the error does not name the missing variable: {message}"

    def test_absent_url_alongside_an_unkeyed_provider_still_appends_nothing(self):
        """Two unusable providers is still not a licence to invent a third."""
        with patch.dict("os.environ", {"BRAVE_API_KEY": ""}, clear=False):
            chain = build_provider_chain("searxng,brave", brave_api_key=None)

        names = [p.name for p in chain.providers]
        assert names == ["searxng", "brave"]
        assert "duckduckgo" not in names
        assert chain.effective_provider is None

    def test_present_url_selects_searxng(self):
        """The inverse: a configured backend serves, and nothing reads as fallback."""
        with patch.dict("os.environ", {"SEARXNG_URL": "http://searxng.invalid"}):
            chain = build_provider_chain("searxng")

        assert [p.name for p in chain.providers] == ["searxng"]
        assert chain.effective_provider == "searxng"
        assert chain.unconfigured_fallback_active is False

    def test_credentials_alone_do_not_make_the_backend_usable(self):
        """The URL is the availability gate; a credential without one is nothing."""
        with patch.dict(
            "os.environ",
            {"SEARXNG_USERNAME": "searxng-user-placeholder", "SEARXNG_PASSWORD": "searxng-password-placeholder"},
        ):
            chain = build_provider_chain("searxng")

        assert chain.providers[0].is_available() is False
        assert chain.effective_provider is None

    def test_service_search_fails_rather_than_answering_from_elsewhere(self):
        """The operator-observable outcome, driven through the real service."""
        service = SearchService(providers=["searxng"])
        try:
            result = asyncio.run(service.search("anything", limit=1))
        finally:
            asyncio.run(service.close())

        assert result.success is False, "search answered from somewhere while the backend was unconfigured"
        assert result.error is not None
        assert "SEARXNG_URL" in result.error, f"the failure does not name the missing variable: {result.error}"


# SearchService: multi-provider integration


class TestSearchServiceMultiProvider:
    """Tests for SearchService with multi-provider configuration."""

    def test_providers_parameter_creates_chain(self):
        service = SearchService(providers="brave,tavily", brave_api_key="key")
        names = [p.name for p in service.provider_chain.providers]
        assert names == ["brave", "tavily"]

    def test_legacy_provider_parameter_still_works(self):
        service = SearchService(provider="brave", brave_api_key="key")
        assert service._provider == "brave"
        assert len(service.provider_chain.providers) == 1

    @pytest.mark.asyncio
    async def test_search_uses_chain_fallback(self):
        """Verify that search() uses the provider chain (not direct methods)."""
        service = SearchService(providers="brave,tavily", brave_api_key="key")

        # Mock the chain's search method
        mock_results = [
            SearchResultItem(url="https://tavily.com/1", title="Fallback result", source_type=SearchSourceType.WEB)
        ]
        service._chain.search = AsyncMock(return_value=mock_results)  # type: ignore[method-assign]  # intentional: mock

        result = await service.search("test query", limit=5)
        assert result.success
        assert len(result.data) == 1
        assert result.data[0].url == "https://tavily.com/1"

        service._chain.search.assert_called_once()
        await service.close()

    def test_provider_chain_property(self):
        service = SearchService(providers="serper,exa", brave_api_key="key")
        chain = service.provider_chain
        assert len(chain.providers) == 2
        assert chain.providers[0].name == "serper"
