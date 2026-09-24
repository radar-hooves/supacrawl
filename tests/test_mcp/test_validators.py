"""Tests for Supacrawl validators module."""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from supacrawl.mcp.exceptions import SupacrawlValidationError
from supacrawl.mcp.validators import (
    enhance_query_with_current_year,
    validate_limit,
    validate_max_steps,
    validate_prompt,
    validate_query,
    validate_timeout,
    validate_url,
    validate_urls,
)

pytestmark = pytest.mark.mcp


class TestValidateUrl:
    """Test URL validation."""

    def test_valid_http_url(self):
        """Should accept valid HTTP URL."""
        result = validate_url("http://example.com")
        assert result == "http://example.com"

    def test_valid_https_url(self):
        """Should accept valid HTTPS URL."""
        result = validate_url("https://example.com/path?query=1")
        assert result == "https://example.com/path?query=1"

    def test_strips_whitespace(self):
        """Should strip leading/trailing whitespace."""
        result = validate_url("  https://example.com  ")
        assert result == "https://example.com"

    def test_rejects_none(self):
        """Should reject None."""
        with pytest.raises(SupacrawlValidationError) as exc_info:
            validate_url(None)
        assert exc_info.value.field == "url"

    def test_rejects_empty_string(self):
        """Should reject empty string."""
        with pytest.raises(SupacrawlValidationError) as exc_info:
            validate_url("")
        assert exc_info.value.field == "url"

    def test_rejects_invalid_url(self):
        """Should reject invalid URL format."""
        with pytest.raises(SupacrawlValidationError) as exc_info:
            validate_url("not-a-url")
        assert "http" in str(exc_info.value).lower()

    def test_rejects_ftp_protocol(self):
        """Should reject non-HTTP protocols."""
        with pytest.raises(SupacrawlValidationError) as exc_info:
            validate_url("ftp://example.com")
        assert "http" in str(exc_info.value).lower()

    def test_rejects_cloud_metadata_ip(self):
        """The MCP argument validator refuses a link-local/metadata IP literal (#152)."""
        with pytest.raises(SupacrawlValidationError) as exc_info:
            validate_url("http://169.254.169.254/latest/meta-data/")
        assert "blocked" in str(exc_info.value).lower()

    def test_rejects_ipv4_mapped_metadata_ip(self):
        """The IPv4-mapped IPv6 spelling of the metadata endpoint is refused too (#152)."""
        with pytest.raises(SupacrawlValidationError) as exc_info:
            validate_url("http://[::ffff:169.254.169.254]/latest/meta-data/")
        assert "blocked" in str(exc_info.value).lower()

    def test_allows_public_url(self):
        """A normal public URL passes the SSRF pre-check unchanged."""
        assert validate_url("https://docs.example.com/guide") == "https://docs.example.com/guide"


class TestValidateQuery:
    """Test search query validation."""

    def test_valid_query(self):
        """Should accept valid query."""
        result = validate_query("search term")
        assert result == "search term"

    def test_strips_whitespace(self):
        """Should strip whitespace."""
        result = validate_query("  search term  ")
        assert result == "search term"

    def test_rejects_none(self):
        """Should reject None with helpful message."""
        with pytest.raises(SupacrawlValidationError) as exc_info:
            validate_query(None)
        assert exc_info.value.field == "query"
        assert "required" in str(exc_info.value).lower()

    def test_rejects_empty_string(self):
        """Should reject empty string."""
        with pytest.raises(SupacrawlValidationError) as exc_info:
            validate_query("")
        assert exc_info.value.field == "query"

    def test_rejects_whitespace_only(self):
        """Should reject whitespace-only string."""
        with pytest.raises(SupacrawlValidationError) as exc_info:
            validate_query("   ")
        assert exc_info.value.field == "query"

    def test_rejects_too_long_query(self):
        """Should reject query exceeding max length."""
        long_query = "x" * 1001
        with pytest.raises(SupacrawlValidationError) as exc_info:
            validate_query(long_query)
        assert "1000" in str(exc_info.value)


class TestValidateUrls:
    """Test URL list validation."""

    def test_valid_urls(self):
        """Should accept valid URL list."""
        urls = ["https://example.com/1", "https://example.com/2"]
        result = validate_urls(urls, "urls")
        assert result == urls

    def test_rejects_empty_list(self):
        """Should reject empty list."""
        with pytest.raises(SupacrawlValidationError) as exc_info:
            validate_urls([], "urls")
        assert "at least" in str(exc_info.value).lower()

    def test_rejects_none(self):
        """Should reject None."""
        with pytest.raises(SupacrawlValidationError) as exc_info:
            validate_urls(None, "urls")
        assert "urls" in str(exc_info.value).lower()

    def test_rejects_too_many_urls(self):
        """Should reject list exceeding max count."""
        urls = [f"https://example.com/{i}" for i in range(15)]
        with pytest.raises(SupacrawlValidationError) as exc_info:
            validate_urls(urls, "urls", max_count=10)
        assert "10" in str(exc_info.value)

    def test_validates_each_url(self):
        """Should validate each URL in the list."""
        urls = ["https://example.com", "invalid-url"]
        with pytest.raises(SupacrawlValidationError) as exc_info:
            validate_urls(urls, "urls")
        assert "invalid-url" in str(exc_info.value)

    def test_accepts_a_single_bare_url_string(self):
        """A non-JSON string is treated as one URL, not refused as 'not a list'
        (the failure mode from correlation_id 6a66d610)."""
        result = validate_urls("https://example.com/a", "urls")
        assert result == ["https://example.com/a"]

    def test_accepts_a_json_encoded_array_string(self):
        """A JSON-encoded array string is parsed into its URL list."""
        result = validate_urls('["https://example.com/1", "https://example.com/2"]', "urls")
        assert result == ["https://example.com/1", "https://example.com/2"]

    def test_rejects_invalid_bare_url_string(self):
        """A bare string that isn't a valid URL still fails URL validation."""
        with pytest.raises(SupacrawlValidationError) as exc_info:
            validate_urls("not-a-url", "urls")
        assert "not-a-url" in str(exc_info.value)

    def test_rejects_non_string_non_list(self):
        """A type that is neither list nor string is still refused."""
        with pytest.raises(SupacrawlValidationError) as exc_info:
            validate_urls(42, "urls")
        assert "int" in str(exc_info.value)


class TestValidateLimit:
    """Test limit validation."""

    def test_valid_limit(self):
        """Should accept valid limit."""
        result = validate_limit(10, "limit")
        assert result == 10

    def test_uses_default_for_none(self):
        """Should use default for None."""
        result = validate_limit(None, "limit", default=5)
        assert result == 5

    def test_rejects_below_minimum(self):
        """Should reject value below minimum."""
        with pytest.raises(SupacrawlValidationError) as exc_info:
            validate_limit(0, "limit", min_value=1, default=5)
        assert "positive" in str(exc_info.value).lower()

    def test_rejects_above_maximum(self):
        """Should reject value above maximum."""
        with pytest.raises(SupacrawlValidationError) as exc_info:
            validate_limit(1000, "limit", max_value=100, default=5)
        assert "100" in str(exc_info.value)


class TestValidateTimeout:
    """Test timeout validation."""

    def test_valid_timeout(self):
        """Should accept valid timeout."""
        result = validate_timeout(30000, "timeout")
        assert result == 30000

    def test_returns_none_for_none(self):
        """Should return None for None input."""
        result = validate_timeout(None, "timeout")
        assert result is None

    def test_rejects_negative(self):
        """Should reject negative timeout."""
        with pytest.raises(SupacrawlValidationError) as exc_info:
            validate_timeout(-1000, "timeout")
        # The mcp-common validator says "at least 1000ms"
        assert "1000" in str(exc_info.value) or "negative" in str(exc_info.value).lower()

    def test_rejects_too_large(self):
        """Should reject timeout exceeding maximum."""
        with pytest.raises(SupacrawlValidationError) as exc_info:
            validate_timeout(1000000, "timeout")
        assert "300000" in str(exc_info.value)


class TestValidatePrompt:
    """Test prompt validation."""

    def test_valid_prompt(self):
        """Should accept valid prompt."""
        result = validate_prompt("Extract the product name")
        assert result == "Extract the product name"

    def test_allows_none_when_specified(self):
        """Should allow None when allow_none=True."""
        result = validate_prompt(None, allow_none=True)
        assert result is None

    def test_rejects_none_by_default(self):
        """Should reject None by default."""
        with pytest.raises(SupacrawlValidationError) as exc_info:
            validate_prompt(None)
        assert "prompt" in str(exc_info.value).lower()

    def test_rejects_short_prompt(self):
        """Should reject prompt shorter than min_length."""
        with pytest.raises(SupacrawlValidationError) as exc_info:
            validate_prompt("hi", min_length=10)
        assert "10" in str(exc_info.value)


class TestValidateMaxSteps:
    """Test max_steps validation."""

    def test_valid_max_steps(self):
        """Should accept valid max_steps."""
        result = validate_max_steps(10)
        assert result == 10

    def test_clamps_to_minimum(self):
        """Should clamp to minimum (1)."""
        result = validate_max_steps(0)
        assert result == 1

    def test_clamps_to_maximum(self):
        """Should clamp to maximum (20)."""
        result = validate_max_steps(50)
        assert result == 20

    def test_uses_default_for_none(self):
        """Should use default for None."""
        result = validate_max_steps(None)
        assert result == 10


class TestEnhanceQueryWithCurrentYear:
    """Test query enhancement with current year for time-sensitive searches."""

    @patch("supacrawl.mcp.validators.datetime")
    def test_adds_year_to_time_sensitive_query(self, mock_datetime):
        """Should add current year to queries with time-sensitive keywords."""
        mock_datetime.now.return_value = datetime(2026, 1, 24, tzinfo=timezone.utc)

        # Test various time-sensitive keywords
        assert enhance_query_with_current_year("best python frameworks") == "best python frameworks 2026"
        assert enhance_query_with_current_year("latest react tutorial") == "latest react tutorial 2026"
        assert enhance_query_with_current_year("GitHub mobile app APK download F-Droid alternative") == (
            "GitHub mobile app APK download F-Droid alternative 2026"
        )

    @patch("supacrawl.mcp.validators.datetime")
    def test_replaces_stale_year_in_time_sensitive_query(self, mock_datetime):
        """Should replace past years with current year in time-sensitive queries."""
        mock_datetime.now.return_value = datetime(2026, 1, 24, tzinfo=timezone.utc)

        # Stale years should be replaced (LLMs often use their training cutoff year)
        assert enhance_query_with_current_year("best python frameworks 2024") == "best python frameworks 2026"
        assert enhance_query_with_current_year("python 2024 tutorial") == "python 2026 tutorial"
        assert enhance_query_with_current_year("latest react 2025") == "latest react 2026"

    @patch("supacrawl.mcp.validators.datetime")
    def test_preserves_current_and_future_years(self, mock_datetime):
        """Should not modify current or future years in time-sensitive queries."""
        mock_datetime.now.return_value = datetime(2026, 1, 24, tzinfo=timezone.utc)

        # Current year should be preserved
        assert enhance_query_with_current_year("best python frameworks 2026") == "best python frameworks 2026"
        # Future years should be preserved
        assert enhance_query_with_current_year("best python frameworks 2027") == "best python frameworks 2027"

    @patch("supacrawl.mcp.validators.datetime")
    def test_preserves_years_in_non_time_sensitive_queries(self, mock_datetime):
        """Should not modify years in non-time-sensitive queries."""
        mock_datetime.now.return_value = datetime(2026, 1, 24, tzinfo=timezone.utc)

        # Non-time-sensitive queries should keep their years unchanged (may be intentional)
        assert enhance_query_with_current_year("python syntax 2024") == "python syntax 2024"
        assert enhance_query_with_current_year("what happened in 2020") == "what happened in 2020"
        assert enhance_query_with_current_year("history of computing 2015") == "history of computing 2015"

    @patch("supacrawl.mcp.validators.datetime")
    def test_ignores_non_time_sensitive_queries(self, mock_datetime):
        """Should not modify queries without time-sensitive keywords."""
        mock_datetime.now.return_value = datetime(2026, 1, 24, tzinfo=timezone.utc)

        # Non-time-sensitive queries should be unchanged
        assert enhance_query_with_current_year("python syntax") == "python syntax"
        assert enhance_query_with_current_year("what is recursion") == "what is recursion"
        assert enhance_query_with_current_year("how to write tests") == "how to write tests"

    @patch("supacrawl.mcp.validators.datetime")
    def test_handles_case_insensitivity(self, mock_datetime):
        """Should detect time-sensitive keywords regardless of case."""
        mock_datetime.now.return_value = datetime(2026, 1, 24, tzinfo=timezone.utc)

        assert enhance_query_with_current_year("BEST python frameworks") == "BEST python frameworks 2026"
        assert enhance_query_with_current_year("Latest React Tutorial") == "Latest React Tutorial 2026"

    @patch("supacrawl.mcp.validators.datetime")
    def test_various_time_sensitive_keywords(self, mock_datetime):
        """Should recognise all defined time-sensitive keywords."""
        mock_datetime.now.return_value = datetime(2026, 1, 24, tzinfo=timezone.utc)

        # Recency indicators
        assert "2026" in enhance_query_with_current_year("latest news")
        assert "2026" in enhance_query_with_current_year("recent developments")
        assert "2026" in enhance_query_with_current_year("newest features")
        assert "2026" in enhance_query_with_current_year("current status")

        # Comparison/alternatives
        assert "2026" in enhance_query_with_current_year("best tools")
        assert "2026" in enhance_query_with_current_year("top frameworks")
        assert "2026" in enhance_query_with_current_year("alternative to X")
        assert "2026" in enhance_query_with_current_year("X vs Y comparison")

        # Software/tech specific
        assert "2026" in enhance_query_with_current_year("python download")
        assert "2026" in enhance_query_with_current_year("node install guide")
        assert "2026" in enhance_query_with_current_year("react release notes")

        # Rankings/reviews
        assert "2026" in enhance_query_with_current_year("product review")
        assert "2026" in enhance_query_with_current_year("framework ranking")
        assert "2026" in enhance_query_with_current_year("beginner guide")
