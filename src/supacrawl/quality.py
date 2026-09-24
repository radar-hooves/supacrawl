"""Shared scrape-quality vocabulary: one definition for offline and online.

This module is the single home of the *reference-free* quality metrics — the
ones that need only the scraper's own output, no independent browser capture to
compare against. The benchmark (``benchmark.metrics``) consumes these for its
fitness function, and the runtime quality assessor (:func:`assess_quality`)
consumes the same functions so a page judged "good" offline is judged "good"
live. Keeping them here, below both consumers, stops the two definitions of
"quality" from drifting apart.

Everything here is pure and side-effect free: no I/O, no network, no browser.
The reference-*based* metrics (token F1, ROUGE-L, char-coverage against a gold
capture) stay in ``benchmark.metrics`` because they only make sense offline.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urljoin, urlparse

from supacrawl.models import HARD_FAIL_VERDICTS, QualityAssessment, QualityVerdict

# ``supacrawl.services.detection`` holds the pure CDN/framework/bot heuristics
# this module shares. It is imported lazily inside ``_classify`` rather than at
# module load: importing the ``services`` package runs its ``__init__`` (which
# eager-imports ScrapeService), and ScrapeService imports this module — a cycle
# at import time. Deferring to call-time, after both modules are fully loaded,
# breaks it. The functions are pure and sys.modules-cached, so the call-time cost
# is a dict lookup.

# ---------------------------------------------------------------------------
# Reference-free metric primitives (shared with benchmark.metrics)
# ---------------------------------------------------------------------------

# Word-spacing sanity. A token longer than this many characters that is pure
# ASCII letters is almost certainly a fused word run from a PDF-extraction
# spacing defect ("Thedominantsequencetransduction") rather than a genuine word.
# Non-Latin scripts tokenise into long runs legitimately, so only all-ASCII
# alphabetic tokens count. A page whose fused share reaches the full-penalty
# ratio scores 0 on the spacing dimension; below the minimum token count there
# is too little prose to judge.
_WORD_SPACING_LONG_TOKEN_CHARS = 30
_WORD_SPACING_FULL_PENALTY_RATIO = 0.05
_WORD_SPACING_MIN_TOKENS = 50

_WORD_RE = re.compile(r"\w+", re.UNICODE)

# Markdown syntax stripped before tokenising so that formatting characters do
# not masquerade as content tokens.
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_CODE_FENCE_RE = re.compile(r"```[^\n]*")
_MD_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)

# Structure counters operate on raw markdown.
_HEADING_LINE_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S", re.MULTILINE)
_FENCE_RE = re.compile(r"^\s{0,3}```", re.MULTILINE)
_TABLE_ROW_RE = re.compile(r"^\s{0,3}\|.*\|\s*$", re.MULTILINE)
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_RE = re.compile(r"(?<!\!)\[[^\]]*\]\([^)]*\)")


def strip_markdown(text: str) -> str:
    """Reduce markdown to its visible prose for tokenising.

    Images are dropped entirely, links collapse to their anchor text, and code
    fence markers and heading hashes are removed. The result is not meant to be
    read; it is an intermediate for word tokenisation.

    Args:
        text: Markdown source.

    Returns:
        Plain-ish text with markdown scaffolding removed.
    """
    text = _MD_IMAGE_RE.sub(" ", text)
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _MD_CODE_FENCE_RE.sub(" ", text)
    text = _MD_HEADING_RE.sub("", text)
    return text


def tokenize(text: str, *, markdown: bool = False) -> list[str]:
    """Lowercase word-token a string.

    Args:
        text: Source text.
        markdown: When True, strip markdown scaffolding first.

    Returns:
        Lowercased word tokens. For languages without whitespace word
        boundaries (e.g. CJK) this under-segments; character-level metrics carry
        the completeness signal for those pages instead.
    """
    if markdown:
        text = strip_markdown(text)
    return [match.group(0).lower() for match in _WORD_RE.finditer(text)]


def substring_hit_rate(text: str, needles: list[str]) -> float | None:
    """Fraction of anchor substrings present in ``text`` (case-insensitive).

    Args:
        text: Haystack (typically the scraper markdown).
        needles: Curated anchor substrings.

    Returns:
        Fraction in ``[0, 1]``, or ``None`` when no anchors were supplied.
    """
    if not needles:
        return None
    haystack = text.lower()
    hits = sum(1 for needle in needles if needle.lower() in haystack)
    return hits / len(needles)


def substring_absent_rate(text: str, needles: list[str]) -> float | None:
    """Fraction of boilerplate substrings correctly absent (case-insensitive).

    Args:
        text: Haystack (typically the scraper markdown).
        needles: Substrings that good output should not contain.

    Returns:
        Fraction in ``[0, 1]`` (1 means none of the boilerplate leaked), or
        ``None`` when no anchors were supplied.
    """
    if not needles:
        return None
    haystack = text.lower()
    absent = sum(1 for needle in needles if needle.lower() not in haystack)
    return absent / len(needles)


def count_structure(markdown: str) -> dict[str, int]:
    """Count structural markdown elements.

    Code fences are counted in pairs (an opening and closing fence make one
    block); table rows are counted whole, then the customary header/separator
    pair is netted out so a simple table reports its body row count.

    Args:
        markdown: Markdown source.

    Returns:
        Mapping with ``headings``, ``code_blocks``, ``tables``, ``images`` and
        ``links`` counts.
    """
    fences = len(_FENCE_RE.findall(markdown))
    table_rows = len(_TABLE_ROW_RE.findall(markdown))
    return {
        "headings": len(_HEADING_LINE_RE.findall(markdown)),
        "code_blocks": fences // 2,
        "tables": max(0, table_rows - 2) if table_rows else 0,
        "images": len(_IMAGE_RE.findall(markdown)),
        "links": len(_LINK_RE.findall(markdown)),
    }


def link_density(link_count: int, word_count: int) -> float:
    """Links per 1000 words.

    A high density on a content page is a strong tell that navigation menus or
    link farms were not stripped.

    Args:
        link_count: Number of markdown links.
        word_count: Number of word tokens.

    Returns:
        Links per 1000 words; 0 when there are no words.
    """
    if word_count <= 0:
        return 0.0
    return link_count * 1000.0 / word_count


def word_spacing(markdown: str) -> float | None:
    """Score how well inter-word spacing survived, penalising fused word runs.

    A recurring PDF-extraction defect collapses adjacent words into a single
    token ("Thedominantsequencetransduction") when the source font's space
    glyph is narrower than the extractor's gap threshold. This metric flags that
    by measuring the share of tokens that are improbably long runs of ASCII
    letters. Non-Latin scripts (CJK, Arabic) legitimately tokenise into long
    runs under whitespace tokenisation, so only all-ASCII alphabetic tokens
    count, and very short bodies return ``None`` (too little signal to judge).

    Args:
        markdown: Scraper markdown output.

    Returns:
        Score in ``[0, 1]`` — 1.0 when spacing is clean, falling to 0.0 once a
        full-penalty share of tokens are fused — or ``None`` when there is too
        little prose to judge.
    """
    tokens = tokenize(markdown, markdown=True)
    if len(tokens) < _WORD_SPACING_MIN_TOKENS:
        return None
    fused = sum(
        1 for token in tokens if len(token) > _WORD_SPACING_LONG_TOKEN_CHARS and token.isascii() and token.isalpha()
    )
    ratio = fused / len(tokens)
    return max(0.0, 1.0 - ratio / _WORD_SPACING_FULL_PENALTY_RATIO)


# ---------------------------------------------------------------------------
# Crawler-tarpit / link-maze detection
# ---------------------------------------------------------------------------
# Nepenthes/iocaine-style AI-crawler tarpits serve real HTTP 200s with a
# plausible word count, so the density gate above waves them through — the
# tell is structural, not textual: every link on the page is a freshly minted
# child of the page's OWN url, and the crawler that follows one just gets
# another page with the same shape, forever. A genuine article's links point
# sideways (other articles) or up (categories, home), never recursively into
# paths that only exist because the page itself made them up.

# Markdown link/image target extraction, capturing the URL rather than merely
# counting it (unlike `_LINK_RE`/`_IMAGE_RE` above).
_LINK_TARGET_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_IMAGE_TARGET_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")

# A path segment built from three or more hyphen-joined dictionary-looking
# words ("stood-thinning-unreeling") reads as generated filler: a real slug is
# usually one to three words lifted from the page's own title, not an
# arbitrary chain of unrelated terms. Combined with a raw non-ASCII codepoint
# (a tarpit's poison keywords, CJK included, land straight in the URL) this
# distinguishes generated children from a legitimate nested doc/category page.
_GENERATED_WORD_RUN_RE = re.compile(r"^[a-z]+(?:-[a-z]+){2,}", re.IGNORECASE)

# Minimum same-host links needed before the maze ratio means anything — a page
# with a handful of links proves nothing either way.
_TARPIT_MIN_LINKS = 5
# Share of same-host links that must be strict path-children of the page's own
# URL. High on purpose: a real category/TOC page mixes in siblings, a home
# link, breadcrumbs — a page that links to almost nothing BUT its own children
# is not a plausible human information architecture.
_TARPIT_DESCENDANT_RATIO = 0.8
# Of those descendant links, the share that must look machine-generated.
_TARPIT_GENERATED_RATIO = 0.5


def _extract_link_targets(markdown: str) -> list[str]:
    """Pull every markdown link/image target URL, in document order.

    Args:
        markdown: Markdown source.

    Returns:
        Raw target strings exactly as written (may be relative).
    """
    return _LINK_TARGET_RE.findall(markdown) + _IMAGE_TARGET_RE.findall(markdown)


def _is_generated_slug(path: str) -> bool:
    """True when a URL path's last segment looks machine-generated, not authored.

    Args:
        path: A URL path (not the full URL).

    Returns:
        True when the final segment is a raw non-ASCII codepoint or a run of
        three or more hyphen-joined alphabetic words.
    """
    segment = unquote(path.rstrip("/").rsplit("/", 1)[-1])
    if not segment:
        return False
    if any(ord(ch) > 127 for ch in segment):
        return True
    return bool(_GENERATED_WORD_RUN_RE.match(segment))


def link_maze_signal(markdown: str, page_url: str | None) -> tuple[float, float, int] | None:
    """Measure how much a page's own links look like a self-referential maze.

    Args:
        markdown: Extracted markdown carrying the page's links/images.
        page_url: The URL that was scraped, used to resolve relative links and
            to test whether a link is a path-descendant of the page itself.

    Returns:
        ``(descendant_ratio, generated_ratio, same_host_count)`` — the share of
        same-host links that are a strict path-descendant of the page's own
        URL, the share of those descendants with a generated-looking slug, and
        how many same-host links were found — or ``None`` when there is no
        page URL to resolve against or no same-host links to judge.
    """
    if not page_url:
        return None
    targets = _extract_link_targets(markdown)
    if not targets:
        return None

    page = urlparse(page_url)
    own_path = page.path.rstrip("/")

    same_host = 0
    descendant = 0
    generated = 0
    for target in targets:
        resolved = urlparse(urljoin(page_url, target))
        if resolved.netloc != page.netloc:
            continue
        same_host += 1
        candidate_path = resolved.path.rstrip("/")
        if candidate_path != own_path and candidate_path.startswith(own_path + "/"):
            descendant += 1
            if _is_generated_slug(resolved.path):
                generated += 1

    if same_host == 0:
        return None
    descendant_ratio = descendant / same_host
    generated_ratio = generated / descendant if descendant else 0.0
    return descendant_ratio, generated_ratio, same_host


# ---------------------------------------------------------------------------
# Runtime quality assessment (gate-then-grade)
# ---------------------------------------------------------------------------

# Below this word count a page is "thin": not enough prose to be a useful answer.
# Matches the floor used elsewhere for platform/CAPTCHA thin-content checks.
_THIN_WORD_FLOOR = 50
# Word count at which density confidence saturates: a substantial article.
_DENSITY_SATURATION_WORDS = 300
# Prefix sampled for the binary/non-text ratio check (chars).
_NON_PRINTABLE_SAMPLE = 8192
# Non-printable ratio above this means the body is not readable text.
_NON_PRINTABLE_RATIO = 0.30
# Links-per-1000-words at which the link-density score reaches its floor.
_LINK_DENSITY_FLOOR = 50.0
# word_spacing below this on a PDF means the text is fused/garbled.
_GARBLED_PDF_SPACING = 0.5

# Score ceilings per non-OK verdict, so the numeric score can never imply a
# usable page when the verdict says otherwise.
_VERDICT_SCORE_CEILING: dict[QualityVerdict, int] = {
    QualityVerdict.JS_SHELL: 30,
    QualityVerdict.PAYWALL: 45,
    QualityVerdict.THIN: 45,
    QualityVerdict.BOT_CHALLENGE: 15,
    QualityVerdict.CAPTCHA: 15,
    QualityVerdict.ERROR_STATUS: 10,
    QualityVerdict.GARBLED_PDF: 25,
    QualityVerdict.EMPTY: 0,
    QualityVerdict.TARPIT: 10,
}


def _reference_free_score(*, word_count: int, link_count: int, spacing: float | None) -> int:
    """Grade a usable page 0-100 from signals that need no reference capture.

    Blends completeness (does the page carry enough prose?), spacing fidelity
    (PDF fused-word defect), and link cleanliness (was navigation chrome left
    in?). Each component is in ``[0, 1]``; the weighted blend is scaled to 100.

    Args:
        word_count: Visible word count of the extracted content.
        link_count: Number of links in the extracted markdown.
        spacing: ``word_spacing`` value, or None when there is too little prose.

    Returns:
        Integer score in ``[0, 100]``.
    """
    density = min(1.0, word_count / _DENSITY_SATURATION_WORDS)
    spacing_component = spacing if spacing is not None else 1.0
    ld = link_density(link_count, word_count)
    link_component = max(0.0, 1.0 - ld / _LINK_DENSITY_FLOOR)

    blended = 0.55 * density + 0.30 * spacing_component + 0.15 * link_component
    return round(100 * blended)


def _classify(
    *,
    status_code: int | None,
    html: str | None,
    text: str,
    word_count: int,
    is_pdf: bool,
    spacing: float | None,
    page_url: str | None = None,
) -> tuple[QualityVerdict, list[str]]:
    """Resolve the verdict from cheap structural signals (the "gate").

    Returns the verdict and the human-readable reasons behind it. Ordering is
    deliberate: hard, unambiguous signals (HTTP status, non-text body, explicit
    challenge fingerprints) are checked before softer density heuristics so a
    blocked page is never mistaken for merely "thin".
    """
    from supacrawl.services.detection import (
        detect_bot_protection,
        detect_js_framework,
        detect_login_required,
    )

    reasons: list[str] = []

    if is_pdf:
        if word_count == 0:
            return QualityVerdict.EMPTY, ["PDF produced no extractable text"]
        if spacing is not None and spacing < _GARBLED_PDF_SPACING:
            return QualityVerdict.GARBLED_PDF, [f"PDF word-spacing score {spacing:.2f} indicates fused text"]
        if word_count < _THIN_WORD_FLOOR:
            return QualityVerdict.THIN, [f"PDF extracted only {word_count} words"]
        return QualityVerdict.OK, reasons

    # HTTP status is the most reliable hard signal.
    if status_code is not None:
        if status_code in (403, 429, 503):
            return QualityVerdict.BOT_CHALLENGE, [f"HTTP {status_code} (access throttled or blocked)"]
        if status_code >= 400:
            return QualityVerdict.ERROR_STATUS, [f"HTTP {status_code} error response"]

    if not html:
        if word_count == 0:
            return QualityVerdict.EMPTY, ["no HTML and no extracted text"]
        return QualityVerdict.OK, reasons

    # Binary / non-text body (e.g. an encrypted challenge payload served at 200).
    sample = html[:_NON_PRINTABLE_SAMPLE]
    if sample:
        non_printable = sum(1 for ch in sample if ord(ch) < 32 and ch not in "\t\n\r")
        if non_printable / len(sample) > _NON_PRINTABLE_RATIO:
            return QualityVerdict.BOT_CHALLENGE, ["response body is largely non-text (challenge or binary)"]

    indicators = detect_bot_protection(html)
    challenge = indicators["challenge_detected"] or indicators["access_denied"]
    # A challenge/CAPTCHA fingerprint only condemns the page when little real
    # content came through — a content page that merely embeds a CAPTCHA widget
    # (e.g. a comment form) still extracted fine.
    if indicators["captcha_present"] and word_count < _THIN_WORD_FLOOR:
        # A CAPTCHA widget wrapped in a CDN "managed challenge" interstitial
        # (Cloudflare "just a moment"/"checking your browser"/challenge-form) is
        # passable by a stealth engine, so it stays a BOT_CHALLENGE and keeps
        # escalating. A bare CAPTCHA widget is a hard third-party wall
        # (reCAPTCHA/hCaptcha) that a stronger engine will not defeat — it is a
        # CAPTCHA, and the ladder fails fast on it (#153). Key off
        # challenge_detected only, NOT access_denied: an "access denied"/"blocked"
        # page carrying a bare CAPTCHA is a terminal wall, not a passable
        # interstitial, and must keep failing fast.
        if indicators["challenge_detected"]:
            return QualityVerdict.BOT_CHALLENGE, [
                "CAPTCHA inside an anti-bot challenge interstitial with no usable content"
            ]
        return QualityVerdict.CAPTCHA, ["CAPTCHA challenge present with no usable content"]
    if challenge and word_count < _THIN_WORD_FLOOR:
        return QualityVerdict.BOT_CHALLENGE, ["anti-bot challenge fingerprint with no usable content"]

    if word_count == 0:
        return QualityVerdict.EMPTY, ["no text content extracted"]

    if word_count < _THIN_WORD_FLOOR:
        # Distinguish a JS shell (real content is rendered client-side and never
        # arrived) from a genuinely thin page, and a paywall from both. A short
        # body alone is not enough to call "shell" — require a real client-side
        # framework marker, else a small static page would be mislabelled.
        framework = detect_js_framework(html)
        if framework is not None:
            return QualityVerdict.JS_SHELL, [f"only {word_count} words on a {framework} page (pre-hydration shell)"]
        if detect_login_required(html):
            return QualityVerdict.PAYWALL, [f"only {word_count} words behind an apparent login/paywall"]
        return QualityVerdict.THIN, [f"only {word_count} words extracted"]

    maze = link_maze_signal(text, page_url)
    if maze is not None:
        descendant_ratio, generated_ratio, same_host_count = maze
        if (
            same_host_count >= _TARPIT_MIN_LINKS
            and descendant_ratio >= _TARPIT_DESCENDANT_RATIO
            and generated_ratio >= _TARPIT_GENERATED_RATIO
        ):
            return QualityVerdict.TARPIT, [
                f"{descendant_ratio:.0%} of {same_host_count} same-host links are generated-looking children "
                "of this page's own URL (Nepenthes/iocaine-style crawler tarpit)"
            ]

    return QualityVerdict.OK, reasons


def assess_quality(
    *,
    status_code: int | None,
    html: str | None,
    markdown: str | None,
    visible_text: str | None = None,
    is_pdf: bool = False,
    url: str | None = None,
) -> QualityAssessment:
    """Assess how usable a scrape result is: a verdict plus a 0-100 score.

    Gate-then-grade. The verdict is resolved first from cheap structural signals
    (HTTP status, anti-bot fingerprints, content density, link-maze shape). The
    score then grades fidelity within that verdict using the shared
    reference-free metrics, and is capped so a numeric score can never
    contradict a non-OK verdict.

    Args:
        status_code: HTTP status of the response, or None when unknown.
        html: Raw HTML of the page, or None for the PDF path.
        markdown: Extracted markdown, when a markdown format was requested.
        visible_text: Pre-computed visible text fallback when no markdown was
            produced (e.g. a links-only request), so density is still judged.
        is_pdf: True when this is a PDF extraction (no HTML; spacing matters).
        url: The URL that was scraped, needed to detect a self-referential
            crawler-tarpit link maze; omitted, that check simply never fires.

    Returns:
        A :class:`QualityAssessment` carrying the verdict, score, reasons, and a
        concrete suggestion when the result is poor.
    """
    text = markdown if markdown is not None else (visible_text or "")
    word_count = len(text.split())
    spacing = word_spacing(text) if text else None

    verdict, reasons = _classify(
        status_code=status_code,
        html=html,
        text=text,
        word_count=word_count,
        is_pdf=is_pdf,
        spacing=spacing,
        page_url=url,
    )

    link_count = count_structure(text)["links"] if text else 0
    score = _reference_free_score(word_count=word_count, link_count=link_count, spacing=spacing)

    if verdict in _VERDICT_SCORE_CEILING:
        score = min(score, _VERDICT_SCORE_CEILING[verdict])

    # ``suggestion`` is filled by QualityAssessment itself from VERDICT_SUGGESTIONS,
    # so the contract holds for every construction site, not just this one.
    return QualityAssessment(verdict=verdict, score=score, reasons=reasons)


__all__ = [
    "HARD_FAIL_VERDICTS",
    "QualityAssessment",
    "QualityVerdict",
    "assess_quality",
    "count_structure",
    "link_density",
    "link_maze_signal",
    "strip_markdown",
    "substring_absent_rate",
    "substring_hit_rate",
    "tokenize",
    "word_spacing",
]
