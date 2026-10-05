"""Tests for Supacrawl MCP tool execution."""

from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.mcp


class TestScrapeTools:
    """Test scrape-related tools."""

    @pytest.mark.asyncio
    async def test_scrape_single_url(self, mock_api_client):
        """Scrape tool should call service with correct parameters."""
        from supacrawl.mcp.tools.scrape import supacrawl_scrape

        result = await supacrawl_scrape(
            api_client=mock_api_client,
            url="https://example.com",
            formats=["markdown"],
            only_main_content=True,
        )

        assert result["success"] is True
        mock_api_client.scrape_service.scrape.assert_called_once()

    @pytest.mark.asyncio
    async def test_scrape_returns_content(self, mock_api_client):
        """Scrape tool should return markdown content."""
        from supacrawl.mcp.tools.scrape import supacrawl_scrape

        result = await supacrawl_scrape(
            api_client=mock_api_client,
            url="https://example.com",
        )

        assert result["success"] is True
        assert "data" in result


class TestSearchTools:
    """Test search-related tools."""

    @pytest.mark.asyncio
    async def test_search_basic_query(self, mock_api_client):
        """Search tool should call service with query."""
        from supacrawl.mcp.tools.search import supacrawl_search

        ctx = MagicMock()
        result = await supacrawl_search(
            api_client=mock_api_client,
            ctx=ctx,
            query="test query",
            limit=5,
        )

        assert result["success"] is True
        mock_api_client.search_service.search.assert_called_once()

    @pytest.mark.asyncio
    async def test_search_with_scrape(self, mock_api_client):
        """Search tool should support scraping results."""
        from supacrawl.mcp.tools.search import supacrawl_search

        ctx = MagicMock()
        ctx.report_progress = AsyncMock()
        ctx.info = AsyncMock()
        result = await supacrawl_search(
            api_client=mock_api_client,
            ctx=ctx,
            query="test query",
            scrape_results=True,
            formats=["markdown"],
        )

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_search_with_include_metadata(self, mock_api_client, monkeypatch):
        """Search tool should fetch metadata via HEAD requests when include_metadata=True."""
        from supacrawl.mcp.tools import search as search_module
        from supacrawl.mcp.tools.search import supacrawl_search

        # Mock the metadata fetcher to return predictable results
        async def mock_fetch_metadata(url, timeout=5.0):
            return {
                "content_type": "text/html",
                "content_length": 12345,
            }

        monkeypatch.setattr(search_module, "_fetch_url_metadata", mock_fetch_metadata)

        ctx = MagicMock()
        result = await supacrawl_search(
            api_client=mock_api_client,
            ctx=ctx,
            query="test query",
            include_metadata=True,
        )

        assert result["success"] is True
        assert "data" in result
        # Web results should have metadata added
        for item in result["data"]:
            if item.get("source_type") == "web":
                assert "metadata" in item
                assert item["metadata"]["content_type"] == "text/html"
                assert item["metadata"]["content_length"] == 12345

    @pytest.mark.asyncio
    async def test_search_metadata_not_fetched_when_scraping(self, mock_api_client, monkeypatch):
        """Metadata should not be fetched when scrape_results=True (scraping includes richer metadata)."""
        from supacrawl.mcp.tools import search as search_module
        from supacrawl.mcp.tools.search import supacrawl_search

        fetch_called = False

        async def mock_fetch_metadata(url, timeout=5.0):
            nonlocal fetch_called
            fetch_called = True
            return {"content_type": "text/html"}

        monkeypatch.setattr(search_module, "_fetch_url_metadata", mock_fetch_metadata)

        ctx = MagicMock()
        ctx.report_progress = AsyncMock()
        ctx.info = AsyncMock()
        await supacrawl_search(
            api_client=mock_api_client,
            ctx=ctx,
            query="test query",
            include_metadata=True,
            scrape_results=True,  # Scraping takes precedence
        )

        assert not fetch_called, "Metadata fetch should be skipped when scraping is enabled"


class TestExtractTools:
    """Test extract-related tools."""

    @pytest.mark.asyncio
    async def test_extract_partial_batch_success(self, mock_api_client, llm_wire):
        """Extract should surface successful URL data even when one URL fails."""
        from supacrawl.mcp.tools.extract import supacrawl_extract

        # First URL succeeds (uses default mock), second raises
        call_count = 0

        async def scrape_side_effect(url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Return the default successful mock result
                return MagicMock(
                    success=True,
                    data=MagicMock(
                        markdown="# Test Page\n\nTest content",
                        metadata=MagicMock(title="Test Page", description="Test description"),
                    ),
                    error=None,
                )
            # Second URL fails
            raise RuntimeError("Connection refused")

        mock_api_client.scrape_service.scrape = AsyncMock(side_effect=scrape_side_effect)
        llm_wire.replies.append('{"title": "Test Page"}')

        result = await supacrawl_extract(
            api_client=mock_api_client,
            urls=["https://example.com/a", "https://example.com/b"],
            prompt="Extract titles",
        )

        # Top-level success is True because at least one URL succeeded
        assert result["success"] is True
        # Partial flag signals mixed outcome
        assert result["partial"] is True
        assert result["succeeded_count"] == 1
        assert result["failed_count"] == 1

        data = result["data"]
        assert len(data) == 2

        # Successful entry carries the extracted data
        successful = [r for r in data if r["success"]]
        assert len(successful) == 1
        assert successful[0]["url"] == "https://example.com/a"
        assert successful[0]["data"] == {"title": "Test Page"}

        # Failed entry carries an error field
        failed = [r for r in data if not r["success"]]
        assert len(failed) == 1
        assert failed[0]["url"] == "https://example.com/b"
        assert "error" in failed[0]

    @pytest.mark.asyncio
    async def test_extract_whole_call_failure_raises_a_typed_error(self, mock_api_client):
        """A whole-call failure raises a typed MCPError, never a success-shaped dict.

        Returning ``{"success": False, ...}`` is the rule-05 forbidden envelope:
        FastMCP serialises it as a SUCCESSFUL tool result, so the model sees a
        success and has no signal the call failed. A typed error on the
        ``MCPError`` lineage inherits ``FastMCPError``, so its real message
        reaches the model instead of being masked.
        """
        import supacrawl.mcp.tools.extract as extract_module
        from supacrawl.mcp.exceptions import SupacrawlMCPError
        from supacrawl.mcp.tools.extract import supacrawl_extract

        # Force the outer except by patching the validator to raise a non-validation error.

        original_validate = extract_module.validate_urls

        def exploding_validate(urls, *args, **kwargs):
            raise RuntimeError("Simulated infrastructure failure")

        extract_module.validate_urls = exploding_validate  # type: ignore[assignment]  # intentional: mock
        try:
            with pytest.raises(SupacrawlMCPError) as exc_info:
                await supacrawl_extract(
                    api_client=mock_api_client,
                    urls=["https://example.com"],
                    prompt="Extract data",
                )
        finally:
            extract_module.validate_urls = original_validate

        assert "Simulated infrastructure failure" in str(exc_info.value)
        assert isinstance(exc_info.value.__cause__, RuntimeError)

    @pytest.mark.asyncio
    async def test_extract_validation_error_propagates_untranslated(self, mock_api_client):
        """A validation error reaches the caller as itself, not remapped or swallowed."""
        from supacrawl.mcp.exceptions import SupacrawlValidationError
        from supacrawl.mcp.tools.extract import supacrawl_extract

        # An empty URL list triggers validate_urls -> SupacrawlValidationError (min_count=1 violated).
        with pytest.raises(SupacrawlValidationError) as exc_info:
            await supacrawl_extract(
                api_client=mock_api_client,
                urls=[],
                prompt="Extract data",
            )

        assert str(exc_info.value)


class TestMapTools:
    """Test map-related tools."""

    @pytest.mark.asyncio
    async def test_map_basic(self, mock_api_client):
        """Map tool should discover URLs."""
        from supacrawl.mcp.tools.map import supacrawl_map

        result = await supacrawl_map(
            api_client=mock_api_client,
            url="https://example.com",
        )

        assert result["success"] is True
        mock_api_client.map_service.map_all.assert_called_once()


class TestCrawlTools:
    """Test crawl-related tools."""

    @pytest.mark.asyncio
    async def test_crawl_basic(self, mock_api_client):
        """Crawl tool should discover and scrape pages."""
        from supacrawl.mcp.tools.crawl import supacrawl_crawl

        result = await supacrawl_crawl(
            api_client=mock_api_client,
            url="https://example.com",
            limit=10,
        )

        assert result["success"] is True
        assert result["status"] == "completed"


class TestDiagnoseTools:
    """Test diagnose-related tools."""

    @pytest.mark.asyncio
    async def test_diagnose_detection_functions(self):
        """Test internal detection functions."""
        from supacrawl.services.detection import (
            detect_bot_protection,
            detect_cdn,
            detect_js_framework,
            detect_login_required,
        )

        # Test CDN detection
        assert detect_cdn({"cf-ray": "abc123"}) == "cloudflare"
        assert detect_cdn({"server": "cloudflare"}) == "cloudflare"
        assert detect_cdn({"x-akamai-transformed": "1"}) == "akamai"
        assert detect_cdn({"x-amz-cf-id": "xyz"}) == "aws_cloudfront"
        assert detect_cdn({"content-type": "text/html"}) is None

        # Test JS framework detection
        assert detect_js_framework('<div id="root"></div>') == "react"
        assert detect_js_framework("__NEXT_DATA__") == "react"
        assert detect_js_framework("__NUXT__") == "vue"
        assert detect_js_framework("<app-root></app-root>") == "angular"
        assert detect_js_framework("<html><body>Plain</body></html>") is None

        # Test bot protection detection
        result = detect_bot_protection("g-recaptcha")
        assert result["captcha_present"] is True

        result = detect_bot_protection("just a moment")
        assert result["challenge_detected"] is True

        result = detect_bot_protection("Access Denied")
        assert result["access_denied"] is True

        # Test login detection
        assert detect_login_required('type="password"') is True
        assert detect_login_required("Sign in to continue") is True
        assert detect_login_required("<html><body>Normal page</body></html>") is False

    @pytest.mark.asyncio
    async def test_diagnose_recommendations(self):
        """Test recommendation generation."""
        from supacrawl.services.detection import generate_recommendations

        # Cloudflare detected
        recs = generate_recommendations(
            cdn="cloudflare",
            framework=None,
            bot_indicators={"challenge_detected": True},
            requires_js=False,
            login_required=False,
        )
        assert recs.get("stealth_mode") is True
        assert recs.get("wait_for", 0) >= 5000

        # React SPA detected
        recs = generate_recommendations(
            cdn=None,
            framework="react",
            bot_indicators={},
            requires_js=True,
            login_required=False,
        )
        assert recs.get("wait_for", 0) >= 3000

        # CAPTCHA detected
        recs = generate_recommendations(
            cdn=None,
            framework=None,
            bot_indicators={"captcha_present": True},
            requires_js=False,
            login_required=False,
        )
        assert recs.get("captcha_solving") is True

        # No issues
        recs = generate_recommendations(
            cdn=None,
            framework=None,
            bot_indicators={},
            requires_js=False,
            login_required=False,
        )
        assert "No issues detected" in recs.get("reason", "")
