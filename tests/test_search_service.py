"""Tests for search service."""

import warnings
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from supacrawl.exceptions import ProviderError
from supacrawl.models import SearchResult, SearchResultItem, SearchSourceType
from supacrawl.services.search import SearchService
from supacrawl.services.search.duckduckgo import DuckDuckGoProvider


def _ddg_service(**kwargs) -> SearchService:
    """Create a SearchService using DuckDuckGo (deprecated) for network tests."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return SearchService(provider="duckduckgo", **kwargs)


def _require_results(result: SearchResult) -> None:
    """Skip (not fail) when the live search provider is unavailable.

    These tests query the keyless DuckDuckGo endpoint, which is frequently
    bot-blocked (HTTP 403/CAPTCHA). A provider being unreachable is an
    environment gap, not a defect, so the test skips rather than failing the suite.
    """
    if not result.success:
        pytest.skip(f"live search provider unavailable: {result.error}")


@pytest.mark.e2e
class TestSearchService:
    """Tests for SearchService using DuckDuckGo (network-dependent)."""

    @pytest.mark.asyncio
    async def test_search_returns_web_results(self):
        """Test that search returns web results by default."""
        service = _ddg_service()
        try:
            result = await service.search("python programming language", limit=3)
            assert isinstance(result, SearchResult)
            _require_results(result)
            # Web search may return empty results due to rate limiting
            # Just check that any results have correct source_type
            for item in result.data:
                assert item.source_type == SearchSourceType.WEB
        finally:
            await service.close()

    @pytest.mark.asyncio
    async def test_search_respects_limit(self):
        """Test that search respects the limit parameter."""
        service = _ddg_service()
        try:
            result = await service.search("python", limit=2)
            _require_results(result)
            assert len(result.data) <= 2
        finally:
            await service.close()

    @pytest.mark.asyncio
    async def test_search_result_structure(self):
        """Test that search results have correct structure."""
        service = _ddg_service()
        try:
            result = await service.search("example", limit=1)
            _require_results(result)
            if result.data:
                item = result.data[0]
                assert isinstance(item, SearchResultItem)
                assert isinstance(item.url, str)
                assert len(item.url) > 0
                assert isinstance(item.title, str)
                assert item.source_type == SearchSourceType.WEB
        finally:
            await service.close()

    @pytest.mark.asyncio
    async def test_search_with_web_source(self):
        """Test search with explicit web source."""
        service = _ddg_service()
        try:
            result = await service.search("python", limit=3, sources=["web"])
            _require_results(result)
            for item in result.data:
                assert item.source_type == SearchSourceType.WEB
        finally:
            await service.close()

    @pytest.mark.asyncio
    async def test_search_with_images_source(self):
        """Test search with images source type."""
        service = _ddg_service()
        try:
            result = await service.search("cat", limit=3, sources=["images"])
            assert isinstance(result, SearchResult)
            # Image search may return empty results if vqd token extraction fails
            # so we just check the structure is correct
            for item in result.data:
                assert item.source_type == SearchSourceType.IMAGES
        finally:
            await service.close()

    @pytest.mark.asyncio
    async def test_search_with_news_source(self):
        """Test search with news source type."""
        service = _ddg_service()
        try:
            result = await service.search("technology", limit=3, sources=["news"])
            assert isinstance(result, SearchResult)
            # News search may return empty results if vqd token extraction fails
            for item in result.data:
                assert item.source_type == SearchSourceType.NEWS
        finally:
            await service.close()

    @pytest.mark.asyncio
    async def test_search_with_multiple_sources(self):
        """Test search with multiple source types."""
        service = _ddg_service()
        try:
            result = await service.search("technology", limit=3, sources=["web", "news"])
            assert isinstance(result, SearchResult)
            _require_results(result)
            # May have results from web, news, or both
            # Just verify that all results have valid source types
            for item in result.data:
                assert item.source_type in (SearchSourceType.WEB, SearchSourceType.NEWS)
        finally:
            await service.close()

    @pytest.mark.asyncio
    async def test_search_image_result_fields(self):
        """Test that image results have image-specific fields."""
        service = _ddg_service()
        try:
            result = await service.search("landscape", limit=5, sources=["images"])
            # If we got image results, check image-specific fields
            for item in result.data:
                if item.source_type == SearchSourceType.IMAGES:
                    # Image results should have URL
                    assert item.url is not None
                    # Thumbnail may or may not be present
                    # Width/height may or may not be present
        finally:
            await service.close()

    @pytest.mark.asyncio
    async def test_search_news_result_fields(self):
        """Test that news results have news-specific fields."""
        service = _ddg_service()
        try:
            result = await service.search("technology", limit=5, sources=["news"])
            # If we got news results, check news-specific fields
            for item in result.data:
                if item.source_type == SearchSourceType.NEWS:
                    assert item.url is not None
                    # published_at and source_name may or may not be present
        finally:
            await service.close()

    @pytest.mark.asyncio
    async def test_search_handles_unknown_source_type(self):
        """Test that search handles unknown source types gracefully."""
        service = _ddg_service()
        try:
            # This should not raise, just skip unknown sources
            result = await service.search(
                "python",
                limit=3,
                sources=["web", "unknown"],  # type: ignore
            )
            assert isinstance(result, SearchResult)
        finally:
            await service.close()

    @pytest.mark.asyncio
    async def test_search_clamps_limit(self):
        """Test that search clamps limit to valid range."""
        service = _ddg_service()
        try:
            # Limit below minimum should be clamped to 1
            result = await service.search("python", limit=0)
            _require_results(result)

            # Limit above maximum should be clamped to 10
            result = await service.search("python", limit=100)
            _require_results(result)
            # Should not return more than 10 results
            assert len(result.data) <= 10
        finally:
            await service.close()


class TestSearchSourceType:
    """Tests for SearchSourceType enum."""

    def test_source_type_values(self):
        """Test that source type enum has expected values."""
        assert SearchSourceType.WEB.value == "web"
        assert SearchSourceType.IMAGES.value == "images"
        assert SearchSourceType.NEWS.value == "news"

    def test_source_type_is_string_enum(self):
        """Test that source type values can be used as strings."""
        # SearchSourceType inherits from str, so .value is the string
        assert SearchSourceType.WEB.value == "web"
        assert SearchSourceType.IMAGES.value == "images"
        assert SearchSourceType.NEWS.value == "news"
        # Can be compared to strings
        assert SearchSourceType.WEB == "web"
        assert SearchSourceType.IMAGES == "images"
        assert SearchSourceType.NEWS == "news"


class TestSearchResultItem:
    """Tests for SearchResultItem model."""

    def test_default_source_type_is_web(self):
        """Test that default source_type is web."""
        item = SearchResultItem(url="https://example.com", title="Test")
        assert item.source_type == SearchSourceType.WEB

    def test_image_result_fields(self):
        """Test image-specific fields on SearchResultItem."""
        item = SearchResultItem(
            url="https://example.com/image.jpg",
            title="Test Image",
            source_type=SearchSourceType.IMAGES,
            thumbnail="https://example.com/thumb.jpg",
            image_width=800,
            image_height=600,
        )
        assert item.source_type == SearchSourceType.IMAGES
        assert item.thumbnail == "https://example.com/thumb.jpg"
        assert item.image_width == 800
        assert item.image_height == 600

    def test_news_result_fields(self):
        """Test news-specific fields on SearchResultItem."""
        item = SearchResultItem(
            url="https://example.com/article",
            title="Test Article",
            source_type=SearchSourceType.NEWS,
            published_at="2024-12-26T10:00:00Z",
            source_name="Example News",
        )
        assert item.source_type == SearchSourceType.NEWS
        assert item.published_at == "2024-12-26T10:00:00Z"
        assert item.source_name == "Example News"

    def test_description_is_optional(self):
        """Test that description field is optional."""
        item = SearchResultItem(url="https://example.com", title="Test")
        assert item.description is None

    def test_scraped_content_fields(self):
        """Test scraped content fields on SearchResultItem."""
        item = SearchResultItem(
            url="https://example.com",
            title="Test",
            markdown="# Heading\n\nContent",
            html="<h1>Heading</h1><p>Content</p>",
        )
        assert item.markdown == "# Heading\n\nContent"
        assert item.html == "<h1>Heading</h1><p>Content</p>"


class TestDefaultProviderSelection:
    """Tests for default provider selection and fallback behaviour."""

    def test_default_provider_is_brave(self):
        """Default provider should be brave."""
        service = SearchService(brave_api_key="test-key")
        assert service._provider == "brave"

    def test_brave_without_key_falls_back_to_duckduckgo(self):
        """Brave without API key should fall back to DuckDuckGo."""
        with patch.dict("os.environ", {"BRAVE_API_KEY": ""}, clear=False):
            # brave_api_key=None forces reading from env, which is empty
            service = SearchService(brave_api_key=None)
            assert service._provider == "duckduckgo"

    def test_duckduckgo_explicit_emits_deprecation_warning(self):
        """Explicitly selecting DuckDuckGo should emit a DeprecationWarning."""
        with pytest.warns(DeprecationWarning, match="DuckDuckGo search is deprecated"):
            SearchService(provider="duckduckgo")

    def test_brave_with_key_uses_brave(self):
        """Brave with API key should use Brave."""
        service = SearchService(brave_api_key="test-key")
        assert service._provider == "brave"
        assert service._brave_api_key == "test-key"


@pytest.mark.asyncio
class TestKeylessLoudFailure:
    """A keyless chain that returns nothing must fail loudly, not silently (#132)."""

    async def test_empty_keyless_result_is_loud(self):
        # No key configured -> brave unavailable, only the DuckDuckGo fallback is
        # active. An empty DDG response must surface an actionable error.
        service = SearchService(brave_api_key=None)
        assert service._has_keyed_provider() is False
        service._chain.search = AsyncMock(return_value=[])  # type: ignore[method-assign]
        result = await service.search("anything", limit=3)
        assert result.success is False
        assert result.error is not None and "BRAVE_API_KEY" in result.error
        assert "brave.com/search/api" in result.error

    async def test_empty_keyed_result_is_not_loud(self):
        # With a real key configured, an empty result is a genuine no-match, not a
        # block — it must stay a clean success, not a false alarm.
        service = SearchService(brave_api_key="test-key")
        assert service._has_keyed_provider() is True
        service._chain.search = AsyncMock(return_value=[])  # type: ignore[method-assign]
        result = await service.search("anything", limit=3)
        assert result.success is True
        assert result.data == []

    async def test_circuit_broken_key_with_empty_is_loud(self):
        # A keyed provider that is circuit-broken (cooled down after repeated
        # failures) must not mask the keyless case: an empty result behind a dead
        # key should still fail loudly, not silently succeed.
        service = SearchService(providers="brave,duckduckgo", brave_api_key="test-key")
        brave_health = service._chain._health["brave"]
        for _ in range(brave_health.UNAVAILABLE_THRESHOLD):
            brave_health.record_failure("boom")
        assert service._has_keyed_provider() is False  # brave excluded from active
        service._chain.search = AsyncMock(return_value=[])  # type: ignore[method-assign]
        result = await service.search("anything", limit=3)
        assert result.success is False
        assert result.error is not None and "BRAVE_API_KEY" in result.error


class TestDuckDuckGoCaptchaDetection:
    """Tests for DuckDuckGo CAPTCHA/bot detection handling."""

    CAPTCHA_HTML = """
    <html><body>
    <div class="anomaly-modal__title">Unfortunately, bots use DuckDuckGo too.</div>
    <div class="anomaly-modal__description">Please complete the following challenge...</div>
    <div class="anomaly-modal__instructions">Select all squares containing a duck:</div>
    </body></html>
    """

    NORMAL_HTML = """
    <html><body>
    <table>
    <tr><td><a class="result-link" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com">Example</a></td></tr>
    <tr><td class="result-snippet">A test snippet.</td></tr>
    </table>
    </body></html>
    """

    @pytest.mark.asyncio
    async def test_captcha_http_202_raises_provider_error(self):
        """HTTP 202 from DDG Lite should raise ProviderError."""
        mock_response = httpx.Response(
            status_code=202,
            text=self.CAPTCHA_HTML,
            request=httpx.Request("GET", "https://lite.duckduckgo.com/lite/"),
        )
        provider = DuckDuckGoProvider()
        try:
            with patch.object(provider, "_get_client") as mock_get:
                mock_client = AsyncMock()
                mock_client.get.return_value = mock_response
                mock_get.return_value = mock_client

                with pytest.raises(ProviderError, match="CAPTCHA"):
                    await provider.search_web("hello", 5, "test-corr")
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_captcha_anomaly_modal_in_html_raises_provider_error(self):
        """anomaly-modal in response body should raise ProviderError even with HTTP 200."""
        mock_response = httpx.Response(
            status_code=200,
            text=self.CAPTCHA_HTML,
            request=httpx.Request("GET", "https://lite.duckduckgo.com/lite/"),
        )
        provider = DuckDuckGoProvider()
        try:
            with patch.object(provider, "_get_client") as mock_get:
                mock_client = AsyncMock()
                mock_client.get.return_value = mock_response
                mock_get.return_value = mock_client

                with pytest.raises(ProviderError, match="CAPTCHA"):
                    await provider.search_web("hello", 5, "test-corr")
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_normal_response_returns_results(self):
        """Normal DDG response should return parsed results, not raise."""
        mock_response = httpx.Response(
            status_code=200,
            text=self.NORMAL_HTML,
            request=httpx.Request("GET", "https://lite.duckduckgo.com/lite/"),
        )
        provider = DuckDuckGoProvider()
        try:
            with patch.object(provider, "_get_client") as mock_get:
                mock_client = AsyncMock()
                mock_client.get.return_value = mock_response
                mock_get.return_value = mock_client

                results = await provider.search_web("hello", 5, "test-corr")
                assert len(results) == 1
                assert results[0].url == "https://example.com"
                assert results[0].title == "Example"
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_captcha_surfaces_as_search_failure(self):
        """CAPTCHA should surface as success=False through the search() method."""
        mock_response = httpx.Response(
            status_code=202,
            text=self.CAPTCHA_HTML,
            request=httpx.Request("GET", "https://lite.duckduckgo.com/lite/"),
        )
        service = _ddg_service()
        try:
            with patch("supacrawl.services.search.duckduckgo.DuckDuckGoProvider._get_client") as mock_get:
                mock_client = AsyncMock()
                mock_client.get.return_value = mock_response
                mock_get.return_value = mock_client

                result = await service.search("hello", limit=5)
                assert not result.success
                assert result.error is not None
                assert "CAPTCHA" in result.error
        finally:
            await service.close()

    def test_user_agent_is_browser_like(self):
        """HTTP client should use a browser-like User-Agent, not Supacrawl/1.0."""
        from supacrawl.services.search import _SEARCH_USER_AGENT

        assert "Supacrawl" not in _SEARCH_USER_AGENT
        assert "Mozilla" in _SEARCH_USER_AGENT


class TestBrowserHeaders:
    """Tests for realistic browser header profile."""

    def test_browser_headers_include_all_required_fields(self):
        """Header profile should include all headers a real Chrome session sends."""
        from supacrawl.services.search import _BROWSER_HEADERS

        required = [
            "User-Agent",
            "Accept",
            "Accept-Language",
            "Accept-Encoding",
            "Sec-Fetch-Dest",
            "Sec-Fetch-Mode",
            "Sec-Fetch-Site",
            "Sec-Fetch-User",
            "Upgrade-Insecure-Requests",
            "Sec-Ch-Ua",
            "Sec-Ch-Ua-Mobile",
            "Sec-Ch-Ua-Platform",
            "Connection",
        ]
        for header in required:
            assert header in _BROWSER_HEADERS, f"Missing header: {header}"

    def test_sec_ch_ua_version_matches_user_agent(self):
        """Sec-Ch-Ua Chrome version should match the User-Agent version."""
        import re

        from supacrawl.services.search import _BROWSER_HEADERS, _SEARCH_USER_AGENT

        # Extract Chrome version from User-Agent (e.g., "Chrome/131.0.0.0")
        ua_match = re.search(r"Chrome/(\d+)", _SEARCH_USER_AGENT)
        assert ua_match, "Could not extract Chrome version from User-Agent"
        ua_version = ua_match.group(1)

        # Sec-Ch-Ua should reference the same major version
        assert ua_version in _BROWSER_HEADERS["Sec-Ch-Ua"]

    def test_default_accept_language_is_en_us(self):
        """Default Accept-Language should be en-US when no locale is configured."""
        service = SearchService(brave_api_key="test-key")
        headers = service._build_headers()
        assert headers["Accept-Language"] == "en-US,en;q=0.9"

    def test_locale_config_sets_accept_language(self):
        """Locale config should override Accept-Language header."""
        from supacrawl.models import LocaleConfig

        locale = LocaleConfig(language="en-AU")
        service = SearchService(brave_api_key="test-key", locale_config=locale)
        headers = service._build_headers()
        assert headers["Accept-Language"] == "en-AU,en;q=0.9"

    def test_env_locale_sets_accept_language(self):
        """SUPACRAWL_LOCALE env var should set Accept-Language when no locale_config."""
        with patch.dict("os.environ", {"SUPACRAWL_LOCALE": "de-DE"}):
            service = SearchService(brave_api_key="test-key")
            headers = service._build_headers()
            assert headers["Accept-Language"] == "de-DE,de;q=0.9"

    def test_locale_config_takes_precedence_over_env(self):
        """Explicit locale_config should take precedence over env var."""
        from supacrawl.models import LocaleConfig

        locale = LocaleConfig(language="fr-FR")
        with patch.dict("os.environ", {"SUPACRAWL_LOCALE": "de-DE"}):
            service = SearchService(brave_api_key="test-key", locale_config=locale)
            headers = service._build_headers()
            assert headers["Accept-Language"] == "fr-FR,fr;q=0.9"

    @pytest.mark.asyncio
    async def test_client_uses_full_browser_headers(self):
        """HTTP client should be created with the full browser header set."""
        service = SearchService(brave_api_key="test-key")
        try:
            client = await service._get_client()
            # Check key headers are set on the client
            assert "Mozilla" in client.headers["user-agent"]
            assert "text/html" in client.headers["accept"]
            assert client.headers["sec-fetch-dest"] == "document"
            assert client.headers["upgrade-insecure-requests"] == "1"
        finally:
            await service.close()


class TestRateLimiting:
    """Tests for search service rate limiting."""

    def test_ddg_default_rate_limit(self):
        """DuckDuckGo should default to 1 req/s."""
        service = _ddg_service()
        assert service._rate_limiter.requests_per_second == 1.0

    def test_brave_default_rate_limit(self):
        """Brave should default to 10 req/s."""
        service = SearchService(brave_api_key="test-key")
        assert service._rate_limiter.requests_per_second == 10.0

    def test_explicit_rate_limit_overrides_default(self):
        """Explicit rate_limit parameter should override provider default."""
        service = SearchService(brave_api_key="test-key", rate_limit=5.0)
        assert service._rate_limiter.requests_per_second == 5.0

    def test_env_var_rate_limit(self):
        """SUPACRAWL_SEARCH_RATE_LIMIT env var should override provider default."""
        with patch.dict("os.environ", {"SUPACRAWL_SEARCH_RATE_LIMIT": "3.5"}):
            service = SearchService(brave_api_key="test-key")
            assert service._rate_limiter.requests_per_second == 3.5

    def test_invalid_env_var_uses_provider_default(self):
        """Invalid SUPACRAWL_SEARCH_RATE_LIMIT should fall back to provider default."""
        with patch.dict("os.environ", {"SUPACRAWL_SEARCH_RATE_LIMIT": "not-a-number"}):
            service = SearchService(brave_api_key="test-key")
            assert service._rate_limiter.requests_per_second == 10.0

    @pytest.mark.asyncio
    async def test_rate_limiter_allows_burst(self):
        """Rate limiter should allow burst requests up to burst limit."""
        from supacrawl.services.search import _RateLimiter

        limiter = _RateLimiter(requests_per_second=100.0, burst=3)
        # Should be able to acquire 3 immediately
        for _ in range(3):
            await limiter.acquire(timeout=1.0)
            limiter.release()

    @pytest.mark.asyncio
    async def test_rate_limiter_timeout(self):
        """Rate limiter should raise TimeoutError when queue is full."""
        import asyncio

        from supacrawl.services.search import _RateLimiter

        # Very slow rate, burst=1 — second request should time out quickly
        limiter = _RateLimiter(requests_per_second=0.1, burst=1)
        await limiter.acquire(timeout=1.0)
        # Don't release — the semaphore is held

        with pytest.raises(asyncio.TimeoutError, match="rate limit queue timeout"):
            await limiter.acquire(timeout=0.1)

        limiter.release()

    @pytest.mark.asyncio
    async def test_rate_limit_timeout_surfaces_as_search_failure(self):
        """Rate limit timeout should surface as success=False in search result."""
        from supacrawl.services.search import _RateLimiter

        # Create service with DDG provider
        service = _ddg_service(rate_limit=0.1)

        # Replace with burst=1 + short queue_timeout so the test is fast
        service._rate_limiter = _RateLimiter(requests_per_second=0.1, burst=1, queue_timeout=0.1)

        # Hold the single semaphore slot
        await service._rate_limiter.acquire(timeout=1.0)

        try:
            result = await service.search("hello", limit=1)
            assert not result.success
            assert result.error is not None
            assert "rate limit" in result.error.lower()
        finally:
            service._rate_limiter.release()
            await service.close()


class TestDomainFilterHolds:
    @pytest.mark.asyncio
    async def test_results_off_the_requested_domains_are_dropped(self):
        from supacrawl.models import SearchFilters

        service = _ddg_service()
        items = [
            SearchResultItem(url="https://www.target.com.au/p/a", title="a"),
            SearchResultItem(url="https://news.com.au/x", title="b"),
        ]
        try:
            with patch.object(service._chain, "search", AsyncMock(return_value=items)):
                result = await service.search("q", filters=SearchFilters(include_domains=["target.com.au"]))
                assert [r.url for r in result.data] == ["https://www.target.com.au/p/a"]
                result = await service.search("q", filters=SearchFilters(include_domains=["bigw.com.au"]))
                assert not result.success and "ignored the site: filter" in (result.error or "")
        finally:
            await service.close()
