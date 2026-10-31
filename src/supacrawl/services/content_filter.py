"""Content extraction cascade with a precision/recall tuning dial.

Provides a three-strategy cascade for selecting the main content element
from parsed HTML. Each strategy is tried in order; the first that clears
its density threshold wins and the rest are skipped.

Strategy 1 — CSS heuristic (``_strategy1``)
    Re-uses the existing selector list in ``MarkdownConverter``. Accepted
    when the extracted text is dense enough relative to ``content_mode``.

Strategy 2 — readability-lxml
    ``readability-lxml`` extracts an article-like summary. Import-guarded:
    if the package is absent the strategy is silently skipped.

Strategy 3 — BM25 section pruning
    Splits the content root into sections by heading/sectioning elements,
    scores each section with BM25 (``rank_bm25``), and discards sections
    that score below the ``content_mode``-scaled threshold. Import-guarded.
    When a ``query`` is provided this strategy doubles as a query-relevance
    filter after the cascade picks a content root.

The public interface is ``extract()``.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar

from bs4 import BeautifulSoup, Tag

from supacrawl.excerpt import heading_names, query_tokens
from supacrawl.models import QueryFilter

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tuning constants (adjust here, never inline)
# ---------------------------------------------------------------------------

# Minimum word count for Strategy 1.  Only applied when content_mode > 0.5:
#     if content_mode <= 0.5: threshold = _S1_MIN_WORDS_FLOOR (near-empty guard only)
#     if content_mode > 0.5:  threshold = _S1_MIN_WORDS_FLOOR + int((_S1_MIN_WORDS - _S1_MIN_WORDS_FLOOR) * (content_mode - 0.5) / 0.5)
# At mode=0.0: threshold = 5   (near-empty guard only)
# At mode=0.5: threshold = 5   (default; matches old unconditional behaviour)
# At mode=1.0: threshold = 100 (strict; same as old hardcoded value)
# This preserves the old selector-match behaviour at the default mode: a thin
# but valid <main> is returned just as the old _find_main_content returned it.
_S1_MIN_WORDS = 100
_S1_MIN_WORDS_FLOOR = 5

# Minimum text-to-markup ratio for Strategy 1.  Only applied when content_mode > 0.5:
#     if content_mode <= 0.5: threshold = 0.0 (no ratio gate; old behaviour)
#     if content_mode > 0.5:  threshold = _S1_RATIO_BASE * (content_mode - 0.5) / 0.5
# At mode=0.0: threshold = 0.0   (no ratio check)
# At mode=0.5: threshold = 0.0   (default; old code had no ratio gate)
# At mode=1.0: threshold ≈ 0.075 (strict; dense-content requirement)
_S1_RATIO_BASE = 0.075

# Minimum word count for Strategy 2 (readability) to be accepted.
_S2_MIN_WORDS = 80

# BM25 section-drop threshold multiplier.  Sections scoring below
#     _BM25_DROP_THRESHOLD_BASE * content_mode
# of the top section are pruned.  At mode=0 nothing is pruned; at mode=1
# sections scoring below 20% of the top section are dropped.
_BM25_DROP_THRESHOLD_BASE = 0.20

# Heading/sectioning tags used to split content into BM25 sections.
_SECTION_SPLIT_TAGS = frozenset({"h2", "h3", "section", "article"})


# ---------------------------------------------------------------------------
# Availability guards
# ---------------------------------------------------------------------------


def _is_readability_available() -> bool:
    """Return True if readability-lxml is importable."""
    try:
        from readability import Document  # noqa: F401

        return True
    except ImportError:
        return False


def _is_rank_bm25_available() -> bool:
    """Return True if rank_bm25 is importable."""
    try:
        from rank_bm25 import BM25Okapi  # noqa: F401

        return True
    except ImportError:
        return False


def available_extras() -> dict[str, bool]:
    """Report which optional extraction dependencies are importable here.

    ``query_filter`` is what a caller's ``query`` needs: only ``rank_bm25``
    scores sections; ``readability_lxml`` powers cascade Strategy 2.
    """
    bm25 = _is_rank_bm25_available()
    return {"readability_lxml": _is_readability_available(), "rank_bm25": bm25, "query_filter": bm25}


# The outcome of the most recent query filter in this context. ``scrape()``
# clears it on entry and reads it on exit; a task-local ContextVar keeps
# concurrent scrapes from reading each other's outcome.
QUERY_OUTCOME: ContextVar[QueryFilter | None] = ContextVar("query_outcome", default=None)


# ---------------------------------------------------------------------------
# Density helpers
# ---------------------------------------------------------------------------


def _word_count(element: Tag) -> int:
    """Count whitespace-split words in a BS4 element's text."""
    return len(element.get_text(separator=" ").split())


def _text_ratio(element: Tag) -> float:
    """Return ratio of text characters to total HTML characters.

    Avoids division by zero for empty elements.
    """
    html_len = len(str(element))
    if html_len == 0:
        return 0.0
    return len(element.get_text(separator=" ")) / html_len


def _is_dense_enough(element: Tag, content_mode: float) -> bool:
    """Return True when the element clears the Strategy-1 density bar.

    At mode <= 0.5 (the default) the thresholds are near-zero to match the old
    unconditional CSS-selector logic: any positively-matched element is accepted
    unless it is completely empty.  Above 0.5 both thresholds scale linearly to
    their precision-end limits at mode=1.0 so a caller that wants strict content
    gets progressively stricter output.
    """
    # Piecewise word-count floor: flat _S1_MIN_WORDS_FLOOR below 0.5, then
    # linear ramp to _S1_MIN_WORDS at 1.0.
    if content_mode <= 0.5:
        min_words = _S1_MIN_WORDS_FLOOR
    else:
        scale = (content_mode - 0.5) / 0.5
        min_words = _S1_MIN_WORDS_FLOOR + int((_S1_MIN_WORDS - _S1_MIN_WORDS_FLOOR) * scale)

    words = _word_count(element)
    if words < min_words:
        LOGGER.debug("Strategy 1 thin: %d words (< %d at mode=%.2f)", words, min_words, content_mode)
        return False

    # Piecewise ratio gate: no gate below 0.5, then linear ramp to _S1_RATIO_BASE at 1.0.
    if content_mode > 0.5:
        ratio_scale = (content_mode - 0.5) / 0.5
        threshold = _S1_RATIO_BASE * ratio_scale
        ratio = _text_ratio(element)
        if ratio < threshold:
            LOGGER.debug("Strategy 1 sparse: ratio %.3f < threshold %.3f", ratio, threshold)
            return False

    return True


# ---------------------------------------------------------------------------
# Section splitting for BM25
# ---------------------------------------------------------------------------


_HEADING_TAGS = frozenset({"h2", "h3"})


def _opens_section(tag: Tag) -> bool:
    """A section tag, or a wrapper whose first element is a heading (e.g. ``div.mw-heading``)."""
    if tag.name in _SECTION_SPLIT_TAGS:
        return True
    first = next((c for c in tag.children if isinstance(c, Tag)), None)
    return first is not None and first.name in _HEADING_TAGS


def _descend_wrappers(root: Tag) -> Tag:
    """Step into the dominant child until a level has two or more sections.

    Pages wrap their content (``div > div > section > section...``), so the
    root's own children are rarely the sections. Stops where the sections are,
    or where no single child holds most of the text.
    """
    while True:
        children = [c for c in root.children if isinstance(c, Tag)]
        if sum(_opens_section(c) for c in children) >= 2:
            return root
        total = len(root.get_text())
        biggest = max(children, key=lambda c: len(c.get_text()), default=None)
        if biggest is None or len(biggest.get_text()) < 0.8 * total:
            return root
        root = biggest


def _split_into_sections(root: Tag) -> list[Tag]:
    """Split a content root into sections at heading/sectioning boundaries.

    Each section is returned as a BS4 Tag (a synthetic ``<div>``) whose
    children are the heading that opens the section plus all following
    siblings up to the next section boundary.

    When the root contains no recognised split tags the whole root is
    returned as a single section so callers never receive an empty list.
    """
    root = _descend_wrappers(root)
    sections: list[Tag] = []
    current_parts: list[Tag] = []

    def flush() -> None:
        if not current_parts:
            return
        wrapper = BeautifulSoup("<div></div>", "html.parser").find("div")
        if not isinstance(wrapper, Tag):
            raise TypeError("BeautifulSoup failed to produce a div wrapper")
        for part in current_parts:
            wrapper.append(part.__copy__())
        sections.append(wrapper)
        current_parts.clear()

    for child in root.children:
        if not isinstance(child, Tag):
            continue
        if _opens_section(child):
            flush()
        current_parts.append(child)

    flush()

    if not sections:
        # No split tags found — treat whole root as one section.
        wrapper = BeautifulSoup("<div></div>", "html.parser").find("div")
        if not isinstance(wrapper, Tag):
            raise TypeError("BeautifulSoup failed to produce a div wrapper")
        wrapper.append(root.__copy__())
        sections = [wrapper]

    return sections


def _tokenise(text: str) -> list[str]:
    """Minimal tokeniser: lowercase split, no stemming or stopwords."""
    return text.lower().split()


def _heading_text(section: Tag) -> str:
    """The text of the heading that opens a section, or empty."""
    heading = section.find(["h1", "h2", "h3", "h4", "h5", "h6"])
    return heading.get_text(" ", strip=True) if isinstance(heading, Tag) else ""


# Of the best section's score, what another section needs to stay when the
# query names a section by its heading.
_NAMED_SECTION_FLOOR = 0.5


# ---------------------------------------------------------------------------
# BM25 pruning
# ---------------------------------------------------------------------------


def _bm25_prune_sections(sections: list[Tag], content_mode: float, query: str | None) -> list[Tag]:
    """Return sections that score above the content_mode-scaled threshold.

    When only one section exists, pruning is skipped to avoid returning an
    empty result (returning everything is always safer than returning nothing
    for a flat page).

    Args:
        sections: Pre-split section Tags.
        content_mode: Precision/recall dial [0.0, 1.0].
        query: Optional query string; when None a generic corpus-coherence
               score is used (each section scored against the full corpus).

    Returns:
        Filtered list of sections; never empty (falls back to all sections
        when BM25 would discard everything).
    """
    if len(sections) == 1:
        LOGGER.debug("BM25: single section, skipping prune")
        return sections

    try:
        from rank_bm25 import BM25Okapi
    except ImportError:
        return sections

    tokenised = [_tokenise(section.get_text(separator=" ")) for section in sections]

    # Build a parallel index so each BM25 score maps back to the exact section
    # it was computed from.  Empty tokenisations are excluded from the BM25
    # corpus (to avoid divide-by-zero) but their sections are kept; they
    # receive a score of 0.0 and survive only if the drop threshold is also 0.
    scored_indices: list[int] = []
    non_empty_tokens: list[list[str]] = []
    for i, tokens in enumerate(tokenised):
        if tokens:
            scored_indices.append(i)
            non_empty_tokens.append(tokens)

    if not non_empty_tokens:
        return sections

    bm25 = BM25Okapi(non_empty_tokens)

    if query:
        q_tokens = _tokenise(query)
    else:
        # No query: score each section by word count (a density proxy).
        # Self-referential BM25 rewards rare vocabulary which is the opposite
        # of "keep dense/central content"; length scoring is a better proxy.
        section_scores: list[float] = [0.0] * len(sections)
        for i in scored_indices:
            section_scores[i] = float(len(tokenised[i]))
        max_score = max(section_scores)
        drop_threshold = _BM25_DROP_THRESHOLD_BASE * content_mode * max_score
        kept = [sec for sec, score in zip(sections, section_scores, strict=True) if score >= drop_threshold]
        if not kept:
            LOGGER.debug("BM25 prune would drop all sections; returning all")
            return sections
        LOGGER.debug(
            "BM25 prune (no-query density): kept %d/%d sections (mode=%.2f, threshold=%.4f)",
            len(kept),
            len(sections),
            content_mode,
            drop_threshold,
        )
        return kept

    bm25_scores = bm25.get_scores(q_tokens)

    # Map BM25 scores back to the full sections list; unscored (empty) sections get 0.
    section_scores = [0.0] * len(sections)
    for bm25_idx, section_idx in enumerate(scored_indices):
        section_scores[section_idx] = float(bm25_scores[bm25_idx])

    scores = section_scores

    max_score = max(scores)
    if max_score == 0:
        # All sections scored zero — can't prune meaningfully.
        return sections

    drop_threshold = _BM25_DROP_THRESHOLD_BASE * content_mode * max_score
    # A query that names a section by its heading (an option's identifier on a
    # reference page) gets that section whole, and the rest only where it is
    # nearly as relevant: with hundreds of sibling sections sharing the query's
    # ordinary words, the base threshold alone kept 179 of 183.
    named = {i for i, sec in enumerate(sections) if heading_names(_heading_text(sec), query_tokens(query))}
    if named:
        drop_threshold = max(drop_threshold, _NAMED_SECTION_FLOOR * max_score)
    kept = [
        sec
        for i, (sec, score) in enumerate(zip(sections, scores, strict=True))
        if i in named or score >= drop_threshold
    ]

    if not kept:
        LOGGER.debug("BM25 prune would drop all sections; returning all")
        return sections

    LOGGER.debug(
        "BM25 prune: kept %d/%d sections (mode=%.2f, threshold=%.4f)",
        len(kept),
        len(sections),
        content_mode,
        drop_threshold,
    )
    return kept


# ---------------------------------------------------------------------------
# Strategy implementations
# ---------------------------------------------------------------------------


def _strategy1(soup: BeautifulSoup, main_content_selectors: list[str], content_mode: float) -> Tag | None:
    """CSS-selector heuristic (Strategy 1).

    Tries each selector in ``main_content_selectors`` in priority order and
    returns the first match that clears the density bar. Returns ``None``
    when nothing is good enough.

    Args:
        soup: Parsed document.
        main_content_selectors: CSS selectors in priority order (from
            ``MarkdownConverter.MAIN_CONTENT_SELECTORS``).
        content_mode: Precision/recall dial.

    Returns:
        Accepted Tag or None.
    """
    for selector in main_content_selectors:
        try:
            element = soup.select_one(selector)
        except Exception:
            continue
        if element is None:
            continue
        if _is_dense_enough(element, content_mode):
            LOGGER.debug("Strategy 1 accepted via selector: %s", selector)
            return element
        LOGGER.debug("Strategy 1 rejected selector %s (too sparse)", selector)

    return None


def _strategy2(html: str, content_mode: float) -> Tag | None:
    """readability-lxml extraction (Strategy 2).

    Parses the full original HTML (not the pre-cleaned soup) through
    ``readability.Document``, then re-parses its ``summary()`` output with
    BeautifulSoup and checks the word count.

    Args:
        html: Original raw HTML string.
        content_mode: Not used for acceptance threshold (fixed at
            ``_S2_MIN_WORDS``) but included for signature consistency.

    Returns:
        BeautifulSoup Tag wrapping the summary body, or None.
    """
    if not _is_readability_available():
        return None

    try:
        from readability import Document

        doc = Document(html)
        summary_html = doc.summary(html_partial=False)
        if not summary_html:
            return None

        summary_soup = BeautifulSoup(summary_html, "html.parser")
        body = summary_soup.find("body")
        root = body if isinstance(body, Tag) else summary_soup

        words = _word_count(root)
        if words < _S2_MIN_WORDS:
            LOGGER.debug("Strategy 2 thin: %d words (< %d)", words, _S2_MIN_WORDS)
            return None

        LOGGER.debug("Strategy 2 accepted: %d words", words)
        return root

    except Exception as e:
        LOGGER.debug("Strategy 2 (readability) failed: %s", e)
        return None


def _strategy3_body_fallback(soup: BeautifulSoup, content_mode: float) -> Tag:
    """BM25 section-prune of the full page body (Strategy 3).

    Falls through to ``<body>`` as the content root, splits it into sections
    by heading/section tags, and prunes with BM25. Returns the pruned body
    (or the full body when BM25 is unavailable or would prune everything).

    Args:
        soup: Pre-cleaned document.
        content_mode: Precision/recall dial.

    Returns:
        A Tag (never None); callers can always use this result.
    """
    body = soup.find("body")
    root: Tag = body if isinstance(body, Tag) else soup  # type: ignore[assignment]

    if not _is_rank_bm25_available():
        LOGGER.debug("Strategy 3: rank_bm25 unavailable, returning body as-is")
        return root

    sections = _split_into_sections(root)
    kept = _bm25_prune_sections(sections, content_mode, query=None)

    if len(kept) == len(sections):
        # Nothing pruned — return original root to avoid rebuilding
        return root

    # Rebuild a synthetic body from kept sections
    new_body = BeautifulSoup("<div></div>", "html.parser").find("div")
    if not isinstance(new_body, Tag):
        raise TypeError("BeautifulSoup failed to produce a div wrapper")
    for sec in kept:
        for child in list(sec.children):
            new_body.append(child.__copy__())

    LOGGER.debug("Strategy 3 body-prune: kept %d/%d sections", len(kept), len(sections))
    return new_body


# ---------------------------------------------------------------------------
# Optional query-relevance filter (post-cascade)
# ---------------------------------------------------------------------------


def _apply_query_filter(root: Tag, content_mode: float, query: str) -> Tag:
    """Filter root sections by BM25 query relevance.

    Called after the cascade selects a content root when ``query`` is
    provided. Flat pages (single section) are returned unchanged — returning
    everything beats filtering out everything. Records what happened in
    ``QUERY_OUTCOME``.

    Args:
        root: The selected content root.
        content_mode: Precision/recall dial.
        query: The user's query string.

    Returns:
        Filtered Tag (or the original root for flat pages / unavailable BM25).
    """
    if not _is_rank_bm25_available():
        QUERY_OUTCOME.set(QueryFilter(applied=False, reason="rank_bm25_not_installed"))
        return root

    sections = _split_into_sections(root)
    if len(sections) <= 1:
        LOGGER.debug("Query filter: single section, skipping")
        QUERY_OUTCOME.set(QueryFilter(applied=False, reason="flat_page", sections_total=1, sections_kept=1))
        return root

    kept = _bm25_prune_sections(sections, content_mode, query=query)
    QUERY_OUTCOME.set(QueryFilter(applied=True, sections_total=len(sections), sections_kept=len(kept)))

    if len(kept) == len(sections):
        return root

    new_root = BeautifulSoup("<div></div>", "html.parser").find("div")
    if not isinstance(new_root, Tag):
        raise TypeError("BeautifulSoup failed to produce a div wrapper")
    for sec in kept:
        for child in list(sec.children):
            new_root.append(child.__copy__())

    LOGGER.debug(
        "Query filter: kept %d/%d sections for query=%r",
        len(kept),
        len(sections),
        query[:50],
    )
    return new_root


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract(
    soup: BeautifulSoup,
    html: str,
    main_content_selectors: list[str],
    content_mode: float = 0.5,
    query: str | None = None,
) -> Tag:
    """Run the extraction cascade and return the best content root.

    Tries strategies in order; the first to produce dense-enough output wins.
    Strategy 3 always produces a result, so this function never returns None.

    Flat pages (one section, no headings) bypass query filtering so an
    overly selective query cannot erase the only content available.

    Args:
        soup: Pre-cleaned BeautifulSoup document (boilerplate already stripped
              by ``MarkdownConverter``).
        html: The original raw HTML string, passed to Strategy 2 so
              readability can re-parse it without the pre-cleaning stripping
              its internal heuristic signals.
        main_content_selectors: CSS selector list from ``MarkdownConverter``
              (tried in priority order by Strategy 1).
        content_mode: Precision/recall dial in [0.0, 1.0].  Low ≈ recall-
              biased (accept more, prune less).  High ≈ precision-biased
              (demand denser output, prune more aggressively).  Default 0.5.
        query: Optional free-text query.  When set, sections are filtered
              post-cascade to retain only query-relevant parts.

    Returns:
        Selected Tag (never None; Strategy 3 body-fallback always succeeds).
    """
    content_mode = max(0.0, min(1.0, content_mode))

    # Strategy 1: CSS-selector heuristic
    result = _strategy1(soup, main_content_selectors, content_mode)

    # Strategy 2: readability-lxml
    if result is None:
        result = _strategy2(html, content_mode)

    # Strategy 3: BM25 body-section prune
    if result is None:
        LOGGER.debug("Falling through to Strategy 3 (BM25 body prune)")
        result = _strategy3_body_fallback(soup, content_mode)

    # Post-cascade query filter
    if query:
        result = _apply_query_filter(result, content_mode, query)

    return result
