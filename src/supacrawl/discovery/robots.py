"""robots.txt parsing and compliance utilities.

This module provides functionality to fetch, parse, and enforce
robots.txt rules during web crawling.
"""

import contextlib
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from supacrawl.services.url_guard import guarded_request

LOGGER = logging.getLogger(__name__)


@dataclass
class RobotsConfig:
    """
    Parsed robots.txt configuration.

    Attributes:
        user_agent: User agent these rules apply to.
        crawl_delay: Delay between requests in seconds (optional).
        sitemaps: List of sitemap URLs from Sitemap directives.
        disallow_patterns: URL patterns that are disallowed.
        allow_patterns: URL patterns that are explicitly allowed.
        request_rate: Requests per second (optional).
    """

    user_agent: str = "*"
    crawl_delay: float | None = None
    sitemaps: list[str] = field(default_factory=list)
    disallow_patterns: list[str] = field(default_factory=list)
    allow_patterns: list[str] = field(default_factory=list)
    request_rate: float | None = None


@dataclass
class RobotsEnforcement:
    """
    Configuration for robots.txt enforcement.

    Attributes:
        respect: Whether to respect robots.txt at all.
        enforcement: Level of enforcement (strict, warn, ignore).
        user_agent: User agent for robots.txt matching.
        min_delay: Minimum delay between requests.
    """

    respect: bool = True
    enforcement: str = "warn"  # strict, warn, ignore
    user_agent: str = "SupacrawlBot/1.0"
    min_delay: float = 0.0


async def fetch_robots(base_url: str, timeout: float = 30.0) -> RobotsConfig:
    """
    Fetch and parse robots.txt for a domain.

    Args:
        base_url: Base URL of the site (e.g., "https://example.com").
        timeout: HTTP timeout in seconds.

    Returns:
        RobotsConfig with parsed rules.
    """
    parsed = urlsplit(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    robots_url = f"{origin}/robots.txt"

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await guarded_request(client, "GET", robots_url)

            if response.status_code == 404:
                # 404 means no robots.txt - allow all
                LOGGER.debug("No robots.txt found at %s (404)", robots_url)
                return RobotsConfig()

            if response.status_code >= 500:
                # Server error - assume allow all but log warning
                LOGGER.warning(
                    "robots.txt returned %d at %s, assuming allow all",
                    response.status_code,
                    robots_url,
                )
                return RobotsConfig()

            if response.status_code != 200:
                LOGGER.warning(
                    "robots.txt returned unexpected status %d at %s",
                    response.status_code,
                    robots_url,
                )
                return RobotsConfig()

            return parse_robots_txt(response.text)

    except Exception as e:
        LOGGER.warning("Failed to fetch robots.txt from %s: %s", robots_url, e)
        return RobotsConfig()


def parse_robots_txt(content: str, user_agent: str = "*") -> RobotsConfig:
    """
    Parse robots.txt content.

    Args:
        content: Raw robots.txt content.
        user_agent: User agent to match rules for.

    Returns:
        RobotsConfig with parsed rules.
    """
    config = RobotsConfig(user_agent=user_agent)

    # Track which user-agent section we're in
    current_ua: str | None = None
    ua_matched = False

    for line in content.splitlines():
        line = line.strip()

        # Skip comments and empty lines
        if not line or line.startswith("#"):
            continue

        # Remove inline comments
        if "#" in line:
            line = line.split("#", 1)[0].strip()

        if not line:
            continue

        # Parse directive
        if ":" not in line:
            continue

        directive, value = line.split(":", 1)
        directive = directive.strip().lower()
        value = value.strip()

        if directive == "user-agent":
            current_ua = value.lower()
            # Check if this section matches our user agent exactly
            if current_ua == user_agent.lower():
                ua_matched = True

        elif directive == "sitemap":
            # Sitemap directives are global
            if value and value not in config.sitemaps:
                config.sitemaps.append(value)

        elif current_ua is not None:
            # Only process if we're in a matching user-agent section
            is_matching = (current_ua == user_agent.lower()) or (current_ua == "*" and not ua_matched)

            if not is_matching:
                continue

            if directive == "disallow" and value:
                config.disallow_patterns.append(value)

            elif directive == "allow" and value:
                config.allow_patterns.append(value)

            elif directive == "crawl-delay":
                with contextlib.suppress(ValueError):
                    config.crawl_delay = float(value)

            # Format: requests/seconds (e.g., "1/10" means 1 request per 10 seconds)
            elif directive == "request-rate" and "/" in value:
                with contextlib.suppress(ValueError):
                    requests, seconds = value.split("/")
                    config.request_rate = float(requests) / float(seconds)

    return config


def is_url_allowed(
    url: str,
    robots: RobotsConfig,
) -> bool:
    """
    Check if URL is allowed by robots.txt rules.

    Args:
        url: URL to check.
        robots: Parsed robots configuration.

    Returns:
        True if URL is allowed, False if disallowed.
    """
    parsed = urlsplit(url)
    path = parsed.path or "/"

    # RFC 9309: the most specific (longest) matching rule wins; on a tie the
    # Allow rule takes precedence. Checking Allow first and short-circuiting
    # would let a broad ``Allow: /`` override a narrower ``Disallow: /admin/``.
    longest_allow = max(
        (len(pattern) for pattern in robots.allow_patterns if _matches_pattern(path, pattern)),
        default=-1,
    )
    longest_disallow = max(
        (len(pattern) for pattern in robots.disallow_patterns if _matches_pattern(path, pattern)),
        default=-1,
    )

    if longest_disallow == -1:
        # No disallow rule matches → allowed (whether or not an allow matched).
        return True

    # A disallow matches; allow only if an equally- or more-specific allow does too.
    return longest_allow >= longest_disallow


def _matches_pattern(path: str, pattern: str) -> bool:
    """
    Check if path matches a robots.txt pattern.

    Handles:
    - Exact prefix matching
    - * wildcard (matches any sequence)
    - $ end anchor

    Args:
        path: URL path to check.
        pattern: robots.txt pattern.

    Returns:
        True if pattern matches.
    """
    if not pattern:
        return False

    # Handle end anchor
    has_end_anchor = pattern.endswith("$")
    if has_end_anchor:
        pattern = pattern[:-1]

    # Handle wildcards by converting to regex
    if "*" in pattern:
        # Escape regex special chars except *
        regex_pattern = re.escape(pattern).replace(r"\*", ".*")
        if has_end_anchor:
            regex_pattern += "$"
        else:
            regex_pattern = f"^{regex_pattern}"
        try:
            return bool(re.match(regex_pattern, path))
        except re.error:
            return False

    # Simple prefix matching
    if has_end_anchor:
        return path == pattern
    return path.startswith(pattern)


def filter_urls_by_robots(
    urls: list[str],
    robots: RobotsConfig,
    log_skipped: bool = True,
) -> tuple[list[str], list[str]]:
    """
    Filter URLs based on robots.txt rules.

    Args:
        urls: List of URLs to filter.
        robots: Parsed robots configuration.
        log_skipped: Whether to log skipped URLs.

    Returns:
        Tuple of (allowed_urls, disallowed_urls).
    """
    allowed: list[str] = []
    disallowed: list[str] = []

    for url in urls:
        if is_url_allowed(url, robots):
            allowed.append(url)
        else:
            disallowed.append(url)
            if log_skipped:
                LOGGER.info("Skipping URL (robots.txt): %s", url)

    return allowed, disallowed
