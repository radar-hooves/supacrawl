"""Tests for headless parameter propagation across services.

Ensures that when headless is set, it flows through to any BrowserManager
instances created internally by ScrapeService, MapService, and CrawlService.
See: https://github.com/supacrawl/supacrawl/issues/78
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from supacrawl.services.browser import BrowserManager, PageContent, PageMetadata
from supacrawl.services.crawl import CrawlService
from supacrawl.services.map import MapService
from supacrawl.services.scrape import ScrapeService


class TestBrowserManagerHeadlessDefaults:
    """Test BrowserManager headless logic."""

    def test_headless_default_without_stealth(self):
        """Without stealth, headless defaults to True."""
        bm = BrowserManager()
        assert bm.headless is True

    def test_headless_default_with_stealth(self, monkeypatch):
        """With stealth, headless defaults to False (headful is more effective) where a display exists."""
        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.delenv("SUPACRAWL_HEADLESS", raising=False)
        bm = BrowserManager(stealth=True)
        assert bm.headless is False

    def test_stealth_is_headless_on_a_host_without_a_display(self, monkeypatch):
        """A headed Chromium cannot start without a display, and the ladder would die at the Patchright rung."""
        monkeypatch.setattr("supacrawl.services.browser.sys.platform", "linux")
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.delenv("SUPACRAWL_HEADLESS", raising=False)
        assert BrowserManager(stealth=True).headless is True
        monkeypatch.setenv("SUPACRAWL_HEADLESS", "false")
        assert BrowserManager(stealth=True).headless is False

    def test_explicit_headless_overrides_stealth_default(self):
        """Explicitly passing headless=True overrides stealth's headful default."""
        bm = BrowserManager(headless=True, stealth=True)
        assert bm.headless is True

    def test_explicit_headless_false(self):
        """Explicitly passing headless=False is honoured."""
        bm = BrowserManager(headless=False)
        assert bm.headless is False


class TestScrapeServiceHeadlessPropagation:
    """Test that ScrapeService stores and propagates headless."""

    def test_stores_headless_parameter(self):
        """ScrapeService should store the headless parameter."""
        service = ScrapeService(headless=True)
        assert service._headless is True

    def test_stores_headless_none_by_default(self):
        """ScrapeService should default headless to None."""
        service = ScrapeService()
        assert service._headless is None

    @pytest.mark.asyncio
    async def test_owns_browser_passes_headless(self):
        """When creating its own BrowserManager, ScrapeService passes headless through.

        Drives the real browser path (``http_first=False`` skips the HTTP-first fast
        path) against a fake BrowserManager, so the constructor call is genuine and the
        scrape completes successfully. The #78 propagation contract is the assertion;
        the successful result proves the path actually executed rather than being
        short-circuited and asserted vacuously.
        """
        service = ScrapeService(headless=True, stealth=True)
        assert service._owns_browser is True

        fake_browser = MagicMock()
        fake_browser.__aenter__ = AsyncMock(return_value=fake_browser)
        fake_browser.__aexit__ = AsyncMock(return_value=None)
        fake_browser.fetch_page = AsyncMock(
            return_value=PageContent(
                url="https://example.com",
                html="<html><body><h1>Example</h1><p>Real page content.</p></body></html>",
                title="Example",
                status_code=200,
            )
        )
        fake_browser.extract_metadata = AsyncMock(
            return_value=PageMetadata(
                title="Example",
                description=None,
                language=None,
                keywords=None,
                robots=None,
                canonical_url=None,
                og_title=None,
                og_description=None,
                og_image=None,
                og_url=None,
                og_site_name=None,
            )
        )

        make_browser = MagicMock(return_value=fake_browser)
        with patch("supacrawl.services.scrape.BrowserManager", make_browser):
            result = await service.scrape("https://example.com", formats=["markdown"], http_first=False)

        make_browser.assert_called_once()
        assert make_browser.call_args.kwargs["headless"] is True
        assert result.success


class TestMapServiceHeadlessPropagation:
    """Test that MapService stores and propagates headless."""

    def test_stores_headless_parameter(self):
        """MapService should store the headless parameter."""
        service = MapService(headless=True)
        assert service._headless is True

    def test_stores_headless_none_by_default(self):
        """MapService should default headless to None."""
        service = MapService()
        assert service._headless is None

    def test_shared_browser_skips_creation(self):
        """When browser is provided, MapService does not create its own."""
        browser = MagicMock(spec=BrowserManager)
        service = MapService(browser=browser, headless=True)
        assert service._owns_browser is False


class TestCrawlServiceHeadlessParameter:
    """Test that CrawlService.crawl() accepts headless parameter."""

    def test_crawl_signature_includes_headless(self):
        """CrawlService.crawl() should accept a headless parameter."""
        import inspect

        sig = inspect.signature(CrawlService.crawl)
        assert "headless" in sig.parameters
        # Default should be None
        assert sig.parameters["headless"].default is None


@pytest.mark.mcp
class TestMCPSettingsHeadless:
    """Test MCP settings default headless to True."""

    def test_mcp_settings_default_headless_true(self):
        """MCP settings should default headless to True."""
        from supacrawl.mcp.config import SupacrawlSettings

        settings = SupacrawlSettings()
        assert settings.headless is True

    def test_mcp_settings_headless_overridable(self):
        """MCP settings headless should be overridable."""
        from supacrawl.mcp.config import SupacrawlSettings

        settings = SupacrawlSettings(headless=False)
        assert settings.headless is False
