"""Crawl service for full-site scraping."""

import hashlib
import json
import logging
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlparse

from supacrawl.discovery.robots import RobotsConfig, fetch_robots, is_url_allowed
from supacrawl.models import CrawlEvent, ScrapeData
from supacrawl.services.browser import BrowserManager
from supacrawl.services.map import MapService
from supacrawl.services.scrape import ScrapeService
from supacrawl.services.throttle import HostRateLimiter, host_of
from supacrawl.utils import normalise_url_for_dedupe

if TYPE_CHECKING:
    from supacrawl.services.strategy_memory import StrategyStore
    from supacrawl.telemetry import MetricsSink


def _url_origin(url: str) -> str:
    """Return the scheme+host+port origin of a URL.

    Args:
        url: URL string.

    Returns:
        Origin string such as ``"https://example.com"``.
    """
    parsed = urlparse(url)
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.netloc.split(':')[0]}{port}"


def _scope_headers_to_origin(
    headers: dict[str, str] | None,
    target_url: str,
    start_origin: str,
) -> dict[str, str] | None:
    """Return headers only if target_url shares the crawl's start origin.

    Prevents custom auth headers (e.g. Authorization, Cookie) from being sent
    to third-party domains when allow_external_links is True.

    Args:
        headers: Caller-supplied headers dict, or None.
        target_url: URL about to be scraped.
        start_origin: Origin of the crawl's starting URL.

    Returns:
        The headers dict when origins match, None otherwise.
    """
    if not headers:
        return None
    if _url_origin(target_url) == start_origin:
        return headers
    LOGGER.debug(
        "Dropping custom headers for external-origin URL %s (start origin: %s)",
        target_url,
        start_origin,
    )
    return None


LOGGER = logging.getLogger(__name__)

# Type alias for wait_until options
type WaitUntilType = Literal["commit", "domcontentloaded", "load", "networkidle"]


class CrawlService:
    """Crawl entire websites by combining map and scrape.

    Usage:
        service = CrawlService()
        async for event in service.crawl("https://example.com"):
            if event.type == "page":
                print(f"Scraped: {event.url}")
            elif event.type == "progress":
                print(f"Progress: {event.completed}/{event.total}")
    """

    def __init__(
        self,
        browser: BrowserManager | None = None,
        map_service: MapService | None = None,
        scrape_service: ScrapeService | None = None,
        strategy_store: "StrategyStore | None" = None,
        telemetry: "MetricsSink | None" = None,
    ):
        """Initialize crawl service.

        Args:
            browser: Optional shared BrowserManager. If provided, the service
                uses it instead of creating its own (caller manages lifecycle).
            map_service: Optional shared MapService. Created internally if not provided.
            scrape_service: Optional shared ScrapeService. Created internally if not provided.
            strategy_store: Optional per-domain strategy memory (#130). When the
                crawl owns its ScrapeService (no ``scrape_service`` injected), it
                is threaded into that service so a crawl learns each domain's
                cheapest working strategy on the first page and seeds the rest.
                Ignored when ``scrape_service`` is injected (the caller's service
                already carries — or deliberately omits — its own store).
            telemetry: Optional field telemetry sink (#137), threaded into a
                crawl-owned ScrapeService so per-page quality/usage is recorded.
        """
        self._injected_browser = browser
        self._injected_map_service = map_service
        self._injected_scrape_service = scrape_service
        self._strategy_store = strategy_store
        self._telemetry = telemetry
        self._browser: BrowserManager | None = None
        self._map_service: MapService | None = None
        self._scrape_service: ScrapeService | None = None

    async def crawl(
        self,
        url: str,
        limit: int = 100,
        max_depth: int = 3,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        output_dir: Path | None = None,
        resume: bool = False,
        formats: list[str] | None = None,
        deduplicate_similar_urls: bool = False,
        allow_external_links: bool = False,
        locale_config: object | None = None,  # LocaleConfig
        stealth: bool = False,
        proxy: str | None = None,
        save_files: bool = True,
        concurrency: int = 10,
        wait_until: WaitUntilType | None = None,
        headless: bool | None = None,
        engine: str | None = None,
        cache_dir: Path | None = None,
        change_tracking_modes: list[str] | None = None,
        expand_iframes: str = "same-origin",
        headers: dict[str, str] | None = None,
        respect_robots: bool = False,
        request_delay: float = 0.0,
    ) -> AsyncGenerator[CrawlEvent]:
        """Crawl a website, yielding events as pages complete.

        Args:
            url: Starting URL
            limit: Maximum pages to crawl
            max_depth: Maximum crawl depth
            include_patterns: URL patterns to include
            exclude_patterns: URL patterns to exclude
            output_dir: Directory to save scraped content (simple flat output)
            resume: Resume from previous crawl state
            formats: Output formats to save (default: ["markdown"])
            deduplicate_similar_urls: Deduplicate URLs that differ only by
                tracking parameters or fragments (default: False)
            allow_external_links: Follow and scrape links to external domains
                (default: False)
            locale_config: Optional LocaleConfig for browser locale/timezone settings
            stealth: Enable stealth mode via Patchright for anti-bot evasion.
                Ignored when engine is explicitly set.
            proxy: Proxy URL (e.g., http://user:pass@host:port, socks5://host:port)
            save_files: Save scraped content files to output_dir (default: True).
                When False with output_dir set, only manifest.json is saved for
                resume capability without saving content files.
            concurrency: Max concurrent requests for URL processing (default: 10)
            wait_until: Page load strategy. Options: commit, domcontentloaded (default),
                load, networkidle. Falls back to SUPACRAWL_WAIT_UNTIL env var if None.
            headless: Run browser in headless mode. Only used when CrawlService
                creates its own BrowserManager (i.e. no injected browser).
            engine: Browser engine ("playwright", "patchright", "camoufox").
                Overrides the stealth flag when set.
            cache_dir: Cache directory for change tracking. Auto-resolved when
                changeTracking format is requested.
            change_tracking_modes: Optional diff modes for change tracking.
                Supports: ["git-diff", "json"]. Passed to per-page scrape calls.
            expand_iframes: Iframe expansion mode. "none" strips all (legacy),
                "same-origin" expands same-origin iframes (default),
                "all" expands all non-blocked iframes.
            headers: Custom HTTP headers to send with every request (e.g.
                Authorization, Cookie). Only sent to URLs whose origin matches
                the start URL's origin; dropped for external-origin URLs when
                allow_external_links is True. Only header KEYS are logged;
                values are never written to logs or persisted.
            respect_robots: When True, consult each origin's robots.txt and skip
                disallowed URLs before scraping (a declared Crawl-delay also raises
                the per-host gap). Default False: the crawl fetches the URLs it is
                given without consulting robots.txt.
            request_delay: Minimum seconds between requests to the same host
                (default 0.0, i.e. no throttle). Set a positive value to space out
                requests to a single origin. When respect_robots is True and a site
                declares a larger Crawl-delay, the larger value wins.

        Yields:
            CrawlEvent for each page and progress update
        """
        self._formats = formats or ["markdown"]
        self._save_files = save_files

        try:
            if self._injected_browser is not None:
                # Use injected services (caller manages browser lifecycle)
                self._browser = self._injected_browser
                self._map_service = self._injected_map_service or MapService(
                    browser=self._browser,
                    concurrency=concurrency,
                    wait_until=wait_until,
                    headers=headers,
                )
                self._scrape_service = self._injected_scrape_service or ScrapeService(
                    browser=self._browser,
                    locale_config=locale_config,
                    cache_dir=cache_dir,
                    strategy_store=self._strategy_store,
                    telemetry=self._telemetry,
                )
                async for event in self._crawl_inner(
                    url=url,
                    limit=limit,
                    max_depth=max_depth,
                    include_patterns=include_patterns,
                    exclude_patterns=exclude_patterns,
                    output_dir=output_dir,
                    resume=resume,
                    deduplicate_similar_urls=deduplicate_similar_urls,
                    allow_external_links=allow_external_links,
                    concurrency=concurrency,
                    wait_until=wait_until,
                    change_tracking_modes=change_tracking_modes,
                    expand_iframes=expand_iframes,
                    engine=engine,
                    headers=headers,
                    respect_robots=respect_robots,
                    request_delay=request_delay,
                ):
                    yield event
            else:
                # Crawl-level shared-engine seeding (#139): a crawl shares ONE
                # browser across pages. If the start domain's learned champion
                # needs a stronger engine than the default (camoufox / stealth),
                # build the shared browser with it once, so every same-domain page
                # reuses one strong browser instead of the per-page seed path
                # spinning up a temporary stealth browser per page. Uses the
                # exploration-free ``get`` (not ``seed``), so the shared engine is
                # the deterministic champion and the per-page seeds match it — the
                # coordination the issue flagged: no shared pin fighting a per-page
                # explore. A user-pinned engine/stealth still wins over memory.
                seed_engine, seed_stealth = engine, stealth
                if self._strategy_store is not None and engine is None and not stealth:
                    from supacrawl.services.scrape import _engine_available, _resolve_engine_stealth
                    from supacrawl.services.strategy_memory import _engine_cost, registrable_domain

                    start_domain = registrable_domain(url)
                    champion = self._strategy_store.get(start_domain) if start_domain else None
                    # Gate on the RESOLVED engine's availability: a patchright
                    # champion is stored as (None, True), which the raw check would
                    # wave through as "playwright" and then fail launching the one
                    # shared browser — aborting the whole crawl instead of
                    # degrading to the default engine.
                    resolved_engine = (
                        _resolve_engine_stealth(champion.engine, champion.stealth)[0] if champion is not None else None
                    )
                    if (
                        champion is not None
                        and _engine_cost(champion.engine, champion.stealth) > 0
                        and _engine_available(resolved_engine)
                    ):
                        seed_engine, seed_stealth = champion.engine, champion.stealth
                        LOGGER.info(
                            "Crawl seeding shared %s browser from %s champion (stealth=%s)",
                            resolved_engine,
                            start_domain,
                            champion.stealth,
                        )

                # Create and manage own browser lifecycle
                async with BrowserManager(
                    headless=headless,
                    locale_config=locale_config,
                    stealth=seed_stealth,
                    proxy=proxy,
                    engine=seed_engine,
                ) as browser:
                    self._browser = browser
                    self._map_service = MapService(
                        browser=browser,
                        concurrency=concurrency,
                        wait_until=wait_until,
                        headers=headers,
                    )
                    self._scrape_service = ScrapeService(
                        browser=browser,
                        locale_config=locale_config,
                        cache_dir=cache_dir,
                        strategy_store=self._strategy_store,
                        telemetry=self._telemetry,
                    )
                    async for event in self._crawl_inner(
                        url=url,
                        limit=limit,
                        max_depth=max_depth,
                        include_patterns=include_patterns,
                        exclude_patterns=exclude_patterns,
                        output_dir=output_dir,
                        resume=resume,
                        deduplicate_similar_urls=deduplicate_similar_urls,
                        allow_external_links=allow_external_links,
                        concurrency=concurrency,
                        wait_until=wait_until,
                        change_tracking_modes=change_tracking_modes,
                        expand_iframes=expand_iframes,
                        headers=headers,
                        respect_robots=respect_robots,
                        request_delay=request_delay,
                    ):
                        yield event

        except Exception as e:
            LOGGER.error(f"Crawl failed: {e}", exc_info=True)
            yield CrawlEvent(
                type="error",
                error=str(e),
            )

    async def _crawl_inner(
        self,
        url: str,
        limit: int,
        max_depth: int,
        include_patterns: list[str] | None,
        exclude_patterns: list[str] | None,
        output_dir: Path | None,
        resume: bool,
        deduplicate_similar_urls: bool,
        allow_external_links: bool,
        concurrency: int,
        wait_until: WaitUntilType | None,
        change_tracking_modes: list[str] | None = None,
        expand_iframes: str = "same-origin",
        engine: str | None = None,
        headers: dict[str, str] | None = None,
        respect_robots: bool = False,
        request_delay: float = 0.0,
    ) -> AsyncGenerator[CrawlEvent]:
        """Core crawl logic. Assumes _browser, _map_service, _scrape_service are set."""
        assert self._map_service is not None
        assert self._scrape_service is not None

        # Per-host courtesy throttle and per-origin robots.txt cache. robots is
        # fetched lazily per origin so an external-link crawl honours each site's
        # rules without a wasted fetch when respect_robots is off.
        rate_limiter = HostRateLimiter(min_delay=request_delay)
        robots_cache: dict[str, RobotsConfig] = {}

        async def robots_for(target_url: str) -> RobotsConfig | None:
            """Return the (cached) robots.txt config for *target_url*'s origin."""
            if not respect_robots:
                return None
            origin = _url_origin(target_url)
            if origin not in robots_cache:
                config = await fetch_robots(origin)
                robots_cache[origin] = config
                # A declared Crawl-delay raises the per-host minimum gap.
                rate_limiter.set_host_delay(host_of(target_url), config.crawl_delay)
            return robots_cache[origin]

        # Derive the start-URL origin so we can scope custom headers to same-origin
        # URLs only (prevents leaking auth headers to third-party domains during a
        # crawl with allow_external_links=True).
        start_origin = _url_origin(url)

        # Load resume state
        scraped_urls: set[str] = set()
        if resume and output_dir:
            scraped_urls = self._load_resume_state(output_dir)
            if scraped_urls:
                LOGGER.info(f"Resuming crawl with {len(scraped_urls)} already scraped")

        # Discover URLs with streaming progress
        LOGGER.info(f"Mapping URLs from {url}")
        if allow_external_links:
            LOGGER.info("External links enabled - will follow cross-domain links")

        # Consume map generator and yield progress events
        map_result = None
        async for map_event in self._map_service.map(
            url=url,
            limit=limit,
            max_depth=max_depth,
            allow_external_links=allow_external_links,
        ):
            # Translate map events to crawl events
            if map_event.type == "complete":
                map_result = map_event.result
            elif map_event.type == "error":
                yield CrawlEvent(
                    type="error",
                    error=f"Map failed: {map_event.message}",
                )
                return
            else:
                # Yield mapping progress events
                yield CrawlEvent(
                    type="mapping",
                    completed=map_event.discovered,
                    total=map_event.total or 0,
                    message=map_event.message,
                )

        if map_result is None or not map_result.success:
            yield CrawlEvent(
                type="error",
                error=f"Map failed: {map_result.error if map_result else 'No result'}",
            )
            return

        # Filter URLs
        urls_to_scrape = []
        normalised_urls: set[str] = set()
        dedupe_count = 0
        robots_skipped = 0

        for link in map_result.links:
            if link.url in scraped_urls:
                continue
            if include_patterns and not self._matches_patterns(link.url, include_patterns):
                continue
            if exclude_patterns and self._matches_patterns(link.url, exclude_patterns):
                continue

            # Honour robots.txt: skip disallowed URLs before scraping.
            robots = await robots_for(link.url)
            if robots is not None and not is_url_allowed(link.url, robots):
                LOGGER.info("Skipping URL (robots.txt disallow): %s", link.url)
                robots_skipped += 1
                continue

            # Deduplicate similar URLs if enabled
            if deduplicate_similar_urls:
                normalised = normalise_url_for_dedupe(link.url)
                if normalised in normalised_urls:
                    LOGGER.debug(f"Deduplicated URL: {link.url}")
                    dedupe_count += 1
                    continue
                normalised_urls.add(normalised)

            urls_to_scrape.append(link.url)

        total = len(urls_to_scrape)
        if dedupe_count > 0:
            LOGGER.info(f"Deduplicated {dedupe_count} similar URLs")
        if robots_skipped > 0:
            LOGGER.info("Skipped %d URL(s) disallowed by robots.txt", robots_skipped)
        LOGGER.info(f"Found {total} URLs to scrape")

        yield CrawlEvent(
            type="progress",
            completed=0,
            total=total,
        )

        # Scrape each URL
        completed = 0
        errors = []
        wants_change_tracking = "changeTracking" in self._formats

        # Track change statuses for crawl-level summary
        change_counts: dict[str, int] = {"new": 0, "same": 0, "changed": 0, "removed": 0}

        for url_to_scrape in urls_to_scrape:
            try:
                # Map output formats to scrape formats
                scrape_formats: list[str] = []
                if "markdown" in self._formats or "json" in self._formats:
                    scrape_formats.append("markdown")
                if "html" in self._formats or "json" in self._formats:
                    scrape_formats.append("html")
                if wants_change_tracking:
                    scrape_formats.append("changeTracking")
                if not scrape_formats:
                    scrape_formats = ["markdown"]

                # Apply same-origin scoping: only send custom headers to URLs
                # whose origin matches the crawl's start URL.  External-origin
                # URLs receive no custom headers to prevent auth header leakage.
                scoped_headers = _scope_headers_to_origin(
                    headers=headers,
                    target_url=url_to_scrape,
                    start_origin=start_origin,
                )

                # Courtesy throttle: wait out the per-host minimum gap (raised by
                # any robots.txt Crawl-delay) before hitting the origin again.
                await rate_limiter.acquire(url_to_scrape)

                result = await self._scrape_service.scrape(
                    url_to_scrape,
                    formats=scrape_formats,  # type: ignore[arg-type]
                    change_tracking_modes=change_tracking_modes,
                    expand_iframes=expand_iframes,  # type: ignore[arg-type]
                    engine=engine,
                    headers=scoped_headers,
                )

                if result.success and result.data:
                    # Track change status
                    if result.data.change_tracking:
                        status = result.data.change_tracking.change_status
                        change_counts[status] = change_counts.get(status, 0) + 1

                    # Save to output directory
                    if output_dir:
                        self._save_page(output_dir, url_to_scrape, result.data)

                    yield CrawlEvent(
                        type="page",
                        url=url_to_scrape,
                        data=result.data,
                        completed=completed + 1,
                        total=total,
                    )
                else:
                    # Track "removed" if change tracking returned it
                    if result.data and result.data.change_tracking:
                        status = result.data.change_tracking.change_status
                        change_counts[status] = change_counts.get(status, 0) + 1

                    errors.append(f"{url_to_scrape}: {result.error}")
                    yield CrawlEvent(
                        type="error",
                        url=url_to_scrape,
                        error=result.error,
                        completed=completed + 1,
                        total=total,
                    )

            except Exception as e:
                errors.append(f"{url_to_scrape}: {e!s}")
                LOGGER.error(f"Scrape failed for {url_to_scrape}: {e}")
                yield CrawlEvent(
                    type="error",
                    url=url_to_scrape,
                    error=str(e),
                    completed=completed + 1,
                    total=total,
                )

            completed += 1

            yield CrawlEvent(
                type="progress",
                completed=completed,
                total=total,
            )

        # Build change summary if tracking was active
        change_summary = None
        if wants_change_tracking:
            change_summary = {k: v for k, v in change_counts.items() if v > 0}

        # Final complete event
        yield CrawlEvent(
            type="complete",
            completed=completed,
            total=total,
            change_summary=change_summary,
        )

    def _matches_patterns(self, url: str, patterns: list[str]) -> bool:
        """Check if URL matches any pattern.

        Args:
            url: URL to check
            patterns: Patterns to match against

        Returns:
            True if URL matches any pattern
        """
        import fnmatch

        return any(fnmatch.fnmatch(url, pattern) for pattern in patterns)

    def _load_resume_state(self, output_dir: Path) -> set[str]:
        """Load URLs that have already been scraped.

        Args:
            output_dir: Output directory

        Returns:
            Set of already-scraped URLs
        """
        scraped = set()
        manifest_path = output_dir / "manifest.json"

        if manifest_path.exists():
            with open(manifest_path) as f:
                manifest = json.load(f)
                scraped = set(manifest.get("scraped_urls", []))

        return scraped

    def _save_page(self, output_dir: Path, url: str, data: ScrapeData) -> None:
        """Save scraped page to output directory.

        Args:
            output_dir: Output directory
            url: Source URL
            data: ScrapeData to save
        """
        output_dir.mkdir(parents=True, exist_ok=True)

        # Only save content files if save_files is enabled
        if getattr(self, "_save_files", True):
            # Generate base filename from URL
            parsed = urlparse(url)
            path = parsed.path.strip("/").replace("/", "_") or "index"

            # Check for duplicates and add hash suffix if needed
            base_path = path
            if (output_dir / f"{path}.md").exists() or (output_dir / f"{path}.html").exists():
                url_hash = hashlib.sha256(url.encode()).hexdigest()[:8]
                base_path = f"{path}_{url_hash}"

            # Save requested formats
            formats = getattr(self, "_formats", ["markdown"])

            if "markdown" in formats and data.markdown:
                with open(output_dir / f"{base_path}.md", "w") as f:
                    # Add frontmatter
                    f.write("---\n")
                    f.write(f"source_url: {url}\n")
                    if data.metadata and data.metadata.title:
                        f.write(f"title: {data.metadata.title}\n")
                    f.write("---\n\n")
                    f.write(data.markdown)

            if "html" in formats and data.html:
                with open(output_dir / f"{base_path}.html", "w") as f:
                    f.write(data.html)

            if "json" in formats:
                with open(output_dir / f"{base_path}.json", "w") as f:
                    json.dump(
                        {
                            "url": url,
                            "markdown": data.markdown,
                            "html": data.html,
                            "metadata": {
                                "title": data.metadata.title if data.metadata else None,
                                "description": data.metadata.description if data.metadata else None,
                                "source_url": data.metadata.source_url if data.metadata else url,
                            },
                        },
                        f,
                        indent=2,
                    )

        # Always update manifest for resume capability
        manifest_path = output_dir / "manifest.json"
        manifest: dict[str, list[str]] = {"scraped_urls": []}
        if manifest_path.exists():
            with open(manifest_path) as f:
                manifest = json.load(f)

        if "scraped_urls" not in manifest:
            manifest["scraped_urls"] = []

        manifest["scraped_urls"].append(url)
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)
