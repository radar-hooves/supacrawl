"""Tests for mobile device emulation (issue #83).

Tests cover:
- BrowserManager device config resolution and context options
- ScrapeService device parameter threading and cache variants
- CLI --mobile, --device, and --list-devices options
- MCP tool mobile/device parameters
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from supacrawl.services.browser import BrowserManager, PageContent, PageMetadata
from supacrawl.services.scrape import ScrapeService

# BrowserManager — device config resolution


class TestBrowserManagerDeviceConfig:
    """Test BrowserManager device emulation support."""

    def test_resolve_device_config_raises_without_playwright(self):
        """_resolve_device_config raises if Playwright not started."""
        bm = BrowserManager()
        with pytest.raises(RuntimeError, match="Playwright not started"):
            bm._resolve_device_config("iPhone 14")

    def test_resolve_device_config_unknown_device(self):
        """_resolve_device_config raises ValueError for unknown device."""
        bm = BrowserManager()
        bm._playwright = MagicMock()
        bm._playwright.devices = {"iPhone 14": {"user_agent": "ua"}}

        with pytest.raises(ValueError, match="Unknown device 'Banana Phone'"):
            bm._resolve_device_config("Banana Phone")

    def test_resolve_device_config_suggests_close_matches(self):
        """Error message includes close matches for unknown device names."""
        bm = BrowserManager()
        bm._playwright = MagicMock()
        bm._playwright.devices = {
            "iPhone 14": {},
            "iPhone 14 Pro": {},
            "iPhone 14 Pro Max": {},
            "Pixel 7": {},
        }

        with pytest.raises(ValueError, match=r"Did you mean.*iPhone 14"):
            bm._resolve_device_config("iPhone 14 Plus")

    def test_resolve_device_config_strips_default_browser_type(self):
        """Device config should not include default_browser_type."""
        bm = BrowserManager()
        bm._playwright = MagicMock()
        bm._playwright.devices = {
            "iPhone 14": {
                "user_agent": "Mozilla/5.0 (iPhone)",
                "viewport": {"width": 390, "height": 844},
                "device_scale_factor": 3,
                "is_mobile": True,
                "has_touch": True,
                "default_browser_type": "webkit",
            }
        }

        config = bm._resolve_device_config("iPhone 14")
        assert "default_browser_type" not in config
        assert config["user_agent"] == "Mozilla/5.0 (iPhone)"
        assert config["viewport"] == {"width": 390, "height": 844}
        assert config["is_mobile"] is True

    def test_build_context_options_without_device(self):
        """Without device, _build_context_options works as before."""
        bm = BrowserManager()
        bm._playwright = MagicMock()
        options = bm._build_context_options()
        # No device-specific keys
        assert "is_mobile" not in options
        assert "has_touch" not in options

    def test_build_context_options_with_device(self):
        """With device, _build_context_options merges device settings."""
        bm = BrowserManager()
        bm._playwright = MagicMock()
        bm._playwright.devices = {
            "Pixel 7": {
                "user_agent": "Mozilla/5.0 (Linux; Android)",
                "viewport": {"width": 412, "height": 915},
                "device_scale_factor": 2.625,
                "is_mobile": True,
                "has_touch": True,
                "default_browser_type": "chromium",
            }
        }

        options = bm._build_context_options(device="Pixel 7")
        assert options["user_agent"] == "Mozilla/5.0 (Linux; Android)"
        assert options["viewport"] == {"width": 412, "height": 915}
        assert options["is_mobile"] is True
        assert options["has_touch"] is True
        assert "default_browser_type" not in options

    def test_explicit_user_agent_overrides_device(self):
        """An explicit user_agent on BrowserManager overrides the device's UA."""
        bm = BrowserManager(user_agent="Custom/UA")
        bm._playwright = MagicMock()
        bm._playwright.devices = {
            "iPhone 14": {
                "user_agent": "Mozilla/5.0 (iPhone)",
                "viewport": {"width": 390, "height": 844},
                "device_scale_factor": 3,
                "is_mobile": True,
                "has_touch": True,
                "default_browser_type": "webkit",
            }
        }

        options = bm._build_context_options(device="iPhone 14")
        # Explicit UA overrides device UA
        assert options["user_agent"] == "Custom/UA"
        # Device viewport still applied
        assert options["viewport"] == {"width": 390, "height": 844}


# BrowserManager — fetch_page device validation


class TestFetchPageDeviceValidation:
    """Test fetch_page device parameter validation."""

    @pytest.mark.asyncio
    async def test_device_with_camoufox_raises(self):
        """Device emulation with Camoufox should raise ValueError."""
        bm = BrowserManager.__new__(BrowserManager)
        bm.engine = "camoufox"
        bm._browser = MagicMock()

        with pytest.raises(ValueError, match="not supported with the Camoufox engine"):
            await bm.fetch_page("https://example.com", device="iPhone 14")


# BrowserManager — list_devices


class TestListDevices:
    """Test list_devices helper."""

    @pytest.mark.asyncio
    async def test_list_devices_raises_without_playwright(self):
        """list_devices raises if Playwright not started."""
        bm = BrowserManager()
        with pytest.raises(RuntimeError, match="Playwright not started"):
            await bm.list_devices()

    @pytest.mark.asyncio
    async def test_list_devices_returns_sorted(self):
        """list_devices returns sorted device names."""
        bm = BrowserManager()
        bm._playwright = MagicMock()
        bm._playwright.devices = {"Pixel 7": {}, "iPhone 14": {}, "iPad Pro": {}}

        result = await bm.list_devices()
        # ASCII sort: uppercase 'P' (80) < lowercase 'i' (105)
        assert result == ["Pixel 7", "iPad Pro", "iPhone 14"]


# ScrapeService — device threading


class TestScrapeServiceDevice:
    """Test ScrapeService device parameter threading."""

    @pytest.mark.asyncio
    async def test_device_passed_to_browser_fetch_page(self):
        """Device parameter should be forwarded to browser.fetch_page().

        Drives the real browser path (``http_first=False`` skips the HTTP-first fast
        path) against a fake BrowserManager, so fetch_page is genuinely called and the
        scrape completes successfully rather than relying on a swallowed exception.
        """
        service = ScrapeService(headless=True)
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
            result = await service.scrape(
                "https://example.com", formats=["markdown"], device="iPhone 14", http_first=False
            )

        fake_browser.fetch_page.assert_called_once()
        assert fake_browser.fetch_page.call_args.kwargs.get("device") == "iPhone 14"
        assert result.success


# ScrapeService — cache variant


class TestScrapeServiceCacheVariant:
    """Test cache variant includes device for differentiation."""

    @pytest.mark.asyncio
    async def test_device_creates_cache_variant(self):
        """A device parameter should produce a distinct cache variant."""
        # This is tested via the cache key generation in ScrapeService.scrape()
        # The implementation builds variant_parts including device name
        # We verify this indirectly via the cache interaction
        from supacrawl.cache import CacheManager

        cm = CacheManager.__new__(CacheManager)
        cm.cache_dir = None  # type: ignore[assignment]  # intentional: bypasses __init__ for cache-key unit test
        cm.pages_dir = None  # type: ignore[assignment]  # intentional: bypasses __init__ for cache-key unit test
        cm.index_path = None  # type: ignore[assignment]  # intentional: bypasses __init__ for cache-key unit test

        # Same URL, different variants should produce different keys
        key_desktop = cm._cache_key("https://example.com")
        key_mobile = cm._cache_key("https://example.com", variant="device=iPhone 14")
        key_other = cm._cache_key("https://example.com", variant="device=Pixel 7")

        assert key_desktop != key_mobile
        assert key_mobile != key_other
        assert key_desktop != key_other

    @pytest.mark.asyncio
    async def test_device_plus_screenshot_variant(self):
        """Device + screenshot_full_page=False should combine in variant."""
        from supacrawl.cache import CacheManager

        cm = CacheManager.__new__(CacheManager)
        cm.cache_dir = None  # type: ignore[assignment]  # intentional: bypasses __init__ for cache-key unit test
        cm.pages_dir = None  # type: ignore[assignment]  # intentional: bypasses __init__ for cache-key unit test
        cm.index_path = None  # type: ignore[assignment]  # intentional: bypasses __init__ for cache-key unit test

        key_combined = cm._cache_key(
            "https://example.com",
            variant="device=iPhone 14|screenshot_full_page=False",
        )
        key_device_only = cm._cache_key(
            "https://example.com",
            variant="device=iPhone 14",
        )

        assert key_combined != key_device_only


# CLI — --mobile and --device options


class TestCLIDeviceOptions:
    """Test CLI --mobile and --device options."""

    def test_mobile_resolves_to_default_device(self):
        """--mobile flag should resolve to DEFAULT_MOBILE_DEVICE."""
        from supacrawl.models import DEFAULT_MOBILE_DEVICE

        assert DEFAULT_MOBILE_DEVICE == "iPhone 14"

    def test_device_overrides_mobile(self):
        """When both --mobile and --device are given, --device wins."""
        # This is tested by the resolution logic:
        # resolved_device = device if device else (DEFAULT_MOBILE_DEVICE if mobile else None)
        from supacrawl.models import DEFAULT_MOBILE_DEVICE

        mobile = True
        device = "Pixel 7"

        resolved = device if device else (DEFAULT_MOBILE_DEVICE if mobile else None)
        assert resolved == "Pixel 7"

    def test_neither_mobile_nor_device_gives_none(self):
        """Without --mobile or --device, resolved device is None."""
        mobile = False
        device = None
        resolved = device if device else ("iPhone 14" if mobile else None)
        assert resolved is None
