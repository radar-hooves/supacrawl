"""Unit tests for custom HTTP header support (#86).

Covers:
- CLI header string parsing (parse_header_string)
- SUPACRAWL_HEADERS env var parsing (parse_headers_env)
- Cache variant hash: different headers → different variant; same headers → same
  variant; no raw header values appear in the variant string
- Same-origin scoping helpers in CrawlService
- Security: no header values leak into DEBUG log output
"""

import logging
from typing import ClassVar

import pytest

from supacrawl.cli._common import parse_header_string, parse_headers_env
from supacrawl.services.crawl import _scope_headers_to_origin, _url_origin
from supacrawl.services.scrape import _compute_headers_hash

# parse_header_string


class TestParseHeaderString:
    def test_simple_header(self):
        name, value = parse_header_string("Authorization: Bearer abc")
        assert name == "Authorization"
        assert value == "Bearer abc"

    def test_strips_whitespace(self):
        name, value = parse_header_string("  X-Custom-Header :  some value  ")
        assert name == "X-Custom-Header"
        assert value == "some value"

    def test_value_contains_colon(self):
        """A URL in the value must not be split at its own colon."""
        name, value = parse_header_string("Location: https://example.com/path")
        assert name == "Location"
        assert value == "https://example.com/path"

    def test_no_colon_raises(self):
        import click

        with pytest.raises(click.BadParameter):
            parse_header_string("NoColonHere")

    def test_empty_value_allowed(self):
        name, value = parse_header_string("X-Empty:")
        assert name == "X-Empty"
        assert value == ""

    def test_multiple_headers_parsed_independently(self):
        pairs = [parse_header_string(r) for r in ["A: 1", "B: 2", "C: 3"]]
        assert pairs == [("A", "1"), ("B", "2"), ("C", "3")]


# parse_headers_env


class TestParseHeadersEnv:
    def test_unset_returns_none(self, monkeypatch):
        monkeypatch.delenv("SUPACRAWL_HEADERS", raising=False)
        assert parse_headers_env() is None

    def test_empty_string_returns_none(self, monkeypatch):
        monkeypatch.setenv("SUPACRAWL_HEADERS", "")
        assert parse_headers_env() is None

    def test_single_header(self, monkeypatch):
        monkeypatch.setenv("SUPACRAWL_HEADERS", "Authorization: Bearer tok")
        result = parse_headers_env()
        assert result == {"Authorization": "Bearer tok"}

    def test_multiple_comma_separated(self, monkeypatch):
        monkeypatch.setenv("SUPACRAWL_HEADERS", "X-A: 1, X-B: 2, X-C: 3")
        result = parse_headers_env()
        assert result == {"X-A": "1", "X-B": "2", "X-C": "3"}

    def test_malformed_entry_skipped(self, monkeypatch):
        """A malformed entry (no colon) is skipped; the rest are parsed."""
        monkeypatch.setenv("SUPACRAWL_HEADERS", "X-Good: ok, bad-entry, X-Also: fine")
        result = parse_headers_env()
        assert result == {"X-Good": "ok", "X-Also": "fine"}


# Cache variant hash (_compute_headers_hash)


class TestComputeHeadersHash:
    def test_same_headers_same_hash(self):
        h1 = {"Authorization": "Bearer token", "X-Tenant": "acme"}
        h2 = {"Authorization": "Bearer token", "X-Tenant": "acme"}
        assert _compute_headers_hash(h1) == _compute_headers_hash(h2)

    def test_different_values_different_hash(self):
        h1 = {"Authorization": "Bearer token-a"}
        h2 = {"Authorization": "Bearer token-b"}
        assert _compute_headers_hash(h1) != _compute_headers_hash(h2)

    def test_order_independent(self):
        h1 = {"A": "1", "B": "2"}
        h2 = {"B": "2", "A": "1"}
        assert _compute_headers_hash(h1) == _compute_headers_hash(h2)

    def test_no_raw_values_in_hash_string(self):
        """The hash must be a short hex digest, not a repr of the values."""
        secret = "super-secret-bearer-token-XYZ"
        h = {"Authorization": f"Bearer {secret}"}
        digest = _compute_headers_hash(h)
        assert secret not in digest
        # Must be a short hex string (12 chars per implementation)
        assert len(digest) == 12
        assert all(c in "0123456789abcdef" for c in digest)

    def test_empty_headers_not_called(self):
        """Calling with an empty dict is allowed but would not normally occur."""
        result = _compute_headers_hash({})
        assert isinstance(result, str)
        assert len(result) == 12


# Same-origin scoping helpers


class TestUrlOrigin:
    def test_standard_https(self):
        assert _url_origin("https://example.com/path?q=1") == "https://example.com"

    def test_standard_http(self):
        assert _url_origin("http://example.com/") == "http://example.com"

    def test_explicit_port(self):
        assert _url_origin("https://example.com:8443/path") == "https://example.com:8443"

    def test_subdomain(self):
        assert _url_origin("https://blog.example.com/post") == "https://blog.example.com"


class TestScopeHeadersToOrigin:
    HEADERS: ClassVar = {"Authorization": "Bearer tok"}

    def test_same_origin_passes_headers(self):
        result = _scope_headers_to_origin(
            headers=self.HEADERS,
            target_url="https://example.com/page",
            start_origin="https://example.com",
        )
        assert result is self.HEADERS

    def test_different_origin_drops_headers(self):
        result = _scope_headers_to_origin(
            headers=self.HEADERS,
            target_url="https://external.com/page",
            start_origin="https://example.com",
        )
        assert result is None

    def test_none_headers_returns_none(self):
        result = _scope_headers_to_origin(
            headers=None,
            target_url="https://example.com/page",
            start_origin="https://example.com",
        )
        assert result is None

    def test_subdomain_treated_as_different_origin(self):
        """blog.example.com is not the same origin as example.com."""
        result = _scope_headers_to_origin(
            headers=self.HEADERS,
            target_url="https://blog.example.com/post",
            start_origin="https://example.com",
        )
        assert result is None

    def test_port_mismatch_is_different_origin(self):
        result = _scope_headers_to_origin(
            headers=self.HEADERS,
            target_url="https://example.com:8080/page",
            start_origin="https://example.com",
        )
        assert result is None


# Security: no header values leak into log output


class TestHeaderSecurityLogging:
    def test_parse_header_string_does_not_log_value(self, caplog):
        """parse_header_string should not emit the value to any logger."""
        secret = "top-secret-value-12345"
        with caplog.at_level(logging.DEBUG):
            parse_header_string(f"X-Secret: {secret}")
        for record in caplog.records:
            assert secret not in record.getMessage()

    def test_parse_headers_env_does_not_log_value(self, monkeypatch, caplog):
        """parse_headers_env must not emit header values to any logger."""
        secret = "env-secret-XYZ"
        monkeypatch.setenv("SUPACRAWL_HEADERS", f"X-Token: {secret}")
        with caplog.at_level(logging.DEBUG):
            parse_headers_env()
        for record in caplog.records:
            assert secret not in record.getMessage()

    def test_hash_does_not_contain_value(self):
        """The cache variant hash must never contain raw header values."""
        secret = "raw-header-value-ABCDEF"
        digest = _compute_headers_hash({"Authorization": f"Bearer {secret}"})
        assert secret not in digest
