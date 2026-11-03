"""Unit tests for _get_search_config() in mcp/tools/health.py.

The ``settings`` object in health.py is a pydantic-settings singleton
instantiated at import time, so env-var monkeypatching has no effect on its
attributes.  Instead we patch the singleton attributes directly via
``unittest.mock.patch.object`` and control ``os.getenv`` for the API-key
lookups that _are_ read at call time.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from supacrawl.mcp.tools.health import _get_search_config

pytestmark = pytest.mark.mcp

# Helpers

_SETTINGS_PATH = "supacrawl.mcp.tools.health.settings"


def _make_chain(active_names: list[str], all_names: list[str]) -> MagicMock:
    """Build a minimal ProviderChain mock for _get_search_config."""
    chain = MagicMock()

    def _make_provider(name: str) -> MagicMock:
        p = MagicMock()
        p.name = name
        return p

    chain.active_providers = [_make_provider(n) for n in active_names]
    chain.get_health.return_value = {n: {"status": "healthy", "requests_made": 1} for n in all_names}
    # Mirror the real ProviderChain contract: everything in the chain was asked
    # for, so no unconfigured fallback is in play (#158).
    chain.configured_names = list(all_names)
    chain.unconfigured_fallback_active = False
    # Mirror the real contract for a chain that has not served yet: nothing has
    # answered, so no fallback is in play (a bare MagicMock would read truthy).
    chain.fallback_serving = False
    chain.last_provider = None
    return chain


def _make_search_service(active_names: list[str], all_names: list[str]) -> MagicMock:
    """Build a minimal SearchService mock whose provider_chain is pre-wired."""
    svc = MagicMock()
    svc.provider_chain = _make_chain(active_names, all_names)
    return svc


def _settings_kwargs(
    *,
    search_providers: str | None = None,
    search_provider: str = "brave",
    search_rate_limit: float | None = None,
) -> dict:
    """Return keyword arguments for patching the settings singleton."""
    return {
        "search_providers": search_providers,
        "search_provider": search_provider,
        "search_rate_limit": search_rate_limit,
    }


# Static path (no live search service)


class TestGetSearchConfigStatic:
    """Tests for _get_search_config() without a live SearchService."""

    def test_brave_key_present_gives_ready_status(self) -> None:
        env = {"BRAVE_API_KEY": "test-brave-key"}
        with (
            patch(_SETTINGS_PATH, **_settings_kwargs()),
            patch.dict("os.environ", env, clear=False),
        ):
            result = _get_search_config()

        assert result["status"] == "ready"
        assert result["effective_provider"] == "brave"
        assert result["brave_api_key_configured"] is True

    def test_no_keys_gives_degraded_status(self) -> None:
        env_remove = dict.fromkeys(
            ("BRAVE_API_KEY", "TAVILY_API_KEY", "SERPER_API_KEY", "SERPAPI_API_KEY", "EXA_API_KEY"), ""
        )
        with (
            patch(_SETTINGS_PATH, **_settings_kwargs()),
            patch.dict("os.environ", env_remove, clear=False),
        ):
            # Unset by setting to empty — os.getenv returns empty string, bool("") is False
            result = _get_search_config()

        assert result["status"] == "degraded"

    def test_only_duckduckgo_configured_gives_degraded_with_warning(self) -> None:
        with (
            patch(_SETTINGS_PATH, **_settings_kwargs(search_providers="duckduckgo")),
            patch.dict("os.environ", {"BRAVE_API_KEY": ""}, clear=False),
        ):
            result = _get_search_config()

        # DDG needs no key but is marked degraded (deprecated/unreliable)
        assert result["status"] == "degraded"
        assert result["effective_provider"] == "duckduckgo"
        assert "warning" in result
        assert "DuckDuckGo" in result["warning"]

    def test_tavily_key_present_gives_ready(self) -> None:
        env = {"BRAVE_API_KEY": "", "TAVILY_API_KEY": "tv-key"}
        with (
            patch(_SETTINGS_PATH, **_settings_kwargs(search_providers="tavily")),
            patch.dict("os.environ", env, clear=False),
        ):
            result = _get_search_config()

        assert result["status"] == "ready"
        assert result["effective_provider"] == "tavily"

    def test_searxng_url_present_gives_ready(self) -> None:
        """A self-hosted backend is configured by URL, not by API key.

        The static dict enumerated only the keyed providers, so a correctly
        configured SearXNG instance reported ``effective_provider: "none"`` and
        ``degraded`` no matter what SEARXNG_URL said.
        """
        env = {"BRAVE_API_KEY": "", "SEARXNG_URL": "http://searxng.invalid"}
        with (
            patch(_SETTINGS_PATH, **_settings_kwargs(search_providers="searxng")),
            patch.dict("os.environ", env, clear=False),
        ):
            result = _get_search_config()

        assert result["status"] == "ready"
        assert result["effective_provider"] == "searxng"

    def test_searxng_without_url_stays_degraded(self) -> None:
        env = {"BRAVE_API_KEY": "", "SEARXNG_URL": ""}
        with (
            patch(_SETTINGS_PATH, **_settings_kwargs(search_providers="searxng")),
            patch.dict("os.environ", env, clear=False),
        ):
            result = _get_search_config()

        assert result["status"] == "degraded"
        assert result["effective_provider"] == "none"

    def test_mixed_configured_unconfigured_uses_first_configured(self) -> None:
        env = {"BRAVE_API_KEY": "", "TAVILY_API_KEY": "", "SERPER_API_KEY": "sr-key"}
        with (
            patch(_SETTINGS_PATH, **_settings_kwargs(search_providers="brave,tavily,serper")),
            patch.dict("os.environ", env, clear=False),
        ):
            result = _get_search_config()

        assert result["status"] == "ready"
        assert result["effective_provider"] == "serper"

    def test_configured_providers_list_reflects_settings(self) -> None:
        env = {"SERPER_API_KEY": "sr-key", "EXA_API_KEY": ""}
        with (
            patch(_SETTINGS_PATH, **_settings_kwargs(search_providers="serper,exa")),
            patch.dict("os.environ", env, clear=False),
        ):
            result = _get_search_config()

        assert result["configured_providers"] == ["serper", "exa"]

    def test_providers_health_dict_absent_without_service(self) -> None:
        with (
            patch(_SETTINGS_PATH, **_settings_kwargs()),
            patch.dict("os.environ", {"BRAVE_API_KEY": "bk"}, clear=False),
        ):
            result = _get_search_config()

        assert "providers" not in result

    def test_brave_api_key_configured_flag_reflects_env(self) -> None:
        with (
            patch(_SETTINGS_PATH, **_settings_kwargs()),
            patch.dict("os.environ", {"BRAVE_API_KEY": ""}, clear=False),
        ):
            result = _get_search_config()
        assert result["brave_api_key_configured"] is False

        with (
            patch(_SETTINGS_PATH, **_settings_kwargs()),
            patch.dict("os.environ", {"BRAVE_API_KEY": "real-key"}, clear=False),
        ):
            result = _get_search_config()
        assert result["brave_api_key_configured"] is True


# Live path (with a SearchService mock)


class TestGetSearchConfigLive:
    """Tests for _get_search_config() with a live SearchService mock."""

    def test_active_provider_gives_ready_status(self) -> None:
        svc = _make_search_service(active_names=["brave"], all_names=["brave"])
        with patch(_SETTINGS_PATH, **_settings_kwargs()):
            result = _get_search_config(svc)

        assert result["status"] == "ready"
        assert result["effective_provider"] == "brave"

    def test_no_active_providers_gives_degraded_status(self) -> None:
        svc = _make_search_service(active_names=[], all_names=["brave"])
        with patch(_SETTINGS_PATH, **_settings_kwargs(search_providers="brave")):
            result = _get_search_config(svc)

        assert result["status"] == "degraded"

    def test_providers_health_dict_present_with_service(self) -> None:
        svc = _make_search_service(active_names=["brave"], all_names=["brave"])
        with patch(_SETTINGS_PATH, **_settings_kwargs()):
            result = _get_search_config(svc)

        assert "providers" in result
        assert "brave" in result["providers"]

    def test_effective_provider_is_first_active(self) -> None:
        svc = _make_search_service(active_names=["tavily", "serper"], all_names=["brave", "tavily", "serper"])
        with patch(_SETTINGS_PATH, **_settings_kwargs(search_providers="brave,tavily,serper")):
            result = _get_search_config(svc)

        assert result["effective_provider"] == "tavily"

    def test_rate_limit_rps_present(self) -> None:
        with (
            patch(_SETTINGS_PATH, **_settings_kwargs()),
            patch.dict("os.environ", {"BRAVE_API_KEY": "bk"}, clear=False),
        ):
            result = _get_search_config()

        assert "rate_limit_rps" in result
        assert isinstance(result["rate_limit_rps"], float)
