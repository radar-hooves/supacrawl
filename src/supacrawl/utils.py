"""Utility functions for supacrawl."""

import hashlib
import logging
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from supacrawl.exceptions import generate_correlation_id

LOGGER = logging.getLogger(__name__)


def log_with_correlation(
    logger: logging.Logger,
    level: int,
    message: str,
    correlation_id: str | None = None,
    **kwargs: Any,
) -> None:
    """
    Log a message with correlation ID and additional context.

    Args:
        logger: Logger instance to use.
        level: Logging level (e.g., logging.INFO, logging.ERROR).
        message: Log message format string.
        correlation_id: Optional correlation ID. If None, generates a new one.
        **kwargs: Additional context to include in log extra fields.
    """
    corr_id = correlation_id or generate_correlation_id()
    extra = {"correlation_id": corr_id, **kwargs}
    logger.log(level, message, extra=extra)


def content_hash(text: str, url: str | None = None) -> str:
    """
    Return a deterministic SHA-256 hash for page content.

    Args:
        text: Page content to hash.
        url: Optional URL to include for extra stability.

    Returns:
        Hexadecimal hash string.
    """
    basis = (url or "") + "||" + text
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


# URL normalisation utilities


def normalise_url(raw_url: str, html: str | None = None, entrypoint: str | None = None) -> str:
    """
    Normalise URLs for deduping and hashing.

    - Prefer canonical link tags when available.
    - Strip fragments and common tracking parameters (utm_*, fbclid, etc).

    Args:
        raw_url: The URL to normalise.
        html: Optional HTML content to extract canonical URL from.
        entrypoint: Optional entrypoint URL to resolve relative canonicals.

    Returns:
        Normalised URL string.
    """
    cleaned = strip_tracking_params(_strip_fragment(raw_url))
    canonical = extract_canonical_url(html, cleaned, entrypoint=entrypoint)
    return strip_tracking_params(_strip_fragment(canonical))


def _strip_fragment(raw_url: str) -> str:
    """Remove URL fragment (e.g., #section)."""
    parts = urlsplit(raw_url)
    return urlunsplit(parts._replace(fragment=""))


# Tracking parameters to remove during URL normalisation
TRACKING_PARAMS = frozenset(
    {
        # Google Analytics / UTM
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "_ga",
        "_gl",
        # Facebook
        "fbclid",
        # Google Ads
        "gclid",
        # Microsoft / Bing Ads
        "msclkid",
        # Instagram
        "igshid",
        # Mailchimp
        "mc_cid",
        "mc_eid",
        # Generic referral/source
        "ref",
        "ref_src",
        "source",
        "share",
    }
)


def strip_tracking_params(raw_url: str) -> str:
    """
    Remove common tracking parameters from URL.

    Removes UTM parameters, ad platform click IDs, and generic referral params.
    See TRACKING_PARAMS for the full list.

    Args:
        raw_url: URL to clean.

    Returns:
        URL with tracking parameters removed.
    """
    parsed = urlsplit(raw_url)
    if not parsed.query:
        return raw_url

    params = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        key_lower = key.lower()
        # Check for utm_* prefix or exact match in tracking params
        if key_lower.startswith("utm_") or key_lower in TRACKING_PARAMS:
            continue
        params.append((key, value))

    cleaned_query = urlencode(params, doseq=True)
    return urlunsplit(parsed._replace(query=cleaned_query))


def normalise_url_for_dedupe(url: str) -> str:
    """
    Normalise URL for deduplication comparison.

    Performs more aggressive normalisation than normalise_url():
    - Removes fragments
    - Removes tracking parameters
    - Sorts query parameters for consistent comparison

    Args:
        url: URL to normalise.

    Returns:
        Normalised URL suitable for deduplication comparison.
    """
    parsed = urlsplit(url)

    # Remove fragment
    parsed = parsed._replace(fragment="")

    # Filter and sort query params
    if parsed.query:
        params = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            key_lower = key.lower()
            if key_lower.startswith("utm_") or key_lower in TRACKING_PARAMS:
                continue
            params.append((key, value))
        # Sort params for consistent comparison
        params.sort(key=lambda x: (x[0], x[1]))
        cleaned_query = urlencode(params, doseq=True)
        parsed = parsed._replace(query=cleaned_query)

    return urlunsplit(parsed)


def extract_canonical_url(html: str | None, fallback_url: str, entrypoint: str | None = None) -> str:
    """
    Extract canonical URL from HTML when available.

    Args:
        html: HTML content to parse.
        fallback_url: URL to return if no canonical found.
        entrypoint: Optional base URL for resolving relative canonicals.

    Returns:
        Canonical URL or fallback.
    """
    if not html:
        return fallback_url

    try:
        soup = BeautifulSoup(html, "html.parser")
        link = soup.find("link", attrs={"rel": re.compile("canonical", re.I)})
        if not link or not link.has_attr("href"):
            return fallback_url
        href_attr = link["href"]
        # href can be a list in some cases, take first item
        href: str | None = None
        if isinstance(href_attr, list):
            href = href_attr[0] if href_attr else None
        elif isinstance(href_attr, str):
            href = href_attr
        if not href:
            return fallback_url
        canonical = urljoin(entrypoint or fallback_url, href)
        return canonical
    except Exception:
        return fallback_url
