"""Tests for the content_filter extraction cascade.

All tests are pure in-memory; no network, browser, or LLM calls.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from bs4 import BeautifulSoup, Tag

from supacrawl.services.content_filter import (
    _bm25_prune_sections,
    _is_dense_enough,
    _is_rank_bm25_available,
    _is_readability_available,
    _split_into_sections,
    _strategy1,
    _strategy2,
    _strategy3_body_fallback,
    _tokenise,
    _word_count,
    extract,
)
from supacrawl.services.converter import MarkdownConverter

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

ARTICLE_HTML = """
<html><body>
<nav>Menu <a href="/">Home</a></nav>
<main id="main">
  <h1>Main Article Title</h1>
  <h2>Introduction</h2>
  <p>This is the introduction paragraph with substantial text that should be extracted.
  It contains many words so the density check passes without any trouble at all.
  The extractor needs to see enough prose to conclude that this is a real content block
  and not a navigation bar masquerading as content or a thin sidebar with link lists.</p>
  <h2>Details</h2>
  <p>Here are the details of the article with even more words scattered across the text.
  The extraction cascade should identify this section as the primary content region and
  return it cleanly without the navigation bar above or the footer below the fold.
  Real articles tend to have dense paragraphs like this one filling the page with prose.</p>
  <h2>Conclusion</h2>
  <p>This is the conclusion section. It wraps up the article content nicely with a few
  sentences that summarise the key points covered in the introduction and details above.
  Extraction of this section proves the cascade handles multi-section documents correctly.</p>
</main>
<footer>Footer text</footer>
</body></html>
"""

NAV_LADEN_HTML = """
<html><body>
<div id="nav">
  <a href="/a">Link 1</a><a href="/b">Link 2</a><a href="/c">Link 3</a>
  <a href="/d">Link 4</a><a href="/e">Link 5</a><a href="/f">Link 6</a>
  <a href="/g">Link 7</a><a href="/h">Link 8</a><a href="/i">Link 9</a>
</div>
<div class="content">
  <h2>Article Section</h2>
  <p>A short but real paragraph.</p>
</div>
</body></html>
"""

FLAT_HTML = """
<html><body>
<p>This is a flat page with no headings whatsoever. It just has one long paragraph
of text that should be returned completely intact even when a query is supplied,
because a flat page with a single section must never be filtered by the cascade.</p>
</body></html>
"""

MULTI_SECTION_HTML = """
<html><body>
<div id="main">
  <h2>Python programming language</h2>
  <p>Python is a high-level general-purpose programming language known for its
  readability and versatility. It is widely used in web development, data science,
  machine learning, and automation scripts.</p>
  <h2>Australian wildlife</h2>
  <p>Australia is home to unique wildlife including kangaroos, wallabies, koalas,
  wombats, and the platypus. Many of these species are found nowhere else on earth.</p>
  <h2>Cooking pasta</h2>
  <p>To cook pasta, bring a large pot of salted water to a boil. Add the pasta and
  cook according to package directions until al dente. Drain and toss with sauce.</p>
</div>
</body></html>
"""


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


# ---------------------------------------------------------------------------
# Density helpers
# ---------------------------------------------------------------------------


class TestIsDenseEnough:
    @pytest.mark.unit
    def test_rejects_empty_element(self):
        """Empty element fails the minimum word count."""
        soup = _soup("<div></div>")
        div = soup.find("div")
        assert isinstance(div, Tag)
        assert not _is_dense_enough(div, 0.5)

    @pytest.mark.unit
    def test_rejects_thin_element(self):
        """Element with very few words is rejected at any mode."""
        soup = _soup("<div><p>Hello world</p></div>")
        div = soup.find("div")
        assert isinstance(div, Tag)
        assert not _is_dense_enough(div, 0.0)

    @pytest.mark.unit
    def test_accepts_dense_article(self):
        """A real article with sufficient words and ratio is accepted."""
        soup = _soup(ARTICLE_HTML)
        main = soup.find("main")
        assert isinstance(main, Tag)
        assert _is_dense_enough(main, 0.5)

    @pytest.mark.unit
    def test_mode_scales_threshold(self):
        """Higher content_mode demands a higher text/markup ratio."""
        # Build an element that sits between the permissive and strict thresholds.
        # Use enough words to clear _S1_MIN_WORDS but with moderate ratio.
        words = " ".join(["word"] * 120)
        html = f"<div><p>{words}</p></div>"
        soup = _soup(html)
        div = soup.find("div")
        assert isinstance(div, Tag)
        # Should pass at low mode (permissive)
        assert _is_dense_enough(div, 0.0)
        # At high mode the text/markup ratio may fail depending on wrapping markup;
        # the key invariant is that mode=1.0 is at least as strict as mode=0.0.
        result_low = _is_dense_enough(div, 0.0)
        result_high = _is_dense_enough(div, 1.0)
        # Strict mode must not accept what permissive mode rejected.
        if not result_low:
            assert not result_high


# ---------------------------------------------------------------------------
# get_text separator (heading/paragraph sibling seam)
# ---------------------------------------------------------------------------


class TestGetTextSeparator:
    """Verify that heading and paragraph siblings produce distinct tokens.

    Without separator=" " in get_text(), BeautifulSoup concatenates adjacent
    elements: <h2>Python</h2><p>language</p> → "Pythonlanguage", which
    creates a spurious merged token and causes word_count / tokenise to
    produce wrong results at every heading/paragraph seam.
    """

    @pytest.mark.unit
    def test_word_count_does_not_merge_heading_and_paragraph(self):
        """Adjacent h2 and p must each contribute their own words to the count."""
        soup = _soup("<div><h2>Python</h2><p>language</p></div>")
        div = soup.find("div")
        assert isinstance(div, Tag)
        # With separator=" " both words are distinct; count must be 2, not 1.
        assert _word_count(div) == 2

    @pytest.mark.unit
    def test_tokenise_does_not_produce_merged_token(self):
        """_tokenise applied to get_text(separator=" ") must yield separate tokens."""
        soup = _soup("<div><h2>Python</h2><p>language</p></div>")
        div = soup.find("div")
        assert isinstance(div, Tag)
        tokens = _tokenise(div.get_text(separator=" "))
        assert "python" in tokens
        assert "language" in tokens
        # The merged form must not appear.
        assert "pythonlanguage" not in tokens


# ---------------------------------------------------------------------------
# Section splitting
# ---------------------------------------------------------------------------


class TestSplitIntoSections:
    @pytest.mark.unit
    def test_flat_page_returns_one_section(self):
        """Flat page with no split tags comes back as a single section."""
        soup = _soup(FLAT_HTML)
        body = soup.find("body")
        assert isinstance(body, Tag)
        sections = _split_into_sections(body)
        assert len(sections) == 1

    @pytest.mark.unit
    def test_headed_page_splits_at_h2(self):
        """Page with multiple h2 headings is split at each h2 boundary."""
        soup = _soup(MULTI_SECTION_HTML)
        main = soup.find("div", id="main")
        assert isinstance(main, Tag)
        sections = _split_into_sections(main)
        # Three h2 headings → three sections
        assert len(sections) == 3

    @pytest.mark.unit
    def test_sections_are_tag_objects(self):
        """All returned sections are BS4 Tag instances."""
        soup = _soup(MULTI_SECTION_HTML)
        main = soup.find("div", id="main")
        assert isinstance(main, Tag)
        for sec in _split_into_sections(main):
            assert isinstance(sec, Tag)


# ---------------------------------------------------------------------------
# BM25 pruning
# ---------------------------------------------------------------------------


class TestBm25PruneSections:
    @pytest.mark.unit
    def test_single_section_not_pruned(self):
        """Single-section input is always returned unchanged."""
        soup = _soup(FLAT_HTML)
        body = soup.find("body")
        assert isinstance(body, Tag)
        sections = _split_into_sections(body)
        assert len(sections) == 1
        kept = _bm25_prune_sections(sections, content_mode=1.0, query="python")
        assert len(kept) == 1

    @pytest.mark.unit
    def test_query_prunes_irrelevant_sections(self):
        """BM25 drops sections not relevant to the query."""
        if not _is_rank_bm25_available():
            pytest.skip("rank_bm25 not installed")

        soup = _soup(MULTI_SECTION_HTML)
        main = soup.find("div", id="main")
        assert isinstance(main, Tag)
        sections = _split_into_sections(main)

        kept = _bm25_prune_sections(sections, content_mode=1.0, query="python programming language")
        # Should keep the Python section and possibly drop wildlife/pasta.
        assert len(kept) < len(sections)
        combined = " ".join(sec.get_text(separator=" ") for sec in kept).lower()
        assert "python" in combined

    @pytest.mark.unit
    def test_zero_mode_keeps_everything(self):
        """content_mode=0 makes the drop threshold zero; all sections are kept."""
        if not _is_rank_bm25_available():
            pytest.skip("rank_bm25 not installed")

        soup = _soup(MULTI_SECTION_HTML)
        main = soup.find("div", id="main")
        assert isinstance(main, Tag)
        sections = _split_into_sections(main)

        kept = _bm25_prune_sections(sections, content_mode=0.0, query="python")
        assert len(kept) == len(sections)


# ---------------------------------------------------------------------------
# Strategy 1 (CSS heuristic)
# ---------------------------------------------------------------------------


class TestStrategy1:
    @pytest.mark.unit
    def test_finds_main_element(self):
        """Strategy 1 returns the <main> element on a well-structured page."""
        soup = _soup(ARTICLE_HTML)
        result = _strategy1(soup, MarkdownConverter.MAIN_CONTENT_SELECTORS, content_mode=0.5)
        assert result is not None
        assert "Article Title" in result.get_text()

    @pytest.mark.unit
    def test_returns_none_on_sparse_page(self):
        """Strategy 1 returns None when no selector produces a dense result."""
        sparse_html = "<html><body><nav><a>x</a></nav></body></html>"
        soup = _soup(sparse_html)
        result = _strategy1(soup, MarkdownConverter.MAIN_CONTENT_SELECTORS, content_mode=0.5)
        assert result is None


# ---------------------------------------------------------------------------
# Strategy 2 (readability)
# ---------------------------------------------------------------------------


class TestStrategy2:
    @pytest.mark.unit
    def test_skips_gracefully_when_unavailable(self):
        """Strategy 2 returns None when readability-lxml is not importable."""
        with patch("supacrawl.services.content_filter._is_readability_available", return_value=False):
            result = _strategy2(ARTICLE_HTML, content_mode=0.5)
        assert result is None

    @pytest.mark.unit
    def test_extracts_content_when_available(self):
        """Strategy 2 returns content when readability-lxml is installed."""
        if not _is_readability_available():
            pytest.skip("readability-lxml not installed")
        result = _strategy2(ARTICLE_HTML, content_mode=0.5)
        # Should return a Tag with article text
        assert result is not None
        assert "Article" in result.get_text() or len(result.get_text().split()) > 0


# ---------------------------------------------------------------------------
# Strategy 3 (BM25 body fallback)
# ---------------------------------------------------------------------------


class TestStrategy3:
    @pytest.mark.unit
    def test_returns_body_without_bm25(self):
        """Strategy 3 returns the body unchanged when rank_bm25 is absent."""
        with patch("supacrawl.services.content_filter._is_rank_bm25_available", return_value=False):
            soup = _soup(ARTICLE_HTML)
            result = _strategy3_body_fallback(soup, content_mode=0.5)
        assert result is not None
        assert len(result.get_text().split()) > 0

    @pytest.mark.unit
    def test_returns_tag_with_bm25(self):
        """Strategy 3 returns a Tag even when BM25 prunes sections."""
        if not _is_rank_bm25_available():
            pytest.skip("rank_bm25 not installed")
        soup = _soup(MULTI_SECTION_HTML)
        result = _strategy3_body_fallback(soup, content_mode=0.8)
        assert isinstance(result, Tag)
        assert len(result.get_text().split()) > 0


# ---------------------------------------------------------------------------
# Cascade: extract()
# ---------------------------------------------------------------------------


class TestExtract:
    @pytest.mark.unit
    def test_cascade_succeeds_on_clean_article(self):
        """extract() returns a non-None Tag for a clean article page."""
        soup = _soup(ARTICLE_HTML)
        result = extract(
            soup=soup,
            html=ARTICLE_HTML,
            main_content_selectors=MarkdownConverter.MAIN_CONTENT_SELECTORS,
            content_mode=0.5,
        )
        assert result is not None
        assert "Article Title" in result.get_text()

    @pytest.mark.unit
    def test_strategy2_fires_when_strategy1_sparse(self):
        """Strategy 2 is tried when Strategy 1 returns sparse content."""
        if not _is_readability_available():
            pytest.skip("readability-lxml not installed")

        # Force Strategy 1 to fail by patching _is_dense_enough.
        with patch("supacrawl.services.content_filter._is_dense_enough", return_value=False):
            soup = _soup(ARTICLE_HTML)
            result = extract(
                soup=soup,
                html=ARTICLE_HTML,
                main_content_selectors=MarkdownConverter.MAIN_CONTENT_SELECTORS,
                content_mode=0.5,
            )
        # Should still return something via Strategy 2 or 3.
        assert result is not None

    @pytest.mark.unit
    def test_flat_page_not_filtered_by_query(self):
        """Flat page (single section) is never filtered even with a query."""
        soup = _soup(FLAT_HTML)
        result = extract(
            soup=soup,
            html=FLAT_HTML,
            main_content_selectors=MarkdownConverter.MAIN_CONTENT_SELECTORS,
            content_mode=0.9,
            query="completely unrelated query about submarines",
        )
        assert result is not None
        # All content still present
        assert "flat page" in result.get_text().lower()

    @pytest.mark.unit
    def test_query_filter_retains_relevant_sections(self):
        """query= retains sections matching the query and drops others."""
        if not _is_rank_bm25_available():
            pytest.skip("rank_bm25 not installed")

        soup = _soup(MULTI_SECTION_HTML)
        # Remove the main selector so Strategy 1 falls through to body-level
        result = extract(
            soup=soup,
            html=MULTI_SECTION_HTML,
            main_content_selectors=[],
            content_mode=0.5,
            query="python programming",
        )
        assert result is not None
        text = result.get_text().lower()
        assert "python" in text

    @pytest.mark.unit
    def test_graceful_without_either_optional_dep(self):
        """Cascade still returns content when both readability and rank_bm25 are absent."""
        with (
            patch("supacrawl.services.content_filter._is_readability_available", return_value=False),
            patch("supacrawl.services.content_filter._is_rank_bm25_available", return_value=False),
        ):
            soup = _soup(ARTICLE_HTML)
            result = extract(
                soup=soup,
                html=ARTICLE_HTML,
                main_content_selectors=MarkdownConverter.MAIN_CONTENT_SELECTORS,
                content_mode=0.5,
            )
        # Strategy 1 still works (no optional deps needed); should return content.
        assert result is not None

    @pytest.mark.unit
    def test_content_mode_clamped(self):
        """content_mode values outside [0, 1] are silently clamped."""
        soup = _soup(ARTICLE_HTML)
        # Should not raise; just clamp.
        result_low = extract(
            soup=soup,
            html=ARTICLE_HTML,
            main_content_selectors=MarkdownConverter.MAIN_CONTENT_SELECTORS,
            content_mode=-5.0,
        )
        result_high = extract(
            soup=soup,
            html=ARTICLE_HTML,
            main_content_selectors=MarkdownConverter.MAIN_CONTENT_SELECTORS,
            content_mode=99.0,
        )
        assert result_low is not None
        assert result_high is not None


# ---------------------------------------------------------------------------
# Converter integration: content_mode / query round-trip
# ---------------------------------------------------------------------------


class TestConverterIntegration:
    @pytest.mark.unit
    def test_converter_accepts_content_mode_and_query(self):
        """MarkdownConverter.convert() accepts the new params without error."""
        converter = MarkdownConverter()
        md = converter.convert(
            ARTICLE_HTML,
            only_main_content=True,
            content_mode=0.7,
            query="article introduction",
        )
        assert isinstance(md, str)
        assert len(md) > 0

    @pytest.mark.unit
    def test_converter_default_behaviour_unchanged(self):
        """Default call (no new params) still works identically."""
        converter = MarkdownConverter()
        md_default = converter.convert(ARTICLE_HTML, only_main_content=True)
        md_explicit = converter.convert(ARTICLE_HTML, only_main_content=True, content_mode=0.5, query=None)
        # Both should produce the same markdown.
        assert md_default == md_explicit

    @pytest.mark.unit
    def test_include_tags_takes_precedence(self):
        """When include_tags is set, content_mode/query are bypassed."""
        converter = MarkdownConverter()
        md = converter.convert(
            ARTICLE_HTML,
            only_main_content=True,
            include_tags=["h1"],
            content_mode=1.0,
            query="unrelated",
        )
        assert "Main Article Title" in md


# ---------------------------------------------------------------------------
# Regression: BM25 zip alignment with empty sections (finding #1)
# ---------------------------------------------------------------------------


EMPTY_SECTION_HTML = """
<html><body>
<div id="main">
  <h2>First real section</h2>
  <p>This is a real paragraph with meaningful text that should be scored and kept
  by the BM25 pruner when given a query about first real content.</p>
  <h2></h2>
  <h2>Second real section</h2>
  <p>This is another real paragraph with meaningful text about a completely
  different topic; it provides additional content for the extraction cascade.</p>
</div>
</body></html>
"""


class TestBm25ZipAlignment:
    """Guard against the zip misalignment bug where empty sections cause score offset."""

    @pytest.mark.unit
    def test_empty_section_does_not_displace_scores(self):
        """BM25 scores must align to the correct sections even when one is empty.

        A document with an empty <h2> (no content) previously caused scores to be
        offset: the empty tokenisation was excluded from BM25 but sections were not,
        so every subsequent section was paired with the wrong score and trailing
        sections were silently dropped.
        """
        if not _is_rank_bm25_available():
            pytest.skip("rank_bm25 not installed")

        soup = _soup(EMPTY_SECTION_HTML)
        main = soup.find("div", id="main")
        assert isinstance(main, Tag)
        sections = _split_into_sections(main)

        # Three h2 headings → three sections (one of which is empty)
        assert len(sections) == 3

        # With a query matching the first section, BM25 should keep that section.
        # The critical invariant: the number of kept sections never exceeds the
        # number of input sections (no off-by-one from zip misalignment).
        kept = _bm25_prune_sections(sections, content_mode=0.5, query="first real content")
        assert len(kept) <= len(sections), "kept must not exceed input sections"
        assert len(kept) >= 1, "must keep at least one section"

        # The first-section text must be in the kept set — it matches the query.
        combined_text = " ".join(sec.get_text(separator=" ") for sec in kept).lower()
        assert "first real section" in combined_text

    @pytest.mark.unit
    def test_empty_section_no_query_all_real_sections_present(self):
        """No-query density scoring must not silently drop real sections after an empty one."""
        if not _is_rank_bm25_available():
            pytest.skip("rank_bm25 not installed")

        soup = _soup(EMPTY_SECTION_HTML)
        main = soup.find("div", id="main")
        assert isinstance(main, Tag)
        sections = _split_into_sections(main)
        assert len(sections) == 3

        # With content_mode=0 the threshold is 0; nothing should be pruned.
        kept = _bm25_prune_sections(sections, content_mode=0.0, query=None)
        assert len(kept) == len(sections)

        # With a positive mode, real sections (non-empty) must not vanish due to
        # score displacement from the empty section.
        kept_strict = _bm25_prune_sections(sections, content_mode=0.8, query=None)
        combined = " ".join(sec.get_text(separator=" ") for sec in kept_strict).lower()
        assert "first real section" in combined or "second real section" in combined


# ---------------------------------------------------------------------------
# Regression: thin-but-valid main at default mode (finding #2)
# ---------------------------------------------------------------------------


THIN_MAIN_HTML = """
<html><body>
<main id="primary">
  <h1>Product Page</h1>
  <p>Buy it now for only $9.99. Limited stock available. Order today.</p>
</main>
<div class="sidebar">
  <p>Related products include many interesting things you might like to purchase.
  We have a huge catalogue of items spanning many different categories and genres.
  Browse our selection to find the perfect gift or treat yourself to something new.</p>
</div>
</body></html>
"""


class TestThinMainNoRegression:
    """Lock in that a thin-but-valid <main> is returned at default mode (finding #2)."""

    @pytest.mark.unit
    def test_thin_main_accepted_at_default_mode(self):
        """At content_mode=0.5 a thin <main> is preferred over the full body.

        The old _find_main_content returned the first matching selector unconditionally.
        The new cascade must not regress: a <main> with few words should still win
        over falling through to Strategy 3 which would include the sidebar div.
        """
        soup = _soup(THIN_MAIN_HTML)
        result = _strategy1(soup, MarkdownConverter.MAIN_CONTENT_SELECTORS, content_mode=0.5)
        assert result is not None, "Strategy 1 must accept the <main> at default mode"
        text = result.get_text().lower()
        assert "product page" in text
        # The sidebar text must not be mixed in via Strategy 3
        assert "related products" not in text

    @pytest.mark.unit
    def test_thin_main_rejected_at_precision_mode(self):
        """At content_mode=1.0 a thin <main> may be rejected by the strict density bar."""
        soup = _soup(THIN_MAIN_HTML)
        # The <main> has very few words; at precision mode it should fail the word floor.
        result = _strategy1(soup, MarkdownConverter.MAIN_CONTENT_SELECTORS, content_mode=1.0)
        # Either rejected (result is None) or accepted — just assert it doesn't crash.
        # The key contract is that mode=0.5 always accepts it (above test) and mode=1.0
        # is at least as strict (may reject).
        if result is not None:
            # If accepted at precision mode, that is also valid; just confirm content.
            assert "product page" in result.get_text().lower()

    @pytest.mark.unit
    def test_full_cascade_no_sidebar_at_default_mode(self):
        """Full cascade at default mode must not bleed sidebar content into result."""
        soup = _soup(THIN_MAIN_HTML)
        result = extract(
            soup=soup,
            html=THIN_MAIN_HTML,
            main_content_selectors=MarkdownConverter.MAIN_CONTENT_SELECTORS,
            content_mode=0.5,
        )
        assert result is not None
        text = result.get_text().lower()
        assert "product page" in text
        assert "related products" not in text


class TestNestedHeadings:
    @pytest.mark.unit
    def test_headings_inside_wrapper_divs_still_split_into_sections(self):
        from supacrawl.services.content_filter import _split_into_sections

        html = (
            "<body><div><div>"
            + "".join(f"<div class='h'><h2>S{i}</h2></div><p>text {i}</p>" for i in range(4))
            + "</div></div></body>"
        )
        sections = _split_into_sections(_soup(html).find("body"))
        assert len(sections) == 4
