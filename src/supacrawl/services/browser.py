"""Browser manager for Playwright-based page fetching.

ANTI-BOT PROTECTION (three tiers):

    Tier 1 - Basic stealth scripts (always active, no configuration):
        - navigator.webdriver = false (hides automation)
        - Chrome runtime objects (window.chrome.runtime, etc.)
        - Non-empty plugins array (avoids empty plugins fingerprint)
        - Standard languages array (['en-US', 'en'])
        - WebGL vendor/renderer spoofing (Intel Inc. / Intel Iris OpenGL Engine)
        - Canvas fingerprint noise (unique per session, non-destructive)
        - Standard browser headers (Accept-Language, Sec-Fetch-*, etc.)

    Tier 2 - Patchright (optional, for heavily protected sites):
        Install: pip install supacrawl[stealth]
        Usage: BrowserManager(stealth=True) or --stealth flag or --engine patchright
        Provides: Patched Chromium with enhanced anti-detection

    Tier 3 - Camoufox (optional, for Akamai and advanced bot detection):
        Install: pip install supacrawl[camoufox]
        Usage: BrowserManager(engine="camoufox") or --engine camoufox
        Provides: Patched Firefox with C++ engine-level anti-detection
        Effective against: Akamai Bot Manager, advanced TLS fingerprinting

CAPTCHA SOLVING (optional, requires third-party service):
    Install: pip install supacrawl[captcha]
    Configure: export CAPTCHA_API_KEY=your-2captcha-api-key
    Usage: ScrapeService(solve_captcha=True) or --solve-captcha flag
    Supports: reCAPTCHA v2/v3, hCaptcha, Cloudflare Turnstile
    WARNING: Each solve costs ~$0.002-0.003

OTHER FEATURES:
    - Proxy support: --proxy http://user:pass@host:port or socks5://host:port
    - Locale/timezone: Automatic or via LocaleConfig
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from supacrawl.exceptions import ValidationError
from supacrawl.services.url_guard import block_page_scripts, guarded_hop, resolve_and_pin, strict_mode

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Page

LOGGER = logging.getLogger(__name__)


_DROP_REQUEST_HEADERS = frozenset({"host", "content-length", "connection", "accept-encoding"})
_DROP_RESPONSE_HEADERS = frozenset({"content-encoding", "content-length", "transfer-encoding", "connection"})


async def _fulfil_pinned(route: Any, request: Any) -> None:
    """Serve a request from a pinned httpx hop so the browser never connects itself.

    Strict mode only: DNS cannot rebind between check and connect when the
    connect is ours. Redirects go back to the browser as 3xx and are routed
    (and pinned) again.
    """
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _DROP_REQUEST_HEADERS}
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            response = await guarded_hop(
                client, request.method, request.url, headers=headers, content=request.post_data_buffer
            )
    except ValidationError as exc:
        LOGGER.warning("Blocked browser request to %s: %s", request.url, exc)
        await route.abort("blockedbyclient")
        return
    except httpx.HTTPError as exc:
        LOGGER.warning("Strict fetch failed for %s: %s", request.url, exc)
        await route.abort("failed")
        return
    await route.fulfill(
        status=response.status_code,
        headers={k: v for k, v in response.headers.items() if k.lower() not in _DROP_RESPONSE_HEADERS},
        body=response.content,
    )


async def _install_navigation_guard(page: Any) -> None:
    """Refuse browser navigations and subresource loads to blocked addresses (#152).

    The browser path cannot be pinned the way the httpx path is: Chromium owns
    its own resolver and socket, so there is no seam to hand it a validated
    address. The residual exposure is therefore a genuine time-of-check gap —
    a name validated here can answer differently when Chromium resolves it.

    What IS closed is the redirect and subresource hole: a route handler sees
    every request the page makes, including each redirect hop, and aborts any
    whose host is a blocked IP literal or resolves into a blocked range. That
    turns "unchecked after the first URL" into "checked every hop, unpinned".
    """

    async def _route(route: Any, request: Any) -> None:
        if strict_mode():
            await _fulfil_pinned(route, request)
            return
        try:
            # Off the event loop: resolve_and_pin does a blocking getaddrinfo,
            # and this handler fires once per request/redirect/subresource —
            # a slow or black-holed resolver on a hostile page must not stall
            # the whole event loop (and every other concurrent scrape in the
            # process) for the lookup's duration.
            await asyncio.to_thread(resolve_and_pin, request.url)
        except ValidationError as exc:
            LOGGER.warning("Blocked browser request to %s: %s", request.url, exc)
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    await page.route("**/*", _route)


# Basic stealth scripts for non-stealth mode (subset of puppeteer-extra-plugin-stealth)
STEALTH_SCRIPTS = [
    # navigator.webdriver = false
    """
    Object.defineProperty(navigator, 'webdriver', {
        get: () => false,
    });
    """,
    # Chrome runtime objects
    """
    window.chrome = {
        runtime: {},
        loadTimes: function() {},
        csi: function() {},
        app: {}
    };
    """,
    # Plugins array (non-empty)
    """
    Object.defineProperty(navigator, 'plugins', {
        get: () => [1, 2, 3, 4, 5],
    });
    """,
    # Languages array
    """
    Object.defineProperty(navigator, 'languages', {
        get: () => ['en-US', 'en'],
    });
    """,
    # WebGL vendor/renderer spoofing (prevents GPU fingerprinting)
    # Safely checks if WebGL exists before patching
    """
    (function() {
        // Only patch if WebGL is available
        if (typeof WebGLRenderingContext === 'undefined') return;

        const getParameterOriginal = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(parameter) {
            // UNMASKED_VENDOR_WEBGL
            if (parameter === 37445) {
                return 'Intel Inc.';
            }
            // UNMASKED_RENDERER_WEBGL
            if (parameter === 37446) {
                return 'Intel Iris OpenGL Engine';
            }
            return getParameterOriginal.call(this, parameter);
        };

        // Also patch WebGL2 if available
        if (typeof WebGL2RenderingContext !== 'undefined') {
            const getParameter2Original = WebGL2RenderingContext.prototype.getParameter;
            WebGL2RenderingContext.prototype.getParameter = function(parameter) {
                if (parameter === 37445) {
                    return 'Intel Inc.';
                }
                if (parameter === 37446) {
                    return 'Intel Iris OpenGL Engine';
                }
                return getParameter2Original.call(this, parameter);
            };
        }
    })();
    """,
    # Canvas fingerprint noise - NON-DESTRUCTIVE approach
    # Only affects the OUTPUT of toDataURL/toBlob, not the actual canvas content
    # This ensures screenshots and visual rendering are unaffected
    """
    (function() {
        // Session-stable noise seed (consistent within page, different per session)
        const noiseSeed = Math.floor(Math.random() * 1000);

        // Simple seeded random for consistent noise
        function seededRandom(seed) {
            const x = Math.sin(seed) * 10000;
            return x - Math.floor(x);
        }

        // Add subtle noise to a COPY of image data (non-destructive)
        function addNoiseToData(data, seed) {
            for (let i = 0; i < data.length; i += 4) {
                // Modify RGB channels, not alpha
                for (let c = 0; c < 3; c++) {
                    const noise = Math.floor(seededRandom(seed + i + c) * 3) - 1;
                    data[i + c] = Math.max(0, Math.min(255, data[i + c] + noise));
                }
            }
        }

        // Patch toDataURL - creates noisy copy without modifying original canvas
        const originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
        HTMLCanvasElement.prototype.toDataURL = function(type, quality) {
            // Only add noise for fingerprinting-relevant types
            if (!type || type === 'image/png' || type === 'image/jpeg') {
                try {
                    const context = this.getContext('2d');
                    if (context && this.width > 0 && this.height > 0) {
                        // Create a temporary canvas to avoid modifying original
                        const tempCanvas = document.createElement('canvas');
                        tempCanvas.width = this.width;
                        tempCanvas.height = this.height;
                        const tempContext = tempCanvas.getContext('2d');

                        // Copy original content
                        tempContext.drawImage(this, 0, 0);

                        // Get image data and add noise
                        const imageData = tempContext.getImageData(0, 0, this.width, this.height);
                        addNoiseToData(imageData.data, noiseSeed);
                        tempContext.putImageData(imageData, 0, 0);

                        // Return noisy version
                        return originalToDataURL.call(tempCanvas, type, quality);
                    }
                } catch (e) {
                    // Fall through to original on any error (e.g., WebGL canvas, tainted canvas)
                }
            }
            return originalToDataURL.call(this, type, quality);
        };

        // Patch toBlob - creates noisy copy without modifying original canvas
        const originalToBlob = HTMLCanvasElement.prototype.toBlob;
        HTMLCanvasElement.prototype.toBlob = function(callback, type, quality) {
            if (!type || type === 'image/png' || type === 'image/jpeg') {
                try {
                    const context = this.getContext('2d');
                    if (context && this.width > 0 && this.height > 0) {
                        // Create a temporary canvas
                        const tempCanvas = document.createElement('canvas');
                        tempCanvas.width = this.width;
                        tempCanvas.height = this.height;
                        const tempContext = tempCanvas.getContext('2d');

                        // Copy and add noise
                        tempContext.drawImage(this, 0, 0);
                        const imageData = tempContext.getImageData(0, 0, this.width, this.height);
                        addNoiseToData(imageData.data, noiseSeed);
                        tempContext.putImageData(imageData, 0, 0);

                        // Return noisy version
                        return originalToBlob.call(tempCanvas, callback, type, quality);
                    }
                } catch (e) {
                    // Fall through to original
                }
            }
            return originalToBlob.call(this, callback, type, quality);
        };

        // NOTE: We intentionally do NOT patch getImageData
        // Patching getImageData breaks legitimate canvas operations (games, image editors)
        // Fingerprinting typically uses toDataURL, so this is sufficient protection
    })();
    """,
]


def _parse_proxy_url(proxy_url: str) -> dict[str, Any]:
    """Parse a proxy URL into Playwright proxy config.

    Supports formats:
        - http://host:port
        - http://user:pass@host:port
        - socks5://host:port
        - socks5://user:pass@host:port

    Args:
        proxy_url: Proxy URL string

    Returns:
        Dictionary for Playwright proxy config

    Raises:
        ValueError: If proxy URL format is invalid
    """
    # Handle socks5 scheme (Playwright expects socks5://)
    parsed = urlparse(proxy_url)

    if parsed.scheme not in ("http", "https", "socks5"):
        raise ValueError(f"Invalid proxy scheme '{parsed.scheme}'. Supported: http, https, socks5")

    # Build proxy config
    config: dict[str, Any] = {
        "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port or 80}",
    }

    if parsed.username:
        config["username"] = parsed.username
    if parsed.password:
        config["password"] = parsed.password

    return config


class StealthNotAvailableError(ImportError):
    """Raised when stealth mode is requested but patchright is not installed."""

    def __init__(self) -> None:
        super().__init__("Stealth mode requires patchright. Install with: pip install supacrawl[stealth]")


class CamoufoxNotAvailableError(ImportError):
    """Raised when Camoufox engine is requested but camoufox is not installed."""

    def __init__(self) -> None:
        super().__init__("Camoufox engine requires camoufox. Install with: pip install supacrawl[camoufox]")


class BrowserUnavailableError(RuntimeError):
    """Raised when supacrawl's own browser engine is unusable.

    Distinct from every site-shaped failure: it means the fetch never left the
    building. A long-lived server relaunches a dead engine in-process, so this
    escapes only when the relaunch itself failed or is in crash-loop backoff —
    i.e. the environment is broken, and no amount of retrying the URL, changing
    engines, or waiting longer will help.
    """


# Substrings identifying a Playwright error that means the browser PROCESS is
# gone (as opposed to the page or the site misbehaving). Matching one of these is
# necessary but never sufficient: the same wording appears when a single page is
# closed under a perfectly healthy browser, so every use is paired with a live
# ``is_connected()`` check before anything is relaunched.
_CLOSED_BROWSER_SIGNATURES = (
    "target page, context or browser has been closed",
    "browser has been closed",
    "browser closed",
    "target closed",
    "connection closed",
    "playwright has been closed",
    "browser has disconnected",
)

# Crash-loop guard for in-process relaunch. Consecutive FAILED relaunches back off
# exponentially from this base; a successful relaunch resets the counter, so a
# sidecar that legitimately loses its browser once a week is never throttled while
# a genuinely broken environment (no browser binaries, no memory) is.
_RELAUNCH_BACKOFF_BASE_S = 5.0
_RELAUNCH_BACKOFF_MAX_S = 300.0
# Enough doublings to reach the ceiling many times over; caps the exponent so a
# long-running failure streak cannot overflow the shift itself.
_RELAUNCH_BACKOFF_MAX_DOUBLINGS = 16


def is_closed_browser_error(error: BaseException) -> bool:
    """Whether an exception's text says the underlying browser process is gone.

    Args:
        error: The raised exception.

    Returns:
        True when the message matches a known closed-browser signature. Callers
        must confirm with :meth:`BrowserManager.is_alive` before treating it as a
        dead engine — a closed *page* produces the same wording.
    """
    text = str(error).lower()
    return any(signature in text for signature in _CLOSED_BROWSER_SIGNATURES)


# Valid engine choices
ENGINE_CHOICES = ("playwright", "patchright", "camoufox")


# --- Per-engine availability, without launching a browser (#144) ---------------
# Report which engines are usable so an operator or agent can see which stealth
# tiers are available and understand why a tool failed — cheaply and safely,
# with no browser process started. The companion CI smoke-test (#145) actually
# launches each engine, so an "available" verdict here is independently proven
# launchable: this check must never be a confident liar (the supacrawl#158 class,
# where a component reported ready while it was not).

# Reason categories reported when an engine is unusable. Deliberately coarse and
# machine-readable — they say WHAT is missing, and never leak an internal path.
# Bound on a graceful browser close before the driver reaps it (see _close_timeout_s).
_DEFAULT_CLOSE_TIMEOUT_S = 3.0

ENGINE_REASON_NOT_INSTALLED = "not_installed"  # the Python package is absent
ENGINE_REASON_BROWSER_NOT_INSTALLED = "browser_not_installed"  # package present, browser binary not fetched

# The Python package that backs each engine.
_ENGINE_PACKAGE = {
    "playwright": "playwright",
    "patchright": "patchright",
    "camoufox": "camoufox",
}

# The command that fetches an engine's browser binary once its package is present.
# Split from the install hint because the two failures need different remedies: a
# missing package needs pip, a missing binary needs only the fetch — and telling
# someone to pip-install what they already have reads as the tool being broken.
_ENGINE_FETCH_HINT = {
    "playwright": "playwright install chromium",
    "patchright": "patchright install chromium",
    "camoufox": "camoufox fetch",
}

# An actionable install hint per engine, surfaced when its package is absent.
_ENGINE_INSTALL_HINT = {
    "playwright": "playwright is a base dependency; run: playwright install chromium",
    "patchright": "pip install supacrawl[stealth]; then: patchright install chromium",
    "camoufox": "pip install supacrawl[camoufox]; then: camoufox fetch",
}


def _package_installed(package: str) -> bool:
    """Whether an engine's Python package is importable, without importing it."""
    try:
        return importlib.util.find_spec(package) is not None
    except ImportError, ValueError:
        return False


def _playwright_browsers_dir() -> Path | None:
    """Resolve Playwright's browser-download directory without starting the driver.

    Mirrors Playwright's own default: ``PLAYWRIGHT_BROWSERS_PATH`` when set (the
    literal ``"0"`` means "bundled next to the package", which cannot be resolved
    cheaply, so we report it as unknown), otherwise the per-OS cache directory.
    Both playwright and patchright fetch Chromium into this same registry.
    """
    env = os.getenv("PLAYWRIGHT_BROWSERS_PATH")
    if env:
        if env == "0":
            return None  # bundled next to the package; not resolvable cheaply
        return Path(env)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    if sys.platform == "win32":
        local = os.getenv("LOCALAPPDATA")
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def _chromium_browser_installed() -> bool:
    """Whether a Chromium build has been fetched (playwright/patchright share it).

    Filesystem-only, no subprocess: looks for a non-empty ``chromium*`` build
    directory in Playwright's browsers dir. When the location cannot be resolved
    (``PLAYWRIGHT_BROWSERS_PATH=0``, a bundled install), returns True rather than
    a false negative — the package is present and the #145 launch smoke-test is
    the launch-truth backstop; the honest failure direction is never to claim
    "available" when it is not, and a false "missing" here would do that inverted.
    """
    browsers_dir = _playwright_browsers_dir()
    if browsers_dir is None:
        return True  # cannot resolve; do not assert "missing" on no evidence
    try:
        return any(child.is_dir() and any(child.iterdir()) for child in browsers_dir.glob("chromium*"))
    except OSError:
        return True


def _camoufox_browser_installed() -> bool:
    """Whether Camoufox's Firefox build has been fetched, without launching it."""
    try:
        from camoufox.pkgman import launch_path

        launch_path()  # raises CamoufoxNotInstalled when the binary is absent
        return True
    except Exception:
        return False


def engine_availability(engine: str) -> dict[str, Any]:
    """Report whether one engine is usable, WITHOUT launching a browser (#144).

    Returns a dict with ``installed`` (package present), ``available`` (package
    present AND its browser binary fetched), and ``reason`` (None when available,
    otherwise a coarse ``ENGINE_REASON_*`` category). No internal paths are
    leaked; an ``install`` hint is included when the engine is unavailable.
    """
    if engine not in ENGINE_CHOICES:
        raise ValueError(f"Invalid engine '{engine}'. Choose from: {', '.join(ENGINE_CHOICES)}")
    if not _package_installed(_ENGINE_PACKAGE[engine]):
        return {
            "installed": False,
            "available": False,
            "reason": ENGINE_REASON_NOT_INSTALLED,
            "install": _ENGINE_INSTALL_HINT[engine],
        }
    binary_present = _camoufox_browser_installed() if engine == "camoufox" else _chromium_browser_installed()
    if not binary_present:
        return {
            "installed": True,
            "available": False,
            "reason": ENGINE_REASON_BROWSER_NOT_INSTALLED,
            "install": _ENGINE_FETCH_HINT[engine],
        }
    return {"installed": True, "available": True, "reason": None}


def all_engine_availability() -> dict[str, dict[str, Any]]:
    """Per-engine availability for every stealth tier, no browser launched (#144)."""
    return {engine: engine_availability(engine) for engine in ENGINE_CHOICES}


# --- Container-aware Chromium launch args (#146) -------------------------------
# Docker's default /dev/shm is 64 MB; Chromium can exhaust it and crash on heavy
# real pages even when it launches cleanly on a trivial one. --disable-dev-shm-usage
# makes Chromium use a temp dir on disk instead. Applied only to the Chromium
# engines (playwright/patchright) — it is a Chromium flag, and Camoufox is Firefox.


def _retrieve_outcome(task: "asyncio.Future[Any]") -> None:
    """Consume an abandoned task's exception so asyncio does not print it.

    An exception on a future nobody reads is reported to stderr when it is
    collected, which on a CLI whose output is meant to compose with a pipe is
    noise on every scrape.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        LOGGER.debug("Ignoring browser close error: %s", exc)


def _close_timeout_s() -> float:
    """Seconds to allow a graceful browser close before reaping via the driver.

    The content is already extracted when teardown starts, so every second here
    is dead time on the caller's clock. Chromium's own close regularly stalls on
    a page with live connections and is released only by an internal 30s timeout;
    measured on abc.net.au (4 alternating pairs, same page, back to back): a
    graceful close averaged 20.1s, the same close bounded at 2s averaged 1.6s,
    with the browser process reaped either way and no residue in either arm.

    Nothing depends on the graceful path — supacrawl records no video, HAR or
    trace, so there is nothing to flush and the browser is discarded outright.
    ``SUPACRAWL_BROWSER_CLOSE_TIMEOUT`` raises the bound where a caller wants the
    politer close; 0 or a negative value reaps immediately.
    """
    raw = os.getenv("SUPACRAWL_BROWSER_CLOSE_TIMEOUT")
    if raw is None:
        return _DEFAULT_CLOSE_TIMEOUT_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        LOGGER.debug("Ignoring non-numeric SUPACRAWL_BROWSER_CLOSE_TIMEOUT=%r", raw)
        return _DEFAULT_CLOSE_TIMEOUT_S


def _is_containerised() -> bool:
    """Best-effort detection of running inside a container.

    Checks the two portable signals: Docker's sentinel file and container markers
    in PID 1's cgroup. Cheap and read-only; any error is treated as "not a
    container" — the safe default that never adds a flag to a normal desktop run.
    """
    try:
        if os.path.exists("/.dockerenv"):
            return True
        cgroup = Path("/proc/1/cgroup")
        if cgroup.exists():
            text = cgroup.read_text(errors="ignore")
            return any(marker in text for marker in ("docker", "containerd", "kubepods", "libpod"))
    except OSError:
        pass
    return False


def _should_disable_dev_shm() -> bool:
    """Whether to pass ``--disable-dev-shm-usage`` to Chromium (#146).

    An explicit ``SUPACRAWL_DISABLE_DEV_SHM`` wins in both directions; otherwise
    it is on inside a container and off on a normal host.
    """
    override = os.getenv("SUPACRAWL_DISABLE_DEV_SHM")
    if override is not None:
        return override.strip().lower() in {"1", "true", "yes", "on"}
    return _is_containerised()


def chromium_launch_args() -> list[str]:
    """Chromium CLI launch args for the current environment (#146).

    Only ``--disable-dev-shm-usage`` is added, and only under a constrained
    ``/dev/shm`` container. ``--no-sandbox`` is deliberately NOT added here: it
    weakens the browser sandbox and would regress every non-container user, and a
    container that needs it sets it in its own launch environment (the downstream
    Dockerfile already does). Keeping it out of the library is the correct
    default — it is the one other classic container flag, and it does not belong
    on by default, so scope is not silently expanded.
    """
    args: list[str] = []
    if _should_disable_dev_shm():
        args.append("--disable-dev-shm-usage")
    return args


# Domains to skip when expanding iframes (ads, analytics, tracking, CAPTCHA)
IFRAME_BLOCKED_DOMAINS = frozenset(
    {
        # Advertising
        "doubleclick.net",
        "googlesyndication.com",
        "amazon-adsystem.com",
        "adnxs.com",
        "adsrvr.org",
        "moatads.com",
        "taboola.com",
        "outbrain.com",
        # Analytics / tracking
        "google-analytics.com",
        "googletagmanager.com",
        "analytics.google.com",
        "connect.facebook.net",
        "bat.bing.com",
        "sc-static.net",
        "hotjar.com",
        "clarity.ms",
        # CAPTCHA
        "recaptcha.net",
        "hcaptcha.com",
        "challenges.cloudflare.com",
    }
)

# Maximum iframes to expand per page (performance guard)
MAX_IFRAMES_PER_PAGE = 10

# Upper bound for the expect-content selector wait, so a selector that never
# appears does not block for the full page-load timeout (and double that again
# on an escalated retry). Generous enough for normal hydration.
EXPECT_SELECTOR_TIMEOUT_MS = 15000


def _is_blocked_iframe_domain(hostname: str) -> bool:
    """Check if hostname matches a blocked iframe domain."""
    hostname = hostname.lower()
    return any(hostname == domain or hostname.endswith("." + domain) for domain in IFRAME_BLOCKED_DOMAINS)


@dataclass
class PageContent:
    """Result of fetching a page."""

    url: str
    html: str
    title: str | None
    status_code: int
    screenshot: bytes | None = None
    pdf: bytes | None = None
    action_results: list[Any] | None = None  # Results from ActionRunner


@dataclass
class PageMetadata:
    """Metadata extracted from a page."""

    # Core metadata
    title: str | None
    description: str | None
    language: str | None
    keywords: str | None
    robots: str | None
    canonical_url: str | None

    # OpenGraph metadata
    og_title: str | None
    og_description: str | None
    og_image: str | None
    og_url: str | None
    og_site_name: str | None

    # Detected timezone (IANA format, e.g. "America/New_York")
    timezone: str | None = None


def _extract_timezone_from_jsonld(data: Any, iana_tz_pattern: re.Pattern[str]) -> str | None:
    """Recursively search JSON-LD data for IANA timezone values.

    Args:
        data: Parsed JSON-LD data (dict, list, or primitive)
        iana_tz_pattern: Compiled regex for IANA timezone identifiers

    Returns:
        IANA timezone string or None
    """
    if isinstance(data, dict):
        # Check timezone-related keys directly
        for key in ("timezone", "timeZone", "time_zone"):
            val = data.get(key)
            if isinstance(val, str):
                match = iana_tz_pattern.search(val)
                if match:
                    return match.group(0)
        # Check address/location for timezone
        for key in ("address", "location", "geo"):
            val = data.get(key)
            if isinstance(val, dict):
                result = _extract_timezone_from_jsonld(val, iana_tz_pattern)
                if result:
                    return result
        # Check @graph for nested items
        graph = data.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                result = _extract_timezone_from_jsonld(item, iana_tz_pattern)
                if result:
                    return result
    elif isinstance(data, list):
        for item in data:
            result = _extract_timezone_from_jsonld(item, iana_tz_pattern)
            if result:
                return result
    return None


class BrowserManager:
    """Manages Playwright browser for page fetching.

    Usage:
        async with BrowserManager() as browser:
            content = await browser.fetch_page("https://example.com")

        # With locale configuration
        from supacrawl.models import LocaleConfig
        locale = LocaleConfig.from_country("AU")
        async with BrowserManager(locale_config=locale) as browser:
            content = await browser.fetch_page("https://example.com")

        # With stealth mode (requires: pip install supacrawl[stealth])
        async with BrowserManager(stealth=True) as browser:
            content = await browser.fetch_page("https://protected-site.com")
    """

    def __init__(
        self,
        headless: bool | None = None,
        timeout_ms: int | None = None,
        user_agent: str | None = None,
        locale_config: Any | None = None,  # LocaleConfig, avoid circular import
        stealth: bool = False,
        proxy: str | None = None,
        engine: str | None = None,
        firefox_user_prefs: dict[str, Any] | None = None,
    ):
        """Initialize browser manager.

        Args:
            headless: Run headless (default from SUPACRAWL_HEADLESS env, or True).
                Note: When stealth=True and headless is not explicitly set, defaults
                to False (headful) because stealth mode is more effective headful.
                Camoufox supports headless=True with humanize=True effectively.
            timeout_ms: Page load timeout (default from SUPACRAWL_TIMEOUT env, or 30000)
            user_agent: User agent string (default from SUPACRAWL_USER_AGENT env)
            locale_config: LocaleConfig for browser locale/timezone settings
            stealth: Enable stealth mode via Patchright for anti-bot evasion.
                Automatically enables headful mode unless headless is explicitly set.
                Ignored when engine is explicitly set.
            proxy: Proxy URL (e.g., http://user:pass@host:port, socks5://host:port)
            engine: Browser engine to use. Options:
                - "playwright" (default): Standard Playwright Chromium with basic stealth scripts
                - "patchright": Patched Chromium via Patchright (Tier 2 anti-detection)
                - "camoufox": Patched Firefox via Camoufox (Tier 3 anti-detection,
                  effective against Akamai Bot Manager)
                When not set, falls back to "patchright" if stealth=True, else "playwright".
            firefox_user_prefs: Firefox about:config preferences for Camoufox.
                Only used when engine="camoufox". Example:
                {"network.http.http2.enabled": False} to force HTTP/1.1.
        """
        # Resolve engine from explicit engine param, stealth flag, or default
        if engine is not None:
            if engine not in ENGINE_CHOICES:
                raise ValueError(f"Invalid engine '{engine}'. Choose from: {', '.join(ENGINE_CHOICES)}")
            self.engine = engine
        elif stealth:
            self.engine = "patchright"
        else:
            self.engine = "playwright"

        # Keep stealth flag for backwards compat (True when engine is patchright or camoufox)
        self.stealth = self.engine in ("patchright", "camoufox")

        # Headless defaults: Patchright prefers headful, Camoufox works headless
        if headless is not None:
            self.headless = headless
        elif self.engine == "patchright":
            # Patchright + headless has worse detection scores than headful
            self.headless = self._env_bool("SUPACRAWL_HEADLESS", False)
        elif self.engine == "camoufox":
            # Camoufox works well headless with humanize=True
            self.headless = self._env_bool("SUPACRAWL_HEADLESS", True)
        else:
            self.headless = self._env_bool("SUPACRAWL_HEADLESS", True)

        self.timeout_ms = timeout_ms or int(os.getenv("SUPACRAWL_TIMEOUT", "30000"))
        # Only set user_agent if explicitly provided or env var is set
        # Otherwise let Playwright use its real browser UA (reduces fingerprint mismatch)
        # Note: Camoufox generates its own fingerprint, so user_agent is not set for it
        self.user_agent = user_agent or os.getenv("SUPACRAWL_USER_AGENT")
        self.locale_config = locale_config
        self.proxy = proxy or os.getenv("SUPACRAWL_PROXY")
        self.firefox_user_prefs = firefox_user_prefs
        self._browser: Browser | BrowserContext | None = None
        self._playwright: Any = None
        # Self-healing state (#160). A long-lived server hands the same manager to
        # every request, so a browser that dies is a silent outage for all of them
        # until something notices; these track that noticing.
        self._relaunch_lock = asyncio.Lock()
        self._ever_started = False
        # Distinguishes "never started yet" (lazy-start on first use, #143) from
        # "deliberately stopped" (a retired manager, not to be resurrected).
        self._deliberately_stopped = False
        # Monotonic: True once any engine has launched, and never reset (stop()
        # clears _ever_started, but a manager that launched-then-stopped is still
        # "has launched" — health must not read a retired engine as "never
        # started, fine"). Backs the public has_launched property.
        self._has_ever_launched = False
        self.relaunches = 0
        self._consecutive_relaunch_failures = 0
        # None, never 0.0: monotonic() starts near process uptime, so 0.0 reads as
        # "ages ago" on a long-lived box and "just now" on a fresh container.
        self._last_relaunch_failure_at: float | None = None

    @staticmethod
    def _env_bool(key: str, default: bool) -> bool:
        """Get boolean from environment variable."""
        val = os.getenv(key)
        if val is None:
            return default
        return val.strip().lower() in {"1", "true", "yes", "on"}

    def _resolve_device_config(self, device: str) -> dict[str, Any]:
        """Resolve a Playwright device name to context options.

        Looks up the device in Playwright's built-in device descriptors and
        returns a dict suitable for passing to ``browser.new_context()``.

        Args:
            device: Device name (e.g. "iPhone 14", "Pixel 7").

        Returns:
            Dict with user_agent, viewport, device_scale_factor, is_mobile,
            has_touch keys.

        Raises:
            ValueError: If the device name is not found in Playwright's
                device descriptors.
        """
        if self._playwright is None:
            raise RuntimeError("Playwright not started. Cannot resolve device config.")

        devices = self._playwright.devices
        if device not in devices:
            # Build helpful error with close matches
            available = sorted(devices.keys())
            lower = device.lower()
            suggestions = [d for d in available if lower in d.lower() or d.lower() in lower][:5]
            hint = f" Did you mean: {', '.join(suggestions)}" if suggestions else ""
            raise ValueError(
                f"Unknown device '{device}'. Use BrowserManager.list_devices() to see available devices.{hint}"
            )

        config = dict(devices[device])
        # Remove default_browser_type — it's metadata, not a context option
        config.pop("default_browser_type", None)
        return config

    async def list_devices(self) -> list[str]:
        """Return sorted list of available Playwright device names.

        Requires the browser to be started (Playwright instance available).
        """
        if self._playwright is None:
            raise RuntimeError("Playwright not started. Call start() first.")
        return sorted(self._playwright.devices.keys())

    def _build_context_options(
        self,
        device: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Build browser context options including locale and device settings.

        When no explicit configuration is provided, returns minimal options
        to let Playwright use browser defaults. This reduces fingerprint
        mismatch that can trigger bot detection.

        Args:
            device: Optional Playwright device name for mobile emulation
                (e.g. "iPhone 14", "Pixel 7").
            extra_headers: Optional caller-supplied HTTP headers to send with
                every request in this context. Values win over any locale-derived
                Accept-Language header when keys collide.

        Returns:
            Dictionary of options for browser.new_context()
        """
        options: dict[str, Any] = {}

        if block_page_scripts():
            options["java_script_enabled"] = False

        # Apply device emulation first (provides base viewport, UA, etc.)
        if device:
            device_config = self._resolve_device_config(device)
            options.update(device_config)
            LOGGER.debug("Applying device emulation: %s", device)

        # Only set user agent if explicitly provided (reduces fingerprint mismatch)
        # When device emulation is active, the device's UA is already set above;
        # an explicit user_agent overrides it (intentional user choice).
        if self.user_agent:
            options["user_agent"] = self.user_agent

        # Build extra_http_headers: start from locale defaults, then overlay caller headers.
        # Caller values win so that e.g. an explicit Accept-Language overrides the locale.
        merged_headers: dict[str, str] = {}

        # Apply locale config if explicitly provided
        if self.locale_config is not None:
            locale = self.locale_config.get_language()
            timezone = self.locale_config.get_timezone()
            accept_lang = self.locale_config.get_accept_language_header()

            options["locale"] = locale
            options["timezone_id"] = timezone
            merged_headers["Accept-Language"] = accept_lang
            LOGGER.debug(
                "Using locale config: language=%s, timezone=%s",
                locale,
                timezone,
            )
        else:
            # Check for explicit environment variable configuration
            locale_env = os.getenv("SUPACRAWL_LOCALE")
            timezone_env = os.getenv("SUPACRAWL_TIMEZONE")

            # Only set if explicitly configured via env vars
            if locale_env:
                options["locale"] = locale_env
            if timezone_env:
                options["timezone_id"] = timezone_env

        # Overlay caller-supplied headers — their values take precedence.
        # Only header KEYS are logged; values are never emitted to logs.
        if extra_headers:
            merged_headers.update(extra_headers)
            LOGGER.debug("Applying %d custom request header(s): %s", len(extra_headers), list(extra_headers.keys()))

        if merged_headers:
            options["extra_http_headers"] = merged_headers

        return options

    async def __aenter__(self) -> "BrowserManager":
        """Start browser (async context manager entry)."""
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Close browser (async context manager exit)."""
        await self.stop()

    def _get_playwright_module(self) -> Any:
        """Get the appropriate playwright module based on engine setting.

        Returns:
            The async_playwright function from either patchright or playwright.
            Returns None when engine is "camoufox" (uses separate launch path).

        Raises:
            StealthNotAvailableError: If patchright engine requested but not installed.
            CamoufoxNotAvailableError: If camoufox engine requested but not installed.
        """
        if self.engine == "camoufox":
            # Camoufox has its own launch path, validated in start()
            try:
                import camoufox  # noqa: F401

                LOGGER.debug("Using Camoufox engine (patched Firefox)")
                return None  # Camoufox doesn't use async_playwright
            except ImportError as e:
                raise CamoufoxNotAvailableError() from e
        elif self.engine == "patchright":
            try:
                from patchright.async_api import async_playwright

                LOGGER.debug("Using Patchright engine (patched Chromium)")
                return async_playwright
            except ImportError as e:
                raise StealthNotAvailableError() from e
        else:
            from playwright.async_api import async_playwright  # type: ignore[assignment]

            return async_playwright

    async def start(self) -> None:
        """Start the browser.

        Can be called directly for manual lifecycle management, or implicitly
        via async context manager (async with BrowserManager() as browser).

        Raises:
            StealthNotAvailableError: If patchright engine requested but not installed.
            CamoufoxNotAvailableError: If camoufox engine requested but not installed.
        """
        if self._browser is not None:
            return  # Already started

        if self.engine == "camoufox":
            await self._start_camoufox()
        else:
            await self._start_playwright()
        self._ever_started = True
        self._deliberately_stopped = False
        self._has_ever_launched = True

    async def _start_playwright(self) -> None:
        """Start browser via Playwright or Patchright."""
        async_playwright = self._get_playwright_module()
        self._playwright = await async_playwright().start()

        # Build launch options
        launch_options: dict[str, Any] = {"headless": self.headless}

        # Container-aware Chromium flags (#146): --disable-dev-shm-usage under a
        # constrained-/dev/shm container so heavy pages do not crash Chromium.
        launch_args = chromium_launch_args()
        if launch_args:
            launch_options["args"] = launch_args

        # Add proxy if configured
        if self.proxy:
            try:
                launch_options["proxy"] = _parse_proxy_url(self.proxy)
                LOGGER.debug("Using proxy: %s", self.proxy.split("@")[-1])  # Hide credentials
            except ValueError as e:
                LOGGER.error(f"Invalid proxy URL: {e}")
                raise

        self._browser = await self._playwright.chromium.launch(**launch_options)
        LOGGER.debug(
            "Browser started (engine=%s, headless=%s, proxy=%s)",
            self.engine,
            self.headless,
            bool(self.proxy),
        )

    async def _start_camoufox(self) -> None:
        """Start browser via Camoufox (patched Firefox).

        Camoufox provides AsyncCamoufox, an async context manager that
        handles Playwright acquisition internally and returns a browser
        instance compatible with the Playwright API.
        """
        try:
            from camoufox.async_api import AsyncCamoufox
        except ImportError as e:
            raise CamoufoxNotAvailableError() from e

        # Build Camoufox options
        camoufox_options: dict[str, Any] = {
            "headless": self.headless,
            "humanize": True,  # Natural cursor movement
        }

        # Add proxy if configured
        if self.proxy:
            try:
                camoufox_options["proxy"] = _parse_proxy_url(self.proxy)
                LOGGER.debug("Using proxy: %s", self.proxy.split("@")[-1])
            except ValueError as e:
                LOGGER.error(f"Invalid proxy URL: {e}")
                raise

        # Apply locale if configured
        if self.locale_config is not None:
            locale = self.locale_config.get_language()
            if locale:
                camoufox_options["locale"] = locale

        # Apply Firefox user preferences (e.g. disable HTTP/2)
        if self.firefox_user_prefs:
            camoufox_options["firefox_user_prefs"] = self.firefox_user_prefs

        # Camoufox returns a context manager; we enter it and store the browser
        self._camoufox_cm = AsyncCamoufox(**camoufox_options)
        self._browser = await self._camoufox_cm.__aenter__()

        LOGGER.debug(
            "Browser started (engine=camoufox, headless=%s, humanize=True, proxy=%s, firefox_prefs=%s)",
            self.headless,
            bool(self.proxy),
            bool(self.firefox_user_prefs),
        )

    async def stop(self) -> None:
        """Stop the browser and cleanup resources.

        Safe to call multiple times. A deliberate stop also retires the manager:
        a later fetch is a programming error, not an engine to relaunch (only
        :meth:`_force_stop`, the relaunch path, keeps the manager live).
        """
        self._ever_started = False
        self._deliberately_stopped = True
        if self.engine == "camoufox" and hasattr(self, "_camoufox_cm"):
            # Camoufox cleanup via its context manager, deliberately unbounded.
            # AsyncCamoufox.__aexit__ closes the browser and THEN stops
            # playwright, and only that second step reaps the Firefox and driver
            # processes — so abandoning it partway through leaks them. The close
            # bound below applies to the Chromium engines, where stopping the
            # driver is a separate call that reaps on its own (verified: no
            # residue in either arm).
            try:
                await self._camoufox_cm.__aexit__(None, None, None)
            except Exception as e:
                LOGGER.debug("Ignoring camoufox teardown error: %s", e)
            self._browser = None
            del self._camoufox_cm
        else:
            budget = _close_timeout_s()
            closing: asyncio.Future[None] | None = None
            if self._browser:
                closing = asyncio.ensure_future(self._browser.close())
                closing.add_done_callback(_retrieve_outcome)
                done, _ = await asyncio.wait({closing}, timeout=budget)
                if not done:
                    LOGGER.debug("Graceful browser close exceeded %.1fs; reaping via the driver instead", budget)
                self._browser = None
            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception as e:
                    LOGGER.debug("Ignoring playwright stop error: %s", e)
                self._playwright = None
            if closing is not None and not closing.done():
                # Stopping the driver resolves a close that was still in flight.
                # Wait for it rather than cancelling: cancelling orphans
                # Playwright's internal protocol future, and asyncio then prints
                # "Future exception was never retrieved" to stderr on every
                # browser scrape. asyncio.wait never cancels on timeout.
                await asyncio.wait({closing}, timeout=max(budget, 1.0))
        LOGGER.debug("Browser stopped")

    @property
    def is_alive(self) -> bool:
        """Whether the underlying browser process is still connected.

        A local flag read (no IPC), so it is cheap enough to check on every page
        checkout.
        """
        return self._instance_is_alive(self._browser)

    @property
    def has_launched(self) -> bool:
        """Whether an engine has ever been launched on this manager.

        With lazy start (#143), a fresh server-side manager has not launched
        anything until the first tool needs it, so ``is_alive`` is legitimately
        False without any outage. Health reporting uses this to tell "not started
        yet" (fine) apart from "started then died" (a real #160 outage), rather
        than reading an un-launched lazy engine as degraded. Monotonic — a manager
        that launched then stopped still reads True, so a retired engine is never
        mistaken for one that simply has not started yet.
        """
        return self._has_ever_launched

    @staticmethod
    def _instance_is_alive(browser: Any | None) -> bool:
        """Whether one specific browser object is still connected.

        Always ask about the instance a call actually used, never the manager's
        current one: under concurrency another request may already have swapped a
        fresh browser in, which would make the manager read healthy while the
        engine this call was riding is the one that died — and the failure would
        then be blamed on the site.

        Camoufox's persistent context exposes no ``is_connected``; when liveness
        genuinely cannot be determined the answer is optimistic, and the
        relaunch-on-detect path in :meth:`_open_page` is the backstop.
        """
        if browser is None:
            return False
        for candidate in (browser, getattr(browser, "browser", None)):
            is_connected = getattr(candidate, "is_connected", None)
            if callable(is_connected):
                try:
                    return bool(is_connected())
                except Exception:  # a raising is_connected() means it is not connected
                    return False
        return True

    async def ensure_started(self) -> None:
        """Guarantee a live browser before a page is opened, relaunching a dead one.

        This is the liveness check on checkout. A crashed browser otherwise stays
        crashed for the life of the process: every ``new_context()`` fails with
        "Target page, context or browser has been closed" and the caller cannot
        tell a dead engine from a broken site.

        Raises:
            RuntimeError: If the browser was deliberately stopped — a retired
                manager is a programming error to reuse, distinct from one that
                has simply never launched (lazy first start) or an engine that
                died under a running server and can be relaunched.
            StealthNotAvailableError | CamoufoxNotAvailableError: On lazy first
                start when the configured stealth engine's package is missing —
                the typed, per-engine error naming what to install (#143).
            BrowserUnavailableError: If the relaunch failed or is in backoff.
        """
        if self.is_alive:
            return
        if self._browser is None and not self._ever_started:
            # Lazy first start (#143): nothing has launched yet. Launch the engine
            # here, on first real use — not at server startup — so a server whose
            # configured stealth engine is missing still starts and serves every
            # non-stealth tool, and only a tool that needs this engine fails, with
            # the typed per-engine error (Stealth/Camoufox NotAvailableError).
            #
            # Serialised on the relaunch lock: a shared server-side manager takes a
            # burst of concurrent first-use calls (scrape/crawl/map/search all ride
            # one manager), and an unguarded start would launch one engine PER
            # caller and orphan every loser. The double-check under the lock lets
            # the first caller launch while the rest see the started engine.
            async with self._relaunch_lock:
                if self.is_alive:
                    return  # a concurrent first-use caller already launched it
                if self._browser is None and not self._ever_started:
                    if self._deliberately_stopped:
                        raise RuntimeError(
                            "Browser was stopped and is not initialized. Create a new BrowserManager "
                            "(or use 'async with BrowserManager()') to use it again."
                        )
                    await self.start()
                    return
            # Fell through: another caller started it but it is not alive — a start
            # that raced with an immediate death. Fall to the relaunch path below.
        LOGGER.warning("Browser engine is not connected before fetch; relaunching in-process")
        await self.relaunch()

    async def relaunch(self) -> None:
        """Tear down a dead browser and start a fresh one, in-process.

        Serialised so concurrent requests that all notice the same dead browser
        relaunch it once, not once each. Consecutive failures back off
        exponentially: a genuinely broken environment must not be hammered with a
        launch attempt per inbound request.

        Raises:
            BrowserUnavailableError: If the relaunch failed, or a previous one
                failed recently enough that the backoff has not elapsed.
        """
        async with self._relaunch_lock:
            if self.is_alive:
                return  # a concurrent caller already healed it
            self._assert_relaunch_allowed()
            await self._force_stop()
            try:
                await self.start()
            except Exception as e:
                self._note_relaunch_failure()
                raise BrowserUnavailableError(f"Browser engine relaunch failed: {e}") from e
            if not self.is_alive:
                self._note_relaunch_failure()
                raise BrowserUnavailableError("Browser engine exited immediately after relaunch")
            self._consecutive_relaunch_failures = 0
            self.relaunches += 1
            LOGGER.warning(
                "Browser engine relaunched in-process (engine=%s, relaunches=%d)",
                self.engine,
                self.relaunches,
            )

    def _assert_relaunch_allowed(self) -> None:
        """Refuse a relaunch while the crash-loop backoff is still running."""
        failures = self._consecutive_relaunch_failures
        if failures == 0:
            return
        # Cap the exponent, not just the result: the backoff plateaus long before
        # this, and an uncapped 2**failures overflows float after days of a
        # continuously failing environment — turning the guard into a crash.
        doublings = min(failures - 1, _RELAUNCH_BACKOFF_MAX_DOUBLINGS)
        backoff = min(_RELAUNCH_BACKOFF_BASE_S * 2**doublings, _RELAUNCH_BACKOFF_MAX_S)
        last_failure = self._last_relaunch_failure_at
        elapsed = float("inf") if last_failure is None else time.monotonic() - last_failure
        if elapsed < backoff:
            raise BrowserUnavailableError(
                f"Browser engine relaunch failed {failures} time(s) in a row; waiting "
                f"{backoff - elapsed:.0f}s before trying again. The scraper environment is likely broken "
                f"(missing browser binaries, out of memory, or no shared memory)."
            )

    def _note_relaunch_failure(self) -> None:
        """Record a failed relaunch so the next attempt backs off further."""
        self._consecutive_relaunch_failures += 1
        self._last_relaunch_failure_at = time.monotonic()

    async def _force_stop(self) -> None:
        """Tear down the current browser, tolerating a corpse that cannot be closed.

        Unlike :meth:`stop`, this keeps the manager live: it is the first half of a
        relaunch, not a retirement, so a failed relaunch still leaves a manager
        that will try again (under backoff) rather than one that reads as never
        started.
        """
        try:
            await self.stop()
        except Exception as e:
            LOGGER.debug("Ignoring teardown error on a dead browser: %s", e)
        finally:
            self._browser = None
            self._playwright = None
            self._ever_started = True
            self._deliberately_stopped = False
            if hasattr(self, "_camoufox_cm"):
                del self._camoufox_cm

    async def _open_page(
        self,
        *,
        owns_context: bool,
        device: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[BrowserContext | None, Page]:
        """Open a fresh context and page, relaunching once if the browser is dead.

        The liveness check in :meth:`ensure_started` closes the common case; this
        closes the race where the browser dies between that check and this call.
        The relaunch is gated on the browser actually being disconnected, so a
        closed *page* — which produces the same Playwright message — is never
        mistaken for a dead engine and never triggers a retry of a site failure.

        Args:
            owns_context: True when a separate BrowserContext must be created
                (every engine except Camoufox, whose browser *is* the context).
            device: Playwright device name for mobile emulation, or None.
            extra_headers: Custom request headers for this context.

        Returns:
            The created context (None for Camoufox) and the page.

        Raises:
            BrowserUnavailableError: If the browser is dead and cannot be revived.
        """
        in_use = self._browser
        try:
            return await self._new_context_and_page(
                owns_context=owns_context, device=device, extra_headers=extra_headers
            )
        except Exception as e:
            # Liveness of the instance THIS call used — see _instance_is_alive.
            if not (is_closed_browser_error(e) and not self._instance_is_alive(in_use)):
                raise
            LOGGER.warning("Browser engine died (%s); relaunching in-process and retrying this fetch", e)
            await self.relaunch()
            try:
                return await self._new_context_and_page(
                    owns_context=owns_context, device=device, extra_headers=extra_headers
                )
            except Exception as retry_error:
                raise BrowserUnavailableError(
                    f"Browser engine was relaunched but still cannot open a page: {retry_error}"
                ) from retry_error

    async def _new_context_and_page(
        self,
        *,
        owns_context: bool,
        device: str | None,
        extra_headers: dict[str, str] | None,
    ) -> tuple[BrowserContext | None, Page]:
        """Create the context and page, cleaning up a half-built context on failure."""
        if not owns_context:
            # Camoufox: the browser IS the context, so just create a page.
            if block_page_scripts():
                raise RuntimeError("Camoufox cannot run with page scripts blocked (SUPACRAWL_BLOCK_PAGE_SCRIPTS)")
            return None, await cast("BrowserContext", self._browser).new_page()

        context_options = self._build_context_options(device=device, extra_headers=extra_headers)
        context = await cast("Browser", self._browser).new_context(**context_options)
        try:
            # Inject stealth scripts at context level so all pages inherit them.
            # Patchright and Camoufox handle anti-detection at the engine level.
            if self.engine == "playwright":
                for script in STEALTH_SCRIPTS:
                    await context.add_init_script(script)
                LOGGER.debug("Injected %d stealth scripts at context level", len(STEALTH_SCRIPTS))
            page = await context.new_page()
        except Exception:
            try:
                await context.close()
            except Exception:
                pass
            raise
        return context, page

    def _as_engine_failure(self, error: BaseException, in_use: Any | None) -> BaseException:
        """Re-label a mid-fetch error that was really the engine dying.

        A browser that dies *after* the page opened surfaces as an ordinary
        Playwright error indistinguishable from a site problem. Naming it lets the
        caller report an infrastructure fault instead of blaming the site; the
        engine itself is relaunched on the next checkout rather than here, so a
        fetch with side-effecting actions is never silently replayed.

        Args:
            error: The exception the fetch raised.
            in_use: The browser instance this fetch was actually running on —
                never the manager's current one, which a concurrent relaunch may
                already have replaced with a healthy engine.
        """
        if isinstance(error, BrowserUnavailableError):
            return error
        if is_closed_browser_error(error) and not self._instance_is_alive(in_use):
            return BrowserUnavailableError(f"Browser engine died mid-fetch: {error}")
        return error

    async def fetch_page(
        self,
        url: str,
        wait_for_spa: bool = True,
        spa_timeout_ms: int = 5000,
        capture_screenshot: bool = False,
        capture_pdf: bool = False,
        screenshot_full_page: bool = True,
        actions: list[Any] | None = None,
        wait_until: Literal["commit", "domcontentloaded", "load", "networkidle"] | None = None,
        expand_iframes: Literal["none", "same-origin", "all"] = "same-origin",
        expand_disclosures: bool = True,
        device: str | None = None,
        extra_headers: dict[str, str] | None = None,
        wait_for_selector: str | None = None,
    ) -> PageContent:
        """Fetch a page with browser rendering.

        Args:
            url: URL to fetch
            wait_for_spa: Wait for SPA content to stabilize
            spa_timeout_ms: Max time to wait for SPA stability
            wait_for_selector: Optional CSS selector to wait for before extracting
                content, so caller-asserted (post-hydration) content is present.
                Best-effort: a timeout or invalid selector is swallowed and the
                page is returned regardless (the caller re-checks the assertion).
            capture_screenshot: Capture PNG screenshot of page
            capture_pdf: Generate PDF of page
            screenshot_full_page: Capture full scrollable page (default True)
            actions: List of Action objects to execute before capturing content
            wait_until: Page load strategy. Options: commit, domcontentloaded (default),
                load, networkidle. Falls back to SUPACRAWL_WAIT_UNTIL env var if None.
            expand_iframes: Iframe expansion mode. "none" strips all iframes (legacy),
                "same-origin" expands same-origin iframes inline (default),
                "all" expands all non-blocked iframes including cross-origin.
            expand_disclosures: When True (default), expand collapsed disclosure
                regions (aria-expanded="false" content controls, closed <details>)
                before capturing HTML, so click-gated content is faithfully captured.
                Navigation chrome (nav menus, hamburgers) is excluded. Set False to
                opt out, e.g. when testing the raw collapsed state.
            device: Playwright device name for mobile emulation (e.g. "iPhone 14",
                "Pixel 7"). Sets viewport, user agent, device scale factor, and
                touch support. Not supported with the Camoufox engine.
            extra_headers: Custom HTTP headers to send with every request in this
                context (e.g. Authorization, Cookie, X-Api-Key). Only header KEYS
                are logged; values are never written to logs.

        Returns:
            PageContent with HTML, metadata, and optional screenshot/PDF

        Raises:
            RuntimeError: If browser not initialized or fetch fails
            BrowserUnavailableError: If the browser engine is dead and cannot be
                relaunched — a scraper-side fault, not a statement about the site.
            ValueError: If device is specified with Camoufox engine or device
                name is not recognised.
            ValidationError: If the URL or its resolved address is blocked (#152).
                This is a pre-flight check only — the browser engine re-resolves
                the hostname itself when it connects, so a DNS answer that
                changes in the window between this check and the engine's own
                connect is not pinned (see services/url_guard.py's module
                docstring for why the browser paths cannot close that gap).
        """
        await self.ensure_started()

        # Pre-flight SSRF check (#152): refuses now if the URL or its resolved
        # address is blocked. Not a full pin — see the Raises note above.
        # Off the event loop: resolve_and_pin does a blocking getaddrinfo.
        await asyncio.to_thread(resolve_and_pin, url)

        if device and self.engine == "camoufox":
            raise ValueError(
                "Device emulation is not supported with the Camoufox engine. "
                "Camoufox generates its own fingerprint. Use --engine playwright or --engine patchright."
            )

        context: BrowserContext | None = None
        page: Page | None = None
        # Camoufox browser IS the context — don't create a separate one
        owns_context = self.engine != "camoufox"
        # The engine this fetch runs on. Re-read after _open_page, which may have
        # relaunched, so a mid-fetch death is judged against the right instance.
        engine_in_use: Any = self._browser

        try:
            context, page = await self._open_page(owns_context=owns_context, device=device, extra_headers=extra_headers)
            engine_in_use = self._browser

            # Navigate to URL - use parameter if provided, else fall back to env var
            if wait_until is None:
                wait_until_env = os.getenv("SUPACRAWL_WAIT_UNTIL", "domcontentloaded")
                wait_until_resolved: Literal["commit", "domcontentloaded", "load", "networkidle"] = (
                    wait_until_env  # type: ignore[assignment]
                    if wait_until_env in ("commit", "domcontentloaded", "load", "networkidle")
                    else "domcontentloaded"
                )
            else:
                wait_until_resolved = wait_until
            # Chromium resolves and connects itself, so the pinned-address
            # guarantee the httpx path gets is not available here (#152). The
            # target was already validated above (pre-flight), and every
            # navigation from here on (redirect hops included) is re-validated
            # by the route handler installed in _install_navigation_guard.
            await _install_navigation_guard(page)
            response = await page.goto(url, wait_until=wait_until_resolved, timeout=self.timeout_ms)

            # Wait for SPA stability if requested
            # Skip if using networkidle - it already waits for JS to finish
            # and the extra polling can trigger bot detection
            if wait_for_spa and wait_until_resolved != "networkidle":
                await self._wait_for_spa_stability(page, spa_timeout_ms)

            # Best-effort wait for caller-asserted content (expect-content gate).
            # Returns as soon as the selector appears; a timeout or invalid
            # selector is swallowed so the caller can re-check and escalate.
            if wait_for_selector:
                selector_timeout = min(self.timeout_ms, EXPECT_SELECTOR_TIMEOUT_MS)
                try:
                    await page.wait_for_selector(wait_for_selector, timeout=selector_timeout)
                except Exception as e:
                    LOGGER.debug("wait_for_selector %r did not resolve: %s", wait_for_selector, e)

            # Execute actions if provided
            action_results_list: list[Any] | None = None
            if actions:
                from supacrawl.services.actions import ActionRunner

                runner = ActionRunner(timeout_ms=self.timeout_ms)
                action_results_list = await runner.run(page, actions)
                LOGGER.debug(f"Executed {len(action_results_list)} actions")

            # Expand iframe content inline before extracting HTML. This is
            # best-effort: a detached frame, a cross-origin navigation mid-expand,
            # or an unexpected frame state (seen on Reddit) must not crash the
            # whole fetch — fall back to the un-expanded page instead.
            if expand_iframes != "none":
                try:
                    await self._expand_iframes(page, mode=expand_iframes)
                except Exception as exc:
                    LOGGER.debug("Iframe expansion failed for %s, continuing without it: %s", url, exc)

            # Expand collapsed disclosure regions (aria-expanded controls, <details>)
            # so click-gated content is captured faithfully. Best-effort: any failure
            # falls back to the un-expanded page rather than crashing the fetch.
            if expand_disclosures:
                try:
                    await self._expand_disclosures(page)
                except Exception as exc:
                    LOGGER.debug("Disclosure expansion failed for %s, continuing without it: %s", url, exc)

            # Extract HTML and title
            html = await page.content()
            title = await page.title() or None
            status_code = response.status if response else 200

            # Capture screenshot if requested
            screenshot_bytes: bytes | None = None
            if capture_screenshot:
                screenshot_bytes = await page.screenshot(
                    full_page=screenshot_full_page,
                    type="png",
                )

            # Generate PDF if requested
            # Note: page.pdf() is only available on Chromium (Playwright/Patchright),
            # not on Firefox (Camoufox). Log a warning instead of crashing.
            pdf_bytes: bytes | None = None
            if capture_pdf:
                if self.engine == "camoufox":
                    LOGGER.warning("PDF generation is not supported with Camoufox (Firefox). Skipping PDF capture.")
                else:
                    pdf_bytes = await page.pdf(
                        format="A4",
                        print_background=True,
                    )

            return PageContent(
                url=url,
                html=html,
                title=title,
                status_code=status_code,
                screenshot=screenshot_bytes,
                pdf=pdf_bytes,
                action_results=action_results_list,
            )

        except Exception as e:
            # An engine that dies mid-fetch reads as an ordinary Playwright error;
            # re-label it so the caller is told the scraper broke, not the site.
            relabelled = self._as_engine_failure(e, engine_in_use)
            if relabelled is e:
                raise
            raise relabelled from e

        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass
            if owns_context and context:
                try:
                    await context.close()
                except Exception:
                    pass

    async def _expand_iframes(
        self,
        page: Page,
        mode: Literal["same-origin", "all"] = "same-origin",
    ) -> int:
        """Expand iframe content inline in the page DOM.

        Replaces ``<iframe>`` elements with ``<div>`` wrappers containing the
        frame body HTML so that the content is captured by ``page.content()``.

        Args:
            page: Playwright page with loaded content
            mode: "same-origin" expands only same-origin frames,
                  "all" expands all non-blocked frames

        Returns:
            Number of iframes expanded
        """
        page_origin = urlparse(page.url).scheme + "://" + urlparse(page.url).netloc

        # Process frames innermost-first so nested content is captured
        frames = list(page.frames)
        frames.reverse()

        expanded = 0

        for frame in frames:
            if frame == page.main_frame:
                continue
            if expanded >= MAX_IFRAMES_PER_PAGE:
                LOGGER.debug("Reached max iframe expansion limit (%d)", MAX_IFRAMES_PER_PAGE)
                break

            frame_url = frame.url
            if not frame_url or frame_url == "about:blank":
                continue

            # Check blocked domains
            frame_hostname = urlparse(frame_url).hostname or ""
            if _is_blocked_iframe_domain(frame_hostname):
                LOGGER.debug("Skipping blocked iframe domain: %s", frame_hostname)
                continue

            # Same-origin check
            if mode == "same-origin":
                frame_origin = urlparse(frame_url).scheme + "://" + urlparse(frame_url).netloc
                if frame_origin != page_origin:
                    continue

            try:
                iframe_element = await frame.frame_element()

                # Skip tracking pixels (very small iframes)
                bounding = await iframe_element.bounding_box()
                if bounding and (bounding["width"] <= 5 or bounding["height"] <= 5):
                    LOGGER.debug("Skipping tracking-pixel iframe: %s", frame_url)
                    continue

                # Extract frame body content
                body_html = await frame.evaluate("document.body ? document.body.innerHTML : ''")
                if not body_html or not body_html.strip():
                    continue

                # Replace the <iframe> element with a <div> containing the content
                await iframe_element.evaluate(
                    """(el, args) => {
                        const [bodyHtml, src, hostname] = args;
                        const wrapper = document.createElement('div');
                        wrapper.setAttribute('data-sc-iframe-src', src);
                        const marker = document.createElement('p');
                        marker.innerHTML = '<em>[Embedded content from ' + hostname + ']</em>';
                        wrapper.appendChild(marker);
                        const content = document.createElement('div');
                        content.innerHTML = bodyHtml;
                        wrapper.appendChild(content);
                        el.replaceWith(wrapper);
                    }""",
                    [body_html, frame_url, frame_hostname],
                )
                expanded += 1

            except Exception as e:
                LOGGER.debug("Could not expand iframe %s: %s", frame_url, e)
                continue

        if expanded > 0:
            LOGGER.info("Expanded %d iframe(s) inline", expanded)
        return expanded

    async def _expand_disclosures(self, page: "Page") -> int:
        """Expand collapsed disclosure regions before content capture.

        Clicks aria-expanded="false" content controls and opens closed <details>
        elements so click-gated content is present in the captured HTML. Navigation
        chrome (nav menus, hamburger buttons, dropdown navigations) is deliberately
        excluded so site menus are not spuriously opened.

        Idempotency: only collapsed controls are acted on; already-open controls
        are skipped to avoid re-toggling them closed.

        Args:
            page: Playwright page with loaded content

        Returns:
            Number of disclosure regions expanded
        """
        # One querySelectorAll returning nothing is the entire cost on pages
        # without collapsed disclosures — the JS short-circuits immediately.
        js = """
        async () => {
            // Containers that mark navigation chrome; controls inside these are excluded.
            function isNavChrome(el) {
                let node = el;
                while (node && node !== document.body) {
                    const tag = (node.tagName || '').toLowerCase();
                    if (tag === 'nav') return true;
                    const role = (node.getAttribute && node.getAttribute('role') || '').toLowerCase();
                    if (role === 'navigation' || role === 'menubar' || role === 'menu') return true;
                    const cls = (node.className && typeof node.className === 'string'
                        ? node.className : '').toLowerCase();
                    // Common nav-chrome class signals (hamburger, site-nav, main-menu, etc.)
                    if (/\\b(hamburger|mobile-nav|site-nav|main-nav|primary-nav|mega-menu|dropdown-nav|nav-menu|menu-toggle|topnav|global-nav|utility-nav|nav__toggle|breadcrumb|toc-toggle)\\b/.test(cls)) return true;
                    node = node.parentElement;
                }
                return false;
            }

            let expanded = 0;
            let clickCount = 0;

            // 1. Open closed <details> elements by setting the open attribute directly.
            //    Clicking would work but attribute-set is safer (no scroll jump, no event side-effects).
            //    <details> are opened synchronously — no settle wait needed for this path.
            const details = document.querySelectorAll('details:not([open])');
            for (const d of details) {
                if (isNavChrome(d)) continue;
                d.setAttribute('open', '');
                expanded++;
            }

            // 2. Click collapsed aria-expanded="false" content disclosure controls.
            //    Require an aria-controls attribute (non-empty): this is the ARIA spec
            //    signal for a genuine disclosure widget (accordion, collapsible panel).
            //    The target panel need not already exist — some sites inject
            //    the panel into the DOM on first click, so getElementById pre-click returns
            //    null even though the panel is coming. The attribute presence is the signal.
            //    Bare action buttons (sort, filter, submit, load-more) that happen to set
            //    aria-expanded as a state flag do not carry aria-controls, so they are
            //    safely excluded without needing a class-pattern heuristic.
            //    <summary> elements are excluded: they only appear inside <details>, which
            //    are already handled by the setAttribute('open') pass above.
            const collapsed = document.querySelectorAll('[aria-expanded="false"]');
            for (const el of collapsed) {
                if (isNavChrome(el)) continue;
                // Skip if it is inside a <details> we already opened above
                if (el.closest && el.closest('details')) continue;
                // Require the aria-controls attribute (non-empty) as the disclosure signal.
                // We do NOT require the target to exist yet — panels may be injected on click.
                const controlsId = el.getAttribute('aria-controls');
                if (!controlsId) continue;
                // Skip form-submission and reset buttons regardless of aria-controls
                const elType = (el.getAttribute('type') || '').toLowerCase();
                if (elType === 'submit' || elType === 'reset') continue;
                try {
                    el.click();
                    clickCount++;
                    expanded++;
                } catch (e) {
                    // Ignore individual click failures; we are best-effort
                }
            }

            // Only pay the settle wait when at least one JS click occurred.
            // <details> opens are synchronous attribute mutations; no wait needed for them.
            if (clickCount > 0) {
                await new Promise(r => setTimeout(r, 1200));
            }

            return expanded;
        }
        """
        count: int = await page.evaluate(js)
        if count > 0:
            LOGGER.info("Expanded %d disclosure region(s)", count)
        return count

    async def extract_links(
        self,
        url: str,
        wait_until: Literal["commit", "domcontentloaded", "load", "networkidle"] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> list[str]:
        """Extract all links from a rendered page.

        Args:
            url: URL to fetch and extract links from
            wait_until: Page load strategy. Options: commit, domcontentloaded (default),
                load, networkidle. Falls back to SUPACRAWL_WAIT_UNTIL env var if None.
            extra_headers: Custom HTTP headers to send with every request in this
                context. Only header KEYS are logged; values are never written to logs.

        Returns:
            List of absolute URLs found on the page

        Raises:
            RuntimeError: If browser not initialized or fetch fails
            BrowserUnavailableError: If the browser engine is dead and cannot be
                relaunched — a scraper-side fault, not a statement about the site.
            ValidationError: If the URL or its resolved address is blocked
                (#152); see :meth:`fetch_page` for the pre-flight-only caveat.
        """
        await self.ensure_started()

        # Pre-flight SSRF check (#152): see fetch_page's Raises note.
        # Off the event loop: resolve_and_pin does a blocking getaddrinfo.
        await asyncio.to_thread(resolve_and_pin, url)

        context: BrowserContext | None = None
        page: Page | None = None
        owns_context = self.engine != "camoufox"
        engine_in_use: Any = self._browser

        try:
            context, page = await self._open_page(owns_context=owns_context, extra_headers=extra_headers)
            engine_in_use = self._browser

            # Navigate to URL - use parameter if provided, else fall back to env var
            if wait_until is None:
                wait_until_env = os.getenv("SUPACRAWL_WAIT_UNTIL", "domcontentloaded")
                wait_until_resolved: Literal["commit", "domcontentloaded", "load", "networkidle"] = (
                    wait_until_env  # type: ignore[assignment]
                    if wait_until_env in ("commit", "domcontentloaded", "load", "networkidle")
                    else "domcontentloaded"
                )
            else:
                wait_until_resolved = wait_until
            await _install_navigation_guard(page)
            await page.goto(url, wait_until=wait_until_resolved, timeout=self.timeout_ms)

            # Extract all links using JavaScript
            links = await page.evaluate(
                """
                () => {
                    const anchors = Array.from(document.querySelectorAll('a[href]'));
                    return anchors.map(a => a.href).filter(href => href && href.startsWith('http'));
                }
            """
            )

            return links

        except Exception as e:
            relabelled = self._as_engine_failure(e, engine_in_use)
            if relabelled is e:
                raise
            raise relabelled from e

        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass
            if owns_context and context:
                try:
                    await context.close()
                except Exception:
                    pass

    async def extract_images(self, html: str, base_url: str) -> list[str]:
        """Extract all image URLs from HTML content.

        Extracts from ``<img>`` tags, ``<picture>``/``<source>`` tags, and
        CSS ``background-image`` declarations (both inline styles and
        ``<style>`` blocks).

        Args:
            html: HTML content to extract images from
            base_url: Base URL for resolving relative URLs

        Returns:
            List of absolute image URLs, deduplicated and sorted
        """
        import re
        from urllib.parse import urljoin

        soup = BeautifulSoup(html, "html.parser")
        images: set[str] = set()

        # Extract from <img> tags
        for img in soup.find_all("img"):
            # Get src attribute
            src = img.get("src")
            if src and isinstance(src, str) and not src.startswith("data:"):
                absolute_url = urljoin(base_url, src)
                images.add(absolute_url)

            # Get srcset attribute (responsive images)
            srcset = img.get("srcset")
            if srcset and isinstance(srcset, str):
                for part in srcset.split(","):
                    url = part.strip().split()[0]  # Take URL, ignore size descriptor
                    if url and not url.startswith("data:"):
                        absolute_url = urljoin(base_url, url)
                        images.add(absolute_url)

        # Extract from <picture> <source> tags
        for source in soup.find_all("source"):
            # Get srcset attribute
            srcset = source.get("srcset")
            if srcset and isinstance(srcset, str):
                for part in srcset.split(","):
                    url = part.strip().split()[0]
                    if url and not url.startswith("data:"):
                        absolute_url = urljoin(base_url, url)
                        images.add(absolute_url)

            # Get src attribute (some sources use src instead of srcset)
            src = source.get("src")
            if src and isinstance(src, str) and not src.startswith("data:"):
                absolute_url = urljoin(base_url, src)
                images.add(absolute_url)

        # Extract from CSS background-image declarations
        bg_url_pattern = re.compile(r"""background(?:-image)?\s*:[^;]*url\(\s*(['"]?)(.*?)\1\s*\)""")

        # Inline style attributes on elements
        for el in soup.find_all(style=True):
            style = el.get("style", "")
            if isinstance(style, str):
                for match in bg_url_pattern.finditer(style):
                    url = match.group(2).strip()
                    if url and not url.startswith("data:"):
                        images.add(urljoin(base_url, url))

        # <style> blocks
        for style_tag in soup.find_all("style"):
            if style_tag.string:
                for match in bg_url_pattern.finditer(style_tag.string):
                    url = match.group(2).strip()
                    if url and not url.startswith("data:"):
                        images.add(urljoin(base_url, url))

        # Filter out common tracking pixels and tiny images
        filtered_images = []
        for url in images:
            # Skip common tracking pixel patterns
            if any(pattern in url.lower() for pattern in ["1x1", "pixel", "tracking", "analytics"]):
                continue
            filtered_images.append(url)

        return sorted(filtered_images)

    @staticmethod
    def _extract_head_section(html: str) -> str:
        """Extract the head section from HTML for lightweight metadata parsing.

        Includes everything from document start through ``</head>``, which
        captures ``<html lang="...">`` and the full ``<head>`` block. Falls
        back to the full HTML if ``<head>`` boundaries are not found.
        """
        head_end = html.lower().find("</head>")
        if head_end == -1:
            return html
        return html[: head_end + len("</head>")]

    async def extract_metadata(self, html: str) -> PageMetadata:
        """Extract metadata from HTML.

        Only parses the ``<head>`` section for efficiency, since all metadata
        tags (title, meta, link, structured data) appear there.

        Args:
            html: HTML content

        Returns:
            PageMetadata with title, description, og tags, and other metadata
        """
        head_html = self._extract_head_section(html)
        soup = BeautifulSoup(head_html, "html.parser")

        def get_meta_content(name: str | None = None, property: str | None = None) -> str | None:
            """Helper to extract meta tag content."""
            if name:
                tag = soup.find("meta", attrs={"name": name})
            elif property:
                tag = soup.find("meta", attrs={"property": property})
            else:
                return None
            if not tag:
                return None
            content = tag.get("content", None)
            # Handle case where content could be a list
            if isinstance(content, list):
                return content[0] if content else None
            return content

        # Extract title
        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else None

        # Extract core metadata
        description = get_meta_content(name="description")
        keywords = get_meta_content(name="keywords")
        robots = get_meta_content(name="robots")

        # Extract language from html tag
        html_tag = soup.find("html")
        language_attr = html_tag.get("lang", None) if html_tag else None
        language = language_attr[0] if isinstance(language_attr, list) else language_attr

        # Extract canonical URL
        canonical_tag = soup.find("link", attrs={"rel": "canonical"})
        canonical_attr = canonical_tag.get("href", None) if canonical_tag else None
        canonical_url = canonical_attr[0] if isinstance(canonical_attr, list) else canonical_attr

        # Extract OpenGraph tags
        og_title = get_meta_content(property="og:title")
        og_description = get_meta_content(property="og:description")
        og_image = get_meta_content(property="og:image")
        og_url = get_meta_content(property="og:url")
        og_site_name = get_meta_content(property="og:site_name")

        # Extract Twitter Card tags
        twitter_title = get_meta_content(name="twitter:title")
        twitter_description = get_meta_content(name="twitter:description")
        twitter_image = get_meta_content(name="twitter:image")

        # Apply title fallback: <title> → og:title → twitter:title
        effective_title = title or og_title or twitter_title

        # Apply description fallback: <meta description> → og:description → twitter:description
        effective_description = description or og_description or twitter_description

        # Detect timezone from structured data
        detected_timezone = self._detect_timezone(soup)

        return PageMetadata(
            title=effective_title,
            description=effective_description,
            language=language,
            keywords=keywords,
            robots=robots,
            canonical_url=canonical_url,
            og_title=og_title,
            og_description=og_description,
            og_image=og_image or twitter_image,
            og_url=og_url,
            og_site_name=og_site_name,
            timezone=detected_timezone,
        )

    @staticmethod
    def _detect_timezone(soup: BeautifulSoup) -> str | None:
        """Detect timezone from page structured data.

        Checks JSON-LD structured data and meta tags for IANA timezone identifiers.

        Args:
            soup: Parsed HTML

        Returns:
            IANA timezone string (e.g. "America/New_York") or None
        """
        # Pattern for IANA timezone identifiers (e.g. America/New_York, Europe/London)
        iana_tz_pattern = re.compile(
            r"\b(Africa|America|Antarctica|Arctic|Asia|Atlantic|Australia|Europe|Indian|Pacific)"
            r"/[A-Z][a-zA-Z_]+(?:/[A-Z][a-zA-Z_]+)?\b"
        )

        # 1. Check JSON-LD structured data for timezone fields
        for script_tag in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                text = script_tag.get_text(strip=True)
                if not text:
                    continue
                data = json.loads(text)
                tz = _extract_timezone_from_jsonld(data, iana_tz_pattern)
                if tz:
                    return tz
            except json.JSONDecodeError, TypeError:
                continue

        # 2. Check meta tags for timezone info
        for tag in soup.find_all("meta"):
            name = str(tag.get("name") or tag.get("property") or "").lower()
            content = tag.get("content", "")
            if isinstance(content, list):
                content = content[0] if content else ""
            if not content:
                continue

            # Check for timezone-related meta names
            if "timezone" in name or "tz" == name:
                match = iana_tz_pattern.search(content)
                if match:
                    return match.group(0)
                # Accept raw value if it looks like an IANA timezone
                if "/" in content and len(content) < 40:
                    return content.strip()

        return None

    async def _wait_for_spa_stability(
        self,
        page: Page,
        timeout_ms: int = 5000,
    ) -> None:
        """Wait for SPA content to stop changing.

        Checks DOM content hash every 200ms, considers stable after
        3 consecutive identical hashes.

        Args:
            page: Playwright page instance
            timeout_ms: Maximum wait time in milliseconds
        """
        start_time = asyncio.get_event_loop().time()
        max_wait = timeout_ms / 1000

        # Wait for at least one heading to appear
        try:
            await page.wait_for_selector("h1, h2, main, article", timeout=timeout_ms)
        except Exception:
            # If no heading found, continue anyway
            pass

        # Wait for content stability (DOM not changing)
        last_content_hash = ""
        stable_count = 0
        required_stable = 3  # Need 3 consecutive stable checks (600ms total)

        while asyncio.get_event_loop().time() - start_time < max_wait:
            try:
                # Get current page content hash
                content = await page.content()
                current_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]

                if current_hash == last_content_hash:
                    stable_count += 1
                    if stable_count >= required_stable:
                        LOGGER.debug(f"SPA content stable after {stable_count} checks")
                        return
                else:
                    stable_count = 0
                    last_content_hash = current_hash

                await asyncio.sleep(0.2)  # Check every 200ms
            except Exception as e:
                LOGGER.warning(f"Error checking content stability: {e}")
                break

        LOGGER.debug("SPA content wait timed out, proceeding anyway")
