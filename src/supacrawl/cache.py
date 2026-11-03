"""Local cache manager for scrape results.

Provides local caching of scraped content to speed up repeated requests.
Cache respects max_age parameter for cache freshness control.
"""

import hashlib
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

LOGGER = logging.getLogger(__name__)


class CacheEntry(BaseModel):
    """A cached scrape result."""

    url: str
    cached_at: str  # ISO format timestamp
    expires_at: str  # ISO format timestamp
    content_hash: str | None = None  # SHA256 of markdown content for change tracking
    response: dict[str, Any]  # Serialised ScrapeResult


class CacheManager:
    """Manages local cache for scraped content.

    Usage:
        cache = CacheManager()
        result = cache.get(url, max_age=3600)  # Get if cached within 1 hour
        if not result:
            result = await service.scrape(url)
            cache.set(url, result, max_age=3600)

    Cache structure:
        ~/.supacrawl/cache/
        |-- index.json         # URL -> cache entry mapping
        |-- pages/
            |-- a1b2c3d4.json  # Cached response (hash of URL)
    """

    DEFAULT_CACHE_DIR = Path.home() / ".supacrawl" / "cache"

    def __init__(self, cache_dir: Path | None = None):
        """Initialise cache manager.

        Args:
            cache_dir: Cache directory. Defaults to ~/.supacrawl/cache/
                      or SUPACRAWL_CACHE_DIR env var.
        """
        if cache_dir:
            self.cache_dir = cache_dir
        else:
            env_dir = os.environ.get("SUPACRAWL_CACHE_DIR")
            self.cache_dir = Path(env_dir) if env_dir else self.DEFAULT_CACHE_DIR

        self.pages_dir = self.cache_dir / "pages"
        self.index_path = self.cache_dir / "index.json"

        # Ensure directories exist
        self.pages_dir.mkdir(parents=True, exist_ok=True)

    def _cache_key(self, url: str, variant: str | None = None) -> str:
        """Generate cache key from URL and optional variant.

        Uses SHA256 hash of normalised URL (plus variant suffix) for the
        filename.  URL normalisation removes tracking parameters and fragments.
        The *variant* differentiates cache entries for the same URL when
        request parameters affect the output (e.g. screenshot settings).

        Args:
            url: URL to generate key for.
            variant: Optional variant suffix to include in the hash (e.g.
                ``"screenshot_full_page=False"``).

        Returns:
            16-character hex hash for cache filename.
        """
        normalised = self._normalise_url(url)
        if variant:
            normalised = f"{normalised}|{variant}"
        return hashlib.sha256(normalised.encode()).hexdigest()[:16]

    def _normalise_url(self, url: str) -> str:
        """Normalise URL for cache key generation.

        Removes:
        - Fragments (#...)
        - Common tracking parameters (utm_*, fbclid, gclid, etc.)
        - Trailing slashes

        Args:
            url: URL to normalise.

        Returns:
            Normalised URL string.
        """
        from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

        parsed = urlparse(url)

        # Remove fragment
        parsed = parsed._replace(fragment="")

        # Remove tracking parameters
        tracking_params = {
            "utm_source",
            "utm_medium",
            "utm_campaign",
            "utm_term",
            "utm_content",
            "fbclid",
            "gclid",
            "dclid",
            "msclkid",
            "ref",
            "source",
        }

        if parsed.query:
            params = parse_qs(parsed.query, keep_blank_values=True)
            filtered = {k: v for k, v in params.items() if k.lower() not in tracking_params}
            new_query = urlencode(filtered, doseq=True) if filtered else ""
            parsed = parsed._replace(query=new_query)

        # Remove trailing slash from path (but keep root /)
        path = parsed.path.rstrip("/") or "/"
        parsed = parsed._replace(path=path)

        return urlunparse(parsed)

    def _load_index(self) -> dict[str, str]:
        """Load cache index from disk.

        Returns:
            Dict mapping URL to cache key.
        """
        if not self.index_path.exists():
            return {}

        try:
            index: dict[str, str] = json.loads(self.index_path.read_text())
            return index
        except (json.JSONDecodeError, OSError) as e:
            LOGGER.warning(f"Failed to load cache index: {e}")
            return {}

    def _save_index(self, index: dict[str, str]) -> None:
        """Save cache index to disk.

        Args:
            index: Dict mapping URL to cache key.
        """
        try:
            self.index_path.write_text(json.dumps(index, indent=2))
        except OSError as e:
            LOGGER.warning(f"Failed to save cache index: {e}")

    def get(self, url: str, max_age: int, variant: str | None = None) -> dict[str, Any] | None:
        """Get cached result if fresh enough.

        Args:
            url: URL to look up.
            max_age: Maximum age in seconds for the cached result.
            variant: Optional variant suffix that was used when storing the
                entry (must match the value passed to :meth:`set`).

        Returns:
            Cached response dict (ScrapeResult format) or None if not found/expired.
        """
        if max_age <= 0:
            return None

        cache_key = self._cache_key(url, variant=variant)
        cache_file = self.pages_dir / f"{cache_key}.json"

        if not cache_file.exists():
            LOGGER.debug(f"Cache miss (not found): {url}")
            return None

        try:
            entry = CacheEntry.model_validate_json(cache_file.read_text())

            # Check if expired
            expires_at = datetime.fromisoformat(entry.expires_at)
            now = datetime.now(UTC)

            if now > expires_at:
                LOGGER.debug(f"Cache miss (expired): {url}")
                return None

            LOGGER.debug(f"Cache hit: {url}")
            return entry.response

        except Exception as e:
            LOGGER.warning(f"Failed to read cache entry for {url}: {e}")
            return None

    def get_previous(self, url: str, variant: str | None = None) -> CacheEntry | None:
        """Get previous cached entry regardless of expiry (for change tracking).

        Unlike :meth:`get`, this ignores expiry and returns the raw CacheEntry
        so callers can access ``content_hash`` and ``cached_at`` for comparison.

        Args:
            url: URL to look up.
            variant: Optional variant suffix (must match the value used in :meth:`set`).

        Returns:
            CacheEntry if found, None if no cached entry exists.
        """
        cache_key = self._cache_key(url, variant=variant)
        cache_file = self.pages_dir / f"{cache_key}.json"

        if not cache_file.exists():
            return None

        try:
            return CacheEntry.model_validate_json(cache_file.read_text())
        except Exception as e:
            LOGGER.warning(f"Failed to read cache entry for {url}: {e}")
            return None

    def set(
        self,
        url: str,
        response: dict[str, Any],
        max_age: int,
        variant: str | None = None,
        content_hash: str | None = None,
    ) -> None:
        """Cache a scrape result.

        Args:
            url: URL that was scraped.
            response: ScrapeResult as dict.
            max_age: Time-to-live in seconds.
            variant: Optional variant suffix to differentiate cache entries
                for the same URL with different request parameters.
            content_hash: SHA256 hash of markdown content for change tracking.
        """
        if max_age <= 0:
            return

        cache_key = self._cache_key(url, variant=variant)
        cache_file = self.pages_dir / f"{cache_key}.json"

        now = datetime.now(UTC)
        expires_at = datetime.fromtimestamp(now.timestamp() + max_age, tz=UTC)

        entry = CacheEntry(
            url=url,
            cached_at=now.isoformat(),
            expires_at=expires_at.isoformat(),
            content_hash=content_hash,
            response=response,
        )

        try:
            cache_file.write_text(entry.model_dump_json(indent=2))

            # Update index
            index = self._load_index()
            normalised_url = self._normalise_url(url)
            index[normalised_url] = cache_key
            self._save_index(index)

            LOGGER.debug(f"Cached: {url} (expires: {expires_at.isoformat()})")

        except OSError as e:
            LOGGER.warning(f"Failed to cache {url}: {e}")

    def clear(self, url: str | None = None) -> int:
        """Clear cache for URL or all.

        Args:
            url: URL to clear cache for. If None, clears all cache.

        Returns:
            Number of entries cleared.
        """
        cleared = 0

        if url:
            # Clear specific URL
            cache_key = self._cache_key(url)
            cache_file = self.pages_dir / f"{cache_key}.json"

            if cache_file.exists():
                cache_file.unlink()
                cleared = 1
                LOGGER.debug(f"Cleared cache for: {url}")

            # Update index
            index = self._load_index()
            normalised_url = self._normalise_url(url)
            if normalised_url in index:
                del index[normalised_url]
                self._save_index(index)
        else:
            # Clear all cache
            for cache_file in self.pages_dir.glob("*.json"):
                cache_file.unlink()
                cleared += 1

            # Clear index
            if self.index_path.exists():
                self.index_path.unlink()

            LOGGER.debug(f"Cleared all cache ({cleared} entries)")

        return cleared

    def stats(self) -> dict[str, Any]:
        """Get cache statistics.

        Returns:
            Dict with cache stats (entries, size, etc.)
        """
        entries = 0
        total_size = 0
        expired = 0
        now = datetime.now(UTC)

        for cache_file in self.pages_dir.glob("*.json"):
            entries += 1
            total_size += cache_file.stat().st_size

            try:
                entry = CacheEntry.model_validate_json(cache_file.read_text())
                expires_at = datetime.fromisoformat(entry.expires_at)
                if now > expires_at:
                    expired += 1
            except Exception:
                pass

        return {
            "entries": entries,
            "expired": expired,
            "valid": entries - expired,
            "size_bytes": total_size,
            "size_human": self._format_size(total_size),
            "cache_dir": str(self.cache_dir),
        }

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        """Format size in human-readable format."""
        size: float = float(size_bytes)
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"

    def prune_expired(self) -> int:
        """Remove expired cache entries.

        Returns:
            Number of entries pruned.
        """
        pruned = 0
        now = datetime.now(UTC)
        index = self._load_index()
        modified = False

        for cache_file in self.pages_dir.glob("*.json"):
            try:
                entry = CacheEntry.model_validate_json(cache_file.read_text())
                expires_at = datetime.fromisoformat(entry.expires_at)

                if now > expires_at:
                    cache_file.unlink()
                    pruned += 1

                    # Remove from index
                    normalised_url = self._normalise_url(entry.url)
                    if normalised_url in index:
                        del index[normalised_url]
                        modified = True

            except Exception as e:
                LOGGER.warning(f"Error checking cache file {cache_file}: {e}")

        if modified:
            self._save_index(index)

        LOGGER.debug(f"Pruned {pruned} expired entries")
        return pruned
