"""Unit tests for benchmark/metrics.py.

All tests are pure: no I/O, no network, no browser.
"""

from __future__ import annotations

import pytest

from supacrawl.benchmark.metrics import (
    char_coverage,
    composite_quality,
    reference_is_degenerate,
    rouge_l,
    token_prf,
)
from supacrawl.quality import (
    count_structure,
    strip_markdown,
    substring_absent_rate,
    substring_hit_rate,
    tokenize,
    word_spacing,
)

# tokenize / strip_markdown


@pytest.mark.unit
def test_tokenize_basic() -> None:
    tokens = tokenize("Hello World")
    assert tokens == ["hello", "world"]


@pytest.mark.unit
def test_tokenize_markdown_strip() -> None:
    # With markdown=True, heading hashes and link syntax are stripped before
    # tokenising so formatting characters don't become content tokens.
    md = "# Heading\n[link text](https://example.com)\n![img](img.png)"
    tokens = tokenize(md, markdown=True)
    assert "heading" in tokens
    assert "link" in tokens
    assert "text" in tokens
    # Image alt should be gone, URL should not appear as a token
    assert "example" not in tokens


@pytest.mark.unit
def test_tokenize_empty() -> None:
    assert tokenize("") == []
    assert tokenize("   ") == []


@pytest.mark.unit
def test_strip_markdown_removes_fences() -> None:
    md = "```python\nprint('hello')\n```"
    result = strip_markdown(md)
    assert "```" not in result
    assert "python" not in result


# token_prf — multiset capping and edge cases


@pytest.mark.unit
def test_token_prf_perfect_match() -> None:
    tokens = ["cat", "sat", "mat"]
    p, r, f1 = token_prf(tokens, tokens)
    assert p == pytest.approx(1.0)
    assert r == pytest.approx(1.0)
    assert f1 == pytest.approx(1.0)


@pytest.mark.unit
def test_token_prf_no_overlap() -> None:
    p, r, f1 = token_prf(["cat"], ["dog"])
    assert p == 0.0
    assert r == 0.0
    assert f1 == 0.0


@pytest.mark.unit
def test_token_prf_empty_extracted() -> None:
    p, r, f1 = token_prf([], ["cat", "dog"])
    assert (p, r, f1) == (0.0, 0.0, 0.0)


@pytest.mark.unit
def test_token_prf_empty_gold() -> None:
    p, r, f1 = token_prf(["cat"], [])
    assert (p, r, f1) == (0.0, 0.0, 0.0)


@pytest.mark.unit
def test_token_prf_multiset_capping() -> None:
    # Extracted has 'cat' repeated 5 times; gold has it once.
    # Overlap should be capped at 1 (multiset intersection).
    # precision = 1/5, recall = 1/1
    extracted = ["cat"] * 5
    gold = ["cat"]
    p, r, f1 = token_prf(extracted, gold)
    assert p == pytest.approx(1 / 5)
    assert r == pytest.approx(1.0)
    assert f1 == pytest.approx(2 * (1 / 5) * 1.0 / (1 / 5 + 1.0))


@pytest.mark.unit
def test_token_prf_partial_overlap() -> None:
    extracted = ["the", "cat", "sat"]
    gold = ["the", "cat", "on", "mat"]
    p, r, f1 = token_prf(extracted, gold)
    # overlap = 2 (the, cat); precision = 2/3; recall = 2/4
    # f1 = 2*(2/3)*(2/4) / (2/3 + 2/4) = (2/3) / (7/6) = 4/7
    assert p == pytest.approx(2 / 3)
    assert r == pytest.approx(2 / 4)
    assert f1 == pytest.approx(4 / 7)


# rouge_l


@pytest.mark.unit
def test_rouge_l_identical() -> None:
    tokens = ["a", "b", "c"]
    assert rouge_l(tokens, tokens) == pytest.approx(1.0)


@pytest.mark.unit
def test_rouge_l_no_overlap() -> None:
    assert rouge_l(["x", "y"], ["a", "b"]) == pytest.approx(0.0)


@pytest.mark.unit
def test_rouge_l_empty() -> None:
    assert rouge_l([], ["a"]) == 0.0
    assert rouge_l(["a"], []) == 0.0


@pytest.mark.unit
def test_rouge_l_subsequence() -> None:
    # LCS of [a, b, c, d] and [a, c, d] is [a, c, d], length 3
    # precision = 3/4, recall = 3/3 = 1.0
    # F = 2*(3/4)*1.0 / (3/4 + 1.0) = (3/2) / (7/4) = 6/7
    a = ["a", "b", "c", "d"]
    b = ["a", "c", "d"]
    score = rouge_l(a, b)
    assert score == pytest.approx(6 / 7)


# char_coverage — clamping


@pytest.mark.unit
def test_char_coverage_exact() -> None:
    assert char_coverage(100, 100) == pytest.approx(1.0)


@pytest.mark.unit
def test_char_coverage_over_clamped() -> None:
    # Scraper produced more chars than reference; must clamp to 1.0
    assert char_coverage(200, 100) == pytest.approx(1.0)


@pytest.mark.unit
def test_char_coverage_partial() -> None:
    assert char_coverage(50, 100) == pytest.approx(0.5)


@pytest.mark.unit
def test_char_coverage_zero_reference() -> None:
    assert char_coverage(100, 0) == pytest.approx(0.0)


@pytest.mark.unit
def test_char_coverage_zero_extracted() -> None:
    assert char_coverage(0, 100) == pytest.approx(0.0)


# reference_is_degenerate — guard against a renderer that under-captured


@pytest.mark.unit
def test_reference_degenerate_tiny_ref_full_scrape() -> None:
    # The books.toscrape regression: scrape got 266 words, reference only 11.
    # The reference under-captured, so its comparison metrics must be discarded.
    assert reference_is_degenerate(266, 11) is True


@pytest.mark.unit
def test_reference_not_degenerate_when_both_small_and_agree() -> None:
    # A genuinely tiny page (example.com): output and reference both ~19 words
    # and in agreement — the reference is valid, not degenerate.
    assert reference_is_degenerate(19, 19) is False


@pytest.mark.unit
def test_reference_not_degenerate_with_healthy_reference() -> None:
    # A large reference is trustworthy even if the scrape got somewhat less.
    assert reference_is_degenerate(85, 341) is False
    assert reference_is_degenerate(4234, 4238) is False


@pytest.mark.unit
def test_reference_not_degenerate_when_none() -> None:
    # No reference (PDF / failed capture) is handled upstream, not here.
    assert reference_is_degenerate(7113, None) is False


@pytest.mark.unit
def test_reference_degenerate_at_ratio_boundary() -> None:
    # Tiny reference but the scrape did not extract substantially more: the page
    # really is short, so do not discard the reference.
    assert reference_is_degenerate(30, 11) is False  # 30 <= 11 * 3
    assert reference_is_degenerate(34, 11) is True  # 34 > 11 * 3


@pytest.mark.unit
def test_reference_degenerate_lifts_composite_to_reference_free_signals() -> None:
    # With the reference-based metrics nulled (the runner's behaviour on a
    # degenerate reference), a clean scrape scores on coverage + anchors +
    # spacing instead of being punished by a garbage token_f1 / noise.
    punished = composite_quality(
        success=True,
        char_coverage_value=1.0,
        token_f1=0.08,
        noise=0.96,
        expect_hit=1.0,
        expect_absent_ok=None,
        link_density_value=0.0,
        word_spacing_value=1.0,
    )
    recovered = composite_quality(
        success=True,
        char_coverage_value=1.0,
        token_f1=None,
        noise=None,
        expect_hit=1.0,
        expect_absent_ok=None,
        link_density_value=0.0,
        word_spacing_value=1.0,
    )
    assert recovered > punished
    assert recovered > 85.0


# count_structure


@pytest.mark.unit
def test_count_structure_headings() -> None:
    md = "# H1\n## H2\n### H3\nSome text\n#### H4"
    counts = count_structure(md)
    assert counts["headings"] == 4


@pytest.mark.unit
def test_count_structure_code_blocks() -> None:
    md = "```python\ncode here\n```\n\n```bash\necho hi\n```"
    counts = count_structure(md)
    assert counts["code_blocks"] == 2


@pytest.mark.unit
def test_count_structure_tables() -> None:
    md = "| A | B |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |"
    counts = count_structure(md)
    # 4 rows; net out the 2 header/separator rows → 2 body rows
    assert counts["tables"] == 2


@pytest.mark.unit
def test_count_structure_images() -> None:
    md = "![alt1](img1.png)\n![alt2](img2.jpg)\nsome text"
    counts = count_structure(md)
    assert counts["images"] == 2


@pytest.mark.unit
def test_count_structure_links() -> None:
    md = "[link1](url1) and [link2](url2) but ![img](img.png)"
    counts = count_structure(md)
    # Only non-image links; the image above should not count
    assert counts["links"] == 2


@pytest.mark.unit
def test_count_structure_empty() -> None:
    counts = count_structure("")
    assert counts == {"headings": 0, "code_blocks": 0, "tables": 0, "images": 0, "links": 0}


# substring_hit_rate / substring_absent_rate — including None when no anchors


@pytest.mark.unit
def test_substring_hit_rate_none_when_empty() -> None:
    assert substring_hit_rate("some text", []) is None


@pytest.mark.unit
def test_substring_hit_rate_all_present() -> None:
    assert substring_hit_rate("Hello World", ["hello", "world"]) == pytest.approx(1.0)


@pytest.mark.unit
def test_substring_hit_rate_partial() -> None:
    assert substring_hit_rate("Hello", ["hello", "missing"]) == pytest.approx(0.5)


@pytest.mark.unit
def test_substring_hit_rate_case_insensitive() -> None:
    assert substring_hit_rate("HELLO WORLD", ["hello", "world"]) == pytest.approx(1.0)


@pytest.mark.unit
def test_substring_absent_rate_none_when_empty() -> None:
    assert substring_absent_rate("some text", []) is None


@pytest.mark.unit
def test_substring_absent_rate_all_absent() -> None:
    # Good output: none of the boilerplate leaked
    assert substring_absent_rate("Main content here", ["navigation", "footer"]) == pytest.approx(1.0)


@pytest.mark.unit
def test_substring_absent_rate_all_present() -> None:
    # Bad output: all boilerplate leaked
    assert substring_absent_rate("navigation footer sidebar", ["navigation", "footer"]) == pytest.approx(0.0)


# composite_quality — None-skipping, renormalisation, success=False → 0


@pytest.mark.unit
def test_composite_quality_failed_scrape() -> None:
    score = composite_quality(
        success=False,
        char_coverage_value=0.9,
        token_f1=0.8,
        noise=0.1,
        expect_hit=1.0,
        expect_absent_ok=1.0,
        link_density_value=5.0,
    )
    assert score == 0.0


@pytest.mark.unit
def test_composite_quality_all_perfect() -> None:
    score = composite_quality(
        success=True,
        char_coverage_value=1.0,
        token_f1=1.0,
        noise=0.0,
        expect_hit=1.0,
        expect_absent_ok=1.0,
        link_density_value=0.0,
    )
    assert score == pytest.approx(100.0, abs=0.1)


@pytest.mark.unit
def test_composite_quality_none_skipping_renormalises() -> None:
    # When only expect_hit is provided (all others None), composite should
    # still produce a meaningful score (the weight is renormalised to 1.0).
    score = composite_quality(
        success=True,
        char_coverage_value=None,
        token_f1=None,
        noise=None,
        expect_hit=0.5,
        expect_absent_ok=None,
        link_density_value=None,
    )
    # Weight renormalises: 0.5 * 0.15 / 0.15 = 0.5 → 50.0
    assert score == pytest.approx(50.0, abs=0.1)


@pytest.mark.unit
def test_composite_quality_no_inputs_gives_floor() -> None:
    # Scrape succeeded but nothing to score on → 50.0 neutral floor
    score = composite_quality(
        success=True,
        char_coverage_value=None,
        token_f1=None,
        noise=None,
        expect_hit=None,
        expect_absent_ok=None,
        link_density_value=None,
    )
    assert score == pytest.approx(50.0)


@pytest.mark.unit
def test_composite_quality_high_link_density_penalised() -> None:
    # High link density should push quality down compared to zero density
    score_low_density = composite_quality(
        success=True,
        char_coverage_value=0.8,
        token_f1=0.8,
        noise=0.1,
        expect_hit=None,
        expect_absent_ok=None,
        link_density_value=0.0,
    )
    score_high_density = composite_quality(
        success=True,
        char_coverage_value=0.8,
        token_f1=0.8,
        noise=0.1,
        expect_hit=None,
        expect_absent_ok=None,
        link_density_value=100.0,  # well above the 50-link penalty cap
    )
    assert score_high_density < score_low_density


# word_spacing — fused-PDF guard, CJK exemption, short-body None


@pytest.mark.unit
def test_word_spacing_clean_text() -> None:
    # Properly spaced prose has no over-long fused tokens.
    clean = "the dominant sequence transduction models are based on recurrent networks " * 12
    assert word_spacing(clean) == pytest.approx(1.0)


@pytest.mark.unit
def test_word_spacing_fused_runs_penalised() -> None:
    # ~10% of tokens are 30+ char fused word runs (a PDF spacing defect); at the
    # 5%-full-penalty ratio this drives the spacing score to 0.
    fused = "Thedominantsequencetransductionmodelsarebasedoncomplexrecurrent a b c d e f g h i " * 12
    assert word_spacing(fused) == pytest.approx(0.0)


@pytest.mark.unit
def test_word_spacing_cjk_not_penalised() -> None:
    # CJK under-segments into long runs under whitespace tokenisation, but those
    # tokens are non-ASCII and must NOT be counted as fused — score stays high.
    cjk = "ウェブスクレイピング は ウェブサイト から 情報 を 抽出 する 技術 です " * 12
    score = word_spacing(cjk)
    assert score is not None
    assert score == pytest.approx(1.0)


@pytest.mark.unit
def test_word_spacing_short_body_returns_none() -> None:
    # Below the minimum token count there is too little prose to judge.
    assert word_spacing("only a handful of words here") is None


@pytest.mark.unit
def test_composite_quality_fused_pdf_scored_below_clean() -> None:
    # The PDF path has no browser reference, so only expect_hit and word_spacing
    # are available. A fused extraction (spacing=0) must score well below a clean
    # one (spacing=1) even though both hit every anchor — this is the regression
    # signal that catches a reintroduced PDF spacing defect.
    clean = composite_quality(
        success=True,
        char_coverage_value=None,
        token_f1=None,
        noise=None,
        expect_hit=1.0,
        expect_absent_ok=None,
        link_density_value=None,
        word_spacing_value=1.0,
    )
    fused = composite_quality(
        success=True,
        char_coverage_value=None,
        token_f1=None,
        noise=None,
        expect_hit=1.0,
        expect_absent_ok=None,
        link_density_value=None,
        word_spacing_value=0.0,
    )
    # expect_hit weight 0.15, word_spacing weight 0.10 → clean=100, fused=60.
    assert clean == pytest.approx(100.0)
    assert fused == pytest.approx(60.0)
