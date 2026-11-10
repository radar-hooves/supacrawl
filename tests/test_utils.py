"""Tests for utility functions."""

from supacrawl.utils import (
    TRACKING_PARAMS,
    normalise_url_for_dedupe,
    strip_tracking_params,
)


class TestStripTrackingParams:
    """Tests for strip_tracking_params function."""

    def test_removes_utm_params(self):
        """Test removal of UTM parameters."""
        url = "https://example.com/page?utm_source=twitter&utm_medium=social&name=test"
        result = strip_tracking_params(url)
        assert result == "https://example.com/page?name=test"

    def test_removes_all_utm_variants(self):
        """Test removal of all UTM parameter variants."""
        url = "https://example.com/page?utm_source=a&utm_medium=b&utm_campaign=c&utm_term=d&utm_content=e"
        result = strip_tracking_params(url)
        assert result == "https://example.com/page"

    def test_removes_facebook_params(self):
        """Test removal of Facebook click ID."""
        url = "https://example.com/page?fbclid=abc123&id=42"
        result = strip_tracking_params(url)
        assert result == "https://example.com/page?id=42"

    def test_removes_google_ads_params(self):
        """Test removal of Google Ads click ID."""
        url = "https://example.com/page?gclid=xyz789&product=widget"
        result = strip_tracking_params(url)
        assert result == "https://example.com/page?product=widget"

    def test_removes_microsoft_params(self):
        """Test removal of Microsoft click ID."""
        url = "https://example.com/page?msclkid=def456&page=1"
        result = strip_tracking_params(url)
        assert result == "https://example.com/page?page=1"

    def test_removes_mailchimp_params(self):
        """Test removal of Mailchimp tracking parameters."""
        url = "https://example.com/page?mc_cid=campaign&mc_eid=email&item=123"
        result = strip_tracking_params(url)
        assert result == "https://example.com/page?item=123"

    def test_removes_google_analytics_params(self):
        """Test removal of Google Analytics _ga and _gl parameters."""
        url = "https://example.com/page?_ga=1.234&_gl=5.678&query=search"
        result = strip_tracking_params(url)
        assert result == "https://example.com/page?query=search"

    def test_removes_ref_params(self):
        """Test removal of referral tracking parameters."""
        url = "https://example.com/page?ref=homepage&ref_src=blog&category=tech"
        result = strip_tracking_params(url)
        assert result == "https://example.com/page?category=tech"

    def test_removes_source_and_share(self):
        """Test removal of source and share parameters."""
        url = "https://example.com/page?source=newsletter&share=twitter&id=1"
        result = strip_tracking_params(url)
        assert result == "https://example.com/page?id=1"

    def test_preserves_meaningful_params(self):
        """Test that meaningful query parameters are preserved."""
        url = "https://example.com/search?q=python&page=2&sort=date"
        result = strip_tracking_params(url)
        assert result == url

    def test_handles_empty_query(self):
        """Test handling of URLs without query parameters."""
        url = "https://example.com/page"
        result = strip_tracking_params(url)
        assert result == url

    def test_handles_fragment_preserved(self):
        """Test that fragments are preserved (strip_tracking_params only removes params)."""
        url = "https://example.com/page?utm_source=test#section"
        result = strip_tracking_params(url)
        assert result == "https://example.com/page#section"

    def test_case_insensitive_params(self):
        """Test that parameter matching is case-insensitive."""
        url = "https://example.com/page?UTM_SOURCE=test&Fbclid=abc"
        result = strip_tracking_params(url)
        assert result == "https://example.com/page"


class TestNormaliseUrlForDedupe:
    """Tests for normalise_url_for_dedupe function."""

    def test_removes_fragment(self):
        """Test that URL fragments are removed."""
        url = "https://example.com/page#section"
        result = normalise_url_for_dedupe(url)
        assert result == "https://example.com/page"

    def test_removes_tracking_params(self):
        """Test that tracking parameters are removed."""
        url = "https://example.com/page?utm_source=twitter&id=123"
        result = normalise_url_for_dedupe(url)
        assert result == "https://example.com/page?id=123"

    def test_sorts_query_params(self):
        """Test that query parameters are sorted for consistent comparison."""
        url1 = "https://example.com/page?b=2&a=1"
        url2 = "https://example.com/page?a=1&b=2"
        assert normalise_url_for_dedupe(url1) == normalise_url_for_dedupe(url2)

    def test_same_path_different_tracking_params_dedupe(self):
        """Test that same paths with different tracking params normalise to same URL."""
        url1 = "https://example.com/page?utm_source=twitter"
        url2 = "https://example.com/page?utm_source=facebook"
        url3 = "https://example.com/page?fbclid=abc123"
        result1 = normalise_url_for_dedupe(url1)
        result2 = normalise_url_for_dedupe(url2)
        result3 = normalise_url_for_dedupe(url3)
        assert result1 == result2 == result3 == "https://example.com/page"

    def test_different_meaningful_params_not_deduped(self):
        """Test that different meaningful params result in different normalised URLs."""
        url1 = "https://example.com/product?id=123"
        url2 = "https://example.com/product?id=456"
        result1 = normalise_url_for_dedupe(url1)
        result2 = normalise_url_for_dedupe(url2)
        assert result1 != result2

    def test_combined_normalisation(self):
        """Test combined fragment removal, tracking param removal, and sorting."""
        url = "https://example.com/page?utm_source=test&z=3&a=1#section"
        result = normalise_url_for_dedupe(url)
        assert result == "https://example.com/page?a=1&z=3"

    def test_handles_no_query_no_fragment(self):
        """Test handling of simple URLs."""
        url = "https://example.com/page"
        result = normalise_url_for_dedupe(url)
        assert result == url

    def test_page_1_not_special_cased(self):
        """Test that page=1 is not removed (could be meaningful)."""
        # Note: We don't remove page=1 as it could be meaningful
        # This test documents the current behaviour
        url = "https://example.com/articles?page=1"
        result = normalise_url_for_dedupe(url)
        assert result == url


class TestTrackingParams:
    """Tests for TRACKING_PARAMS constant."""

    def test_contains_expected_params(self):
        """Test that TRACKING_PARAMS contains all expected parameters."""
        expected = {
            "utm_source",
            "utm_medium",
            "utm_campaign",
            "utm_term",
            "utm_content",
            "_ga",
            "_gl",
            "fbclid",
            "gclid",
            "msclkid",
            "igshid",
            "mc_cid",
            "mc_eid",
            "ref",
            "ref_src",
            "source",
            "share",
        }
        assert expected.issubset(TRACKING_PARAMS)

    def test_is_frozenset(self):
        """Test that TRACKING_PARAMS is immutable."""
        assert isinstance(TRACKING_PARAMS, frozenset)


class TestGatherResults:
    """gather_results keeps asyncio.gather's contract on a TaskGroup."""

    async def test_results_keep_input_order(self):
        import asyncio

        from supacrawl.utils import gather_results

        async def slow(n: int) -> int:
            await asyncio.sleep(0.01 * (3 - n))
            return n

        assert await gather_results(slow(n) for n in range(3)) == [0, 1, 2]

    async def test_first_failure_propagates_unwrapped(self):
        import pytest

        from supacrawl.utils import gather_results

        async def boom() -> None:
            raise ValueError("bad")

        with pytest.raises(ValueError, match="bad"):
            await gather_results([boom()])
