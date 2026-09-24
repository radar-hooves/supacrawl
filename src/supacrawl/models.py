"""Data models for supacrawl."""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# =============================================================================
# Device Emulation
# =============================================================================

# Default device used by the --mobile shortcut
DEFAULT_MOBILE_DEVICE = "iPhone 14"

# =============================================================================
# Locale/Location Configuration
# =============================================================================

# Default country-to-locale mappings
COUNTRY_DEFAULTS: dict[str, tuple[str, str]] = {
    "AU": ("en-AU", "Australia/Sydney"),
    "US": ("en-US", "America/New_York"),
    "GB": ("en-GB", "Europe/London"),
    "DE": ("de-DE", "Europe/Berlin"),
    "FR": ("fr-FR", "Europe/Paris"),
    "JP": ("ja-JP", "Asia/Tokyo"),
    "CN": ("zh-CN", "Asia/Shanghai"),
    "IN": ("en-IN", "Asia/Kolkata"),
    "BR": ("pt-BR", "America/Sao_Paulo"),
    "CA": ("en-CA", "America/Toronto"),
    "NZ": ("en-NZ", "Pacific/Auckland"),
    "SG": ("en-SG", "Asia/Singapore"),
    "HK": ("zh-HK", "Asia/Hong_Kong"),
    "KR": ("ko-KR", "Asia/Seoul"),
    "ES": ("es-ES", "Europe/Madrid"),
    "IT": ("it-IT", "Europe/Rome"),
    "NL": ("nl-NL", "Europe/Amsterdam"),
    "SE": ("sv-SE", "Europe/Stockholm"),
    "CH": ("de-CH", "Europe/Zurich"),
    "AT": ("de-AT", "Europe/Vienna"),
    "BE": ("nl-BE", "Europe/Brussels"),
    "PL": ("pl-PL", "Europe/Warsaw"),
    "RU": ("ru-RU", "Europe/Moscow"),
    "MX": ("es-MX", "America/Mexico_City"),
    "AR": ("es-AR", "America/Buenos_Aires"),
}


class LocaleConfig(BaseModel):
    """Configuration for browser locale and timezone.

    Used to simulate requests from different geographic regions. As a local-first
    tool, Supacrawl sets browser locale/timezone and Accept-Language headers.
    For true geo-targeting, users should configure their own proxy.

    Usage:
        # From country code (uses sensible defaults)
        config = LocaleConfig.from_country("AU")

        # Explicit configuration
        config = LocaleConfig(language="en-AU", timezone="Australia/Sydney")
    """

    model_config = ConfigDict(extra="forbid")

    # ISO country code (e.g., "AU", "US", "DE")
    country: str | None = None

    # Language/locale code (e.g., "en-AU", "de-DE")
    language: str | None = None

    # IANA timezone (e.g., "Australia/Sydney", "Europe/Berlin")
    timezone: str | None = None

    @classmethod
    def from_country(cls, country: str) -> "LocaleConfig":
        """Create config with sensible defaults for a country.

        Args:
            country: ISO 3166-1 alpha-2 country code (e.g., "AU", "US", "DE")

        Returns:
            LocaleConfig with language and timezone defaults for the country.
        """
        country_upper = country.upper()
        lang, tz = COUNTRY_DEFAULTS.get(country_upper, ("en-US", "UTC"))
        return cls(country=country_upper, language=lang, timezone=tz)

    def get_language(self) -> str:
        """Get effective language, with fallback to en-US."""
        return self.language or "en-US"

    def get_timezone(self) -> str:
        """Get effective timezone, with fallback to UTC."""
        return self.timezone or "UTC"

    def get_accept_language_header(self) -> str:
        """Build Accept-Language header value.

        Returns:
            Accept-Language header string (e.g., "en-AU,en;q=0.9")
        """
        lang = self.get_language()
        # Extract base language (e.g., "en" from "en-AU")
        base_lang = lang.split("-")[0]
        if base_lang != lang:
            return f"{lang},{base_lang};q=0.9"
        return lang


class MapLink(BaseModel):
    """A discovered URL with metadata."""

    url: str
    title: str | None = None
    description: str | None = None


class MapResult(BaseModel):
    """Result of a map operation."""

    success: bool
    links: list[MapLink]
    error: str | None = None


class MapEvent(BaseModel):
    """Event emitted during URL mapping.

    Provides progress feedback during the potentially long-running mapping phase.
    """

    type: Literal["sitemap", "discovery", "metadata", "complete", "error"]
    discovered: int = 0
    total: int | None = None
    message: str | None = None
    result: MapResult | None = None  # Only set for "complete" type


class ScrapeMetadata(BaseModel):
    """Metadata extracted from a page."""

    # Core metadata
    title: str | None = None
    description: str | None = None
    language: str | None = None
    keywords: str | None = None
    robots: str | None = None
    canonical_url: str | None = None

    # OpenGraph metadata
    og_title: str | None = None
    og_description: str | None = None
    og_image: str | None = None
    og_url: str | None = None
    og_site_name: str | None = None

    # Source information
    source_url: str | None = None
    status_code: int | None = None

    # Detected timezone (IANA format, e.g. "America/New_York")
    timezone: str | None = None

    # Content metrics (computed)
    word_count: int | None = None

    # PDF document metadata (set when scraping a PDF URL)
    pdf_page_count: int | None = None
    pdf_author: str | None = None
    pdf_creation_date: str | None = None

    # Cache metadata
    cache_hit: bool = False
    cached_at: str | None = None

    def to_frontmatter(
        self,
        url: str | None = None,
        *,
        site_id: str | None = None,
        snapshot_id: str | None = None,
        content_hash: str | None = None,
        provider: str | None = None,
        scraped_at: datetime | None = None,
    ) -> str:
        """Build YAML frontmatter from metadata.

        Args:
            url: URL to use (defaults to source_url if not provided)
            site_id: Optional site identifier
            snapshot_id: Optional snapshot identifier
            content_hash: Optional content hash
            provider: Scraping provider name
            scraped_at: Timestamp to use (defaults to now if not provided)

        Returns:
            YAML frontmatter string including opening and closing delimiters.
        """

        def escape_yaml(s: str | None) -> str:
            """Escape string for YAML double-quoted value."""
            if not s:
                return ""
            return s.replace("\\", "\\\\").replace('"', '\\"')

        source = url or self.source_url or ""
        timestamp = scraped_at or datetime.now(timezone.utc)
        lines = [
            "---",
            f'url: "{escape_yaml(source)}"',
            f'title: "{escape_yaml(self.title)}"',
            f"scraped_at: {timestamp.isoformat()}",
        ]

        # Add optional metadata fields if provided
        if content_hash:
            lines.append(f"content_hash: sha256:{content_hash}")
        if site_id:
            lines.append(f"site_id: {site_id}")
        if snapshot_id:
            lines.append(f"snapshot_id: {snapshot_id}")
        if provider:
            lines.append(f"provider: {provider}")

        # Add detected timezone
        if self.timezone:
            lines.append(f"timezone: {self.timezone}")

        # Add optional core metadata
        if self.description:
            lines.append(f'description: "{escape_yaml(self.description)}"')
        if self.language:
            lines.append(f"language: {self.language}")
        if self.keywords:
            lines.append(f'keywords: "{escape_yaml(self.keywords)}"')
        if self.robots:
            lines.append(f"robots: {self.robots}")
        if self.canonical_url:
            lines.append(f'canonical_url: "{self.canonical_url}"')
        if self.status_code:
            lines.append(f"status_code: {self.status_code}")

        # Add OpenGraph metadata
        if self.og_title:
            lines.append(f'og_title: "{escape_yaml(self.og_title)}"')
        if self.og_description:
            lines.append(f'og_description: "{escape_yaml(self.og_description)}"')
        if self.og_image:
            lines.append(f'og_image: "{self.og_image}"')
        if self.og_url:
            lines.append(f'og_url: "{self.og_url}"')
        if self.og_site_name:
            lines.append(f'og_site_name: "{escape_yaml(self.og_site_name)}"')

        # Add content metrics
        if self.word_count:
            lines.append(f"word_count: {self.word_count}")

        lines.append("---")
        return "\n".join(lines)


class BrandingColors(BaseModel):
    """Brand colour palette."""

    primary: str | None = None
    secondary: str | None = None
    accent: str | None = None
    background: str | None = None
    text_primary: str | None = None
    text_secondary: str | None = None


class BrandingProfile(BaseModel):
    """Brand identity information extracted from page."""

    color_scheme: Literal["light", "dark"] | None = None
    logo: str | None = None
    colors: BrandingColors | None = None
    fonts: list[dict[str, str]] | None = None
    typography: dict[str, Any] | None = None
    spacing: dict[str, Any] | None = None
    components: dict[str, Any] | None = None
    images: dict[str, str] | None = None


class ScrapeActionResult(BaseModel):
    """Result of a mid-workflow scrape action.

    Captures page content at a specific point during an action sequence.
    """

    url: str
    html: str
    markdown: str | None = None


class ActionsOutput(BaseModel):
    """Output from page actions.

    Contains results from screenshot and scrape actions executed during
    a scrape workflow.
    """

    screenshots: list[str] | None = None  # Base64-encoded screenshots
    scrapes: list[ScrapeActionResult] | None = None  # Mid-workflow scrape captures


class ChangeTrackingDiff(BaseModel):
    """Unified diff output for change tracking."""

    text: str  # Unified diff string


class ChangeTrackingData(BaseModel):
    """Change tracking result comparing current scrape to previous cached version.

    Change statuses:
        new — No previous cached version exists for this URL
        same — Content hash matches the cached version (no meaningful change)
        changed — Content differs from the cached version
        removed — URL returns an error but a cached version exists
    """

    previous_scrape_at: str | None = None  # ISO timestamp of previous cached version
    change_status: Literal["new", "same", "changed", "removed"]
    visibility: Literal["visible", "hidden"] = "visible"
    diff: ChangeTrackingDiff | None = None  # Git-style unified diff (when requested)
    json_changes: dict[str, Any] | None = None  # Field-level JSON comparison {field: {previous, current}}
    content_hash: str | None = None  # SHA256 hash of current markdown content


class StructuredData(BaseModel):
    """Structured data harvested deterministically from a page (no LLM).

    Each field is None when that source is absent, so a populated field always
    reflects data the site itself published rather than a scrape heuristic.
    """

    json_ld: list[Any] | None = None  # schema.org application/ld+json objects (@graph flattened)
    microdata: list[dict[str, Any]] | None = None  # itemscope/itemprop items
    opengraph: dict[str, str] | None = None  # og:* meta properties
    next_data: dict[str, Any] | None = None  # Next.js __NEXT_DATA__ hydration payload


class QualityVerdict(str, Enum):
    """Classification of how usable a scrape result is.

    The taxonomy is gate-then-grade: the verdict is resolved first from cheap
    structural signals (status code, challenge fingerprints, content density),
    then a 0-100 score grades fidelity within that verdict. The verdict tells a
    calling agent *what kind* of result it got; the score tells it *how good*.

    Verdicts split into usable (content was returned) and hard-fail (no usable
    content). Hard-fail verdicts force ``success=False`` so an agent never passes
    a block page or soft-404 downstream into a RAG collection.
    """

    OK = "ok"  # clean, usable content
    THIN = "thin"  # very little content; may be all there is, or under-extracted
    JS_SHELL = "js_shell"  # a pre-hydration skeleton; real content is injected client-side
    PAYWALL = "paywall"  # a login/subscription wall; the wall page is what was returned
    BOT_CHALLENGE = "bot_challenge"  # an anti-bot interstitial (Cloudflare/Akamai/DataDome/etc.)
    CAPTCHA = "captcha"  # an explicit CAPTCHA challenge
    ERROR_STATUS = "error_status"  # an HTTP >= 400 response (including soft-404 shells)
    GARBLED_PDF = "garbled_pdf"  # PDF text extracted but spacing/encoding is corrupt
    EMPTY = "empty"  # no content could be extracted at all
    INFRASTRUCTURE = "infrastructure"  # supacrawl's own engine failed; says nothing about the site
    TARPIT = "tarpit"  # a self-referential link maze of generated content (Nepenthes/iocaine-style)


# Verdicts that mean no usable content was returned, so ``success`` must be False.
HARD_FAIL_VERDICTS: frozenset[QualityVerdict] = frozenset(
    {
        QualityVerdict.BOT_CHALLENGE,
        QualityVerdict.CAPTCHA,
        QualityVerdict.ERROR_STATUS,
        QualityVerdict.GARBLED_PDF,
        QualityVerdict.EMPTY,
        QualityVerdict.INFRASTRUCTURE,
        QualityVerdict.TARPIT,
    }
)

# The one verdict that indicts supacrawl itself rather than the target. Kept as a
# set so a caller (and our own code) can branch on "is this our fault?" without
# enumerating verdict names.
SCRAPER_FAULT_VERDICTS: frozenset[QualityVerdict] = frozenset({QualityVerdict.INFRASTRUCTURE})

# The concrete next step a caller should take for each non-OK verdict. This is the
# single source of the documented contract that a non-ok verdict always carries a
# suggestion; ``QualityAssessment`` fills it from here so no construction site can
# hand a caller a bare verdict with nothing to act on.
VERDICT_SUGGESTIONS: dict[QualityVerdict, str] = {
    QualityVerdict.BOT_CHALLENGE: (
        "The site served an anti-bot challenge. supacrawl auto-escalates stealth engines; if it persists, "
        "install supacrawl[stealth] or supacrawl[camoufox], or route through a proxy."
    ),
    QualityVerdict.CAPTCHA: (
        "A CAPTCHA was presented. Enable supacrawl[captcha] with CAPTCHA_API_KEY and solve_captcha=True, "
        "or fetch the same information from a different source."
    ),
    QualityVerdict.JS_SHELL: (
        "The page returned a pre-hydration shell; the real content is rendered client-side. Increase wait_for "
        "(e.g. 5000) so the content can hydrate."
    ),
    QualityVerdict.THIN: (
        "Very little content was extracted. Try only_main_content=False to keep more of the page, a larger "
        "wait_for if it loads late, or check whether the page needs authentication."
    ),
    QualityVerdict.PAYWALL: (
        "The page appears to require login or a subscription; only the wall page was returned. Supply session "
        "headers/cookies if you have access."
    ),
    QualityVerdict.GARBLED_PDF: (
        "PDF text was extracted but inter-word spacing looks corrupt. Try --parse-pdf ocr for a clean re-read."
    ),
    QualityVerdict.EMPTY: (
        "No content could be extracted. Try a larger wait_for, only_main_content=False, or a different engine."
    ),
    QualityVerdict.ERROR_STATUS: (
        "The server answered with an HTTP error, so there was never any content to extract. Check the URL is "
        "current (it may have moved or been withdrawn) and try the canonical or an archived copy. Re-running "
        "the same URL with different scrape options will not change an HTTP status."
    ),
    QualityVerdict.INFRASTRUCTURE: (
        "supacrawl's own browser engine failed — this is a scraper-side fault and says nothing about the "
        "target site. The engine is relaunched automatically, so retry this scrape once. If it fails again, "
        "the scraper's environment is broken (missing browser binaries, out of memory, or no shared memory): "
        "restart the supacrawl server and check its logs. Changing engine, wait_for, or other scrape options "
        "will not help."
    ),
    QualityVerdict.TARPIT: (
        "This page is a crawler tarpit: an infinite maze of generated content designed to poison and trap "
        "bots, not a real page. Do not retry with a different engine or a longer wait_for — the maze "
        "regenerates on every fetch. Treat the URL as unreachable and find the real resource via the site's "
        "own search or navigation instead."
    ),
}

# Used when a verdict somehow has no mapped suggestion, so the contract holds even
# for a verdict added without a mapping.
FALLBACK_SUGGESTION = (
    "The scrape did not return clean content. Read quality.reasons for why, then retry with "
    "only_main_content=False, a larger wait_for, or a different source."
)


class QualityAssessment(BaseModel):
    """Structured, honest signal of how good a scrape result is.

    Surfaced on every ``ScrapeResult`` so an MCP/REST/CLI caller can decide to
    accept, retry, or escalate without re-deriving quality from the raw content.
    Shares its metric vocabulary with the offline benchmark (``benchmark.metrics``)
    so the same definition of "good" governs both the fitness function and the
    live signal.
    """

    verdict: QualityVerdict
    score: int = Field(ge=0, le=100)  # 0-100 confidence/completeness within the verdict
    reasons: list[str] = Field(default_factory=list)  # why this verdict/score was reached
    suggestion: str | None = None  # a concrete next action when the result is poor
    attempts: int = 1  # how many strategies were tried (set by auto-escalation)
    escalated: bool = False  # whether auto-escalation moved beyond the cheapest strategy

    @model_validator(mode="after")
    def _guarantee_suggestion(self) -> "QualityAssessment":
        """Fill ``suggestion`` for any non-OK verdict that arrived without one.

        The tool contract promises a caller that a non-ok verdict always carries a
        concrete next step, and callers act on that (retry vs escalate vs give up).
        Enforcing it here rather than at each construction site is what makes the
        promise true for every failure path, including ones added later.
        """
        if self.verdict is not QualityVerdict.OK and self.suggestion is None:
            self.suggestion = VERDICT_SUGGESTIONS.get(self.verdict, FALLBACK_SUGGESTION)
        return self

    @property
    def is_usable(self) -> bool:
        """Whether the result carries usable content (verdict is not a hard fail)."""
        return self.verdict not in HARD_FAIL_VERDICTS

    @property
    def is_scraper_fault(self) -> bool:
        """Whether the failure was supacrawl's own, not the target site's."""
        return self.verdict in SCRAPER_FAULT_VERDICTS


class ScrapeData(BaseModel):
    """Scraped content from a page."""

    markdown: str | None = None
    html: str | None = None
    raw_html: str | None = None
    screenshot: str | None = None  # Base64-encoded PNG screenshot
    pdf: str | None = None  # Base64-encoded PDF document
    llm_extraction: dict[str, Any] | None = Field(None, alias="json")  # LLM-extracted structured data
    structured_data: StructuredData | None = None  # Deterministic embedded structured data (no LLM)
    summary: str | None = None  # LLM-generated summary of page content
    metadata: ScrapeMetadata
    links: list[str] | None = None
    images: list[str] | None = None  # Image URLs extracted from page
    branding: BrandingProfile | None = None  # Brand identity information
    actions: ActionsOutput | None = None  # Results from action sequence
    change_tracking: ChangeTrackingData | None = None  # Change detection vs previous scrape

    model_config = ConfigDict(populate_by_name=True)


class ScrapeResult(BaseModel):
    """Result of a scrape operation.

    The `warnings` field contains content quality warnings when extraction
    succeeded but the result may be incomplete or problematic. Check warnings
    when `success=True` but content seems minimal or unexpected.

    The `quality` field carries a structured verdict + 0-100 score describing how
    usable the result is (clean page vs JS shell vs bot challenge vs soft-404).
    `success` is honest about hard failures: an HTTP >= 400 response or a
    recognised block/CAPTCHA interstitial is reported `success=False` even when a
    response body was returned.
    """

    success: bool
    data: ScrapeData | None = None
    error: str | None = None
    warnings: list[str] | None = None
    quality: QualityAssessment | None = None


class CrawlEvent(BaseModel):
    """Event emitted during crawl."""

    type: Literal["mapping", "progress", "page", "complete", "error"]
    url: str | None = None
    data: ScrapeData | None = None
    completed: int = 0
    total: int = 0  # 0 indicates unknown during mapping/discovery phase
    error: str | None = None
    message: str | None = None  # Descriptive text for mapping phase
    change_summary: dict[str, int] | None = None  # Crawl-level change tracking summary


# =============================================================================
# Search Models
# =============================================================================


class SearchSourceType(str, Enum):
    """Search source types."""

    WEB = "web"
    IMAGES = "images"
    NEWS = "news"


class SearchFilters(BaseModel):
    """Provider-agnostic search filters mapped onto each provider's native API.

    Filters a provider cannot express natively are applied where possible by
    rewriting the query with ``site:`` operators (domains), or skipped with a
    debug log. ``time_range`` and ``start_date``/``end_date`` are alternative
    ways to express recency; when both are given, explicit dates take precedence.
    """

    model_config = ConfigDict(extra="forbid")

    time_range: Literal["day", "week", "month", "year"] | None = None
    start_date: str | None = None  # ISO 8601 date (YYYY-MM-DD)
    end_date: str | None = None  # ISO 8601 date (YYYY-MM-DD)
    topic: Literal["general", "news", "finance"] | None = None
    include_domains: list[str] | None = None
    exclude_domains: list[str] | None = None

    def is_empty(self) -> bool:
        """True when no filter is set (lets providers skip filter handling)."""
        return not any(
            (
                self.time_range,
                self.start_date,
                self.end_date,
                self.topic,
                self.include_domains,
                self.exclude_domains,
            )
        )


class SearchResultItem(BaseModel):
    """Individual search result."""

    url: str
    title: str
    description: str | None = None
    source_type: SearchSourceType = SearchSourceType.WEB

    # Image-specific fields
    thumbnail: str | None = None
    image_width: int | None = None
    image_height: int | None = None

    # News-specific fields
    published_at: str | None = None
    source_name: str | None = None

    # Scraped content (if scrape_options provided)
    markdown: str | None = None
    html: str | None = None
    metadata: ScrapeMetadata | None = None


class SearchEngineError(BaseModel):
    """An upstream engine that failed during a SearXNG metasearch query.

    SearXNG reports these as ``unresponsive_engines``: the per-engine failures
    that thinned or emptied a result set. Surfacing them lets a caller tell "no
    results exist for this query" from "every engine that would have answered
    was down" — the ambiguity that made an empty result set unreadable (#161).
    """

    engine: str
    reason: str


class SearchResult(BaseModel):
    """Search operation result."""

    success: bool
    data: list[SearchResultItem]
    error: str | None = None
    # Which provider actually answered, and whether that provider was one the
    # operator configured. Without these a caller cannot tell a configured
    # provider from a silent fallback (#158).
    provider: str | None = None
    provider_fallback: bool = False
    # Upstream engines that failed on this query (SearXNG only). Populated when
    # an empty/failed result was caused by engines being down, so `data: []` is
    # never mistaken for "no such thing exists" (#161).
    unresponsive_engines: list[SearchEngineError] = Field(default_factory=list)
    # True when the last full window of caller searches ALL came back empty — the
    # signal that lets a caller tell an empty `data` that is a genuine no-match
    # from one that is a backend which has stopped answering (a CAPTCHA-walled
    # SearXNG replying 200-with-nothing), without polling the health surface.
    # A single no-match against a healthy run leaves this False; it only trips
    # once a full RECENT_WINDOW of real searches has come back empty in a row.
    # Extends the health-only `recent_search_health.all_recent_empty` (#161) onto
    # the response itself.
    all_recent_empty: bool = False


# =============================================================================
# Extract Models
# =============================================================================


class ExtractResultItem(BaseModel):
    """Extraction result for a single URL."""

    url: str
    success: bool
    data: dict[str, Any] | None = None
    error: str | None = None


class ExtractResult(BaseModel):
    """Overall extraction result."""

    success: bool
    data: list[ExtractResultItem]
    error: str | None = None


# =============================================================================
# Agent Models
# =============================================================================


class AgentEvent(BaseModel):
    """Event emitted during agent execution."""

    type: Literal["thinking", "action", "result", "complete", "error"]
    message: str | None = None
    url: str | None = None
    data: dict[str, Any] | None = None


class AgentResult(BaseModel):
    """Final agent result."""

    success: bool
    data: dict[str, Any] | None = None
    urls_visited: list[str] = Field(default_factory=list)
    error: str | None = None
