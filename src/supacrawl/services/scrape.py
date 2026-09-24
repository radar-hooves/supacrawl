"""Scrape service for single URL content extraction.

Anti-Bot Features (automatic, no configuration needed):
    - Basic fingerprint evasion: navigator.webdriver=false, chrome runtime objects,
      plugins array, languages array, WebGL vendor spoofing, canvas noise
    - Standard browser headers: Accept-Language, Sec-Fetch-*, etc.
    - Automatic bot detection: Detects 403/429/503, CAPTCHA pages, Cloudflare challenges

Enhanced Stealth (optional, for heavily protected sites):
    - Install: pip install supacrawl[stealth]
    - Auto-retry: If bot detection is suspected, automatically retries with Patchright
    - Force stealth: Use stealth=True or --stealth flag

CAPTCHA Solving (optional, requires third-party service):
    - Install: pip install supacrawl[captcha]
    - Configure: export CAPTCHA_API_KEY=your-2captcha-api-key
    - Usage: Use solve_captcha=True or --solve-captcha flag
    - Supports: reCAPTCHA v2/v3, hCaptcha, Cloudflare Turnstile
    - COST WARNING: ~$2-3 per 1000 solves
"""

import base64
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

if TYPE_CHECKING:
    from supacrawl.services.strategy_memory import StrategyChoice, StrategyStore
    from supacrawl.telemetry import MetricsSink

from bs4 import BeautifulSoup

from supacrawl.cache import CacheManager
from supacrawl.exceptions import ProviderError, ValidationError, generate_correlation_id
from supacrawl.models import (
    ActionsOutput,
    QualityAssessment,
    QualityVerdict,
    ScrapeActionResult,
    ScrapeData,
    ScrapeMetadata,
    ScrapeResult,
)
from supacrawl.quality import assess_quality
from supacrawl.services.browser import (
    BrowserManager,
    BrowserUnavailableError,
    CamoufoxNotAvailableError,
    PageContent,
    PageMetadata,
    StealthNotAvailableError,
    engine_availability,
)
from supacrawl.services.converter import MarkdownConverter
from supacrawl.services.detection import (
    detect_bot_protection,
    detect_collapsed_disclosures,
    estimate_js_requirement,
)
from supacrawl.services.http_fetch import fetch_static
from supacrawl.services.platform import detect_platform
from supacrawl.services.remediation import remediation_hint, thin_content_hint
from supacrawl.services.structured_data import extract_structured_data

LOGGER = logging.getLogger(__name__)

# Type alias for wait_until options
type WaitUntilType = Literal["commit", "domcontentloaded", "load", "networkidle"]

# Patterns that indicate bot detection or blocking
BOT_DETECTION_PATTERNS = [
    r"captcha",
    r"challenge",
    r"cloudflare",
    r"ddos.protection",
    r"access.denied",
    r"blocked",
    r"robot",
    r"bot.detection",
    r"verify.you.are.human",
    r"please.wait",
    r"checking.your.browser",
    r"just.a.moment",
    r"enable.javascript",
    r"ray.id",  # Cloudflare Ray ID
]
BOT_DETECTION_REGEX = re.compile("|".join(BOT_DETECTION_PATTERNS), re.IGNORECASE)

# Content-quality thresholds for binary/garbage detection.
# The prefix sampled for non-printable ratio analysis (bytes).
_CONTENT_QUALITY_SAMPLE_BYTES = 8192
# Non-printable ratio above this → HARD failure (binary/encrypted body).
_NON_PRINTABLE_RATIO_THRESHOLD = 0.30
# HTML structure tags; absence in a non-trivial body → SOFT suspect.
_HTML_STRUCTURE_TAGS = ("<html", "<head", "<body", "<div", "<p", "<a")
# Minimum HTML length (chars) before the structure / density checks apply.
_MIN_HTML_LENGTH_FOR_QUALITY_CHECK = 200
# Words-per-KB ratio below this (combined with substantial HTML) → SOFT suspect.
# Kept deliberately low: legitimate markup-heavy articles run 3-50+ words/KB, so
# only near-empty or garbled pages (well under 1 word/KB) are flagged.
_LOW_DENSITY_WORDS_PER_KB = 1.0

# --- Adaptive auto-escalation (#129) ---------------------------------------
# Verdicts where a stronger fetch (stealth engine, longer hydration wait) could
# plausibly recover real content. A genuine 404 (ERROR_STATUS), a paywall, or a
# merely-short page are NOT here: escalating them only burns latency.
#
# CAPTCHA is deliberately absent (#153): a bare third-party CAPTCHA wall
# (reCAPTCHA/hCaptcha/standalone Turnstile) is a terminal condition for the
# engine ladder — a stronger stealth engine does not solve a CAPTCHA, so the
# three further escalations are pure wasted wall-clock. Fail fast on it and hand
# the caller the CAPTCHA suggestion (enable solve_captcha, or fetch elsewhere)
# after a single attempt. A CAPTCHA widget wrapped in a CDN "managed challenge"
# interstitial (Cloudflare "just a moment") is a stealth engine's job and is
# classified BOT_CHALLENGE instead (see quality._classify), so it keeps
# escalating. SUPACRAWL_CAPTCHA_FAIL_FAST=0 restores the walk-the-whole-ladder
# behaviour (see _captcha_fail_fast).
#
# TARPIT is likewise deliberately absent: a Nepenthes/iocaine-style crawler
# tarpit serves a fresh page of generated content and generated links on every
# fetch, so a stronger stealth engine or a longer wait_for just buys another
# maze page at higher cost. Fail fast on it the same way as CAPTCHA.
_ESCALATABLE_VERDICTS: frozenset[QualityVerdict] = frozenset(
    {
        QualityVerdict.BOT_CHALLENGE,
        QualityVerdict.JS_SHELL,
        QualityVerdict.EMPTY,
    }
)


def _captcha_fail_fast() -> bool:
    """Whether a genuine CAPTCHA verdict should stop the ladder after one attempt.

    Default True (#153): escalating a CAPTCHA wall through the stealth/engine
    ladder wastes three doomed attempts because no stronger engine solves a
    CAPTCHA. Set ``SUPACRAWL_CAPTCHA_FAIL_FAST=0`` (or ``false``/``no``) to keep
    CAPTCHA escalatable — the override that keeps this heuristic falsifiable: a
    caller who believes a CAPTCHA-classified page would in fact yield to a
    stronger engine can turn the short-circuit off and see the full ladder run.
    """
    return os.environ.get("SUPACRAWL_CAPTCHA_FAIL_FAST", "1").strip().lower() not in ("0", "false", "no")


# Maximum number of *extra* attempts beyond the first (the escalation budget).
# The default ladder is playwright → patchright → camoufox → camoufox+HTTP/1.1,
# so three escalations cover the full ladder.
_MAX_ESCALATIONS = 3
# Hydration wait (ms) injected on every escalated attempt: a stronger engine
# usually also needs time for client-side content to render.
_ESCALATION_WAIT_MS = 5000
# only_main_content recovery: when the main-content selector yields fewer than
# this many words, re-extract the full page and prefer it if it is this many
# times richer (and above the floor) — the selector likely matched a tiny wrapper.
_THIN_MAIN_FLOOR = 50
_THIN_FALLBACK_RATIO = 3


def _looks_like_bot_block(status_code: int, html: str, markdown: str | None) -> bool:
    """Detect if a response looks like bot detection or blocking.

    Args:
        status_code: HTTP status code
        html: Raw HTML content
        markdown: Converted markdown content

    Returns:
        True if bot detection is suspected
    """
    # Check status codes that indicate blocking
    if status_code in (403, 429, 503):
        LOGGER.debug(f"Bot detection suspected: HTTP {status_code}")
        return True

    # Check for very short content (likely a challenge page)
    content_length = len(html) if html else 0
    if content_length < 500:
        # Very short page - might be a redirect or challenge
        if BOT_DETECTION_REGEX.search(html):
            LOGGER.debug("Bot detection suspected: short page with blocking patterns")
            return True

    # Check for bot detection patterns in HTML
    if BOT_DETECTION_REGEX.search(html):
        # Also check if content is suspiciously short for a real page
        word_count = len(markdown.split()) if markdown else 0
        if word_count < 50:
            LOGGER.debug("Bot detection suspected: blocking patterns with low word count")
            return True

    return False


def _assess_content_quality(html: str, markdown: str | None) -> str | None:
    """Assess whether scraped content looks like a usable page or a bot challenge.

    Returns a human-readable reason string when content is suspect, else None.
    Severity is encoded in the prefix of the returned string:
        "HARD:" — binary/garbage content; caller should set success=False.
        "SOFT:" — printable but very low density; caller should add a warning.

    Args:
        html: Raw HTML content from the response.
        markdown: Converted markdown content, or None.

    Returns:
        A reason string prefixed with "HARD:" or "SOFT:", or None when content
        passes all quality checks.
    """
    if not html:
        return None

    html_len = len(html)
    if html_len < _MIN_HTML_LENGTH_FOR_QUALITY_CHECK:
        # Too short to draw meaningful conclusions; rely on _looks_like_bot_block.
        return None

    # --- HARD check: non-printable / binary character ratio ---
    # Sample a prefix to avoid iterating multi-megabyte payloads.
    sample = html[:_CONTENT_QUALITY_SAMPLE_BYTES]
    non_printable = sum(1 for ch in sample if ord(ch) < 32 and ch not in "\t\n\r")
    ratio = non_printable / len(sample)
    if ratio > _NON_PRINTABLE_RATIO_THRESHOLD:
        pct = int(ratio * 100)
        return (
            f"HARD: Response appears to be a bot challenge or non-text content "
            f"({pct}% non-printable characters); page could not be read as text."
        )

    # --- SOFT check: absent HTML structure ---
    html_lower = html[:4096].lower()
    has_structure = any(tag in html_lower for tag in _HTML_STRUCTURE_TAGS)
    if not has_structure:
        return (
            "SOFT: Response may be a bot challenge page; content quality is suspect "
            "(no recognisable HTML structure detected)."
        )

    # --- SOFT check: very low word-to-byte ratio ---
    word_count = len(markdown.split()) if markdown else 0
    words_per_kb = (word_count / html_len) * 1024
    if words_per_kb < _LOW_DENSITY_WORDS_PER_KB:
        return "SOFT: Response may be a bot challenge page; content quality is suspect (low text density)."

    return None


def _quality_error(quality: QualityAssessment) -> str | None:
    """Build an honest error string for a hard-fail quality verdict, else None.

    When the runtime quality assessment flips ``success`` to False (HTTP >= 400
    soft-404, bot challenge, CAPTCHA, garbled PDF, empty), the result needs a
    human/agent-readable reason so the caller is not handed ``error=None``. A
    usable verdict returns None — success is reported normally.

    Args:
        quality: The computed quality assessment for the result.

    Returns:
        An actionable error string, or None when the result is usable.
    """
    if quality.is_usable:
        return None
    detail = "; ".join(quality.reasons) or quality.verdict.value
    message = f"Scrape returned no usable content ({quality.verdict.value}): {detail}."
    if quality.suggestion:
        message += f" {quality.suggestion}"
    return message


def _engine_available(engine: str | None) -> bool:
    """Whether an engine can actually be used for an escalation choice (#143).

    Guards a platform short-circuit or a learned strategy-memory champion from
    picking a stealth engine that cannot launch — which would otherwise turn a
    scrape into a clear-but-avoidable infrastructure failure instead of degrading
    to the engine that IS present. Uses the same binary-aware check that
    supacrawl_diagnose reports (#144): a package installed but with its browser
    binary not fetched (a bare ``uv sync`` / no ``patchright install`` /
    ``camoufox fetch``) is NOT usable, and must be skipped just like a missing
    package — otherwise the launch fails with a raw "executable not found" that
    is not one of the typed engine errors and misclassifies as EMPTY.

    ``playwright`` is the base fallback and is never gated here (the ladder never
    skips its own floor); its own binary health surfaces via diagnose and, on a
    real launch, the lazy-start/relaunch path.
    """
    if engine in (None, "playwright"):
        return True
    if engine in ("patchright", "camoufox"):
        return bool(engine_availability(engine)["available"])
    return False


def _engine_remedy_clause(engine: str) -> str:
    """The next step for an engine that cannot launch, or "" when it can.

    Reuses :func:`engine_availability`'s own remedy so a hint never contradicts
    what ``supacrawl diagnose`` reports: a missing package is answered with pip,
    a package whose browser binary was never fetched with the fetch command.
    """
    state = engine_availability(engine)
    if state["available"]:
        return ""
    return f" To enable {engine}: {state['install']}."


def _resolve_engine_stealth(engine: str | None, stealth: bool) -> tuple[str, bool]:
    """Resolve a (engine, stealth) request to what a BrowserManager will run.

    Mirrors ``BrowserManager.__init__`` exactly: an explicit engine wins; else
    ``stealth`` selects patchright; else playwright. ``stealth`` is then a pure
    function of the resolved engine (True for patchright/camoufox).

    This matters because strategy memory stores a patchright champion as
    ``(None, True)`` and a camoufox champion as ``("camoufox", False)`` — neither
    of which equals the ``(engine, stealth)`` the resulting browser reports
    (``("patchright", True)`` / ``("camoufox", True)``). Comparing a raw seed
    against a live browser's resolved attributes therefore always mismatches,
    which would rebuild a temporary browser on every page (#139). Resolve both
    sides into the same space before comparing.
    """
    if engine is not None:
        resolved = engine
    elif stealth:
        resolved = "patchright"
    else:
        resolved = "playwright"
    return resolved, resolved in ("patchright", "camoufox")


def _stealth_hint(*, bot_suspected: bool = False) -> str:
    """Return a hint about stealth mode, gated on whether a bot challenge was detected.

    When bot_suspected is False (pure network/timeout failure with no challenge
    signature), engine switching is unlikely to help, so a softer note is returned
    rather than promising that --engine patchright will fix the problem.

    Args:
        bot_suspected: True when _looks_like_bot_block or _assess_content_quality
            flagged a bot challenge for this request.

    Returns:
        A structured hint string for humans and LLM consumers.
    """
    if not bot_suspected:
        return (
            " [Note: some sites block all automated access regardless of engine; "
            "switching engines is unlikely to help for a pure network or timeout error.]"
        )

    # Suggest only engines that can actually launch. An engine whose package is
    # installed but whose browser binary was never fetched needs the fetch
    # command, not a pip install it has already run.
    if _engine_available("patchright"):
        hint = (
            " [HINT: Basic anti-bot evasion is already active. "
            "For enhanced stealth, use --stealth flag or --engine patchright."
        )
        if _engine_available("camoufox"):
            hint += " For Akamai/advanced protection, use --engine camoufox."
        else:
            hint += _engine_remedy_clause("camoufox")
        hint += "]"
        return hint
    elif _engine_available("camoufox"):
        return " [HINT: Basic anti-bot evasion is active. For Akamai/advanced protection, use --engine camoufox]"
    else:
        return (
            " [HINT: Basic anti-bot evasion is active but site may need enhanced stealth."
            f"{_engine_remedy_clause('patchright')}{_engine_remedy_clause('camoufox')}]"
        )


def _is_captcha_available() -> bool:
    """Check if 2captcha-python is installed for CAPTCHA solving."""
    try:
        import twocaptcha  # noqa: F401

        return True
    except ImportError:
        return False


def _captcha_hint() -> str:
    """Return a hint about CAPTCHA solving based on availability.

    Returns a structured hint for both humans and LLM consumers.
    """
    if _is_captcha_available():
        import os

        if os.environ.get("CAPTCHA_API_KEY"):
            return (
                " [HINT: CAPTCHA detected. Use --solve-captcha flag to auto-solve. "
                "WARNING: Each solve costs ~$0.002-0.003]"
            )
        else:
            return (
                " [HINT: CAPTCHA detected. CAPTCHA solving is installed but not configured. "
                "Set CAPTCHA_API_KEY environment variable, then use --solve-captcha]"
            )
    else:
        return (
            " [HINT: CAPTCHA detected. To auto-solve CAPTCHAs: "
            "1) pip install supacrawl[captcha]"
            "2) export CAPTCHA_API_KEY=your-2captcha-api-key "
            "3) use --solve-captcha flag. WARNING: Each solve costs ~$0.002-0.003]"
        )


def _compute_headers_hash(headers: dict[str, str]) -> str:
    """Compute a short SHA-256 hash of a headers dict for cache variant discrimination.

    The hash is over the sorted (key, value) pairs so that header order does not
    affect the variant key.  Only the hash is used externally — raw values are never
    persisted or logged.

    Args:
        headers: Dict of header names to values.

    Returns:
        12-character hex prefix of the SHA-256 digest.
    """
    import hashlib

    payload = "&".join(f"{k}={v}" for k, v in sorted(headers.items()))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _compute_content_hash(markdown: str | None) -> str:
    """Compute SHA256 hash of markdown content for change tracking.

    Args:
        markdown: Markdown content to hash. None or empty string produces
            a hash of the empty string.

    Returns:
        Hex-encoded SHA256 hash string.
    """
    import hashlib

    content = (markdown or "").encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def _build_change_tracking(
    previous_entry: Any | None,
    current_hash: str,
    current_markdown: str | None,
    change_tracking_modes: list[str] | None = None,
) -> Any:
    """Build change tracking data by comparing current scrape to cached previous.

    Args:
        previous_entry: Previous CacheEntry (from get_previous), or None.
        current_hash: SHA256 hash of current markdown content.
        current_markdown: Current markdown content (for diff generation).
        change_tracking_modes: Optional list of diff modes (e.g. ["git-diff"]).

    Returns:
        ChangeTrackingData with change status and optional diff.
    """
    from supacrawl.models import ChangeTrackingData

    if previous_entry is None:
        return ChangeTrackingData(
            change_status="new",
            content_hash=current_hash,
        )

    previous_hash = previous_entry.content_hash
    previous_scrape_at = previous_entry.cached_at

    # Fast path: hash comparison
    if previous_hash is not None and previous_hash == current_hash:
        return ChangeTrackingData(
            previous_scrape_at=previous_scrape_at,
            change_status="same",
            content_hash=current_hash,
        )

    # Content has changed (or no previous hash to compare — treat as changed)
    diff = None
    if change_tracking_modes and "git-diff" in change_tracking_modes:
        diff = _generate_unified_diff(previous_entry, current_markdown)

    return ChangeTrackingData(
        previous_scrape_at=previous_scrape_at,
        change_status="changed",
        content_hash=current_hash,
        diff=diff,
    )


def _generate_unified_diff(
    previous_entry: Any,
    current_markdown: str | None,
) -> Any:
    """Generate a unified diff between previous and current markdown.

    Args:
        previous_entry: Previous CacheEntry containing the cached response.
        current_markdown: Current markdown content.

    Returns:
        ChangeTrackingDiff with unified diff text, or None if unable to diff.
    """
    import difflib

    from supacrawl.models import ChangeTrackingDiff

    # Extract previous markdown from cached response
    prev_markdown = ""
    try:
        prev_data = previous_entry.response.get("data", {})
        prev_markdown = prev_data.get("markdown") or ""
    except AttributeError, TypeError:
        return None

    curr_markdown = current_markdown or ""

    prev_lines = prev_markdown.splitlines(keepends=True)
    curr_lines = curr_markdown.splitlines(keepends=True)

    diff_lines = list(
        difflib.unified_diff(
            prev_lines,
            curr_lines,
            fromfile="previous",
            tofile="current",
        )
    )

    if not diff_lines:
        return None

    return ChangeTrackingDiff(text="".join(diff_lines))


class _Rung(NamedTuple):
    """One escalation rung: the strategy deltas for the next attempt.

    ``None`` fields mean "keep the caller's value"; non-None fields override it.
    """

    engine: str | None
    stealth: bool
    prefs: dict[str, Any] | None
    wait_for: int
    only_main_content: bool | None
    expand_iframes: str | None
    actions: list[Any] | None
    label: str


def _next_escalation(
    *,
    engine: str | None,
    stealth: bool,
    prefs: dict[str, Any] | None,
    pinned: bool,
    http2_error: bool,
    platform: Any | None,
) -> _Rung | None:
    """Compute the next-stronger strategy, or None when the ladder is exhausted.

    Default ladder (no engine pinned): playwright → patchright (stealth) →
    camoufox → camoufox with HTTP/1.1. A pinned engine is respected — only the
    HTTP/2 TLS fallback may switch it, since a TLS rejection means the pinned
    engine literally cannot connect. A detected site-builder platform short-
    circuits straight to its tuned engine.

    Args:
        engine: The engine the just-finished attempt used (None == playwright).
        stealth: Whether that attempt ran with stealth (== Patchright on Chromium).
        prefs: Firefox prefs that attempt used (set means HTTP/1.1 already forced).
        pinned: Whether the caller pinned the engine (respected outside HTTP/2).
        http2_error: Whether the attempt failed with an HTTP/2 protocol error.
        platform: A detected site-builder platform descriptor, or None.

    Returns:
        The next :class:`_Rung`, or None when nothing stronger is installed/left.
    """
    # The binary-aware gate, not the import-only check: a package that is
    # installed but whose browser binary was never fetched cannot launch, so
    # choosing it spends an escalation rung on a guaranteed failure (#143/#144).
    camoufox = _engine_available("camoufox")
    patchright = _engine_available("patchright")

    # HTTP/2 TLS rejection: jump to Camoufox (Firefox stack), then HTTP/1.1.
    if http2_error and camoufox:
        if engine != "camoufox":
            return _Rung("camoufox", stealth, None, _ESCALATION_WAIT_MS, None, None, None, "camoufox (HTTP/2 fallback)")
        if not prefs:
            return _Rung(
                "camoufox",
                stealth,
                {"network.http.http2.enabled": False},
                _ESCALATION_WAIT_MS,
                None,
                None,
                None,
                "camoufox + HTTP/1.1",
            )
        return None

    # A pinned engine is a deliberate choice; do not switch it on the generic ladder.
    if pinned:
        return None

    # A known site builder: escalate straight to its tuned engine/settings — but
    # only if that engine is actually installed (#143). A tuned engine that is
    # missing must not be chosen: doing so would fail the scrape on an
    # infrastructure error instead of degrading to the generic ladder below.
    if (
        platform is not None
        and platform.engine
        and platform.engine != (engine or "playwright")
        and _engine_available(platform.engine)
    ):
        return _Rung(
            platform.engine,
            stealth,
            None,
            platform.wait_for if platform.wait_for is not None else _ESCALATION_WAIT_MS,
            platform.only_main_content,
            platform.expand_iframes,
            platform.actions,
            f"platform:{platform.name}",
        )

    # Generic stealth/engine ladder.
    if engine in (None, "playwright") and not stealth:
        if patchright:
            return _Rung(None, True, None, _ESCALATION_WAIT_MS, None, None, None, "patchright (stealth)")
        if camoufox:
            return _Rung("camoufox", False, None, _ESCALATION_WAIT_MS, None, None, None, "camoufox")
        return None
    if engine in (None, "playwright") and stealth:
        # Already on Patchright (stealth Chromium); the next rung is Camoufox.
        if camoufox:
            return _Rung("camoufox", False, None, _ESCALATION_WAIT_MS, None, None, None, "camoufox")
        return None
    if engine == "camoufox" and not prefs:
        return _Rung(
            "camoufox",
            stealth,
            {"network.http.http2.enabled": False},
            _ESCALATION_WAIT_MS,
            None,
            None,
            None,
            "camoufox + HTTP/1.1",
        )
    return None


class ScrapeService:
    """Scrape a single URL and extract content.

    IMPORTANT FOR LLM/AGENT USERS:
        This service has built-in anti-bot protection that works automatically.
        You do NOT need to configure anything for basic scraping.

    What Works Automatically (no configuration):
        - Fingerprint evasion (webdriver, plugins, WebGL, canvas)
        - Standard browser headers (Accept-Language, Sec-Fetch-*, etc.)
        - Bot detection (403/429/503, CAPTCHA, Cloudflare challenges)
        - Auto-retry with enhanced stealth if patchright is installed

    When You Might Need Enhanced Stealth:
        - Error message contains "[HINT:" with stealth suggestion
        - Getting empty content from sites that should have content
        - Seeing "Access Denied", "Please verify you are human", etc.
        - Solution: Install supacrawl[stealth] and use stealth=True

    When You Might Need CAPTCHA Solving:
        - Error message contains "[HINT: CAPTCHA detected"
        - Site requires human verification that cannot be bypassed
        - Solution: Install supacrawl[captcha], set CAPTCHA_API_KEY, use solve_captcha=True
        - WARNING: CAPTCHA solving costs money (~$0.002-0.003 per solve)

    Usage:
        # Basic scraping - anti-bot evasion is automatic
        service = ScrapeService()
        result = await service.scrape("https://example.com")
        print(result.data.markdown)

        # With caching (recommended for repeated requests)
        service = ScrapeService(cache_dir=Path("~/.supacrawl/cache"))
        result = await service.scrape("https://example.com", max_age=3600)

        # Force enhanced stealth (for heavily protected sites)
        # Requires: pip install supacrawl[stealth]
        service = ScrapeService(stealth=True)
        result = await service.scrape("https://protected-site.com")

        # With CAPTCHA solving (for sites with mandatory verification)
        # Requires: pip install supacrawl[captcha] and CAPTCHA_API_KEY env var
        service = ScrapeService(stealth=True, solve_captcha=True)
        result = await service.scrape("https://captcha-protected-site.com")

    Returns:
        ScrapeResult with success=True/False, data (ScrapeData), and error message.
        Check result.success before accessing result.data.
    """

    def __init__(
        self,
        browser: BrowserManager | None = None,
        converter: MarkdownConverter | None = None,
        locale_config: Any | None = None,  # LocaleConfig, avoid circular import
        cache_dir: Path | None = None,
        stealth: bool = False,
        proxy: str | None = None,
        solve_captcha: bool = False,
        headless: bool | None = None,
        engine: str | None = None,
        firefox_user_prefs: dict[str, Any] | None = None,
        strategy_store: "StrategyStore | None" = None,
        telemetry: "MetricsSink | None" = None,
    ):
        """Initialize scrape service.

        Args:
            browser: Optional BrowserManager (created if not provided)
            converter: Optional MarkdownConverter (created if not provided)
            locale_config: Optional LocaleConfig for browser locale/timezone settings
            cache_dir: Optional cache directory (enables caching if provided)
            stealth: Enable stealth mode via Patchright for anti-bot evasion.
                Ignored when engine is explicitly set.
            proxy: Proxy URL (e.g., http://user:pass@host:port, socks5://host:port)
            solve_captcha: Enable CAPTCHA solving via 2Captcha (requires pip install supacrawl[captcha]
                          and CAPTCHA_API_KEY environment variable). WARNING: Each solve costs ~$0.002-0.003.
            headless: Run browser in headless mode. Passed through to any BrowserManager
                instances created internally (e.g. for CAPTCHA solving or standalone usage).
            engine: Browser engine to use ("playwright", "patchright", "camoufox").
                Overrides the stealth flag when set.
            firefox_user_prefs: Firefox about:config preferences for Camoufox.
                Only used when engine="camoufox" and browser is created internally.
                Example: {"network.http.http2.enabled": False} to force HTTP/1.1.
            strategy_store: Optional per-domain strategy memory (#130). When
                provided, a successful strategy for a domain seeds the next hit to
                that domain (short-circuiting the escalation ladder) and the
                outcome of each attempt is recorded back. None == stateless (the
                CLI and MCP wiring enable it by default; library callers opt in).
            telemetry: Optional field telemetry sink (#137). When provided, one
                event per top-level scrape (verdict, score, attempts, escalated,
                latency) is appended to the local metrics log for observability
                over time. None == no telemetry (enabled by default at the CLI and
                MCP boundaries; opt-out via SUPACRAWL_METRICS=0).
        """
        self._browser = browser
        self._converter = converter or MarkdownConverter()
        self._owns_browser = browser is None
        self._locale_config = locale_config
        self._stealth = stealth
        self._proxy = proxy
        self._solve_captcha = solve_captcha
        self._headless = headless
        self._engine = engine
        self._firefox_user_prefs = firefox_user_prefs
        self._cache = CacheManager(cache_dir) if cache_dir else None
        self._strategy_store = strategy_store
        self._telemetry = telemetry
        self._captcha_solver: Any = None  # Lazy-loaded CaptchaSolver

    async def close(self) -> None:
        """Close the scrape service.

        BrowserManager instances are created and torn down per-request inside
        scrape(), so there are no held resources to release here. This method
        exists to satisfy the uniform service lifecycle expected by callers.
        """

    async def scrape(
        self,
        url: str,
        formats: list[
            Literal[
                "markdown",
                "html",
                "rawHtml",
                "links",
                "screenshot",
                "pdf",
                "json",
                "images",
                "branding",
                "structuredData",
                "summary",
                "changeTracking",
            ]
        ]
        | None = None,
        only_main_content: bool = True,
        wait_for: int = 0,
        timeout: int = 30000,
        screenshot_full_page: bool = True,
        actions: list[Any] | None = None,
        json_schema: dict[str, Any] | None = None,
        json_prompt: str | None = None,
        include_tags: list[str] | None = None,
        exclude_tags: list[str] | None = None,
        max_age: int = 0,
        wait_until: WaitUntilType | None = None,
        change_tracking_modes: list[str] | None = None,
        expand_iframes: Literal["none", "same-origin", "all"] = "same-origin",
        expand_disclosures: bool = True,
        device: str | None = None,
        parse_pdf: Literal["fast", "auto", "ocr"] | None = "auto",
        engine: str | None = None,
        proxy: str | None = None,
        headers: dict[str, str] | None = None,
        content_mode: float = 0.5,
        query: str | None = None,
        http_first: bool = True,
        expect: str | None = None,
        escalate: bool = True,
        _escalation_level: int = 0,
    ) -> ScrapeResult:
        """Scrape a URL and return content.

        Args:
            url: URL to scrape
            formats: Content formats to return (default: ["markdown"])
                     Supports: markdown, html, rawHtml, links, screenshot, pdf,
                     json, images, branding, summary, changeTracking
            only_main_content: Extract main content area only
            wait_for: Additional wait time in ms after page load. When > 0,
                     also enables SPA stability polling (DOM hash checking).
            timeout: Page load timeout in ms
            screenshot_full_page: Capture full scrollable page for screenshots
            actions: List of Action objects to execute before capturing content
                     Supports: wait, click, type, scroll, screenshot, press, executeJavascript
            json_schema: JSON schema for structured extraction (for json format)
            json_prompt: Custom prompt for extraction (for json format)
            include_tags: CSS selectors for elements to include.
                         When specified, takes precedence over only_main_content.
            exclude_tags: CSS selectors for elements to exclude.
                         Applied before include_tags filtering.
            max_age: Cache freshness in seconds. 0 = no cache.
                    Returns cached content if available and fresh.
            wait_until: Page load strategy. Options: commit, domcontentloaded (default),
                       load, networkidle. Falls back to SUPACRAWL_WAIT_UNTIL env var if None.
            change_tracking_modes: Optional diff modes for change tracking.
                    Supports: ["git-diff"]. Only used when "changeTracking" is in formats.
            expand_iframes: Iframe expansion mode. "none" strips all (legacy),
                    "same-origin" expands same-origin iframes inline (default),
                    "all" expands all non-blocked iframes including cross-origin.
            expand_disclosures: When True (default), expand collapsed disclosure
                    regions (aria-expanded="false" content controls, closed <details>)
                    in the browser before capturing HTML. Navigation chrome is excluded.
                    Set False to opt out.
            device: Playwright device name for mobile emulation (e.g. "iPhone 14",
                    "Pixel 7"). Sets viewport, user agent, device scale factor, and
                    touch support. Use ``--mobile`` as a shortcut for a default device.
            parse_pdf: PDF parsing mode. "auto" (default) detects PDF URLs by
                    file extension (.pdf) and extracts text, falling back to OCR
                    if available. "fast" uses text extraction only. "ocr" forces
                    OCR. None disables PDF parsing entirely.
            engine: Browser engine override for this request. When set and different
                    from the service's default engine, a temporary browser is created.
                    Options: "playwright", "patchright", "camoufox".
            proxy: Proxy URL override for this request (e.g.
                    "http://user:pass@host:port", "socks5://host:port"). When set and
                    different from the shared browser's proxy, a temporary browser is
                    created so the override is honoured without mutating the shared
                    browser. Overrides the service-level proxy for this request only.
            headers: Custom HTTP headers to send with every request (e.g.
                    Authorization, Cookie, X-Api-Key). These are sent by the browser
                    context for all requests on the page including sub-resources.
                    Only header KEYS are logged; values are never written to logs or
                    persisted in cache entries. A hash of the headers is included in
                    the cache variant so auth'd and anonymous fetches of the same URL
                    are stored separately.
            content_mode: Precision/recall dial in [0.0, 1.0] for the content
                    extraction cascade. Low values favour recall (accept more, prune
                    less); high values favour precision (demand denser output, prune
                    more aggressively). Default 0.5.
            query: Optional free-text query. When set, the extraction cascade filters
                    sections by relevance to the query using BM25. Flat pages (single
                    section, no headings) are not filtered so no content is lost.
            http_first: Try a cheap httpx GET before launching a browser (default
                    True). When the fetched HTML needs no JavaScript and shows no
                    bot-challenge signal, the result is built from it directly,
                    skipping Playwright entirely. Any render-needed or bot signal —
                    or a browser-only request (screenshot/pdf/actions/device/stealth/
                    non-default engine) — falls through to the full browser path.
            expect: Optional content assertion that makes hydration a retryable
                    condition. A bare integer is a minimum word count; any other
                    string is matched first as a CSS selector and then as a
                    visible-text substring. When the assertion is unmet the HTTP-first
                    path escalates to the browser, the browser waits for a
                    selector-shaped expectation, and an unmet assertion after a
                    stealth + longer-wait retry returns success=False rather than a
                    pre-hydration skeleton.
            escalate: Adaptive auto-escalation (default True). On a poor quality
                    verdict (block/CAPTCHA/JS-shell/empty) or an HTTP/2 TLS
                    rejection, supacrawl automatically walks the stealth/engine
                    ladder (Patchright → Camoufox → Camoufox+HTTP/1.1) with a
                    longer hydration wait, within a bounded budget, and keeps the
                    best-scoring attempt — no per-site options required. Set False
                    to take a single cheap attempt (cost/latency control).
            _escalation_level: Internal recursion depth for the escalation ladder;
                    callers leave this at 0.

        Returns:
            ScrapeResult with scraped content
        """
        formats = formats or ["markdown"]

        # A "json" request with neither schema nor prompt has nothing to tell the
        # LLM to pull out, so it is refused here, before any fetch, rather than
        # left to fail quietly inside _extract_json after a full page scrape.
        if "json" in formats and not json_schema and not json_prompt:
            raise ValidationError(
                "formats=['json'] requires json_schema or json_prompt so the extractor "
                "knows what to pull from the page",
                field="json_schema",
            )

        wants_change_tracking = "changeTracking" in formats

        # Field telemetry (#137): time the whole top-level scrape. None on escalated
        # sub-calls (level > 0) and when no sink is configured, so a single event is
        # emitted per user-facing scrape with end-to-end latency.
        telemetry_started = time.monotonic() if (_escalation_level == 0 and self._telemetry is not None) else None

        # Change tracking requires markdown to compute content hash
        if wants_change_tracking and "markdown" not in formats:
            formats = [*formats, "markdown"]

        # Build cache variant for settings that affect output.
        # Different settings produce different content, so they must map to
        # separate cache entries.
        variant_parts: list[str] = []
        if device:
            variant_parts.append(f"device={device}")
        if "screenshot" in formats and not screenshot_full_page:
            variant_parts.append("screenshot_full_page=False")
        # Include a hash of custom headers so authenticated and anonymous fetches
        # of the same URL are stored in separate cache entries.  Only the hash is
        # stored — raw header values are never persisted.
        if headers:
            variant_parts.append(f"headers={_compute_headers_hash(headers)}")
        cache_variant: str | None = "|".join(variant_parts) if variant_parts else None

        # Get previous cached entry for change tracking comparison (ignores expiry)
        previous_entry = None
        if wants_change_tracking and self._cache:
            previous_entry = self._cache.get_previous(url, variant=cache_variant)

        # Check cache if max_age > 0 and cache is configured
        # When change tracking is requested, skip the cache shortcut — always do a fresh scrape
        if max_age > 0 and self._cache and not wants_change_tracking:
            cached = self._cache.get(url, max_age, variant=cache_variant)
            if cached:
                LOGGER.debug(f"Cache hit for {url}")
                result = ScrapeResult.model_validate(cached)
                # Mark as cache hit
                if result.data and result.data.metadata:
                    result.data.metadata.cache_hit = True
                return self._record_telemetry(result, url, telemetry_started)

        # PDF detection: if parse_pdf is enabled, check if URL points to a PDF
        # and route to the PDF extraction pipeline instead of the browser.
        # The extension check is free; the HEAD-based content-type check is only
        # performed on the browser-only path (where the GET has not been made
        # yet) for ambiguous extensionless URLs.  On the http-first path the
        # content-type is already visible from the GET response in _try_http_first,
        # so no HEAD is needed there.
        if parse_pdf is not None:
            from supacrawl.services.pdf import (
                detect_pdf_content_type,
                is_pdf_url,
                needs_content_type_check,
            )

            is_pdf = is_pdf_url(url)

            # HEAD gate: fires only when the http-first GET will NOT be made
            # (screenshots, actions, stealth, device emulation, --no-http-first).
            # On the http-first path the content-type is already visible from
            # fetch_static's GET response, so no extra round-trip is needed.
            # needs_content_type_check() limits this to ambiguous/extensionless
            # URLs, so obvious non-PDF URLs (e.g. .html, .js) never trigger a
            # HEAD request.
            if not is_pdf and not (http_first and self._http_first_eligible(formats, actions, engine, device)):
                if needs_content_type_check(url):
                    is_pdf = await detect_pdf_content_type(url)

            if is_pdf:
                pdf_result = await self._scrape_pdf(
                    url=url,
                    mode=parse_pdf,
                    formats=formats,
                    json_schema=json_schema,
                    json_prompt=json_prompt,
                    max_age=max_age,
                    cache_variant=cache_variant,
                    wants_change_tracking=wants_change_tracking,
                    previous_entry=previous_entry,
                    change_tracking_modes=change_tracking_modes,
                )
                return self._record_telemetry(pdf_result, url, telemetry_started)

        # Determine effective engine and proxy for this request
        effective_engine = engine or self._engine
        effective_proxy = proxy or self._proxy

        # Per-domain strategy memory (#130): on a fresh top-level hit to a domain
        # we already learned, seed the attempt with the champion strategy so the
        # escalation ladder starts where it last succeeded — zero configuration.
        # Seeding applies only when this service owns its browser (the CLI/MCP
        # path) and the caller has not pinned an engine; the result is still
        # re-validated and re-recorded, so a stale champion self-corrects.
        from supacrawl.services.strategy_memory import registrable_domain

        memory_eligible = self._strategy_store is not None and self._memory_eligible(formats, actions, device)
        memory_domain = registrable_domain(url) if memory_eligible else None
        user_pinned = engine is not None or self._engine is not None
        seed: StrategyChoice | None = None
        if memory_domain and self._strategy_store is not None and not user_pinned and _escalation_level == 0:
            seed = self._strategy_store.seed(memory_domain)
            # A champion learned on a machine that had the engine, replayed where
            # that engine is no longer installed, would hard-fail every hit to the
            # domain (#143). Drop a stale seed so the request degrades to the
            # default engine instead of cascade-failing on the missing one. Check
            # the RESOLVED engine: a patchright champion is stored as (None, True),
            # which _engine_available would wave through as "playwright".
            if seed is not None and not _engine_available(_resolve_engine_stealth(seed.engine, seed.stealth)[0]):
                LOGGER.debug(
                    "Champion engine %r (stealth=%s) for %s is not installed; ignoring stale strategy seed",
                    seed.engine,
                    seed.stealth,
                    memory_domain,
                )
                seed = None

        attempt_engine = effective_engine
        attempt_stealth = self._stealth
        if seed is not None:
            attempt_engine = seed.engine
            attempt_stealth = seed.stealth
            wait_for = max(wait_for, seed.wait_for)
            # The seed drives the anti-bot strategy (engine / stealth / wait) only.
            # only_main_content is the caller's content preference, never overridden
            # by memory; the thin-content fallback already recovers over-pruning.
        # A seed that needs a real browser (stealth or a non-default engine) skips
        # the HTTP-first probe — the champion is browser-based for a reason.
        seed_requires_browser = bool(attempt_stealth or attempt_engine not in (None, "playwright"))

        # HTTP-first fast path: try a cheap httpx GET and skip the browser
        # entirely when the page needs no JavaScript and shows no bot challenge.
        # Any render-needed/bot signal returns None here and falls through to
        # the full browser path below.
        if http_first and not seed_requires_browser and self._http_first_eligible(formats, actions, engine, device):
            fast_result = await self._try_http_first(
                url=url,
                formats=formats,
                timeout=timeout,
                only_main_content=only_main_content,
                include_tags=include_tags,
                exclude_tags=exclude_tags,
                content_mode=content_mode,
                query=query,
                expand_iframes=expand_iframes,
                headers=headers,
                proxy=proxy,
                json_schema=json_schema,
                json_prompt=json_prompt,
                wants_change_tracking=wants_change_tracking,
                previous_entry=previous_entry,
                change_tracking_modes=change_tracking_modes,
                max_age=max_age,
                cache_variant=cache_variant,
                expect=expect,
                parse_pdf=parse_pdf,
            )
            if fast_result is not None:
                # A recoverable poor verdict (bot / CAPTCHA / JS-shell / empty) on
                # the cheap static path means a real browser may still recover the
                # page — fall through to the browser ladder instead of returning a
                # poor result, and do not record it as a champion.
                fast_verdict = fast_result.quality.verdict if fast_result.quality else None
                if fast_verdict not in _ESCALATABLE_VERDICTS:
                    if memory_domain and self._strategy_store is not None:
                        self._strategy_store.record(
                            memory_domain,
                            engine=attempt_engine,
                            stealth=attempt_stealth,
                            wait_for=wait_for,
                            only_main_content=only_main_content,
                            result=fast_result,
                        )
                    return self._record_telemetry(fast_result, url, telemetry_started)

        # Closure that re-runs this scrape one rung deeper in the escalation
        # ladder with a stronger strategy (engine / stealth / longer wait),
        # capturing the caller's other options so only the deltas change.
        async def _retry(
            *,
            engine: str | None,
            stealth: bool,
            firefox_user_prefs: dict[str, Any] | None,
            retry_wait_for: int,
            retry_only_main_content: bool | None = None,
            retry_expand_iframes: str | None = None,
            retry_actions: list[Any] | None = None,
        ) -> ScrapeResult:
            next_service = ScrapeService(
                converter=self._converter,
                locale_config=self._locale_config,
                cache_dir=self._cache.cache_dir if self._cache else None,
                stealth=stealth,
                proxy=self._proxy,
                solve_captcha=self._solve_captcha,
                headless=self._headless,
                engine=engine,
                firefox_user_prefs=firefox_user_prefs,
                strategy_store=self._strategy_store,
            )
            return await next_service.scrape(
                url=url,
                formats=formats,
                only_main_content=(only_main_content if retry_only_main_content is None else retry_only_main_content),
                wait_for=max(wait_for, retry_wait_for),
                timeout=timeout,
                screenshot_full_page=screenshot_full_page,
                actions=(actions if retry_actions is None else retry_actions),
                json_schema=json_schema,
                json_prompt=json_prompt,
                include_tags=include_tags,
                exclude_tags=exclude_tags,
                max_age=max_age,
                wait_until=wait_until,
                change_tracking_modes=change_tracking_modes,
                expand_iframes=(expand_iframes if retry_expand_iframes is None else retry_expand_iframes),  # type: ignore[arg-type]
                expand_disclosures=expand_disclosures,
                device=device,
                parse_pdf=parse_pdf,
                engine=engine,
                headers=headers,
                content_mode=content_mode,
                query=query,
                http_first=False,
                expect=expect,
                escalate=escalate,
                _escalation_level=_escalation_level + 1,
            )

        try:
            # Create browser if needed, or use a temporary one for an engine/proxy override
            browser = self._browser
            owns_browser = self._owns_browser

            # Per-request engine or proxy override: if the caller requested a different
            # engine or proxy than the shared browser, create a temporary browser for
            # this request so the override is honoured without mutating the shared one.
            needs_engine_override = (
                engine is not None and not owns_browser and browser is not None and browser.engine != engine
            )
            needs_proxy_override = (
                proxy is not None and not owns_browser and browser is not None and browser.proxy != proxy
            )
            # A learned seed (#130) may want a different engine/stealth than a
            # shared browser; build a temporary one so the champion strategy is
            # honoured without mutating the shared browser. Compare in RESOLVED
            # space: browser.engine/stealth are post-resolution, so the raw seed
            # must be resolved too, else a camoufox/patchright champion that the
            # shared browser already IS would spuriously rebuild every page (#139).
            seed_resolved_engine, seed_resolved_stealth = _resolve_engine_stealth(attempt_engine, attempt_stealth)
            needs_seed_override = (
                seed is not None
                and not owns_browser
                and browser is not None
                and (seed_resolved_engine != browser.engine or seed_resolved_stealth != browser.stealth)
            )

            if owns_browser or needs_engine_override or needs_proxy_override or needs_seed_override:
                # When this service owns its browser, the attempt strategy may be a
                # learned seed (#130); attempt_engine/attempt_stealth fold that in
                # (they equal the service defaults when there is no seed).
                browser = BrowserManager(
                    headless=self._headless,
                    timeout_ms=timeout,
                    locale_config=self._locale_config,
                    stealth=attempt_stealth,
                    proxy=effective_proxy,
                    engine=attempt_engine,
                    firefox_user_prefs=self._firefox_user_prefs,
                )
                await browser.__aenter__()
                owns_browser = True

            # At this point browser is guaranteed to be set
            if browser is None:
                raise RuntimeError("Browser not initialized")

            try:
                # Determine if we need screenshot or PDF capture
                capture_screenshot = "screenshot" in formats
                capture_pdf = "pdf" in formats

                # Fetch page with actions. A selector-shaped expectation is passed
                # as a wait target so the browser holds for that content to hydrate.
                page_content = await browser.fetch_page(
                    url,
                    wait_for_spa=wait_for > 0,
                    spa_timeout_ms=wait_for,
                    capture_screenshot=capture_screenshot,
                    capture_pdf=capture_pdf,
                    screenshot_full_page=screenshot_full_page,
                    actions=actions,
                    wait_until=wait_until,
                    expand_iframes=expand_iframes,
                    expand_disclosures=expand_disclosures,
                    device=device,
                    extra_headers=headers,
                    wait_for_selector=self._expect_selector(expect),
                )

                # Extract metadata
                metadata = await browser.extract_metadata(page_content.html)

                # Markdown underpins the bot/quality checks below and the JSON
                # and summary extraction, so compute it whenever it is needed.
                markdown = None
                if "markdown" in formats or "json" in formats or "summary" in formats:
                    # Always need markdown for JSON extraction and summary generation
                    markdown = self._converter.convert(
                        page_content.html,
                        base_url=url,
                        only_main_content=only_main_content,
                        include_tags=include_tags,
                        exclude_tags=exclude_tags,
                        content_mode=content_mode,
                        query=query,
                    )

                # only_main_content recovery: when main-content extraction is
                # anomalously sparse relative to the full page, the selector likely
                # matched a tiny wrapper and dropped the real content — re-extract
                # the fuller page so content is not silently lost.
                if markdown is not None and only_main_content and not include_tags:
                    markdown = self._recover_thin_main_content(
                        html=page_content.html,
                        main_markdown=markdown,
                        url=url,
                        exclude_tags=exclude_tags,
                        content_mode=content_mode,
                        query=query,
                    )

                # Check for CAPTCHA and solve if enabled
                captcha_detected = self._looks_like_captcha(page_content.html)
                if captcha_detected:
                    if self._solve_captcha:
                        LOGGER.info(f"CAPTCHA detected for {url}, attempting to solve...")
                        try:
                            # Need access to the page for CAPTCHA solving
                            # Re-fetch with CAPTCHA solving
                            solved_result = await self._scrape_with_captcha_solving(
                                url=url,
                                formats=formats,
                                only_main_content=only_main_content,
                                wait_for=wait_for,
                                timeout=timeout,
                                screenshot_full_page=screenshot_full_page,
                                actions=actions,
                                json_schema=json_schema,
                                json_prompt=json_prompt,
                                include_tags=include_tags,
                                exclude_tags=exclude_tags,
                                content_mode=content_mode,
                                query=query,
                            )
                            if solved_result:
                                return solved_result
                        except Exception as e:
                            LOGGER.warning(f"CAPTCHA solving failed: {e}")
                    else:
                        # Check if content was extracted successfully despite CAPTCHA element
                        content_words = len(markdown.split()) if markdown else 0
                        if content_words >= 50:
                            LOGGER.info(f"CAPTCHA element detected for {url} (content extracted successfully)")
                        else:
                            LOGGER.warning(
                                f"CAPTCHA detected for {url} - content extraction may be incomplete.{_captcha_hint()}"
                            )

                this_result = await self._assemble_result(
                    url=url,
                    page_content=page_content,
                    browser=browser,
                    formats=formats,
                    only_main_content=only_main_content,
                    include_tags=include_tags,
                    exclude_tags=exclude_tags,
                    content_mode=content_mode,
                    query=query,
                    markdown=markdown,
                    metadata=metadata,
                    json_schema=json_schema,
                    json_prompt=json_prompt,
                    capture_screenshot=capture_screenshot,
                    capture_pdf=capture_pdf,
                    content_quality_warnings=None,
                    wants_change_tracking=wants_change_tracking,
                    previous_entry=previous_entry,
                    change_tracking_modes=change_tracking_modes,
                    max_age=max_age,
                    cache_variant=cache_variant,
                )
                expect_met = self._expect_satisfied(page_content.html, markdown, expect) if expect is not None else True
                # Site-builder hint for escalation: a thin result on a known
                # platform (Wix/Squarespace/Framer) escalates straight to that
                # platform's tuned engine and settings rather than walking blind.
                escalation_platform = (
                    detect_platform(page_content.html)
                    if (markdown is not None and len(markdown.split()) < _THIN_MAIN_FLOOR)
                    else None
                )

            finally:
                if owns_browser and browser:
                    await browser.__aexit__(None, None, None)

        except ProviderError as e:
            if e.context.get("provider") == "llm":
                # A json-extraction failure is an LLM/config problem, not a
                # site-fetch problem: the escalation ladder below only varies
                # the browser engine, which cannot fix it, so refuse cleanly
                # rather than spending its retry budget on a rescrape.
                raise
            http2_error = "ERR_HTTP2_PROTOCOL_ERROR" in str(e)
            this_result = self._build_failure_result(
                url=url,
                error=e,
                wants_change_tracking=wants_change_tracking,
                previous_entry=previous_entry,
            )
            expect_met = False
            escalation_platform = None
        except Exception as e:
            # A mid-fetch error (network, timeout, TLS/HTTP-2 rejection, browser
            # crash) becomes a clean failure result with an honest verdict and an
            # actionable hint — never a raw traceback escaping to the caller. An
            # HTTP/2 protocol error is the one error the ladder can act on (jump
            # to Camoufox's Firefox TLS stack), so flag it for the tail below.
            http2_error = "ERR_HTTP2_PROTOCOL_ERROR" in str(e)
            this_result = self._build_failure_result(
                url=url,
                error=e,
                wants_change_tracking=wants_change_tracking,
                previous_entry=previous_entry,
            )
            expect_met = False
            escalation_platform = None
        else:
            http2_error = False

        # Adaptive auto-escalation: the cheap attempt above keys the decision. On
        # a recoverable poor verdict (block/CAPTCHA/JS-shell/empty), an unmet
        # `expect`, or an HTTP/2 TLS rejection, walk the stealth/engine ladder
        # within a bounded budget and keep the best-scoring attempt.
        final_result = await self._escalate(
            this_result,
            url=url,
            level=_escalation_level,
            escalate=escalate,
            expect=expect,
            expect_met=expect_met,
            http2_error=http2_error,
            # A user-pinned engine is respected only at the top of the ladder;
            # once escalation is in control (level > 0) it chose the engine itself.
            pinned_engine=((engine is not None or self._engine is not None) if _escalation_level == 0 else False),
            current_engine=attempt_engine,
            current_stealth=attempt_stealth,
            current_prefs=self._firefox_user_prefs,
            platform=escalation_platform,
            retry=_retry,
            store=self._strategy_store,
            record_domain=memory_domain,
            record_wait_for=wait_for,
            record_only_main=only_main_content,
        )
        return self._record_telemetry(final_result, url, telemetry_started)

    def _record_telemetry(self, result: ScrapeResult, url: str, started: float | None) -> ScrapeResult:
        """Emit a field telemetry event for a top-level scrape, returning the
        result unchanged.

        A no-op when telemetry is disabled or this is an escalated sub-call
        (``started`` is None), so exactly one event is recorded per user-facing
        scrape, with end-to-end latency. Errors in telemetry never affect the
        scrape result.

        Args:
            result: The final scrape result.
            url: The scraped URL.
            started: ``time.monotonic()`` captured at the start of the top-level
                scrape, or None to skip recording.

        Returns:
            ``result`` unchanged (pass-through so call sites stay one line).
        """
        if started is not None and self._telemetry is not None:
            latency_ms = int((time.monotonic() - started) * 1000)
            self._telemetry.record_scrape(url=url, result=result, latency_ms=latency_ms)
        return result

    def _recover_thin_main_content(
        self,
        *,
        html: str,
        main_markdown: str,
        url: str,
        exclude_tags: list[str] | None,
        content_mode: float,
        query: str | None,
    ) -> str:
        """Re-extract the full page when only_main_content yielded too little.

        When the main-content selector matched a tiny wrapper, the real content
        is dropped. If the full-page extraction is materially richer (and above
        the floor), prefer it; otherwise the page is simply short and the focused
        extraction is kept (so a genuinely small page does not gain nav chrome).

        Args:
            html: Raw page HTML.
            main_markdown: The only_main_content markdown already produced.
            url: Page URL (base for link resolution).
            exclude_tags: Caller's exclude selectors, reapplied to the full page.
            content_mode: Precision/recall dial.
            query: Optional BM25 relevance query.

        Returns:
            The richer markdown when recovery helps, else ``main_markdown``.
        """
        main_words = len(main_markdown.split())
        if main_words >= _THIN_MAIN_FLOOR:
            return main_markdown
        full = self._converter.convert(
            html,
            base_url=url,
            only_main_content=False,
            include_tags=None,
            exclude_tags=exclude_tags,
            content_mode=content_mode,
            query=query,
        )
        full_words = len(full.split())
        if full_words >= max(_THIN_MAIN_FLOOR, main_words * _THIN_FALLBACK_RATIO):
            LOGGER.info(
                "only_main_content recovered %d->%d words for %s via full-page fallback",
                main_words,
                full_words,
                url,
            )
            return full
        return main_markdown

    def _build_failure_result(
        self,
        *,
        url: str,
        error: BaseException,
        wants_change_tracking: bool,
        previous_entry: Any | None,
    ) -> ScrapeResult:
        """Turn a mid-fetch exception into a clean ScrapeResult with a verdict.

        Adds an availability-aware stealth hint for anti-bot failures and a
        concrete remediation for everything else, maps the error to a quality
        verdict so an agent still gets a structured signal, and preserves the
        change-tracking "removed" status.

        A failure of supacrawl's own browser engine is classified apart from every
        site-shaped failure (#160): the caller has to be able to tell "this site is
        a problem, escalate differently" from "the scraper is broken", because the
        right next action differs and retrying the URL is wasted work for one of
        them.

        Args:
            url: The URL that failed.
            error: The raised exception.
            wants_change_tracking: Whether change tracking was requested.
            previous_entry: The prior cache entry, when change tracking is on.

        Returns:
            A ``success=False`` :class:`ScrapeResult` carrying the verdict and hint.
        """
        error_msg = str(error)
        low_error = error_msg.lower()
        # A missing/broken stealth engine is a scraper-environment fault, not a
        # site problem (#143): its typed error already names the engine and how to
        # install it, so classify it alongside BrowserUnavailableError as
        # INFRASTRUCTURE rather than letting it fall through to a misleading EMPTY
        # verdict that reads as "the site returned nothing".
        scraper_fault = isinstance(
            error, (BrowserUnavailableError, StealthNotAvailableError, CamoufoxNotAvailableError)
        )
        bot_suspected = not scraper_fault and any(p in low_error for p in ["403", "429", "blocked", "denied"])
        if scraper_fault:
            # A stealth or wait hint would be actively misleading here: nothing
            # about the request was ever sent to the site.
            pass
        elif not self._stealth and (bot_suspected or "err_http2_protocol_error" in low_error):
            error_msg += _stealth_hint(bot_suspected=bot_suspected)
        else:
            hint = remediation_hint(error_msg)
            if hint:
                error_msg += f" [HINT: {hint}]"

        LOGGER.error("Scrape failed for %s: %s", url, error, exc_info=True)

        if scraper_fault:
            verdict = QualityVerdict.INFRASTRUCTURE
        elif bot_suspected:
            verdict = QualityVerdict.BOT_CHALLENGE
        else:
            verdict = QualityVerdict.EMPTY
        quality = QualityAssessment(verdict=verdict, score=0, reasons=[str(error)[:200]])

        if wants_change_tracking and previous_entry is not None:
            from supacrawl.models import ChangeTrackingData

            return ScrapeResult(
                success=False,
                error=error_msg,
                quality=quality,
                data=ScrapeData(  # type: ignore[call-arg]
                    metadata=ScrapeMetadata(source_url=url),
                    change_tracking=ChangeTrackingData(
                        previous_scrape_at=previous_entry.cached_at,
                        change_status="removed",
                    ),
                ),
            )
        return ScrapeResult(success=False, error=error_msg, quality=quality)

    async def _escalate(
        self,
        result: ScrapeResult,
        *,
        url: str,
        level: int,
        escalate: bool,
        expect: str | None,
        expect_met: bool,
        http2_error: bool,
        pinned_engine: bool,
        current_engine: str | None,
        current_stealth: bool,
        current_prefs: dict[str, Any] | None,
        platform: Any | None,
        retry: Callable[..., Awaitable[ScrapeResult]],
        store: "StrategyStore | None" = None,
        record_domain: str | None = None,
        record_wait_for: int = 0,
        record_only_main: bool = True,
    ) -> ScrapeResult:
        """Decide whether to try a stronger strategy, and keep the best attempt.

        Keys off the runtime quality verdict (#128): a recoverable block/shell/
        empty verdict, an unmet ``expect`` assertion, or an HTTP/2 TLS rejection
        triggers the next rung of the ladder, bounded by the escalation budget.
        The deeper attempt is run via ``retry`` (a closure capturing the original
        request), and the better-scoring of the two results is returned.

        Args:
            result: This attempt's assembled result.
            url: The URL (for logging).
            level: This attempt's escalation depth.
            escalate: Whether escalation is enabled at all.
            expect: The caller's content assertion, if any.
            expect_met: Whether ``expect`` was satisfied on this attempt.
            http2_error: Whether this attempt failed with an HTTP/2 error.
            pinned_engine: Whether the caller pinned the engine.
            current_engine / current_stealth / current_prefs: This attempt's strategy.
            platform: A detected site-builder platform, or None.
            retry: Closure that re-runs the scrape one rung deeper.

        Returns:
            The best :class:`ScrapeResult` across this attempt and any escalation.
        """
        # Overlay the expect assertion: a page that loaded but never showed the
        # asserted content is a failure regardless of the content verdict.
        result = self._overlay_expect(result, expect=expect, expect_met=expect_met)
        if result.quality is not None:
            result.quality.attempts = level + 1
            result.quality.escalated = level > 0

        # Per-domain strategy memory (#130): fold this attempt's (strategy, outcome)
        # into the domain's champion. Each rung records its own observation, so a
        # blocked playwright + a clean camoufox on the same hit teach the store the
        # right champion for next time.
        if store is not None and record_domain:
            store.record(
                record_domain,
                engine=current_engine,
                stealth=current_stealth,
                wait_for=record_wait_for,
                only_main_content=record_only_main,
                result=result,
            )

        has_response = result.data is not None
        verdict = result.quality.verdict if result.quality else None
        # A CAPTCHA wall fails fast (#153): a stronger engine will not solve a
        # CAPTCHA, so the ladder stops after one attempt unless the operator has
        # explicitly opted back into walking it (SUPACRAWL_CAPTCHA_FAIL_FAST=0).
        captcha_wall = verdict == QualityVerdict.CAPTCHA
        captcha_escalatable = captcha_wall and not _captcha_fail_fast()
        # A thin result on a known site-builder (Wix/Squarespace/Framer/Foleon) is
        # escalatable even though THIN is not in the generic set: the platform has
        # a tuned engine that reliably renders it, where the generic ladder would
        # not fire. `platform` is only set on a thin browser result.
        escalatable_verdict = verdict in _ESCALATABLE_VERDICTS or captcha_escalatable
        wants_escalation = http2_error or (
            has_response and (escalatable_verdict or platform is not None or (expect is not None and not expect_met))
        )
        # Make the fail-fast an audible decision, not a silent skip: when a
        # CAPTCHA is what stopped us (rather than a bare exhausted ladder), record
        # that we chose not to continue and how to override it, so a caller can
        # tell "we deliberately stopped on a CAPTCHA" from "the site was empty".
        # Only at level 0: a CAPTCHA reached after an earlier rung escalated has
        # run more than one attempt, so "stopped after one attempt" would be false.
        if escalate and level == 0 and captcha_wall and not captcha_escalatable and result.quality is not None:
            result.quality.reasons.append(
                "fail-fast: stopped after one attempt — a stronger engine does not solve a CAPTCHA "
                "(set SUPACRAWL_CAPTCHA_FAIL_FAST=0 to walk the full ladder, or enable solve_captcha)"
            )
        if not escalate or not wants_escalation or level >= _MAX_ESCALATIONS:
            return result

        rung = _next_escalation(
            engine=current_engine,
            stealth=current_stealth,
            prefs=current_prefs,
            pinned=pinned_engine,
            http2_error=http2_error,
            platform=platform,
        )
        if rung is None:
            return result

        LOGGER.info("Auto-escalating %s (level %d -> %d): %s", url, level, level + 1, rung.label)
        escalated = await retry(
            engine=rung.engine,
            stealth=rung.stealth,
            firefox_user_prefs=rung.prefs,
            retry_wait_for=rung.wait_for,
            retry_only_main_content=rung.only_main_content,
            retry_expand_iframes=rung.expand_iframes,
            retry_actions=rung.actions,
        )
        best = self._pick_best(result, escalated)
        if best.quality is not None:
            deepest = escalated.quality.attempts if escalated.quality else level + 2
            best.quality.attempts = max(best.quality.attempts, deepest)
            best.quality.escalated = best.quality.attempts > 1
        return best

    @staticmethod
    def _overlay_expect(result: ScrapeResult, *, expect: str | None, expect_met: bool) -> ScrapeResult:
        """Flip a success to a failure when an ``expect`` assertion went unmet.

        Args:
            result: The assembled result.
            expect: The content assertion, or None.
            expect_met: Whether the assertion held.

        Returns:
            The result, with ``success`` and ``error`` rewritten when ``expect``
            was set but never appeared.
        """
        if expect is None or expect_met or not result.success:
            return result
        correlation_id = generate_correlation_id()
        # An availability-aware remediation: point at the stealth extras when no
        # stronger engine is installed, otherwise suggest a longer wait.
        if not _engine_available("patchright") and not _engine_available("camoufox"):
            remediation = (
                "No stealth engine can launch, so a stealth retry is unavailable; increase wait_for."
                f"{_engine_remedy_clause('patchright')}{_engine_remedy_clause('camoufox')}"
            )
        else:
            remediation = "Try a larger wait_for or a stronger engine."
        result.success = False
        result.error = (
            f"Expected content not found: {expect!r}. The page loaded but the asserted content never "
            f"appeared after escalation. {remediation} [correlation_id={correlation_id}]"
        )
        # The verdict stays honest — the content that came back really was fine,
        # it just was not what the caller asserted — but a failure handed to a
        # caller must still carry a next step in the structured field, not only in
        # the error prose.
        if result.quality is not None:
            result.quality.suggestion = (
                f"The page loaded cleanly but never showed the asserted content ({expect!r}). {remediation} "
                f"If the assertion itself is wrong, re-check it against the returned markdown."
            )
        return result

    @staticmethod
    def _pick_best(a: ScrapeResult, b: ScrapeResult) -> ScrapeResult:
        """Pick the better of two attempts.

        A usable (success) result always beats a failure; among equals, the higher
        quality score wins, with ties kept on ``b`` (the later, stronger attempt).
        """
        if a.success != b.success:
            return a if a.success else b
        score_a = a.quality.score if a.quality else (50 if a.success else 0)
        score_b = b.quality.score if b.quality else (50 if b.success else 0)
        return b if score_b >= score_a else a

    @staticmethod
    def _memory_eligible(formats: Sequence[str], actions: list[Any] | None, device: str | None) -> bool:
        """Whether per-domain strategy memory applies to this request.

        Skipped for special-purpose work — screenshots, PDF capture, action
        sequences, device emulation — where a learned content-scrape strategy is
        irrelevant and could mislearn. Content scrapes (markdown/html/links) use it.
        """
        if "screenshot" in formats or "pdf" in formats:
            return False
        return not (actions or device is not None)

    def _http_first_eligible(
        self,
        formats: Sequence[str],
        actions: list[Any] | None,
        engine: str | None,
        device: str | None,
    ) -> bool:
        """Whether the HTTP-first fast path may be attempted for this request.

        The fast path is skipped for browser-only work (screenshots, PDF capture,
        action sequences, device emulation) and whenever stealth or a non-default
        engine is requested — those callers explicitly want the full browser.

        Args:
            formats: Requested output formats.
            actions: Action sequence, if any.
            engine: Per-request engine override.
            device: Per-request device emulation name.

        Returns:
            True when a cheap httpx GET is worth trying first.
        """
        if "screenshot" in formats or "pdf" in formats:
            return False
        if actions:
            return False
        if device is not None:
            return False
        if self._stealth:
            return False
        effective_engine = engine or self._engine
        return effective_engine in (None, "playwright")

    @staticmethod
    def _expect_satisfied(html: str, markdown: str | None, expect: str | None) -> bool:
        """Whether the page satisfies a caller-supplied content assertion.

        Three modes share one ``expect`` string:
            - a bare integer  -> minimum word count;
            - a CSS selector that matches at least one element -> satisfied;
            - otherwise a case-insensitive visible-text substring.

        Args:
            html: Raw HTML of the fetched page.
            markdown: Converted markdown, when available (used for word count and
                text search; falls back to the page's visible text otherwise).
            expect: The assertion string, or None.

        Returns:
            True when the assertion holds (or there is none).
        """
        if expect is None:
            return True

        soup = BeautifulSoup(html, "html.parser")
        text = markdown if markdown is not None else soup.get_text(" ", strip=True)

        if expect.isdigit():
            return len(text.split()) >= int(expect)

        # A matching CSS selector satisfies the assertion outright.
        try:
            if soup.select(expect):
                return True
        except Exception:
            pass  # not valid selector syntax — fall through to text matching

        return expect.lower() in text.lower()

    @staticmethod
    def _expect_selector(expect: str | None) -> str | None:
        """Return ``expect`` when it is selector-shaped, else None.

        Used to decide whether the browser should wait for the asserted content.
        Only single-token strings carrying a CSS structural character qualify, so
        free-text or word-count assertions never become a wasteful selector wait.
        """
        if expect is None or expect.isdigit():
            return None
        if " " in expect.strip():
            return None
        if any(ch in expect for ch in ".#[]>+~"):
            return expect
        return None

    async def _try_http_first(
        self,
        *,
        url: str,
        formats: Sequence[str],
        timeout: int,
        only_main_content: bool,
        include_tags: list[str] | None,
        exclude_tags: list[str] | None,
        content_mode: float,
        query: str | None,
        expand_iframes: str,
        headers: dict[str, str] | None,
        proxy: str | None,
        json_schema: dict[str, Any] | None,
        json_prompt: str | None,
        wants_change_tracking: bool,
        previous_entry: Any | None,
        change_tracking_modes: list[str] | None,
        max_age: int,
        cache_variant: str | None,
        expect: str | None,
        parse_pdf: Literal["fast", "auto", "ocr"] | None,
    ) -> ScrapeResult | None:
        """Attempt to satisfy a scrape with a single httpx GET, no browser.

        Returns a fully-built ScrapeResult when the fetched HTML is usable as-is,
        or None to signal that the caller should escalate to the browser path
        (render-needed, bot challenge, suspect quality, fetch failure, an unmet
        ``expect`` assertion, or a page with iframes that need expanding).

        When ``parse_pdf`` is set and the server responds with
        ``application/pdf`` (or the body contains the ``%PDF-`` signature
        within the sniff window for ambiguous content-types), the
        already-fetched bytes are routed directly to the PDF extractor — no
        second download occurs.
        """
        accept_language = self._locale_config.get_accept_language_header() if self._locale_config else None
        fetched = await fetch_static(
            url,
            timeout_ms=timeout,
            headers=headers,
            accept_language=accept_language,
            proxy=proxy or self._proxy,
        )
        if fetched is None:
            return None

        # PDF detected via Content-Type header (or magic-byte sniffing for
        # application/octet-stream / missing content-type — fetch_static handles
        # the sniff and only sets raw_bytes when bytes are confirmed as PDF).
        if fetched.raw_bytes is not None:
            if parse_pdf is not None:
                # Route to the PDF extractor using the already-fetched bytes —
                # no second download.
                return await self._scrape_pdf(
                    url=url,
                    mode=parse_pdf,
                    formats=formats,
                    json_schema=json_schema,
                    json_prompt=json_prompt,
                    max_age=max_age,
                    cache_variant=cache_variant,
                    wants_change_tracking=wants_change_tracking,
                    previous_entry=previous_entry,
                    change_tracking_modes=change_tracking_modes,
                    pdf_bytes=fetched.raw_bytes,
                )
            # PDF parsing is disabled: escalate to the browser rather than
            # returning an empty success (html="" is not usable content).
            # Mirrors the behaviour of the .pdf-extension path when parse_pdf
            # is None — the browser may at least render the PDF viewer.
            return None

        html = fetched.html

        # Iframes are expanded only in the browser; if the page has any and the
        # caller wants them expanded, escalate so no embedded content is lost.
        if expand_iframes != "none" and "<iframe" in html.lower():
            LOGGER.debug("HTTP-first escalating %s: iframe expansion requested", url)
            return None

        # Keyword bot-protection markers (CAPTCHA, "just a moment", access denied)
        # — the browser and its stealth ladder may help. This needs no word count.
        bot_indicators = detect_bot_protection(html)
        if bot_indicators["challenge_detected"] or bot_indicators["captcha_present"] or bot_indicators["access_denied"]:
            LOGGER.debug("HTTP-first escalating %s: bot-protection markers present", url)
            return None

        # JavaScript shell — the real content is injected client-side.
        if estimate_js_requirement(html, len(html)):
            LOGGER.debug("HTTP-first escalating %s: JavaScript rendering required", url)
            return None

        # Content hidden behind collapsed disclosures. Only the browser runs
        # _expand_disclosures, so serving this page from the fast path drops the
        # gated content silently — the returned text scores fine on the part the
        # page did give up, so no quality signal ever fires.
        if detect_collapsed_disclosures(html):
            LOGGER.debug("HTTP-first escalating %s: collapsed disclosures need expanding", url)
            return None

        # Compute markdown only when a format needs it (mirrors the browser path).
        markdown = None
        if "markdown" in formats or "json" in formats or "summary" in formats:
            markdown = self._converter.convert(
                html,
                base_url=url,
                only_main_content=only_main_content,
                include_tags=include_tags,
                exclude_tags=exclude_tags,
                content_mode=content_mode,
                query=query,
            )

        # The density-based bot and quality heuristics need a real word count.
        # When markdown was not produced for output (e.g. a links-only request),
        # fall back to the page's visible text — otherwise a count of zero would
        # misjudge any page merely *mentioning* a bot keyword (the ubiquitous
        # ``<meta name="robots">`` tag matches the BOT_DETECTION_REGEX) as empty
        # and defeat the fast path for most real pages.
        density_text = (
            markdown if markdown is not None else BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
        )

        # Bot challenge or block (status codes, near-empty challenge pages).
        if _looks_like_bot_block(fetched.status_code, html, density_text):
            LOGGER.debug("HTTP-first escalating %s: bot block suspected (HTTP %d)", url, fetched.status_code)
            return None

        # Content quality. A binary/garbage body (HARD) escalates so the browser
        # can try to recover; a low-density (SOFT) *static* page would render
        # identically in the browser, so it is served but the warning is carried
        # through for parity with the browser path.
        content_quality_warnings: list[str] | None = None
        quality_reason = _assess_content_quality(html, density_text)
        if quality_reason is not None:
            if quality_reason.startswith("HARD:"):
                LOGGER.debug("HTTP-first escalating %s: content quality hard-failed", url)
                return None
            content_quality_warnings = [f"{quality_reason[len('SOFT: ') :]} {thin_content_hint(only_main_content)}"]

        # Caller asserted specific content; if a cheap GET doesn't satisfy it the
        # content is likely hydrated client-side, so escalate to the browser.
        if expect is not None and not self._expect_satisfied(html, markdown, expect):
            LOGGER.debug("HTTP-first escalating %s: expected content %r not present", url, expect)
            return None

        LOGGER.debug("HTTP-first served %s without a browser (HTTP %d)", url, fetched.status_code)

        # extract_metadata/extract_images are pure HTML parsers; an unstarted
        # BrowserManager is enough when no shared browser is injected.
        extractor = self._browser if self._browser is not None else BrowserManager(locale_config=self._locale_config)
        metadata = await extractor.extract_metadata(html)

        page_content = PageContent(url=fetched.url, html=html, title=metadata.title, status_code=fetched.status_code)

        return await self._assemble_result(
            url=url,
            page_content=page_content,
            browser=extractor,
            formats=formats,
            only_main_content=only_main_content,
            include_tags=include_tags,
            exclude_tags=exclude_tags,
            content_mode=content_mode,
            query=query,
            markdown=markdown,
            metadata=metadata,
            json_schema=json_schema,
            json_prompt=json_prompt,
            capture_screenshot=False,
            capture_pdf=False,
            content_quality_warnings=content_quality_warnings,
            wants_change_tracking=wants_change_tracking,
            previous_entry=previous_entry,
            change_tracking_modes=change_tracking_modes,
            max_age=max_age,
            cache_variant=cache_variant,
        )

    async def _assemble_result(
        self,
        *,
        url: str,
        page_content: PageContent,
        browser: BrowserManager,
        formats: Sequence[str],
        only_main_content: bool,
        include_tags: list[str] | None,
        exclude_tags: list[str] | None,
        content_mode: float,
        query: str | None,
        markdown: str | None,
        metadata: PageMetadata,
        json_schema: dict[str, Any] | None,
        json_prompt: str | None,
        capture_screenshot: bool,
        capture_pdf: bool,
        content_quality_warnings: list[str] | None,
        wants_change_tracking: bool,
        previous_entry: Any | None,
        change_tracking_modes: list[str] | None,
        max_age: int,
        cache_variant: str | None,
    ) -> ScrapeResult:
        """Build a ScrapeResult from fetched page content and cache it.

        Shared by the browser path and the HTTP-first fast path: both produce a
        PageContent plus pre-computed markdown and metadata, then this method
        derives the requested output formats, assembles the ScrapeData, computes
        change tracking, and writes the cache entry.
        """
        html = None
        raw_html = None
        links = None
        images = None
        branding = None
        structured_data = None
        summary = None
        screenshot_b64 = None
        pdf_b64 = None
        json_data = None

        if "html" in formats:
            # Clean HTML (boilerplate removed)
            html = self._get_clean_html(
                page_content.html, only_main_content, include_tags=include_tags, exclude_tags=exclude_tags
            )

        if "rawHtml" in formats:
            raw_html = page_content.html

        if "links" in formats:
            links = self._extract_links_from_html(page_content.html, url)

        if "images" in formats:
            images = await browser.extract_images(page_content.html, url)

        if "branding" in formats:
            # Extract branding information
            from supacrawl.services.branding import BrandingExtractor

            extractor = BrandingExtractor()
            branding = extractor.extract(page_content.html, url)

        if "structuredData" in formats:
            # Deterministic, no-LLM extraction of the site's own embedded data.
            structured_data = extract_structured_data(page_content.html, base_url=url)

        if capture_screenshot and page_content.screenshot:
            screenshot_b64 = base64.b64encode(page_content.screenshot).decode("utf-8")

        if capture_pdf and page_content.pdf:
            pdf_b64 = base64.b64encode(page_content.pdf).decode("utf-8")

        if "json" in formats:
            # Perform LLM extraction
            json_data = await self._extract_json(markdown or "", json_schema, json_prompt, strict=True)

        if "summary" in formats:
            # Generate LLM summary of the page content
            summary = await self._generate_summary(markdown or "")

        # Compute word count from markdown
        word_count = len(markdown.split()) if markdown else None

        # Process action results (screenshots and scrapes)
        actions_output = self._process_action_results(
            page_content.action_results,
            only_main_content=only_main_content,
            include_tags=include_tags,
            exclude_tags=exclude_tags,
            content_mode=content_mode,
            query=query,
        )

        # Compute change tracking if requested
        change_tracking = None
        current_content_hash = None
        if wants_change_tracking:
            current_content_hash = _compute_content_hash(markdown)
            change_tracking = _build_change_tracking(
                previous_entry=previous_entry,
                current_hash=current_content_hash,
                current_markdown=markdown,
                change_tracking_modes=change_tracking_modes,
            )

            # JSON comparison mode: extract structured data from both
            # versions and compare field-by-field
            if change_tracking.change_status == "changed" and change_tracking_modes and "json" in change_tracking_modes:
                json_comparison = await self._generate_json_comparison(
                    previous_entry=previous_entry,
                    current_markdown=markdown,
                    current_json=json_data,
                    json_schema=json_schema,
                    json_prompt=json_prompt,
                )
                if json_comparison:
                    change_tracking.json_changes = json_comparison

        # Runtime quality assessment: an honest, structured verdict + 0-100 score
        # over the result, sharing its vocabulary with the offline benchmark. A
        # hard-fail verdict (HTTP >= 400 soft-404, bot challenge, CAPTCHA, empty)
        # flips success to False so a caller never passes a block page downstream.
        quality_text = markdown
        if quality_text is None and page_content.html:
            quality_text = BeautifulSoup(page_content.html, "html.parser").get_text(" ", strip=True)
        quality = assess_quality(
            status_code=page_content.status_code,
            html=page_content.html,
            markdown=markdown,
            visible_text=quality_text if markdown is None else None,
            url=url,
        )
        quality_error = _quality_error(quality)

        result = ScrapeResult(
            success=quality.is_usable,
            error=quality_error,
            warnings=content_quality_warnings,
            quality=quality,
            data=ScrapeData(  # type: ignore[call-arg]
                markdown=markdown,
                html=html,
                raw_html=raw_html,
                screenshot=screenshot_b64,
                pdf=pdf_b64,
                llm_extraction=json_data,
                structured_data=structured_data,
                summary=summary,
                metadata=ScrapeMetadata(
                    # Core metadata
                    title=metadata.title,
                    description=metadata.description,
                    language=metadata.language,
                    keywords=metadata.keywords,
                    robots=metadata.robots,
                    canonical_url=metadata.canonical_url,
                    # OpenGraph metadata
                    og_title=metadata.og_title,
                    og_description=metadata.og_description,
                    og_image=metadata.og_image,
                    og_url=metadata.og_url,
                    og_site_name=metadata.og_site_name,
                    # Source information
                    source_url=url,
                    status_code=page_content.status_code,
                    # Detected timezone
                    timezone=metadata.timezone,
                    # Content metrics
                    word_count=word_count,
                ),
                links=links,
                images=images,
                branding=branding,
                actions=actions_output,
                change_tracking=change_tracking,
            ),
        )

        # Store in cache if max_age > 0 and cache is configured
        # When change tracking is active, auto-enable caching so future
        # comparisons have a previous version to diff against. A hard-fail result
        # (block page, soft-404, empty) is never cached: caching it would serve a
        # stale block on the next hit and clobber a good previous version.
        effective_max_age = max_age
        if wants_change_tracking and effective_max_age <= 0:
            effective_max_age = 365 * 24 * 3600  # 1 year default for change tracking
        if effective_max_age > 0 and self._cache and result.success:
            self._cache.set(
                url,
                result.model_dump(),
                effective_max_age,
                variant=cache_variant,
                content_hash=current_content_hash,
            )

        return result

    async def _scrape_pdf(
        self,
        url: str,
        mode: Literal["fast", "auto", "ocr"],
        formats: Sequence[str],
        json_schema: dict[str, Any] | None = None,
        json_prompt: str | None = None,
        max_age: int = 0,
        cache_variant: str | None = None,
        wants_change_tracking: bool = False,
        previous_entry: Any | None = None,
        change_tracking_modes: list[str] | None = None,
        pdf_bytes: bytes | None = None,
    ) -> ScrapeResult:
        """Scrape a PDF URL by downloading and extracting text.

        Bypasses the browser entirely — downloads the PDF via httpx (or reuses
        ``pdf_bytes`` when already fetched by the HTTP-first path) and processes
        it through the PDF extraction pipeline.

        Args:
            pdf_bytes: Pre-fetched PDF content.  When provided, the download
                step inside ``parse_pdf`` is skipped entirely.
        """
        from supacrawl.services.pdf import parse_pdf as do_parse_pdf

        try:
            pdf_result = await do_parse_pdf(url=url, mode=mode, pdf_bytes=pdf_bytes)
        except ImportError as e:
            # A missing PDF extra is supacrawl's own environment, not the document.
            return ScrapeResult(
                success=False,
                error=str(e),
                quality=QualityAssessment(
                    verdict=QualityVerdict.INFRASTRUCTURE,
                    score=0,
                    reasons=[str(e)[:200]],
                    suggestion=(
                        "supacrawl cannot read PDFs in this environment because its PDF dependency is not "
                        "installed. Install supacrawl[pdf] and retry; the document itself was never assessed."
                    ),
                ),
            )
        except Exception as e:
            LOGGER.error(f"PDF extraction failed for {url}: {e}", exc_info=True)
            return ScrapeResult(
                success=False,
                error=f"PDF extraction failed: {e}",
                quality=QualityAssessment(
                    verdict=QualityVerdict.EMPTY,
                    score=0,
                    reasons=[f"PDF extraction failed: {e}"[:200]],
                    suggestion=(
                        "The PDF could not be parsed. Try parse_pdf='ocr' for a scanned or damaged document, "
                        "or check the URL actually returns a PDF rather than an HTML error page."
                    ),
                ),
            )

        markdown = pdf_result.markdown
        word_count = len(markdown.split()) if markdown else None

        # LLM-powered features on extracted markdown
        json_data = None
        summary = None

        if "json" in formats:
            json_data = await self._extract_json(markdown or "", json_schema, json_prompt, strict=True)

        if "summary" in formats and markdown:
            summary = await self._generate_summary(markdown)

        # Build metadata from PDF properties
        pdf_meta = pdf_result.metadata

        # Change tracking
        change_tracking = None
        current_content_hash = None
        if wants_change_tracking:
            current_content_hash = _compute_content_hash(markdown)
            change_tracking = _build_change_tracking(
                previous_entry=previous_entry,
                current_hash=current_content_hash,
                current_markdown=markdown,
                change_tracking_modes=change_tracking_modes,
            )

        # PDF quality: catch garbled (fused-word) extraction and empty PDFs
        # honestly rather than reporting a clean success over unusable text.
        quality = assess_quality(status_code=200, html=None, markdown=markdown, is_pdf=True)

        result = ScrapeResult(
            success=quality.is_usable,
            error=_quality_error(quality),
            quality=quality,
            data=ScrapeData(  # type: ignore[call-arg]
                markdown=markdown if "markdown" in formats or "json" in formats or "summary" in formats else None,
                metadata=ScrapeMetadata(
                    title=pdf_meta.title,
                    source_url=url,
                    status_code=200,
                    word_count=word_count,
                    pdf_page_count=pdf_meta.page_count,
                    pdf_author=pdf_meta.author,
                    pdf_creation_date=pdf_meta.creation_date,
                ),
                llm_extraction=json_data,
                summary=summary,
                change_tracking=change_tracking,
            ),
        )

        # Cache the result (never cache an unusable extraction)
        effective_max_age = max_age
        if wants_change_tracking and effective_max_age <= 0:
            effective_max_age = 365 * 24 * 3600
        if effective_max_age > 0 and self._cache and result.success:
            self._cache.set(
                url,
                result.model_dump(),
                effective_max_age,
                variant=cache_variant,
                content_hash=current_content_hash,
            )

        return result

    @staticmethod
    def _extract_links_from_html(html: str, base_url: str) -> list[str]:
        """Extract absolute HTTP(S) links from already-fetched HTML.

        Parses anchor tags from the rendered HTML instead of re-navigating
        the browser, avoiding a full page re-fetch.

        Args:
            html: HTML content (post-JavaScript rendering, with iframes expanded)
            base_url: Base URL for resolving relative hrefs

        Returns:
            List of absolute URLs starting with http(s)
        """
        from urllib.parse import urljoin, urlparse

        soup = BeautifulSoup(html, "html.parser")
        links: list[str] = []
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            if isinstance(href, list):
                href = href[0] if href else ""
            if not href:
                continue
            # Resolve relative URLs
            if not urlparse(href).netloc:
                href = urljoin(base_url, href)
            # Only include http(s) links (matches browser.extract_links behaviour)
            if href.startswith("http"):
                links.append(href)
        return links

    def _get_clean_html(
        self,
        html: str,
        only_main_content: bool,
        include_tags: list[str] | None = None,
        exclude_tags: list[str] | None = None,
    ) -> str:
        """Get cleaned HTML with boilerplate removed.

        Args:
            html: Raw HTML
            only_main_content: Extract main content only
            include_tags: CSS selectors for elements to include
            exclude_tags: CSS selectors for elements to exclude

        Returns:
            Cleaned HTML string
        """
        soup = BeautifulSoup(html, "html.parser")

        # Remove boilerplate
        for tag_name in ["script", "style", "nav", "footer", "header", "noscript", "iframe"]:
            for tag in soup.find_all(tag_name):
                tag.decompose()

        # Apply exclude_tags first
        if exclude_tags:
            for selector in exclude_tags:
                try:
                    for element in soup.select(selector):
                        element.decompose()
                except Exception:
                    pass  # Invalid selector, skip

        # Apply include_tags if specified (takes precedence over only_main_content)
        if include_tags:
            matched_elements: list[Any] = []
            for selector in include_tags:
                try:
                    matched_elements.extend(soup.select(selector))
                except Exception:
                    pass  # Invalid selector, skip

            if matched_elements:
                # Create wrapper with matched elements
                wrapper = soup.new_tag("div")
                for element in matched_elements:
                    if element not in [wrapper] + list(wrapper.descendants):
                        wrapper.append(element.extract())
                return str(wrapper)

        # Find main content if requested
        if only_main_content:
            for selector in ["main", "article", "[role='main']", ".content", "#content"]:
                main = soup.select_one(selector)
                if main:
                    return str(main)

        body = soup.find("body")
        return str(body) if body else str(soup)

    async def _extract_json(
        self,
        markdown: str,
        schema: dict[str, Any] | None,
        prompt: str | None,
        *,
        strict: bool = False,
    ) -> dict[str, Any] | None:
        """Extract structured JSON data from markdown using LLM.

        Args:
            markdown: Markdown content to extract from
            schema: JSON schema for structured extraction
            prompt: Custom extraction prompt
            strict: Raise instead of returning None when extraction fails.
                Set by the primary "json" format path, where a caller who asked
                for structured data should get a typed refusal naming what
                went wrong rather than a quiet markdown-only result. Left
                False for the changeTracking json-comparison path, where a
                missed diff is a best-effort extra, not the caller's request.

        Returns:
            Extracted JSON data, or None on failure when not strict.

        Raises:
            ProviderError: strict=True and extraction failed (LLM not
                configured, the extraction call itself failed, or it returned
                no usable data), carrying provider="llm" in its context.
        """
        from supacrawl.services.extract import ExtractService

        # Create ExtractService with self as scrape service
        # We need to avoid circular calls, so we create a minimal scrape wrapper
        class NoOpScrapeService:
            """Wrapper that returns markdown directly without scraping."""

            async def scrape(self, url: str, **kwargs):
                from supacrawl.models import ScrapeData, ScrapeMetadata, ScrapeResult

                return ScrapeResult(
                    success=True,
                    data=ScrapeData(  # type: ignore[call-arg]
                        markdown=markdown,
                        metadata=ScrapeMetadata(),
                    ),
                )

        extract_service = ExtractService(
            scrape_service=NoOpScrapeService(),  # type: ignore[arg-type]
        )

        try:
            # Call extraction with dummy URL (we already have the content)
            result = await extract_service.extract(
                urls=["dummy://content"],
                prompt=prompt,
                schema=schema,
            )

            item = result.data[0] if result.success and result.data else None
            if item is not None and item.success and item.data:
                return item.data

            reason = item.error if item is not None and item.error else "LLM returned no usable data"
            LOGGER.warning(f"JSON extraction failed or returned no data: {reason}")
            if strict:
                raise ProviderError(f"json format extraction failed: {reason}", provider="llm")
            return None

        except ProviderError:
            if strict:
                raise
            LOGGER.error("JSON extraction error", exc_info=True)
            return None
        except Exception as e:
            LOGGER.error(f"JSON extraction error: {e}", exc_info=True)
            if strict:
                # provider="llm" (not the fetch engine) lets the caller in
                # scrape() tell this apart from a site-fetch failure and refuse
                # cleanly instead of spending the anti-bot escalation ladder on
                # a rescrape that could never fix an LLM-side problem.
                raise ProviderError(f"json format extraction failed: {e}", provider="llm") from e
            return None

    async def _generate_summary(self, markdown: str) -> str | None:
        """Generate a concise LLM summary of the page content.

        Args:
            markdown: Markdown content to summarise

        Returns:
            2-3 sentence summary or None if LLM not configured or on failure
        """
        if not markdown.strip():
            return None

        # Limit content to avoid context overflow (~10k chars as per issue spec)
        max_content = 10000
        content = markdown[:max_content]
        if len(markdown) > max_content:
            content += "\n\n[Content truncated...]"

        from supacrawl.llm import LLMClient, LLMNotConfiguredError, load_llm_config

        # Load config - return None if LLM not configured (summary is optional)
        try:
            config = load_llm_config()
        except LLMNotConfiguredError:
            LOGGER.warning("LLM not configured, skipping summary generation")
            return None

        client = LLMClient(config)

        try:
            messages = [
                {
                    "role": "system",
                    "content": (
                        "You are a summarisation assistant. Your task is to provide "
                        "concise summaries of web page content. Always respond with "
                        "plain text only, no markdown formatting."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Summarise the following web page content in 2-3 sentences. "
                        "Focus on the main topic and key information.\n\n"
                        f"Content:\n{content}\n\nSummary:"
                    ),
                },
            ]

            response = await client.chat(messages)

            # Clean up the response
            summary = response.strip()

            # Ensure summary is concise (under 500 chars as per issue spec)
            if len(summary) > 500:
                # Truncate to last complete sentence
                sentences = summary[:500].rsplit(".", 1)
                if len(sentences) > 1:
                    summary = sentences[0] + "."
                else:
                    summary = summary[:497] + "..."

            return summary

        except Exception as e:
            LOGGER.warning(f"Summary generation failed: {e}")
            return None
        finally:
            await client.close()

    async def _generate_json_comparison(
        self,
        previous_entry: Any,
        current_markdown: str | None,
        current_json: dict[str, Any] | None,
        json_schema: dict[str, Any] | None,
        json_prompt: str | None,
    ) -> dict[str, Any] | None:
        """Compare structured JSON fields between previous and current versions.

        Extracts JSON from both previous cached markdown and current markdown
        using the LLM, then returns a field-level comparison of changed values.

        Args:
            previous_entry: Previous CacheEntry with cached response.
            current_markdown: Current markdown content.
            current_json: Already-extracted JSON from current content (avoids
                redundant LLM call when ``json`` is also in formats).
            json_schema: JSON schema for extraction.
            json_prompt: Custom prompt for extraction.

        Returns:
            Dict mapping field names to ``{previous, current}`` values for
            fields that differ, or None if comparison is not possible.
        """
        # Extract previous markdown from cached response
        try:
            prev_data = previous_entry.response.get("data", {})
            prev_markdown = prev_data.get("markdown") or ""
        except AttributeError, TypeError:
            LOGGER.warning("Cannot extract previous markdown for JSON comparison")
            return None

        if not prev_markdown:
            return None

        # Extract JSON from previous version
        prev_json = await self._extract_json(prev_markdown, json_schema, json_prompt)

        # Extract JSON from current version (reuse if already extracted)
        curr_json = current_json
        if curr_json is None:
            curr_json = await self._extract_json(current_markdown or "", json_schema, json_prompt)

        if not prev_json and not curr_json:
            LOGGER.warning("JSON extraction returned no data for either version")
            return None

        # Compare field by field — only include fields that differ
        all_keys: set[str] = set()
        if prev_json:
            all_keys.update(prev_json.keys())
        if curr_json:
            all_keys.update(curr_json.keys())

        changes: dict[str, Any] = {}
        for key in sorted(all_keys):
            prev_val = prev_json.get(key) if prev_json else None
            curr_val = curr_json.get(key) if curr_json else None
            if prev_val != curr_val:
                changes[key] = {"previous": prev_val, "current": curr_val}

        return changes if changes else None

    def _process_action_results(
        self,
        action_results: list[Any] | None,
        only_main_content: bool = True,
        include_tags: list[str] | None = None,
        exclude_tags: list[str] | None = None,
        content_mode: float = 0.5,
        query: str | None = None,
    ) -> ActionsOutput | None:
        """Process action results to extract screenshots and scrapes.

        Args:
            action_results: List of ActionResult objects from ActionRunner
            only_main_content: Whether to extract main content for markdown conversion
            include_tags: CSS selectors for elements to include
            exclude_tags: CSS selectors for elements to exclude
            content_mode: Precision/recall dial in [0.0, 1.0].
            query: Optional query string for section-relevance filtering.

        Returns:
            ActionsOutput with screenshots and scrapes, or None if no results
        """
        if not action_results:
            return None

        screenshots: list[str] = []
        scrapes: list[ScrapeActionResult] = []

        for result in action_results:
            # Handle screenshot actions
            if result.action_type == "screenshot" and result.screenshot:
                screenshot_b64 = base64.b64encode(result.screenshot).decode("utf-8")
                screenshots.append(screenshot_b64)

            # Handle scrape actions
            if result.action_type == "scrape" and result.scrape:
                # Convert HTML to markdown
                scrape_markdown = self._converter.convert(
                    result.scrape.html,
                    base_url=result.scrape.url,
                    only_main_content=only_main_content,
                    include_tags=include_tags,
                    exclude_tags=exclude_tags,
                    content_mode=content_mode,
                    query=query,
                )

                scrapes.append(
                    ScrapeActionResult(
                        url=result.scrape.url,
                        html=result.scrape.html,
                        markdown=scrape_markdown,
                    )
                )

        # Return None if no screenshots or scrapes were captured
        if not screenshots and not scrapes:
            return None

        return ActionsOutput(
            screenshots=screenshots if screenshots else None,
            scrapes=scrapes if scrapes else None,
        )

    def _looks_like_captcha(self, html: str) -> bool:
        """Detect if the page contains a CAPTCHA challenge.

        Args:
            html: Raw HTML content

        Returns:
            True if CAPTCHA is detected
        """
        captcha_patterns = [
            # reCAPTCHA
            r"g-recaptcha",
            r"grecaptcha",
            r"recaptcha/api",
            r"data-sitekey",
            # hCaptcha
            r"h-captcha",
            r"hcaptcha.com",
            r'class="h-captcha"',
            # Cloudflare Turnstile
            r"cf-turnstile",
            r"challenges.cloudflare.com/turnstile",
            # Generic CAPTCHA indicators
            r"iframe[^>]*captcha",
        ]

        import re

        for pattern in captcha_patterns:
            if re.search(pattern, html, re.IGNORECASE):
                return True
        return False

    async def _scrape_with_captcha_solving(
        self,
        url: str,
        formats: list[Any],
        only_main_content: bool,
        wait_for: int,
        timeout: int,
        screenshot_full_page: bool,
        actions: list[Any] | None,
        json_schema: dict[str, Any] | None,
        json_prompt: str | None,
        include_tags: list[str] | None,
        exclude_tags: list[str] | None,
        content_mode: float = 0.5,
        query: str | None = None,
    ) -> ScrapeResult | None:
        """Scrape a URL with CAPTCHA solving enabled.

        This method creates a new browser context, navigates to the page,
        detects and solves any CAPTCHA, then continues scraping.

        Args:
            url: URL to scrape
            formats: Output formats
            only_main_content: Extract main content only
            wait_for: Additional wait time after page load
            timeout: Page load timeout
            screenshot_full_page: Full page screenshot
            actions: Actions to execute
            json_schema: JSON schema for extraction
            json_prompt: Prompt for JSON extraction
            include_tags: CSS selectors to include
            exclude_tags: CSS selectors to exclude
            content_mode: Precision/recall dial in [0.0, 1.0].
            query: Optional query string for section-relevance filtering.

        Returns:
            ScrapeResult if successful, None if CAPTCHA solving failed

        Raises:
            ValidationError: If the URL or its resolved address is blocked
                (#152); see ``BrowserManager.fetch_page``'s docstring for the
                pre-flight-only caveat (this path navigates its own ad-hoc
                page rather than going through ``fetch_page``).
        """
        import asyncio

        from supacrawl.services.browser import _install_navigation_guard
        from supacrawl.services.captcha import (
            CaptchaSolver,
            CaptchaSolverError,
        )
        from supacrawl.services.url_guard import resolve_and_pin

        # Pre-flight SSRF check (#152): this method drives its own page.goto
        # rather than BrowserManager.fetch_page, so it needs its own check.
        # Off the event loop: resolve_and_pin does a blocking getaddrinfo.
        await asyncio.to_thread(resolve_and_pin, url)

        # Create browser context for CAPTCHA solving
        browser = BrowserManager(
            headless=self._headless,
            timeout_ms=timeout,
            locale_config=self._locale_config,
            stealth=self._stealth,
            proxy=self._proxy,
            engine=self._engine,
        )

        try:
            await browser.start()

            # Same page checkout as every other browser path, so this one inherits
            # the closed-engine detection and in-process relaunch rather than
            # hand-rolling context creation that would silently drift from it.
            # Camoufox's browser IS the context, so it owns no separate one.
            context, page = await browser._open_page(owns_context=browser.engine != "camoufox")

            try:
                # The pre-flight above only checks the original URL; a hostile
                # page reached after CAPTCHA-solving can still redirect or
                # issue its own subresource requests, so install the same
                # per-request re-validation BrowserManager's guarded paths get
                # (#152) before navigating.
                await _install_navigation_guard(page)
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout)

                # Detect and solve CAPTCHA
                solver = CaptchaSolver()
                try:
                    solved = await solver.detect_and_solve(page)
                    if solved:
                        LOGGER.info(f"CAPTCHA solved successfully for {url}")

                        # Wait for page to process the solution
                        await page.wait_for_load_state("networkidle", timeout=10000)

                        # Now re-scrape using the normal flow
                        # Get the new HTML after CAPTCHA is solved
                        html = await page.content()

                        # Check if CAPTCHA is still present
                        if self._looks_like_captcha(html):
                            LOGGER.warning("CAPTCHA still present after solving")
                            return None

                        # Build ScrapeResult from the solved page
                        # This is a simplified version - we could extract more data
                        markdown = self._converter.convert(
                            html,
                            base_url=url,
                            only_main_content=only_main_content,
                            include_tags=include_tags,
                            exclude_tags=exclude_tags,
                            content_mode=content_mode,
                            query=query,
                        )

                        metadata = await browser.extract_metadata(html)

                        return ScrapeResult(
                            success=True,
                            data=ScrapeData(  # type: ignore[call-arg]
                                markdown=markdown if "markdown" in formats else None,
                                html=self._get_clean_html(html, only_main_content, include_tags, exclude_tags)
                                if "html" in formats
                                else None,
                                raw_html=html if "rawHtml" in formats else None,
                                metadata=ScrapeMetadata(
                                    title=metadata.title,
                                    description=metadata.description,
                                    language=metadata.language,
                                    keywords=metadata.keywords,
                                    robots=metadata.robots,
                                    canonical_url=metadata.canonical_url,
                                    og_title=metadata.og_title,
                                    og_description=metadata.og_description,
                                    og_image=metadata.og_image,
                                    og_url=metadata.og_url,
                                    og_site_name=metadata.og_site_name,
                                    source_url=url,
                                    status_code=200,
                                    timezone=metadata.timezone,
                                    word_count=len(markdown.split()) if markdown else None,
                                ),
                            ),
                        )
                    else:
                        LOGGER.debug("No CAPTCHA found on page during solve attempt")
                        return None

                except CaptchaSolverError as e:
                    LOGGER.warning(f"CAPTCHA solving error: {e}")
                    return None

            finally:
                await page.close()

        finally:
            await browser.stop()
