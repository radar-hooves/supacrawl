"""Unit tests for content-quality assessment and stealth-hint gating (issues #106, #107)."""

from supacrawl.services.scrape import (
    _assess_content_quality,
    _stealth_hint,
)


class TestAssessContentQuality:
    """Unit tests for _assess_content_quality — no network, no browser."""

    # HARD: binary / garbage bodies

    def test_binary_body_returns_hard_reason(self) -> None:
        """A body with >30% non-printable chars must return a HARD-prefixed reason."""
        # Build a body that is clearly above the threshold: every other byte is a
        # non-printable control char (excluding \t, \n, \r).
        garbage_chunk = "\x01\x02\x03\x04\x05" * 500  # 2500 non-printable chars
        padding = "a" * 500  # 500 printable chars
        html = garbage_chunk + padding  # ratio ≈ 83 %
        result = _assess_content_quality(html, markdown=None)
        assert result is not None
        assert result.startswith("HARD:")
        assert "non-printable" in result.lower()

    def test_binary_body_includes_percentage(self) -> None:
        """The HARD reason string must include a percentage figure."""
        garbage = "\x01" * 400
        printable = "x" * 100  # 80 % non-printable
        html = garbage + printable
        result = _assess_content_quality(html, markdown=None)
        assert result is not None
        assert "%" in result

    def test_normal_whitespace_not_counted_as_non_printable(self) -> None:
        """Tab, newline, and carriage-return are excluded from the non-printable tally."""
        html = "\t\n\r" * 200 + "<html><body><p>Hello world this is a page with real content.</p></body></html>"
        result = _assess_content_quality(html, markdown="Hello world this is a page with real content.")
        # Whitespace chars do not count — should not trip the binary check.
        assert result is None or not result.startswith("HARD:")

    # SOFT: printable but suspect structure / density

    def test_missing_structure_returns_soft_reason(self) -> None:
        """Printable HTML with no recognised structure tags should return a SOFT reason."""
        # Enough length to pass the min-length guard, no structure tags.
        html = "x" * 300  # 300 printable chars, zero HTML structure
        result = _assess_content_quality(html, markdown="hello world")
        assert result is not None
        assert result.startswith("SOFT:")

    def test_very_low_word_density_returns_soft_reason(self) -> None:
        """Substantial HTML with near-zero readable words should return a SOFT reason."""
        # 10 KB of junk-looking but printable ASCII, with only 2 markdown words.
        html = "<html><body>" + ("z" * 9000) + "</body></html>"
        result = _assess_content_quality(html, markdown="one two")
        assert result is not None
        assert result.startswith("SOFT:")

    # None: normal content passes

    def test_normal_article_html_returns_none(self) -> None:
        """A typical article page should pass all quality checks."""
        article_content = " ".join(["word"] * 200)  # 200 words
        html = f"<html><head><title>Test</title></head><body><article><p>{article_content}</p></article></body></html>"
        markdown = article_content
        result = _assess_content_quality(html, markdown=markdown)
        assert result is None

    def test_empty_html_returns_none(self) -> None:
        """Empty HTML must return None — nothing to assess."""
        assert _assess_content_quality("", markdown=None) is None

    def test_short_html_below_min_length_returns_none(self) -> None:
        """HTML shorter than the minimum length guard must return None."""
        html = "<p>Short</p>"  # well under 200 chars
        assert _assess_content_quality(html, markdown="Short") is None

    def test_printable_html_with_structure_and_good_density_returns_none(self) -> None:
        """HTML that has structure tags and decent word density should pass."""
        words = " ".join(["text"] * 50)  # 50 words
        html = f"<html><body><div><p>{words}</p></div></body></html>"
        result = _assess_content_quality(html, markdown=words)
        assert result is None


class TestStealthHintGating:
    """Unit tests for _stealth_hint — verifies hint text is gated on bot_suspected."""

    def test_no_hint_when_bot_not_suspected(self) -> None:
        """Pure timeout / network error: hint must NOT suggest engine switching."""
        hint = _stealth_hint(bot_suspected=False)
        # Should be a soft note, not an engine-switching directive.
        assert "patchright" not in hint.lower()
        assert "camoufox" not in hint.lower()
        assert "switching engines is unlikely" in hint.lower()

    def test_hint_present_when_bot_suspected(self) -> None:
        """Confirmed bot challenge: hint must contain actionable engine-switch guidance."""
        hint = _stealth_hint(bot_suspected=True)
        # Must offer at least one actionable suggestion.
        assert any(kw in hint.lower() for kw in ["patchright", "camoufox", "stealth", "install"]), (
            f"Expected engine-switch guidance, got: {hint!r}"
        )

    def test_default_bot_suspected_is_false(self) -> None:
        """Calling _stealth_hint() with no argument must behave like bot_suspected=False."""
        default_hint = _stealth_hint()
        explicit_hint = _stealth_hint(bot_suspected=False)
        assert default_hint == explicit_hint

    def test_bot_suspected_true_differs_from_false(self) -> None:
        """The two paths must produce different strings."""
        assert _stealth_hint(bot_suspected=True) != _stealth_hint(bot_suspected=False)
