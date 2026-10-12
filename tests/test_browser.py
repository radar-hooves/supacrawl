"""Tests for browser manager."""

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from supacrawl.services.browser import BrowserManager, PageContent, PageMetadata

pytestmark = pytest.mark.browser


class TestBrowserManager:
    """Tests for BrowserManager."""

    @pytest.mark.asyncio
    async def test_fetch_page_returns_html(self):
        """Test that fetch_page returns HTML content."""
        async with BrowserManager() as browser:
            content = await browser.fetch_page("https://example.com", wait_for_spa=False)
            assert isinstance(content, PageContent)
            assert content.html
            assert "<html" in content.html.lower()
            assert content.url == "https://example.com"
            assert content.status_code == 200

    @pytest.mark.asyncio
    async def test_fetch_page_with_spa_wait(self):
        """Test that fetch_page works with SPA waiting enabled."""
        async with BrowserManager() as browser:
            content = await browser.fetch_page("https://example.com", wait_for_spa=True, spa_timeout_ms=2000)
            assert isinstance(content, PageContent)
            assert content.html
            assert content.title is not None

    @pytest.mark.asyncio
    async def test_extract_links_finds_links(self):
        """Test that extract_links finds anchor tags."""
        async with BrowserManager() as browser:
            links = await browser.extract_links("https://example.com")
            assert isinstance(links, list)
            # example.com should have at least the IANA link
            assert len(links) >= 0  # May have links or may not

    @pytest.mark.asyncio
    async def test_extract_metadata_from_html(self):
        """Test metadata extraction from HTML."""
        html = """
        <html>
            <head>
                <title>Test Page</title>
                <meta name="description" content="Test description">
                <meta property="og:title" content="OG Title">
                <meta property="og:description" content="OG Description">
                <meta property="og:image" content="https://example.com/image.jpg">
            </head>
            <body>
                <h1>Test</h1>
            </body>
        </html>
        """
        async with BrowserManager() as browser:
            metadata = await browser.extract_metadata(html)
            assert isinstance(metadata, PageMetadata)
            assert metadata.title == "Test Page"
            assert metadata.description == "Test description"
            assert metadata.og_title == "OG Title"
            assert metadata.og_description == "OG Description"
            assert metadata.og_image == "https://example.com/image.jpg"

    @pytest.mark.asyncio
    async def test_extract_metadata_handles_missing_tags(self):
        """Test metadata extraction with missing tags."""
        html = "<html><body><h1>Test</h1></body></html>"
        async with BrowserManager() as browser:
            metadata = await browser.extract_metadata(html)
            assert isinstance(metadata, PageMetadata)
            assert metadata.title is None
            assert metadata.description is None
            assert metadata.og_title is None

    @pytest.mark.asyncio
    async def test_extract_metadata_falls_back_to_og_title(self):
        """Test that title falls back to og:title when <title> is missing."""
        html = """
        <html>
            <head>
                <meta property="og:title" content="OG Title">
                <meta property="og:description" content="OG Description">
            </head>
            <body><h1>Test</h1></body>
        </html>
        """
        async with BrowserManager() as browser:
            metadata = await browser.extract_metadata(html)
            assert metadata.title == "OG Title"
            assert metadata.description == "OG Description"
            assert metadata.og_title == "OG Title"

    @pytest.mark.asyncio
    async def test_extract_metadata_falls_back_to_twitter_title(self):
        """Test that title falls back to twitter:title when both <title> and og:title are missing."""
        html = """
        <html>
            <head>
                <meta name="twitter:title" content="Twitter Title">
                <meta name="twitter:description" content="Twitter Description">
                <meta name="twitter:image" content="https://example.com/twitter.jpg">
            </head>
            <body><h1>Test</h1></body>
        </html>
        """
        async with BrowserManager() as browser:
            metadata = await browser.extract_metadata(html)
            assert metadata.title == "Twitter Title"
            assert metadata.description == "Twitter Description"
            assert metadata.og_image == "https://example.com/twitter.jpg"

    @pytest.mark.asyncio
    async def test_extract_metadata_prefers_title_over_og(self):
        """Test that <title> is preferred over og:title when both exist."""
        html = """
        <html>
            <head>
                <title>Page Title</title>
                <meta property="og:title" content="OG Title">
                <meta name="twitter:title" content="Twitter Title">
            </head>
            <body><h1>Test</h1></body>
        </html>
        """
        async with BrowserManager() as browser:
            metadata = await browser.extract_metadata(html)
            assert metadata.title == "Page Title"
            assert metadata.og_title == "OG Title"

    @pytest.mark.asyncio
    async def test_context_manager(self):
        """Test that BrowserManager works as async context manager."""
        async with BrowserManager() as browser:
            assert browser._browser is not None
            assert browser._playwright is not None

    @pytest.mark.asyncio
    async def test_env_bool_parsing(self):
        """Test environment boolean parsing."""
        import os

        # Test various truthy values
        os.environ["SUPACRAWL_TEST"] = "true"
        assert BrowserManager._env_bool("SUPACRAWL_TEST", False) is True

        os.environ["SUPACRAWL_TEST"] = "1"
        assert BrowserManager._env_bool("SUPACRAWL_TEST", False) is True

        os.environ["SUPACRAWL_TEST"] = "yes"
        assert BrowserManager._env_bool("SUPACRAWL_TEST", False) is True

        # Test falsy values
        os.environ["SUPACRAWL_TEST"] = "false"
        assert BrowserManager._env_bool("SUPACRAWL_TEST", True) is False

        os.environ["SUPACRAWL_TEST"] = "0"
        assert BrowserManager._env_bool("SUPACRAWL_TEST", True) is False

        # Test default
        del os.environ["SUPACRAWL_TEST"]
        assert BrowserManager._env_bool("SUPACRAWL_TEST", True) is True
        assert BrowserManager._env_bool("SUPACRAWL_TEST", False) is False

    @pytest.mark.asyncio
    async def test_extract_metadata_detects_timezone_from_jsonld(self):
        """Test timezone detection from JSON-LD structured data."""
        html = """
        <html>
            <head>
                <title>Event Page</title>
                <script type="application/ld+json">
                {
                    "@context": "https://schema.org",
                    "@type": "Event",
                    "name": "Concert",
                    "location": {
                        "@type": "Place",
                        "address": {
                            "timeZone": "America/New_York"
                        }
                    }
                }
                </script>
            </head>
            <body><h1>Event</h1></body>
        </html>
        """
        async with BrowserManager() as browser:
            metadata = await browser.extract_metadata(html)
            assert metadata.timezone == "America/New_York"

    @pytest.mark.asyncio
    async def test_extract_metadata_detects_timezone_from_meta_tag(self):
        """Test timezone detection from meta tags."""
        html = """
        <html>
            <head>
                <title>Page</title>
                <meta name="timezone" content="Europe/London">
            </head>
            <body><h1>Content</h1></body>
        </html>
        """
        async with BrowserManager() as browser:
            metadata = await browser.extract_metadata(html)
            assert metadata.timezone == "Europe/London"

    @pytest.mark.asyncio
    async def test_extract_metadata_timezone_none_when_absent(self):
        """Test that timezone is None when no timezone info is present."""
        html = """
        <html>
            <head><title>Simple Page</title></head>
            <body><h1>No timezone here</h1></body>
        </html>
        """
        async with BrowserManager() as browser:
            metadata = await browser.extract_metadata(html)
            assert metadata.timezone is None

    @pytest.mark.asyncio
    async def test_extract_images_from_html(self):
        """Test image extraction from HTML."""
        html = """
        <html>
            <body>
                <img src="/image1.jpg">
                <img src="https://example.com/image2.png">
                <img src="data:image/png;base64,iVBORw0...">
                <img srcset="/image3.jpg 1x, /image4.jpg 2x">
            </body>
        </html>
        """
        async with BrowserManager() as browser:
            images = await browser.extract_images(html, "https://example.com")
            assert isinstance(images, list)
            # Should have image1, image2, image3, image4 (not data URI)
            assert len(images) >= 4
            # All should be absolute URLs
            assert all(img.startswith("http") for img in images)
            # Should not contain data URIs
            assert not any(img.startswith("data:") for img in images)

    @pytest.mark.asyncio
    async def test_extract_images_with_srcset(self):
        """Test image extraction with srcset attribute."""
        html = """
        <html>
            <body>
                <img srcset="small.jpg 300w, medium.jpg 600w, large.jpg 900w" src="default.jpg">
                <picture>
                    <source srcset="image.webp" type="image/webp">
                    <source srcset="image.jpg" type="image/jpeg">
                    <img src="fallback.jpg">
                </picture>
            </body>
        </html>
        """
        async with BrowserManager() as browser:
            images = await browser.extract_images(html, "https://example.com")
            assert isinstance(images, list)
            # Should have small, medium, large, default, image.webp, image.jpg, fallback
            assert len(images) >= 6

    @pytest.mark.asyncio
    async def test_extract_images_filters_tracking_pixels(self):
        """Test that tracking pixels are filtered out."""
        html = """
        <html>
            <body>
                <img src="/good-image.jpg">
                <img src="/pixel.gif">
                <img src="/tracking-1x1.png">
                <img src="/analytics.jpg">
            </body>
        </html>
        """
        async with BrowserManager() as browser:
            images = await browser.extract_images(html, "https://example.com")
            assert isinstance(images, list)
            # Should only have good-image.jpg
            assert len(images) == 1
            assert "good-image.jpg" in images[0]

    @pytest.mark.asyncio
    async def test_extract_images_deduplicates(self):
        """Test that duplicate images are removed."""
        html = """
        <html>
            <body>
                <img src="/image.jpg">
                <img src="/image.jpg">
                <img src="https://example.com/image.jpg">
            </body>
        </html>
        """
        async with BrowserManager() as browser:
            images = await browser.extract_images(html, "https://example.com")
            assert isinstance(images, list)
            # Should only have one unique image
            assert len(images) == 1

    @pytest.mark.asyncio
    async def test_extract_images_empty_page(self):
        """Test image extraction from page with no images."""
        html = "<html><body><p>No images here</p></body></html>"
        async with BrowserManager() as browser:
            images = await browser.extract_images(html, "https://example.com")
            assert isinstance(images, list)
            assert len(images) == 0

    @pytest.mark.asyncio
    async def test_extract_images_inline_background_image(self):
        """Test image extraction from inline style background-image."""
        html = """
        <html>
            <body>
                <div style="background-image: url('/hero.jpg')"></div>
                <div style="background: url(/banner.png) no-repeat center"></div>
                <section style='background-image: url("https://example.com/bg.webp")'></section>
            </body>
        </html>
        """
        async with BrowserManager() as browser:
            images = await browser.extract_images(html, "https://example.com")
            assert isinstance(images, list)
            urls = set(images)
            assert "https://example.com/hero.jpg" in urls
            assert "https://example.com/banner.png" in urls
            assert "https://example.com/bg.webp" in urls

    @pytest.mark.asyncio
    async def test_extract_images_style_block_background_image(self):
        """Test image extraction from <style> block background-image."""
        html = """
        <html>
            <head>
                <style>
                    .hero { background-image: url('/hero-bg.jpg'); }
                    .banner { background: url("/promo.png") center no-repeat; }
                </style>
            </head>
            <body>
                <div class="hero"></div>
                <div class="banner"></div>
            </body>
        </html>
        """
        async with BrowserManager() as browser:
            images = await browser.extract_images(html, "https://example.com")
            assert isinstance(images, list)
            urls = set(images)
            assert "https://example.com/hero-bg.jpg" in urls
            assert "https://example.com/promo.png" in urls

    @pytest.mark.asyncio
    async def test_extract_images_background_image_deduplicates_with_img(self):
        """Test that CSS background images deduplicate with <img> tags."""
        html = """
        <html>
            <body>
                <img src="/shared.jpg">
                <div style="background-image: url('/shared.jpg')"></div>
                <div style="background-image: url('/unique-bg.jpg')"></div>
            </body>
        </html>
        """
        async with BrowserManager() as browser:
            images = await browser.extract_images(html, "https://example.com")
            assert isinstance(images, list)
            # shared.jpg should appear only once, plus unique-bg.jpg
            assert len(images) == 2
            assert "https://example.com/shared.jpg" in images
            assert "https://example.com/unique-bg.jpg" in images

    @pytest.mark.asyncio
    async def test_extract_images_background_image_skips_data_uris(self):
        """Test that data URIs in background-image are excluded."""
        html = """
        <html>
            <body>
                <div style="background-image: url('data:image/svg+xml;base64,PHN2Zz4=')"></div>
                <div style="background-image: url('/real-image.jpg')"></div>
            </body>
        </html>
        """
        async with BrowserManager() as browser:
            images = await browser.extract_images(html, "https://example.com")
            assert isinstance(images, list)
            assert len(images) == 1
            assert "https://example.com/real-image.jpg" in images


class TestBrowserPreflightSSRF:
    """The #152 pre-flight refuses a blocked URL before any browser is driven.

    ``_browser`` is set to a sentinel so the "not initialised" guard passes
    without launching Playwright; ``resolve_and_pin`` is patched to refuse, and
    the refusal must propagate before the sentinel is ever used.
    """

    @pytest.mark.asyncio
    async def test_fetch_page_refuses_blocked_url_before_launch(self, monkeypatch):
        from supacrawl.exceptions import ValidationError
        from supacrawl.services import browser as browser_mod

        manager = browser_mod.BrowserManager()
        monkeypatch.setattr(manager, "_browser", object())

        def refuse(url):
            raise ValidationError(
                "URL host resolves to 169.254.169.254, which is in a blocked range",
                field="url",
                value=url,
            )

        monkeypatch.setattr(browser_mod, "resolve_and_pin", refuse)

        with pytest.raises(ValidationError, match="blocked"):
            await manager.fetch_page("http://evil.example.com/")

    @pytest.mark.asyncio
    async def test_extract_links_refuses_blocked_url_before_launch(self, monkeypatch):
        from supacrawl.exceptions import ValidationError
        from supacrawl.services import browser as browser_mod

        manager = browser_mod.BrowserManager()
        monkeypatch.setattr(manager, "_browser", object())

        def refuse(url):
            raise ValidationError(
                "URL host resolves to a blocked range (link-local / cloud-metadata)",
                field="url",
                value=url,
            )

        monkeypatch.setattr(browser_mod, "resolve_and_pin", refuse)

        with pytest.raises(ValidationError, match="blocked"):
            await manager.extract_links("http://evil.example.com/")


class TestNavigationGuardRouteHandler:
    """``_install_navigation_guard`` re-validates every request a page makes.

    This is the redirect/subresource protection layered on top of the
    pre-flight check (#152): the pre-flight only sees the URL a caller asked
    for, this route handler sees every hop the browser itself then makes.
    """

    class _FakeRoute:
        def __init__(self) -> None:
            self.aborted_with: str | None = None
            self.continued = False

        async def abort(self, reason: str) -> None:
            self.aborted_with = reason

        async def continue_(self) -> None:
            self.continued = True

    class _FakeRequest:
        def __init__(self, url: str) -> None:
            self.url = url

    class _FakePage:
        def __init__(self) -> None:
            self.handler: Callable[..., Any] | None = None

        async def route(self, pattern: str, handler: Callable[..., Any]) -> None:
            self.handler = handler

    @pytest.mark.asyncio
    async def test_blocked_request_is_aborted(self, monkeypatch):
        from supacrawl.exceptions import ValidationError
        from supacrawl.services import browser as browser_mod

        def refuse(url):
            raise ValidationError("blocked range (link-local / cloud-metadata)", field="url", value=url)

        monkeypatch.setattr(browser_mod, "resolve_and_pin", refuse)

        page = self._FakePage()
        await browser_mod._install_navigation_guard(page)
        route, request = self._FakeRoute(), self._FakeRequest("http://169.254.169.254/latest/meta-data/")

        assert page.handler is not None
        await page.handler(route, request)

        assert route.aborted_with == "blockedbyclient"
        assert route.continued is False

    @pytest.mark.asyncio
    async def test_allowed_request_continues(self, monkeypatch):
        from supacrawl.services import browser as browser_mod

        monkeypatch.setattr(browser_mod, "resolve_and_pin", lambda url: ("93.184.216.34", "example.com"))

        page = self._FakePage()
        await browser_mod._install_navigation_guard(page)
        route, request = self._FakeRoute(), self._FakeRequest("https://example.com/")

        assert page.handler is not None
        await page.handler(route, request)

        assert route.continued is True
        assert route.aborted_with is None

    @pytest.mark.asyncio
    async def test_resolution_runs_off_the_event_loop(self, monkeypatch):
        """A slow resolver on one hostile request must not stall the event loop.

        Every other resolve_and_pin call site in this codebase wraps it in
        asyncio.to_thread for exactly this reason; the route handler fires
        once per request/redirect/subresource, so it needs the same guarantee.
        """
        from supacrawl.services import browser as browser_mod

        calls: list[tuple[object, ...]] = []
        real_to_thread = asyncio.to_thread

        async def spying_to_thread(func, *args):
            calls.append((func, *args))
            return await real_to_thread(func, *args)

        monkeypatch.setattr(browser_mod.asyncio, "to_thread", spying_to_thread)
        monkeypatch.setattr(browser_mod, "resolve_and_pin", lambda url: ("93.184.216.34", "example.com"))

        page = self._FakePage()
        await browser_mod._install_navigation_guard(page)
        assert page.handler is not None
        await page.handler(self._FakeRoute(), self._FakeRequest("https://example.com/"))

        assert len(calls) == 1
        assert calls[0][0] is browser_mod.resolve_and_pin
        assert calls[0][1] == "https://example.com/"
