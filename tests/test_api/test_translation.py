"""Tests for request translation logic (models + router helpers)."""

from __future__ import annotations

from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from supacrawl.api.models.scrape import ScrapeRequest
from supacrawl.api.routers.scrape import _build_service_kwargs, _translate_proxy

# Proxy translation


class TestProxyTranslation:
    """v2 proxy values resolve to a usable proxy URL or None.

    A local-first scraper has no managed proxy pool, so the v2 managed-proxy
    keywords and boolean flag resolve to None; only a concrete URL is applied.
    """

    def test_basic_keyword_resolves_to_none(self) -> None:
        assert _translate_proxy("basic") is None

    def test_enhanced_keyword_resolves_to_none(self) -> None:
        assert _translate_proxy("enhanced") is None

    def test_auto_keyword_resolves_to_none(self) -> None:
        assert _translate_proxy("auto") is None

    def test_url_passes_through(self) -> None:
        assert _translate_proxy("http://proxy:8080") == "http://proxy:8080"

    def test_socks_url_passes_through(self) -> None:
        assert _translate_proxy("socks5://user:pass@proxy:1080") == "socks5://user:pass@proxy:1080"

    def test_none_resolves_to_none(self) -> None:
        assert _translate_proxy(None) is None

    def test_bool_managed_proxy_resolves_to_none(self) -> None:
        assert _translate_proxy(True) is None
        assert _translate_proxy(False) is None


# Unknown fields


class TestUnknownFieldsIgnored:
    """The request model silently drops fields Supacrawl does not support."""

    def test_unknown_fields_dropped(self) -> None:
        req = ScrapeRequest.model_validate(
            {
                "url": "https://example.com",
                "skipTlsVerification": True,
                "removeBase64Images": True,
                "blockAds": True,
                "integration": {"id": "xyz"},
                "zeroDataRetention": True,
                "minAge": 5000,
            }
        )
        assert req.url == "https://example.com"
        assert not hasattr(req, "skip_tls_verification")

    def test_camel_case_aliases_accepted(self) -> None:
        req = ScrapeRequest.model_validate(
            {
                "url": "https://example.com",
                "onlyMainContent": False,
                "waitFor": 2000,
                "includeTags": ["article"],
                "excludeTags": ["nav"],
                "maxAge": 60000,
                "storeInCache": False,
            }
        )
        assert req.only_main_content is False
        assert req.wait_for == 2000
        assert req.include_tags == ["article"]
        assert req.exclude_tags == ["nav"]
        assert req.max_age == 60000
        assert req.store_in_cache is False


# Defaults


class TestDefaults:
    """Default values match the v2 protocol."""

    def test_defaults_applied(self) -> None:
        req = ScrapeRequest.model_validate({"url": "https://example.com"})
        assert req.only_main_content is True
        assert req.wait_for == 0
        assert req.timeout == 30000
        assert req.formats is None
        assert req.max_age is None
        assert req.store_in_cache is None


# maxAge millisecond-to-second conversion


class TestMaxAgeConversion:
    """``maxAge`` is divided by 1000 (integer division) for the service."""

    def test_max_age_ms_to_seconds(self) -> None:
        req = ScrapeRequest.model_validate({"url": "https://example.com", "maxAge": 60000})
        kwargs = _build_service_kwargs(req)
        assert kwargs["max_age"] == 60

    def test_max_age_rounds_down(self) -> None:
        req = ScrapeRequest.model_validate({"url": "https://example.com", "maxAge": 1500})
        kwargs = _build_service_kwargs(req)
        assert kwargs["max_age"] == 1

    def test_store_in_cache_false_sets_zero(self) -> None:
        """``storeInCache: false`` overrides maxAge to 0."""
        req = ScrapeRequest.model_validate({"url": "https://example.com", "storeInCache": False, "maxAge": 60000})
        kwargs = _build_service_kwargs(req)
        assert kwargs["max_age"] == 0

    def test_store_in_cache_true_keeps_max_age(self) -> None:
        """``storeInCache: true`` does not interfere with maxAge."""
        req = ScrapeRequest.model_validate({"url": "https://example.com", "storeInCache": True, "maxAge": 30000})
        kwargs = _build_service_kwargs(req)
        assert kwargs["max_age"] == 30


# End-to-end service kwargs via TestClient


class TestServiceKwargsViaClient:
    """Verify the full path from HTTP body to service call arguments."""

    def test_max_age_via_client(self, client: TestClient, mock_scrape_service: AsyncMock) -> None:
        client.post(
            "/scrape",
            json={"url": "https://example.com", "maxAge": 120000},
        )
        call_kwargs = mock_scrape_service.scrape.call_args.kwargs
        assert call_kwargs["max_age"] == 120

    def test_proxy_url_passed_through_via_client(self, client: TestClient, mock_scrape_service: AsyncMock) -> None:
        """A concrete proxy URL reaches ScrapeService.scrape() as a string (regression for #112)."""
        client.post(
            "/scrape",
            json={"url": "https://example.com", "proxy": "http://user:pass@proxy-host:8080"},
        )
        call_kwargs = mock_scrape_service.scrape.call_args.kwargs
        assert call_kwargs["proxy"] == "http://user:pass@proxy-host:8080"

    def test_managed_proxy_keyword_dropped_via_client(self, client: TestClient, mock_scrape_service: AsyncMock) -> None:
        """Unsupported v2 managed-proxy keywords are not forwarded to the service."""
        client.post(
            "/scrape",
            json={"url": "https://example.com", "proxy": "basic"},
        )
        call_kwargs = mock_scrape_service.scrape.call_args.kwargs
        assert "proxy" not in call_kwargs
