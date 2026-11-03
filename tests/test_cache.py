"""Tests for CacheManager."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from supacrawl.cache import CacheEntry, CacheManager


class TestCacheManager:
    """Tests for CacheManager."""

    @pytest.fixture
    def cache_dir(self, tmp_path: Path) -> Path:
        """Create a temporary cache directory."""
        cache = tmp_path / "cache"
        cache.mkdir()
        return cache

    @pytest.fixture
    def cache_manager(self, cache_dir: Path) -> CacheManager:
        """Create a CacheManager with temp directory."""
        return CacheManager(cache_dir)

    def test_init_creates_directories(self, tmp_path: Path) -> None:
        """Test that init creates cache directories."""
        cache_dir = tmp_path / "new_cache"
        CacheManager(cache_dir)

        assert cache_dir.exists()
        assert (cache_dir / "pages").exists()

    def test_cache_key_is_deterministic(self, cache_manager: CacheManager) -> None:
        """Test that same URL produces same cache key."""
        url = "https://example.com/page"
        key1 = cache_manager._cache_key(url)
        key2 = cache_manager._cache_key(url)

        assert key1 == key2
        assert len(key1) == 16  # SHA256[:16]

    def test_normalise_url_removes_fragment(self, cache_manager: CacheManager) -> None:
        """Test that URL fragments are removed."""
        url = "https://example.com/page#section"
        normalised = cache_manager._normalise_url(url)

        assert "#section" not in normalised
        assert "example.com/page" in normalised

    def test_normalise_url_removes_tracking_params(self, cache_manager: CacheManager) -> None:
        """Test that tracking parameters are removed."""
        url = "https://example.com/page?utm_source=google&id=123"
        normalised = cache_manager._normalise_url(url)

        assert "utm_source" not in normalised
        assert "id=123" in normalised

    def test_set_and_get(self, cache_manager: CacheManager) -> None:
        """Test basic set and get operations."""
        url = "https://example.com/page"
        response = {"success": True, "data": {"markdown": "# Test"}}

        cache_manager.set(url, response, max_age=3600)
        cached = cache_manager.get(url, max_age=3600)

        assert cached is not None
        assert cached["success"] is True
        assert cached["data"]["markdown"] == "# Test"

    def test_get_returns_none_when_not_cached(self, cache_manager: CacheManager) -> None:
        """Test that get returns None for uncached URLs."""
        url = "https://example.com/uncached"
        cached = cache_manager.get(url, max_age=3600)

        assert cached is None

    def test_get_returns_none_when_expired(self, cache_manager: CacheManager, cache_dir: Path) -> None:
        """Test that get returns None for expired entries."""
        url = "https://example.com/expired"
        cache_key = cache_manager._cache_key(url)
        cache_file = cache_dir / "pages" / f"{cache_key}.json"

        # Create an expired entry
        now = datetime.now(UTC)
        expired_at = datetime.fromtimestamp(now.timestamp() - 3600, tz=UTC)  # 1 hour ago

        entry = CacheEntry(
            url=url,
            cached_at=now.isoformat(),
            expires_at=expired_at.isoformat(),
            response={"success": True},
        )
        cache_file.write_text(entry.model_dump_json())

        cached = cache_manager.get(url, max_age=3600)

        assert cached is None

    def test_get_returns_none_when_max_age_zero(self, cache_manager: CacheManager) -> None:
        """Test that get returns None when max_age is 0."""
        url = "https://example.com/page"
        response = {"success": True}

        cache_manager.set(url, response, max_age=3600)
        cached = cache_manager.get(url, max_age=0)

        assert cached is None

    def test_set_does_nothing_when_max_age_zero(self, cache_manager: CacheManager, cache_dir: Path) -> None:
        """Test that set does nothing when max_age is 0."""
        url = "https://example.com/page"
        response = {"success": True}

        cache_manager.set(url, response, max_age=0)

        # Check no files were created
        cache_files = list((cache_dir / "pages").glob("*.json"))
        assert len(cache_files) == 0

    def test_clear_specific_url(self, cache_manager: CacheManager) -> None:
        """Test clearing cache for a specific URL."""
        url1 = "https://example.com/page1"
        url2 = "https://example.com/page2"

        cache_manager.set(url1, {"page": 1}, max_age=3600)
        cache_manager.set(url2, {"page": 2}, max_age=3600)

        cleared = cache_manager.clear(url1)

        assert cleared == 1
        assert cache_manager.get(url1, max_age=3600) is None
        assert cache_manager.get(url2, max_age=3600) is not None

    def test_clear_all(self, cache_manager: CacheManager) -> None:
        """Test clearing all cache entries."""
        cache_manager.set("https://example.com/1", {"page": 1}, max_age=3600)
        cache_manager.set("https://example.com/2", {"page": 2}, max_age=3600)

        cleared = cache_manager.clear()

        assert cleared == 2
        assert cache_manager.get("https://example.com/1", max_age=3600) is None
        assert cache_manager.get("https://example.com/2", max_age=3600) is None

    def test_stats_returns_correct_counts(self, cache_manager: CacheManager) -> None:
        """Test that stats returns correct counts."""
        cache_manager.set("https://example.com/1", {"page": 1}, max_age=3600)
        cache_manager.set("https://example.com/2", {"page": 2}, max_age=3600)

        stats = cache_manager.stats()

        assert stats["entries"] == 2
        assert stats["valid"] == 2
        assert stats["expired"] == 0
        assert stats["size_bytes"] > 0
        assert "size_human" in stats

    def test_prune_removes_expired_entries(self, cache_manager: CacheManager, cache_dir: Path) -> None:
        """Test that prune removes expired entries."""
        # Add a valid entry
        cache_manager.set("https://example.com/valid", {"valid": True}, max_age=3600)

        # Add an expired entry manually
        url = "https://example.com/expired"
        cache_key = cache_manager._cache_key(url)
        cache_file = cache_dir / "pages" / f"{cache_key}.json"

        now = datetime.now(UTC)
        expired_at = datetime.fromtimestamp(now.timestamp() - 3600, tz=UTC)

        entry = CacheEntry(
            url=url,
            cached_at=now.isoformat(),
            expires_at=expired_at.isoformat(),
            response={"expired": True},
        )
        cache_file.write_text(entry.model_dump_json())

        pruned = cache_manager.prune_expired()

        assert pruned == 1
        assert not cache_file.exists()
        assert cache_manager.get("https://example.com/valid", max_age=3600) is not None

    def test_format_size(self) -> None:
        """Test size formatting."""
        assert CacheManager._format_size(0) == "0.0 B"
        assert CacheManager._format_size(512) == "512.0 B"
        assert CacheManager._format_size(1024) == "1.0 KB"
        assert CacheManager._format_size(1024 * 1024) == "1.0 MB"
        assert CacheManager._format_size(1024 * 1024 * 1024) == "1.0 GB"

    def test_index_updated_on_set(self, cache_manager: CacheManager, cache_dir: Path) -> None:
        """Test that index is updated when setting cache."""
        url = "https://example.com/page"
        cache_manager.set(url, {"success": True}, max_age=3600)

        index_file = cache_dir / "index.json"
        assert index_file.exists()

        index = json.loads(index_file.read_text())
        normalised_url = cache_manager._normalise_url(url)
        assert normalised_url in index

    def test_variant_produces_different_cache_keys(self, cache_manager: CacheManager) -> None:
        """Test that different variants produce different cache keys."""
        url = "https://example.com/page"
        key_default = cache_manager._cache_key(url)
        key_variant = cache_manager._cache_key(url, variant="screenshot_full_page=False")

        assert key_default != key_variant

    def test_variant_cache_entries_are_independent(self, cache_manager: CacheManager) -> None:
        """Test that variant and non-variant cache entries don't collide."""
        url = "https://example.com/page"
        response_default = {"screenshot": "full_page_b64"}
        response_viewport = {"screenshot": "viewport_only_b64"}

        cache_manager.set(url, response_default, max_age=3600)
        cache_manager.set(url, response_viewport, max_age=3600, variant="screenshot_full_page=False")

        cached_default = cache_manager.get(url, max_age=3600)
        cached_variant = cache_manager.get(url, max_age=3600, variant="screenshot_full_page=False")

        assert cached_default is not None
        assert cached_variant is not None
        assert cached_default["screenshot"] == "full_page_b64"
        assert cached_variant["screenshot"] == "viewport_only_b64"

    def test_variant_none_matches_no_variant(self, cache_manager: CacheManager) -> None:
        """Test that variant=None is equivalent to no variant."""
        url = "https://example.com/page"
        cache_manager.set(url, {"data": "test"}, max_age=3600)

        cached = cache_manager.get(url, max_age=3600, variant=None)

        assert cached is not None
        assert cached["data"] == "test"

    def test_url_with_different_tracking_params_use_same_cache(self, cache_manager: CacheManager) -> None:
        """Test that URLs with different tracking params use same cache."""
        base_url = "https://example.com/page"
        url_with_utm = "https://example.com/page?utm_source=google"

        cache_manager.set(base_url, {"cached": True}, max_age=3600)

        # Should get the same cache entry
        cached = cache_manager.get(url_with_utm, max_age=3600)

        assert cached is not None
        assert cached["cached"] is True
