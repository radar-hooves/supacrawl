"""Map service for URL discovery."""

import asyncio
import logging
from collections import deque
from collections.abc import AsyncGenerator
from typing import Literal
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from supacrawl.models import MapEvent, MapLink, MapResult
from supacrawl.services.browser import BrowserManager
from supacrawl.services.url_guard import guarded_request

LOGGER = logging.getLogger(__name__)

# Default concurrency limit for parallel URL processing
DEFAULT_CONCURRENCY = 10

# Type alias for wait_until options
type WaitUntilType = Literal["commit", "domcontentloaded", "load", "networkidle"]


class MapService:
    """Discover all URLs on a website.

    Usage (streaming with progress):
        service = MapService()
        async for event in service.map("https://example.com"):
            if event.type == "discovery":
                print(f"Found {event.discovered} URLs...")
            elif event.type == "metadata":
                print(f"Extracting metadata: {event.discovered}/{event.total}")
            elif event.type == "complete":
                result = event.result
                for link in result.links:
                    print(link.url, link.title)

    With stealth mode (requires: pip install supacrawl[stealth]):
        service = MapService(stealth=True)
        async for event in service.map("https://protected-site.com"):
            if event.type == "complete":
                result = event.result
    """

    def __init__(
        self,
        browser: BrowserManager | None = None,
        stealth: bool = False,
        proxy: str | None = None,
        concurrency: int = DEFAULT_CONCURRENCY,
        wait_until: WaitUntilType | None = None,
        headless: bool | None = None,
        engine: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        """Initialize map service.

        Args:
            browser: Optional BrowserManager (created if not provided)
            stealth: Enable stealth mode via Patchright for anti-bot evasion.
                Ignored when engine is explicitly set.
            proxy: Proxy URL (e.g., http://user:pass@host:port, socks5://host:port)
            concurrency: Max concurrent requests for URL processing (default: 10)
            wait_until: Page load strategy. Options: commit, domcontentloaded (default),
                load, networkidle. Falls back to SUPACRAWL_WAIT_UNTIL env var if None.
            headless: Run browser in headless mode. Passed through to any BrowserManager
                instances created internally when no shared browser is provided.
            engine: Browser engine ("playwright", "patchright", "camoufox").
                Overrides the stealth flag when set.
            headers: Custom HTTP headers to send with every request (e.g.
                Authorization, Cookie). Only header KEYS are logged; values are
                never written to logs.
        """
        self._browser = browser
        self._owns_browser = browser is None
        self._stealth = stealth
        self._proxy = proxy
        self._headless = headless
        self._engine = engine
        self._concurrency = max(1, concurrency)  # Ensure at least 1
        self._wait_until = wait_until
        self._headers = headers

    async def map(
        self,
        url: str,
        limit: int = 200,
        max_depth: int = 3,
        sitemap: Literal["include", "skip", "only"] = "include",
        include_subdomains: bool = False,
        search: str | None = None,
        ignore_query_params: bool = False,
        allow_external_links: bool = False,
        ignore_cache: bool = False,
        headers: dict[str, str] | None = None,
    ) -> AsyncGenerator[MapEvent]:
        """Map a website and discover URLs, yielding progress events.

        Args:
            url: Starting URL
            limit: Maximum URLs to return
            max_depth: Maximum BFS depth
            sitemap: Sitemap handling mode
            include_subdomains: Include subdomain URLs
            search: Filter URLs containing this text
            ignore_query_params: Remove query params from URLs
            allow_external_links: Follow links to external domains
            ignore_cache: When True, bypass cached results and perform fresh discovery
            headers: Custom HTTP headers for this call; overrides instance headers when
                provided. Only header KEYS are logged; values are never persisted.

        Yields:
            MapEvent for each phase of discovery:
            - sitemap: During sitemap fetch
            - discovery: During BFS crawl (periodic progress)
            - metadata: During metadata extraction (per-URL)
            - complete: Final event with MapResult
            - error: On failure
        """
        effective_headers = headers if headers is not None else self._headers
        try:
            # Parse starting URL for domain
            parsed = urlparse(url)
            domain = parsed.netloc

            # Collect URLs from different sources
            discovered_urls: set[str] = set()

            # Fetch sitemap URLs if requested
            if sitemap != "skip":
                yield MapEvent(
                    type="sitemap",
                    message=f"Fetching sitemap from {url}",
                )
                LOGGER.info(f"Fetching sitemap from {url}")
                sitemap_urls = await self._fetch_sitemap(url, headers=effective_headers)
                discovered_urls.update(sitemap_urls)
                LOGGER.info(f"Found {len(sitemap_urls)} URLs from sitemap")
                yield MapEvent(
                    type="sitemap",
                    discovered=len(sitemap_urls),
                    message=f"Found {len(sitemap_urls)} URLs from sitemap",
                )

            # BFS crawl if requested
            if sitemap != "only":
                yield MapEvent(
                    type="discovery",
                    message=f"Starting URL discovery from {url}",
                )
                LOGGER.info(f"Starting BFS crawl from {url}")
                async for event in self._bfs_crawl_streaming(
                    start_url=url,
                    domain=domain,
                    max_depth=max_depth,
                    limit=limit,
                    include_subdomains=include_subdomains,
                    allow_external_links=allow_external_links,
                    headers=effective_headers,
                ):
                    # Collect URLs from discovery events (message starts with http)
                    if event.message and event.message.startswith("http"):
                        discovered_urls.add(event.message)
                    yield event
                LOGGER.info(f"Found {len(discovered_urls)} URLs from crawling")

            # Strip query params if requested
            if ignore_query_params:
                normalized_urls: set[str] = set()
                for u in discovered_urls:
                    parsed_url = urlparse(u)
                    normalized = f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}"
                    normalized_urls.add(normalized)
                discovered_urls = normalized_urls
                LOGGER.info(f"Normalized to {len(discovered_urls)} URLs (query params removed)")

            # Convert to list and apply limit
            urls_list = list(discovered_urls)[:limit]

            # Apply search filter if provided
            if search:
                urls_list = [u for u in urls_list if search.lower() in u.lower()]
                LOGGER.info(f"Filtered to {len(urls_list)} URLs matching '{search}'")

            # Extract metadata for each URL (parallel with semaphore)
            total_urls = len(urls_list)
            if total_urls > 0:
                yield MapEvent(
                    type="metadata",
                    discovered=0,
                    total=total_urls,
                    message=f"Extracting metadata for {total_urls} URLs (concurrency: {self._concurrency})",
                )
                LOGGER.info(f"Extracting metadata for {total_urls} URLs (concurrency: {self._concurrency})")

            # Use parallel extraction with progress tracking
            links = []
            completed_count = 0
            semaphore = asyncio.Semaphore(self._concurrency)

            async def extract_with_limit(url_str: str) -> MapLink:
                async with semaphore:
                    title, description = await self._extract_metadata(url_str, headers=effective_headers)
                    return MapLink(url=url_str, title=title, description=description)

            # Process in batches to yield progress events
            batch_size = max(20, self._concurrency * 2)
            for batch_start in range(0, total_urls, batch_size):
                batch_end = min(batch_start + batch_size, total_urls)
                batch_urls = urls_list[batch_start:batch_end]

                # Process batch concurrently
                batch_results = await asyncio.gather(*[extract_with_limit(u) for u in batch_urls])
                links.extend(batch_results)
                completed_count += len(batch_results)

                # Yield progress after each batch
                yield MapEvent(
                    type="metadata",
                    discovered=completed_count,
                    total=total_urls,
                    message=f"Extracted metadata: {completed_count}/{total_urls}",
                )

            result = MapResult(success=True, links=links, error=None)
            yield MapEvent(type="complete", result=result)

        except Exception as e:
            LOGGER.error(f"Map failed: {e}", exc_info=True)
            yield MapEvent(
                type="error",
                message=str(e),
                result=MapResult(success=False, links=[], error=str(e)),
            )

    async def map_all(
        self,
        url: str,
        limit: int = 200,
        max_depth: int = 3,
        sitemap: Literal["include", "skip", "only"] = "include",
        include_subdomains: bool = False,
        search: str | None = None,
        ignore_query_params: bool = False,
        allow_external_links: bool = False,
        ignore_cache: bool = False,
        headers: dict[str, str] | None = None,
    ) -> MapResult:
        """Map a website and return the final result (no streaming).

        This is a convenience method that consumes the streaming map() generator
        and returns the final MapResult. Use map() directly if you need progress
        events during URL discovery.

        Args:
            url: Starting URL
            limit: Maximum URLs to return
            max_depth: Maximum BFS depth
            sitemap: Sitemap handling mode
            include_subdomains: Include subdomain URLs
            search: Filter URLs containing this text
            ignore_query_params: Remove query params from URLs
            allow_external_links: Follow links to external domains
            ignore_cache: When True, bypass cached results and perform fresh discovery
            headers: Custom HTTP headers for this call; overrides instance headers when
                provided. Only header KEYS are logged; values are never persisted.

        Returns:
            MapResult with discovered URLs
        """
        result = MapResult(success=False, links=[], error="No result received")
        async for event in self.map(
            url=url,
            limit=limit,
            max_depth=max_depth,
            sitemap=sitemap,
            include_subdomains=include_subdomains,
            search=search,
            ignore_query_params=ignore_query_params,
            allow_external_links=allow_external_links,
            ignore_cache=ignore_cache,
            headers=headers,
        ):
            if (event.type == "complete" and event.result is not None) or (
                event.type == "error" and event.result is not None
            ):
                result = event.result
        return result

    async def _fetch_sitemap(self, base_url: str, headers: dict[str, str] | None = None) -> list[str]:
        """Fetch and parse sitemap.xml.

        Args:
            base_url: Base URL of the site
            headers: Custom HTTP headers; only KEYS are logged.

        Returns:
            List of URLs from sitemap
        """
        urls: list[str] = []
        sitemap_candidates = [
            urljoin(base_url, "/sitemap.xml"),
            urljoin(base_url, "/sitemap_index.xml"),
        ]

        # Pass custom headers to the sitemap HTTP client so auth'd sitemaps
        # can be fetched.  Only header KEYS are logged; values are not.
        if headers:
            LOGGER.debug("Sitemap client using %d custom header(s): %s", len(headers), list(headers.keys()))
        # follow_redirects=False: redirects are followed by hand inside
        # guarded_request so every hop is validated and pinned (#152).
        async with httpx.AsyncClient(
            timeout=10.0,
            follow_redirects=False,
            headers=headers or {},
        ) as client:
            for sitemap_url in sitemap_candidates:
                try:
                    LOGGER.debug(f"Trying sitemap: {sitemap_url}")
                    resp = await guarded_request(client, "GET", sitemap_url)
                    if resp.status_code == 200:
                        urls.extend(await self._parse_sitemap_xml(client, resp.text))
                except Exception as e:
                    LOGGER.debug(f"Sitemap fetch failed for {sitemap_url}: {e}")

        return list(set(urls))  # Deduplicate

    async def _parse_sitemap_xml(self, client: httpx.AsyncClient, xml_content: str) -> list[str]:
        """Parse sitemap XML and extract URLs.

        Args:
            client: HTTP client for fetching nested sitemaps
            xml_content: XML content to parse

        Returns:
            List of URLs found
        """
        urls: list[str] = []
        try:
            soup = BeautifulSoup(xml_content, "xml")

            # Handle sitemap index (nested sitemaps)
            for sitemap_tag in soup.find_all("sitemap"):
                loc = sitemap_tag.find("loc")
                if loc and loc.text:
                    try:
                        LOGGER.debug(f"Fetching nested sitemap: {loc.text}")
                        # loc.text is attacker-influenceable (it comes from XML the
                        # target site served us), which is exactly why this must go
                        # through the same guard as the original request (#152).
                        resp = await guarded_request(client, "GET", loc.text.strip())
                        if resp.status_code == 200:
                            nested_urls = await self._parse_sitemap_xml(client, resp.text)
                            urls.extend(nested_urls)
                    except Exception as e:
                        LOGGER.debug(f"Failed to fetch nested sitemap {loc.text}: {e}")

            # Handle regular sitemap (URL entries)
            for url_tag in soup.find_all("url"):
                loc = url_tag.find("loc")
                if loc and loc.text:
                    urls.append(loc.text.strip())

        except Exception as e:
            LOGGER.debug(f"Failed to parse sitemap XML: {e}")

        return urls

    async def _bfs_crawl_streaming(
        self,
        start_url: str,
        domain: str,
        max_depth: int,
        limit: int,
        include_subdomains: bool,
        allow_external_links: bool = False,
        headers: dict[str, str] | None = None,
    ) -> AsyncGenerator[MapEvent]:
        """BFS crawl to discover URLs, yielding progress events.

        Uses parallel processing with semaphore to crawl multiple URLs concurrently.

        Args:
            start_url: Starting URL
            domain: Base domain to stay within
            max_depth: Maximum depth
            limit: Maximum URLs to discover
            include_subdomains: Include subdomains
            allow_external_links: Allow URLs from external domains
            headers: Custom HTTP headers; only KEYS are logged.

        Yields:
            MapEvent with discovery progress
        """
        visited: set[str] = set()
        queue: deque[tuple[str, int]] = deque([(start_url, 0)])
        discovered_count = 0

        # Track domains visited for logging
        domains_visited: set[str] = set()

        # Create or use existing browser
        browser = self._browser
        close_browser = False
        if browser is None:
            browser = BrowserManager(
                headless=self._headless, stealth=self._stealth, proxy=self._proxy, engine=self._engine
            )
            close_browser = True

        # Semaphore for concurrent link extraction
        semaphore = asyncio.Semaphore(self._concurrency)

        async def extract_links_with_limit(
            url: str,
        ) -> tuple[str, list[str]]:
            """Extract links from a URL with concurrency limit."""
            async with semaphore:
                try:
                    links = await browser.extract_links(
                        url,
                        wait_until=self._wait_until,
                        extra_headers=headers,
                    )
                    return (url, links)
                except Exception as e:
                    LOGGER.warning(f"Failed to extract links from {url}: {e}")
                    return (url, [])

        try:
            # Ensure browser is started
            if browser._browser is None:
                await browser.__aenter__()

            while queue and discovered_count < limit:
                # Collect a batch of URLs to process concurrently
                batch: list[tuple[str, int]] = []
                urls_to_extract: list[tuple[str, int]] = []

                while queue and len(batch) < self._concurrency and discovered_count + len(batch) < limit:
                    url, depth = queue.popleft()

                    # Skip if already visited
                    if url in visited:
                        continue
                    visited.add(url)

                    # Check domain boundaries (unless external links allowed)
                    if not allow_external_links and not self._is_same_domain(url, domain, include_subdomains):
                        continue

                    # Track domain for logging
                    url_domain = urlparse(url).netloc
                    if url_domain not in domains_visited:
                        domains_visited.add(url_domain)
                        if len(domains_visited) > 1:
                            LOGGER.info(f"Crawling external domain: {url_domain}")

                    batch.append((url, depth))
                    # Queue for link extraction if not at max depth
                    if depth < max_depth:
                        urls_to_extract.append((url, depth))

                # If no valid URLs in batch, continue to next iteration
                if not batch:
                    continue

                # Yield discovered URLs
                for url, depth in batch:
                    discovered_count += 1
                    LOGGER.debug(f"Discovered [{discovered_count}/{limit}] depth={depth}: {url}")
                    yield MapEvent(
                        type="discovery",
                        discovered=discovered_count,
                        total=limit,
                        message=url,
                    )

                # Extract links from all URLs in batch concurrently
                if urls_to_extract:
                    extraction_tasks = [extract_links_with_limit(url) for url, _ in urls_to_extract]
                    results = await asyncio.gather(*extraction_tasks)

                    # Process extracted links
                    for (_url, depth), (_, links) in zip(urls_to_extract, results, strict=True):
                        for link in links:
                            # Normalize URL (remove fragments)
                            normalized = link.split("#")[0]
                            if normalized and normalized not in visited:
                                queue.append((normalized, depth + 1))

            # Final discovery complete event
            yield MapEvent(
                type="discovery",
                discovered=discovered_count,
                total=limit,
                message=f"URL discovery complete: found {discovered_count} URLs",
            )

        finally:
            if close_browser and browser._browser is not None:
                await browser.__aexit__(None, None, None)

    async def _extract_metadata(self, url: str, headers: dict[str, str] | None = None) -> tuple[str | None, str | None]:
        """Extract title and description from a URL.

        Args:
            url: URL to extract metadata from
            headers: Custom HTTP headers; only KEYS are logged.

        Returns:
            Tuple of (title, description)
        """
        # Create or use existing browser
        browser = self._browser
        close_browser = False
        if browser is None:
            browser = BrowserManager(
                headless=self._headless, stealth=self._stealth, proxy=self._proxy, engine=self._engine
            )
            close_browser = True

        try:
            # Ensure browser is started
            if browser._browser is None:
                await browser.__aenter__()

            # Fetch page content
            content = await browser.fetch_page(
                url,
                wait_for_spa=False,
                wait_until=self._wait_until,
                extra_headers=headers,
            )

            # Extract metadata
            metadata = await browser.extract_metadata(content.html)

            # Prefer og:title/og:description, fall back to regular tags
            title = metadata.og_title or metadata.title
            description = metadata.og_description or metadata.description

            return (title, description)

        except Exception as e:
            LOGGER.warning(f"Failed to extract metadata from {url}: {e}")
            return (None, None)

        finally:
            if close_browser and browser._browser is not None:
                await browser.__aexit__(None, None, None)

    def _is_same_domain(
        self,
        url: str,
        base_domain: str,
        include_subdomains: bool,
    ) -> bool:
        """Check if URL is within the allowed domain.

        Args:
            url: URL to check
            base_domain: Base domain
            include_subdomains: Include subdomains

        Returns:
            True if URL is within domain
        """
        try:
            parsed = urlparse(url)
            url_domain = parsed.netloc.lower()
            base = base_domain.lower()

            if include_subdomains:
                # Allow exact match or subdomain
                return url_domain == base or url_domain.endswith(f".{base}")
            else:
                # Exact match only
                return url_domain == base

        except Exception:
            return False
