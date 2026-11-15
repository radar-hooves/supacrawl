"""Tests for crawl service."""

from pathlib import Path

import pytest

from supacrawl.services.crawl import CrawlService


class TestCrawlService:
    """Tests for CrawlService."""

    @pytest.mark.e2e
    @pytest.mark.asyncio
    async def test_crawl_yields_events(self, tmp_path: Path):
        """Test that crawl yields events."""
        service = CrawlService()
        events = []
        async for event in service.crawl(
            "https://example.com",
            limit=3,
            output_dir=tmp_path,
        ):
            events.append(event)
            # Break early to speed up test
            if event.type == "complete":
                break

        assert len(events) > 0
        assert any(e.type == "progress" for e in events)
        assert any(e.type == "complete" for e in events)

    @pytest.mark.e2e
    @pytest.mark.asyncio
    async def test_crawl_saves_to_output_dir(self, tmp_path: Path):
        """Test that crawl saves pages to output directory."""
        service = CrawlService()
        pages_scraped = 0
        async for event in service.crawl(
            "https://example.com",
            limit=2,
            output_dir=tmp_path,
        ):
            if event.type == "page":
                pages_scraped += 1
            if event.type == "complete":
                break

        if pages_scraped == 0:
            pytest.skip("crawl scraped no pages for the test URL (site-dependent)")

        # Check that files were created
        md_files = list(tmp_path.glob("*.md"))
        assert len(md_files) > 0

        # Check manifest exists
        assert (tmp_path / "manifest.json").exists()

    @pytest.mark.e2e
    @pytest.mark.asyncio
    async def test_crawl_respects_limit(self, tmp_path: Path):
        """Test that crawl respects page limit."""
        service = CrawlService()
        pages_scraped = 0
        async for event in service.crawl(
            "https://example.com",
            limit=2,
            output_dir=tmp_path,
        ):
            if event.type == "page":
                pages_scraped += 1
            if event.type == "complete":
                break

        assert pages_scraped <= 2

    def test_matches_patterns(self):
        """Test URL pattern matching."""
        service = CrawlService()
        assert service._matches_patterns("https://example.com/api/v1", [".*/api/.*"])
        assert not service._matches_patterns("https://example.com/docs", [".*/api/.*"])
        assert service._matches_patterns("https://example.com/docs/guide", [".*/docs/.*", ".*/api/.*"])

    def test_matches_patterns_documented_mcp_examples(self):
        """The regex examples in the crawl tool's own description must match."""
        service = CrawlService()
        assert service._matches_patterns("https://example.com/docs/intro", [".*\\/docs\\/.*"])
        assert service._matches_patterns("https://example.com/a.pdf", [".*\\.pdf$"])
        assert not service._matches_patterns("https://example.com/a.html", [".*\\.pdf$"])

    @pytest.mark.e2e
    @pytest.mark.asyncio
    async def test_crawl_creates_manifest(self, tmp_path: Path):
        """Test that crawl creates manifest with scraped URLs."""
        import json

        service = CrawlService()
        pages_scraped = 0
        async for event in service.crawl(
            "https://example.com",
            limit=2,
            output_dir=tmp_path,
        ):
            if event.type == "page":
                pages_scraped += 1
            if event.type == "complete":
                break

        if pages_scraped == 0:
            pytest.skip("crawl scraped no pages for the test URL (site-dependent)")

        manifest_path = tmp_path / "manifest.json"
        assert manifest_path.exists()

        with open(manifest_path) as f:
            manifest = json.load(f)
            assert "scraped_urls" in manifest
            assert len(manifest["scraped_urls"]) > 0

    @pytest.mark.e2e
    @pytest.mark.asyncio
    async def test_crawl_allow_external_links_accepted(self, tmp_path: Path):
        """Test that allow_external_links parameter is accepted."""
        service = CrawlService()
        events = []
        async for event in service.crawl(
            "https://example.com",
            limit=2,
            output_dir=tmp_path,
            allow_external_links=True,
        ):
            events.append(event)
            if event.type == "complete":
                break

        # Should complete without error
        assert len(events) > 0
        assert any(e.type == "complete" for e in events)
