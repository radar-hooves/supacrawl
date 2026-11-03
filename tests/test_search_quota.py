"""Tests for search credit/quota visibility and failover (#136).

Per-provider credit capability summary (documented in brave.py):
  Brave    — X-RateLimit-Remaining / X-RateLimit-Limit on every response (proactive)
  Serper   — no headers; dashboard-only (reactive error + failover only)
  SerpAPI  — no headers; reactive only
  Tavily   — no headers; reactive only
  Exa      — no headers; reactive only
  SearXNG  — self-hosted; n/a
"""

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from supacrawl.exceptions import ProviderError
from supacrawl.models import SearchFilters, SearchResultItem, SearchSourceType
from supacrawl.services.search.brave import BraveProvider
from supacrawl.services.search.providers import (
    LOW_CREDIT_THRESHOLD,
    ProviderChain,
    ProviderHealth,
)

CORRELATION_ID = "test-quota-1234"


# Helpers


def _brave_response(
    status: int,
    body: dict,
    *,
    remaining: int | None = None,
    limit: int | None = None,
) -> httpx.Response:
    """Build a minimal Brave API httpx.Response, optionally with quota headers."""
    headers: dict[str, str] = {"content-type": "application/json"}
    if remaining is not None:
        headers["X-RateLimit-Remaining"] = str(remaining)
    if limit is not None:
        headers["X-RateLimit-Limit"] = str(limit)
    return httpx.Response(
        status,
        content=json.dumps(body).encode(),
        headers=headers,
        request=httpx.Request("GET", "https://api.search.brave.com/res/v1/web/search"),
    )


WEB_BODY = {"web": {"results": [{"url": "https://example.com", "title": "Example", "description": "A page."}]}}

EMPTY_BODY: dict = {"web": {"results": []}}


class MockProvider:
    """Minimal SearchProvider test double."""

    def __init__(self, name: str, *, available: bool = True, fail_with: Exception | None = None) -> None:
        self._name = name
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
                url=f"https://{self._name}.com/1",
                title=f"{self._name} result",
                source_type=SearchSourceType.WEB,
            )
        ]

    async def search_images(
        self, query: str, limit: int, correlation_id: str, filters: SearchFilters | None = None
    ) -> list[SearchResultItem]:
        raise NotImplementedError

    async def search_news(
        self, query: str, limit: int, correlation_id: str, filters: SearchFilters | None = None
    ) -> list[SearchResultItem]:
        raise NotImplementedError

    async def close(self) -> None:
        pass


# BraveProvider: quota header parsing


class TestBraveProviderQuotaHeader:
    """BraveProvider reads X-RateLimit-Remaining from responses."""

    @pytest.mark.asyncio
    async def test_remaining_credits_populated_from_header(self) -> None:
        """remaining_credits is set from X-RateLimit-Remaining on a successful response."""
        provider = BraveProvider(api_key="test-key")
        try:
            mock_client = AsyncMock()
            mock_client.get.return_value = _brave_response(200, WEB_BODY, remaining=450)

            with patch.object(provider, "_get_client", return_value=mock_client):
                await provider.search_web("python", 5, CORRELATION_ID)

            assert provider.remaining_credits == 450
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_remaining_credits_updates_on_each_call(self) -> None:
        """remaining_credits reflects the most recent response header."""
        provider = BraveProvider(api_key="test-key")
        try:
            mock_client = AsyncMock()
            mock_client.get.side_effect = [
                _brave_response(200, WEB_BODY, remaining=500),
                _brave_response(200, WEB_BODY, remaining=499),
            ]

            with patch.object(provider, "_get_client", return_value=mock_client):
                await provider.search_web("python", 5, CORRELATION_ID)
                assert provider.remaining_credits == 500
                await provider.search_web("python", 5, CORRELATION_ID)
                assert provider.remaining_credits == 499
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_remaining_credits_none_when_header_absent(self) -> None:
        """remaining_credits stays None when the response carries no quota header."""
        provider = BraveProvider(api_key="test-key")
        try:
            mock_client = AsyncMock()
            mock_client.get.return_value = _brave_response(200, WEB_BODY)  # no header

            with patch.object(provider, "_get_client", return_value=mock_client):
                await provider.search_web("python", 5, CORRELATION_ID)

            assert provider.remaining_credits is None
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_malformed_remaining_header_leaves_previous_value(self) -> None:
        """A non-integer X-RateLimit-Remaining header does not overwrite a good value."""
        provider = BraveProvider(api_key="test-key")
        provider.remaining_credits = 300  # simulate prior good value
        try:
            mock_client = AsyncMock()
            response = _brave_response(200, WEB_BODY)
            response.headers["X-RateLimit-Remaining"] = "not-a-number"
            mock_client.get.return_value = response

            with patch.object(provider, "_get_client", return_value=mock_client):
                await provider.search_web("python", 5, CORRELATION_ID)

            assert provider.remaining_credits == 300
        finally:
            await provider.close()


# ProviderChain: quota synced into ProviderHealth after success


class TestProviderChainQuotaSync:
    """ProviderChain syncs remaining_credits from provider into ProviderHealth."""

    @pytest.mark.asyncio
    async def test_quota_synced_into_health_after_success(self) -> None:
        """Successful Brave call syncs remaining_credits into ProviderHealth."""
        brave = BraveProvider(api_key="test-key")
        try:
            mock_client = AsyncMock()
            mock_client.get.return_value = _brave_response(200, WEB_BODY, remaining=200)

            with patch.object(brave, "_get_client", return_value=mock_client):
                chain = ProviderChain(providers=[brave])
                await chain.search("web", "test", 5, CORRELATION_ID)

            health = chain.get_health()
            assert health["brave"]["remaining_credits"] == 200
        finally:
            await brave.close()

    @pytest.mark.asyncio
    async def test_header_less_provider_no_remaining_credits_in_health(self) -> None:
        """A provider without quota headers does not emit remaining_credits (avoids false signal)."""
        p = MockProvider("serper")  # Serper: no quota headers
        chain = ProviderChain(providers=[p])
        await chain.search("web", "test", 5, CORRELATION_ID)

        health = chain.get_health()
        assert "remaining_credits" not in health["serper"]

    @pytest.mark.asyncio
    async def test_low_credit_warning_logged_when_below_threshold(self) -> None:
        """A low remaining_credits triggers a LOGGER.warning."""
        brave = BraveProvider(api_key="test-key")
        try:
            low = LOW_CREDIT_THRESHOLD - 1
            mock_client = AsyncMock()
            mock_client.get.return_value = _brave_response(200, WEB_BODY, remaining=low)

            with patch.object(brave, "_get_client", return_value=mock_client):
                chain = ProviderChain(providers=[brave])
                with patch("supacrawl.services.search.providers.LOGGER") as mock_logger:
                    await chain.search("web", "test", 5, CORRELATION_ID)

            warning_calls = list(mock_logger.warning.call_args_list)
            assert any("LOW CREDIT WARNING" in str(c) for c in warning_calls)
        finally:
            await brave.close()

    @pytest.mark.asyncio
    async def test_no_low_credit_warning_when_above_threshold(self) -> None:
        """No low-credit warning when remaining credits are above the threshold."""
        brave = BraveProvider(api_key="test-key")
        try:
            mock_client = AsyncMock()
            mock_client.get.return_value = _brave_response(200, WEB_BODY, remaining=LOW_CREDIT_THRESHOLD + 1)

            with patch.object(brave, "_get_client", return_value=mock_client):
                chain = ProviderChain(providers=[brave])
                with patch("supacrawl.services.search.providers.LOGGER") as mock_logger:
                    await chain.search("web", "test", 5, CORRELATION_ID)

            warning_calls = list(mock_logger.warning.call_args_list)
            assert not any("LOW CREDIT WARNING" in str(c) for c in warning_calls)
        finally:
            await brave.close()


# ProviderChain: out-of-credits error triggers failover


class TestProviderChainCreditFailover:
    """Out-of-credits errors on one provider trigger failover to the next."""

    @pytest.mark.asyncio
    async def test_http_402_triggers_failover(self) -> None:
        """HTTP 402 Payment Required triggers failover to next provider."""
        response = httpx.Response(402, request=httpx.Request("GET", "https://api.example.com"))
        error = httpx.HTTPStatusError("payment required", request=response.request, response=response)

        p1 = MockProvider("brave", fail_with=error)
        p2 = MockProvider("serper")
        chain = ProviderChain(providers=[p1, p2])

        results = await chain.search("web", "test", 5, CORRELATION_ID)
        assert results[0].url == "https://serper.com/1"
        assert len(p1._calls) == 1
        assert len(p2._calls) == 1

    @pytest.mark.asyncio
    async def test_out_of_credits_body_on_403_triggers_failover(self) -> None:
        """403 with credit-exhaustion body triggers failover."""
        response = httpx.Response(
            403,
            request=httpx.Request("GET", "https://api.example.com"),
            content=b'{"error": "Quota exceeded for this API key"}',
        )
        error = httpx.HTTPStatusError("forbidden", request=response.request, response=response)

        p1 = MockProvider("brave", fail_with=error)
        p2 = MockProvider("serper")
        chain = ProviderChain(providers=[p1, p2])

        results = await chain.search("web", "test", 5, CORRELATION_ID)
        assert results[0].url == "https://serper.com/1"

    @pytest.mark.asyncio
    async def test_all_out_of_credits_surfaces_last_error(self) -> None:
        """When all providers are exhausted, the last error is raised."""
        err1 = httpx.HTTPStatusError(
            "payment required",
            request=httpx.Request("GET", "https://api.example.com"),
            response=httpx.Response(402, request=httpx.Request("GET", "https://api.example.com")),
        )
        err2 = ProviderError("Quota exceeded", provider="serper")

        p1 = MockProvider("brave", fail_with=err1)
        p2 = MockProvider("serper", fail_with=err2)
        chain = ProviderChain(providers=[p1, p2])

        with pytest.raises(ProviderError, match="Quota"):
            await chain.search("web", "test", 5, CORRELATION_ID)


# ProviderHealth: to_dict includes remaining_credits when populated


class TestProviderHealthQuotaSerialisation:
    """ProviderHealth.to_dict() correctly includes/excludes remaining_credits."""

    def test_remaining_credits_absent_by_default(self) -> None:
        """remaining_credits is not included when never set (None)."""
        health = ProviderHealth()
        assert "remaining_credits" not in health.to_dict()

    def test_remaining_credits_present_after_record_quota(self) -> None:
        """remaining_credits is included in to_dict() after record_quota()."""
        health = ProviderHealth()
        health.record_quota(350)
        d = health.to_dict()
        assert d["remaining_credits"] == 350

    def test_record_quota_updates_value(self) -> None:
        """record_quota() overwrites the previous value."""
        health = ProviderHealth()
        health.record_quota(500)
        health.record_quota(499)
        assert health.to_dict()["remaining_credits"] == 499


# Health tool: low-credit warning surfaced at config level


@pytest.mark.mcp
class TestHealthToolLowCreditWarning:
    """_get_search_config surfaces low-credit warning when a provider is near-exhausted."""

    def test_low_credit_warning_in_config_when_below_threshold(self) -> None:
        """A provider with remaining_credits below threshold triggers a config-level warning."""
        from unittest.mock import MagicMock

        from supacrawl.mcp.tools.health import _get_search_config

        low = LOW_CREDIT_THRESHOLD - 1
        chain = MagicMock()
        chain.active_providers = [MagicMock(name="brave")]
        chain.active_providers[0].name = "brave"
        chain.configured_names = ["brave"]
        chain.unconfigured_fallback_active = False
        # Real-contract fields for a chain that has not served yet; a bare
        # MagicMock would read truthy and fabricate a fallback signal.
        chain.fallback_serving = False
        chain.last_provider = None
        chain.get_health.return_value = {
            "brave": {
                "status": "healthy",
                "requests_made": 10,
                "consecutive_failures": 0,
                "last_error": None,
                "available": True,
                "remaining_credits": low,
            }
        }

        svc = MagicMock()
        svc.provider_chain = chain

        _SETTINGS_PATH = "supacrawl.mcp.tools.health.settings"
        with patch(_SETTINGS_PATH, search_providers=None, search_provider="brave", search_rate_limit=None):
            result = _get_search_config(svc)

        assert "warning" in result
        assert "Low search credits" in result["warning"]
        assert "brave" in result["warning"]

    def test_no_warning_when_credits_above_threshold(self) -> None:
        """No low-credit warning when remaining credits are above the threshold."""
        from unittest.mock import MagicMock

        from supacrawl.mcp.tools.health import _get_search_config

        chain = MagicMock()
        chain.active_providers = [MagicMock()]
        chain.active_providers[0].name = "brave"
        chain.configured_names = ["brave"]
        chain.unconfigured_fallback_active = False
        # Real-contract fields for a chain that has not served yet; a bare
        # MagicMock would read truthy and fabricate a fallback signal.
        chain.fallback_serving = False
        chain.last_provider = None
        chain.get_health.return_value = {
            "brave": {
                "status": "healthy",
                "requests_made": 1,
                "consecutive_failures": 0,
                "last_error": None,
                "available": True,
                "remaining_credits": LOW_CREDIT_THRESHOLD + 50,
            }
        }

        svc = MagicMock()
        svc.provider_chain = chain

        _SETTINGS_PATH = "supacrawl.mcp.tools.health.settings"
        with patch(_SETTINGS_PATH, search_providers=None, search_provider="brave", search_rate_limit=None):
            result = _get_search_config(svc)

        assert "warning" not in result

    def test_no_false_signal_for_header_less_provider(self) -> None:
        """A provider without remaining_credits in health does not trigger a warning."""
        from unittest.mock import MagicMock

        from supacrawl.mcp.tools.health import _get_search_config

        chain = MagicMock()
        chain.active_providers = [MagicMock()]
        chain.active_providers[0].name = "serper"
        chain.configured_names = ["serper"]
        chain.unconfigured_fallback_active = False
        # Real-contract fields for a chain that has not served yet; a bare
        # MagicMock would read truthy and fabricate a fallback signal.
        chain.fallback_serving = False
        chain.last_provider = None
        chain.get_health.return_value = {
            "serper": {
                "status": "healthy",
                "requests_made": 5,
                "consecutive_failures": 0,
                "last_error": None,
                "available": True,
                # No remaining_credits — Serper does not expose quota headers
            }
        }

        svc = MagicMock()
        svc.provider_chain = chain

        _SETTINGS_PATH = "supacrawl.mcp.tools.health.settings"
        with patch(_SETTINGS_PATH, search_providers=None, search_provider="serper", search_rate_limit=None):
            result = _get_search_config(svc)

        assert "warning" not in result
