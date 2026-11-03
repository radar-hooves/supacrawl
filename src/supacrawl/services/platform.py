"""Platform detection for auto-tuning scrape settings.

Detects known interactive content platforms (e.g. Foleon) from raw HTML
and returns optimal scrape settings. Used by ScrapeService to auto-retry
with better settings when default scraping yields thin content.

Platform Profile Registry
=========================
Some platforms produce heavily JavaScript-rendered or iframe-based content
that requires specific scrape settings to extract successfully. We maintain
a registry of platform profiles that auto-apply optimal settings.

To add a new platform profile:
1. Create a detection function: _detect_<name>(html) -> bool
2. Define a PlatformProfile with optimal settings
3. Register in PLATFORM_PROFILES with documentation

See PLATFORM_PROFILES below for the current registry.
"""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlatformProfile:
    """Registration for a platform-specific scrape profile.

    Attributes:
        name: Short identifier (e.g., "foleon")
        description: What this platform is and why it needs special settings
        examples: Example sites using this platform
        detect: Function to check if this platform is present in raw HTML
        engine: Browser engine override (playwright/patchright/camoufox)
        expand_iframes: Iframe expansion mode override
        wait_for: Additional wait time in ms after page load
        only_main_content: Whether to extract main content only
        actions: Pre-scrape actions (scroll, wait sequences)
    """

    name: str
    description: str
    examples: list[str]
    detect: Callable[[str], bool]
    engine: str | None = None
    expand_iframes: Literal["none", "same-origin", "all"] | None = None
    wait_for: int | None = None
    only_main_content: bool | None = None
    actions: list[dict[str, Any]] = field(default_factory=list)


# Platform Detection Functions

_FOLEON_RE = re.compile(
    r"assets\.foleon\.com|foleon-content|data-foleon|class=\"fl-",
    re.IGNORECASE,
)


def _detect_foleon(html: str) -> bool:
    """Detect Foleon interactive magazine/microsite platform.

    Checks for Foleon-specific markers: asset CDN references, data attributes,
    and CSS class prefixes used by the Foleon rendering engine.
    """
    return bool(_FOLEON_RE.search(html))


# Platform Profile Registry

PLATFORM_PROFILES: list[PlatformProfile] = [
    PlatformProfile(
        name="foleon",
        description=(
            "Foleon interactive magazine/microsite platform. Content renders "
            "inside iframes with heavy JavaScript; requires camoufox engine, "
            "full iframe expansion, extended wait, and scroll actions."
        ),
        examples=[
            "solutions.kbr.com/game",
            "Foleon-hosted enterprise product pages",
        ],
        detect=_detect_foleon,
        engine="camoufox",
        expand_iframes="all",
        wait_for=8000,
        only_main_content=False,
        actions=[
            {"type": "scroll", "direction": "down", "amount": 3},
            {"type": "wait", "milliseconds": 2000},
            {"type": "scroll", "direction": "down", "amount": 5},
            {"type": "wait", "milliseconds": 2000},
        ],
    ),
    # Add new platform profiles here following the same pattern:
    # PlatformProfile(
    #     name="example_platform",
    #     description="Example platform. Requires: ...",
    #     examples=["example.com"],
    #     detect=_detect_example,
    #     engine="camoufox",
    # ),
]


def detect_platform(html: str) -> PlatformProfile | None:
    """Detect known interactive content platforms from raw HTML.

    Iterates through registered platform profiles, returning the first match.

    Args:
        html: Raw HTML string from the initial page fetch.

    Returns:
        PlatformProfile with optimal settings, or None if unrecognised.
    """
    for profile in PLATFORM_PROFILES:
        try:
            if profile.detect(html):
                LOGGER.info("Detected %s platform; will apply tuned settings", profile.name)
                return profile
        except Exception as e:
            LOGGER.warning("Platform detection for %s failed: %s", profile.name, e)
    return None
