"""Tests for the outbound URL safety guard (SSRF hardening, #152).

Covers:
- Non-http(s) schemes are rejected.
- Cloud-metadata / link-local IP literals are rejected (169.254.x.x, fe80::).
- Normal public URLs are allowed.
- RFC1918 private ranges are allowed by default, refused under the strict switch.
- A hostname resolving to a blocked address is refused at connect time, not
  just when the URL contains an IP literal (the rebinding window #152 exists
  to close).
- guarded_request/guarded_stream pin every redirect hop, refusing a 3xx pivot
  to an internal address exactly like a direct request to it.

Uses a stubbed resolver (patching socket.getaddrinfo) and a fake httpx client
so no real network egress or DNS is needed, per #152's test requirements.
"""

from __future__ import annotations

import os
import socket
from unittest.mock import MagicMock, patch

import httpx
import pytest

from supacrawl.exceptions import ValidationError
from supacrawl.services.url_guard import (
    _authority,
    _is_blocked_ip,
    assert_safe_url,
    guarded_request,
    is_blocked_address,
    pinned_url,
    resolve_and_pin,
)


class TestAssertSafeUrl:
    """assert_safe_url raises ValidationError for unsafe URLs, passes safe ones."""

    def test_http_allowed(self) -> None:
        assert_safe_url("http://docs.example.com/guide")

    def test_https_allowed(self) -> None:
        assert_safe_url("https://docs.example.com/guide")

    def test_file_scheme_rejected(self) -> None:
        with pytest.raises(ValidationError, match="file"):
            assert_safe_url("file:///etc/passwd")

    def test_ftp_scheme_rejected(self) -> None:
        with pytest.raises(ValidationError, match="ftp"):
            assert_safe_url("ftp://files.example.com/data.csv")

    def test_data_scheme_rejected(self) -> None:
        with pytest.raises(ValidationError, match="data"):
            assert_safe_url("data:text/html,<h1>Hi</h1>")

    def test_javascript_scheme_rejected(self) -> None:
        with pytest.raises(ValidationError, match="javascript"):
            assert_safe_url("javascript:alert(1)")

    def test_aws_metadata_ip_rejected(self) -> None:
        with pytest.raises(ValidationError, match="blocked"):
            assert_safe_url("http://169.254.169.254/latest/meta-data/")

    def test_link_local_other_rejected(self) -> None:
        with pytest.raises(ValidationError, match="blocked"):
            assert_safe_url("http://169.254.0.1/resource")

    def test_ipv6_link_local_rejected(self) -> None:
        with pytest.raises(ValidationError, match="blocked"):
            assert_safe_url("http://[fe80::1]/resource")

    def test_public_ip_allowed(self) -> None:
        assert_safe_url("https://8.8.8.8/dns-query")

    def test_rfc1918_192168_allowed_by_default(self) -> None:
        assert_safe_url("http://192.168.1.10/docs/")

    def test_rfc1918_10_allowed_by_default(self) -> None:
        assert_safe_url("http://10.0.1.2:8080/api/")

    def test_rfc1918_172_allowed_by_default(self) -> None:
        assert_safe_url("http://172.16.0.5/internal/")

    def test_loopback_allowed_by_default(self) -> None:
        assert_safe_url("http://127.0.0.1:8080/")

    def test_hostname_allowed(self) -> None:
        """Plain hostnames are not resolved by this check; caught later by resolve_and_pin."""
        assert_safe_url("https://internal.corp/docs/")

    def test_rfc1918_ip_literal_rejected_in_strict_mode(self) -> None:
        with (
            patch.dict(os.environ, {"SUPACRAWL_BLOCK_PRIVATE_NETWORKS": "1"}),
            pytest.raises(ValidationError, match="blocked"),
        ):
            assert_safe_url("http://192.168.1.10/docs/")

    def test_metadata_still_rejected_in_strict_mode(self) -> None:
        with (
            patch.dict(os.environ, {"SUPACRAWL_BLOCK_PRIVATE_NETWORKS": "1"}),
            pytest.raises(ValidationError, match="blocked"),
        ):
            assert_safe_url("http://169.254.169.254/latest/meta-data/")


class TestParserDivergenceReject:
    """Backslash / control-char URLs are refused before parse (RFC 3986 vs WHATWG, #152).

    Python's urlparse and a WHATWG browser engine disagree on the authority of
    such a URL, so the browser pre-flight would validate a different host than
    Chromium navigates to. Rejecting the raw character at the shared chokepoint
    closes it for both the httpx and browser paths.
    """

    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254\\@allowed-host.example/steal",  # backslash userinfo trick
            "https://allowed.example\\@169.254.169.254/",
            "http://169.254.169.254\t@allowed.example/",  # tab
            "http://169.254.169.254\n@allowed.example/",  # newline
            "http://allowed.example\r/path",  # carriage return
        ],
    )
    def test_parser_divergence_urls_refused(self, url: str) -> None:
        with pytest.raises(ValidationError, match=r"percent-encode|control character|interpret"):
            assert_safe_url(url)

    def test_resolve_and_pin_refuses_backslash_before_resolving(self) -> None:
        with (
            patch("supacrawl.services.url_guard.socket.getaddrinfo") as mock_resolve,
            pytest.raises(ValidationError),
        ):
            resolve_and_pin("http://127.0.0.1\\@example.com/")
        mock_resolve.assert_not_called()

    def test_percent_encoded_backslash_is_allowed(self) -> None:
        """A properly percent-encoded backslash (%5C) is legitimate and passes."""
        assert_safe_url("https://docs.example.com/path%5Cwith-encoded-backslash")

    def test_malformed_bracketed_ipv6_authority_raises_validation_error(self) -> None:
        """urllib.parse itself raises ValueError for some malformed authorities.

        A non-IP literal inside brackets (e.g. userinfo-split around a
        bracketed non-address) makes urlparse raise ValueError rather than
        just returning an odd result. That must surface as ValidationError
        like every other refusal here, not escape as a raw ValueError.
        """
        with pytest.raises(ValidationError, match="could not be parsed"):
            assert_safe_url("http://[notanip]:80@evil.example.com/")


class TestIsBlockedIp:
    """_is_blocked_ip correctly classifies IP literals; non-IP hosts pass through."""

    def test_metadata_ip_blocked(self) -> None:
        assert _is_blocked_ip("169.254.169.254") is True

    def test_link_local_subnet_blocked(self) -> None:
        assert _is_blocked_ip("169.254.1.1") is True

    def test_public_ip_not_blocked(self) -> None:
        assert _is_blocked_ip("1.1.1.1") is False

    def test_rfc1918_not_blocked_by_default(self) -> None:
        assert _is_blocked_ip("10.0.0.1") is False

    def test_hostname_not_blocked(self) -> None:
        assert _is_blocked_ip("docs.example.com") is False

    def test_ipv6_link_local_blocked(self) -> None:
        assert _is_blocked_ip("fe80::1") is True

    def test_ipv6_public_not_blocked(self) -> None:
        assert _is_blocked_ip("2001:db8::1") is False


class TestIsBlockedAddress:
    """is_blocked_address checks a parsed address against the current policy."""

    def test_metadata_address_blocked(self) -> None:
        import ipaddress

        assert is_blocked_address(ipaddress.ip_address("169.254.169.254")) is True

    def test_rfc1918_address_not_blocked_by_default(self) -> None:
        import ipaddress

        assert is_blocked_address(ipaddress.ip_address("192.168.1.1")) is False

    def test_rfc1918_address_blocked_in_strict_mode(self) -> None:
        import ipaddress

        with patch.dict(os.environ, {"SUPACRAWL_BLOCK_PRIVATE_NETWORKS": "true"}):
            assert is_blocked_address(ipaddress.ip_address("192.168.1.1")) is True


class TestResolveAndPin:
    """resolve_and_pin closes the DNS-rebinding window (#152)."""

    @staticmethod
    def _stub_resolver(*addresses: str):
        """Patch getaddrinfo to answer with the given addresses."""
        infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 443)) for a in addresses]
        return patch("supacrawl.services.url_guard.socket.getaddrinfo", return_value=infos)

    def test_public_host_returns_its_pinned_address(self) -> None:
        with self._stub_resolver("93.184.216.34"):
            address, host = resolve_and_pin("https://docs.example.com/guide")

        assert address == "93.184.216.34"
        assert host == "docs.example.com"

    def test_hostname_resolving_to_metadata_is_refused(self) -> None:
        """The attack the IP-literal check cannot see: a name pointing at IMDS."""
        with self._stub_resolver("169.254.169.254"), pytest.raises(ValidationError, match=r"169\.254\.169\.254"):
            resolve_and_pin("http://totally-innocent.example.com/latest/meta-data/")

    def test_hostname_resolving_to_ipv6_link_local_is_refused(self) -> None:
        infos = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fe80::1", 443, 0, 0))]
        with (
            patch("supacrawl.services.url_guard.socket.getaddrinfo", return_value=infos),
            pytest.raises(ValidationError, match="link-local"),
        ):
            resolve_and_pin("http://evil.example.com/")

    def test_any_blocked_address_refuses_the_whole_fetch(self) -> None:
        """A name answering with both a public and an internal address is refused.

        Picking the address that passes would be exactly the rebinding attack:
        the resolver is free to hand the connection the other one.
        """
        with (
            self._stub_resolver("93.184.216.34", "169.254.169.254"),
            pytest.raises(ValidationError, match=r"169\.254\.169\.254"),
        ):
            resolve_and_pin("https://rebind.example.com/")

    def test_unresolvable_host_is_refused(self) -> None:
        with (
            patch(
                "supacrawl.services.url_guard.socket.getaddrinfo",
                side_effect=socket.gaierror("nodename nor servname"),
            ),
            pytest.raises(ValidationError, match="Cannot resolve"),
        ):
            resolve_and_pin("https://nx.example.com/")

    def test_private_address_allowed_by_default(self) -> None:
        """Internal documentation sites stay crawlable: RFC1918 is not blocked by default."""
        with self._stub_resolver("192.168.1.10"):
            address, _host = resolve_and_pin("http://docs.internal.corp/")

        assert address == "192.168.1.10"

    def test_private_address_refused_in_strict_mode(self) -> None:
        with (
            self._stub_resolver("192.168.1.10"),
            patch.dict(os.environ, {"SUPACRAWL_BLOCK_PRIVATE_NETWORKS": "1"}),
            pytest.raises(ValidationError, match="private / loopback"),
        ):
            resolve_and_pin("http://docs.internal.corp/")

    def test_loopback_refused_in_strict_mode(self) -> None:
        with (
            self._stub_resolver("127.0.0.1"),
            patch.dict(os.environ, {"SUPACRAWL_BLOCK_PRIVATE_NETWORKS": "1"}),
            pytest.raises(ValidationError, match="private / loopback"),
        ):
            resolve_and_pin("http://localhost:8080/")

    def test_metadata_still_refused_in_strict_mode(self) -> None:
        """Strict mode adds ranges; it never drops the always-blocked ones."""
        with (
            self._stub_resolver("169.254.169.254"),
            patch.dict(os.environ, {"SUPACRAWL_BLOCK_PRIVATE_NETWORKS": "1"}),
            pytest.raises(ValidationError, match="link-local"),
        ):
            resolve_and_pin("http://evil.example.com/")

    def test_bad_scheme_refused_before_any_resolution(self) -> None:
        with (
            patch("supacrawl.services.url_guard.socket.getaddrinfo") as mock_resolve,
            pytest.raises(ValidationError, match="scheme"),
        ):
            resolve_and_pin("file:///etc/passwd")

        mock_resolve.assert_not_called()

    def test_no_usable_address_is_refused(self) -> None:
        """getaddrinfo answering with no parseable address is refused, not silently passed."""
        infos = [(socket.AF_UNIX, socket.SOCK_STREAM, 0, "", "/tmp/weird.sock")]
        with (
            patch("supacrawl.services.url_guard.socket.getaddrinfo", return_value=infos),
            pytest.raises(ValidationError, match="no usable address"),
        ):
            resolve_and_pin("https://odd.example.com/")


class TestPinnedUrl:
    """pinned_url rewrites the authority without disturbing the rest."""

    def test_replaces_host_keeping_path_and_query(self) -> None:
        assert pinned_url("https://example.com/a/b?x=1", "93.184.216.34") == "https://93.184.216.34/a/b?x=1"

    def test_preserves_explicit_port(self) -> None:
        assert pinned_url("http://example.com:8080/docs", "10.0.0.5") == "http://10.0.0.5:8080/docs"

    def test_brackets_ipv6_literal(self) -> None:
        assert pinned_url("https://example.com/x", "2606:2800:220:1::1") == "https://[2606:2800:220:1::1]/x"


class TestAuthority:
    """_authority builds the Host header: hostname, port preserved, IPv6 bracketed."""

    def test_plain_hostname_no_port(self) -> None:
        assert _authority("example.com", None) == "example.com"

    def test_hostname_with_explicit_port(self) -> None:
        assert _authority("example.com", 8443) == "example.com:8443"

    def test_ipv6_hostname_is_bracketed(self) -> None:
        assert _authority("2606:2800:220:1::1", None) == "[2606:2800:220:1::1]"

    def test_ipv6_hostname_with_port_is_bracketed(self) -> None:
        assert _authority("2606:2800:220:1::1", 8443) == "[2606:2800:220:1::1]:8443"


class TestGuardedRequestRedirectChain:
    """guarded_request/guarded_stream guard and pin every redirect hop (#152)."""

    @staticmethod
    def _client(responses):
        """Build a fake httpx.AsyncClient whose .send() replays *responses* in order.

        Each entry is (status_code, headers dict).
        """
        calls: list[dict[str, object]] = []

        async def mock_send(request, *, stream, follow_redirects):
            status, headers = responses[len(calls)]
            calls.append(
                {
                    "url": str(request.url),
                    "headers": dict(request.headers),
                    "extensions": request.extensions,
                }
            )
            resp = MagicMock()
            resp.is_redirect = 300 <= status < 400
            resp.headers = headers
            resp.status_code = status
            resp.aclose = _async_noop
            resp.aread = _async_noop
            return resp

        class FakeClient:
            """Fake httpx.AsyncClient exposing only build_request/send."""

            def build_request(self, method, url, *, headers=None, extensions=None):
                import httpx

                return httpx.Request(method, url, headers=headers, extensions=extensions)

            send = staticmethod(mock_send)

        return FakeClient(), calls

    async def test_connects_to_the_pinned_address_with_the_real_host(self) -> None:
        client, calls = self._client([(200, {})])

        with patch(
            "supacrawl.services.url_guard.resolve_and_pin",
            return_value=("93.184.216.34", "example.com"),
        ):
            response = await guarded_request(client, "GET", "https://example.com/report.pdf")

        assert response.status_code == 200
        assert calls[0]["url"] == "https://93.184.216.34/report.pdf"
        assert calls[0]["headers"]["host"] == "example.com"
        assert calls[0]["extensions"]["sni_hostname"] == "example.com"

    async def test_redirect_to_an_internal_address_is_refused(self) -> None:
        """A 30x pivot to the metadata endpoint is refused like a direct request."""
        client, _calls = self._client([(302, {"location": "http://169.254.169.254/latest/meta-data/"})])

        def fake_resolve(url, strict=None):
            if "169.254.169.254" in url:
                raise ValidationError("blocked range (link-local / cloud-metadata)", field="url", value=url)
            return ("93.184.216.34", "example.com")

        with (
            patch("supacrawl.services.url_guard.resolve_and_pin", side_effect=fake_resolve),
            pytest.raises(ValidationError, match="link-local"),
        ):
            await guarded_request(client, "GET", "https://example.com/report.pdf")

    async def test_each_hop_is_revalidated(self) -> None:
        """Every hop is resolved and validated, not just the first."""
        client, calls = self._client(
            [
                (302, {"location": "https://cdn.example.com/real.pdf"}),
                (200, {}),
            ]
        )
        seen = []

        def fake_resolve(url, strict=None):
            seen.append(url)
            return ("93.184.216.34", "cdn.example.com" if "cdn" in url else "example.com")

        with patch("supacrawl.services.url_guard.resolve_and_pin", side_effect=fake_resolve):
            response = await guarded_request(client, "GET", "https://example.com/report.pdf")

        assert response.status_code == 200
        assert seen == ["https://example.com/report.pdf", "https://cdn.example.com/real.pdf"]
        assert len(calls) == 2

    async def test_too_many_redirects_is_refused(self) -> None:
        """A redirect chain deeper than max_redirects is refused, not followed forever."""
        client, _calls = self._client([(302, {"location": f"https://example.com/n{i}"}) for i in range(3)])

        with (
            patch(
                "supacrawl.services.url_guard.resolve_and_pin",
                return_value=("93.184.216.34", "example.com"),
            ),
            pytest.raises(ValidationError, match="Too many redirects"),
        ):
            await guarded_request(client, "GET", "https://example.com/start", max_redirects=2)

    async def test_redirect_with_no_location_header_is_refused(self) -> None:
        client, _calls = self._client([(302, {})])

        with (
            patch(
                "supacrawl.services.url_guard.resolve_and_pin",
                return_value=("93.184.216.34", "example.com"),
            ),
            pytest.raises(ValidationError, match="no Location header"),
        ):
            await guarded_request(client, "GET", "https://example.com/report.pdf")

    async def test_relative_location_resolves_against_the_logical_url(self) -> None:
        """A relative Location keeps the real hostname, not the pinned-IP request URL.

        Resolving against the pinned-IP URL would silently drop the vhost /
        certificate identity, so the guard joins against the logical URL.
        """
        client, calls = self._client([(302, {"location": "/moved/here"}), (200, {})])
        seen = []

        def fake_resolve(url, strict=None):
            seen.append(url)
            return ("93.184.216.34", "example.com")

        with patch("supacrawl.services.url_guard.resolve_and_pin", side_effect=fake_resolve):
            response = await guarded_request(client, "GET", "https://example.com/docs/a")

        assert response.status_code == 200
        assert seen == ["https://example.com/docs/a", "https://example.com/moved/here"]
        assert calls[1]["url"] == "https://93.184.216.34/moved/here"

    async def test_host_header_preserves_a_non_default_port(self) -> None:
        """Host must carry a non-default port (RFC 7230 §5.4); SNI stays hostname-only."""
        client, calls = self._client([(200, {})])

        with patch(
            "supacrawl.services.url_guard.resolve_and_pin",
            return_value=("93.184.216.34", "example.com"),
        ):
            await guarded_request(client, "GET", "https://example.com:8443/x")

        assert calls[0]["url"] == "https://93.184.216.34:8443/x"
        assert calls[0]["headers"]["host"] == "example.com:8443"
        assert calls[0]["extensions"]["sni_hostname"] == "example.com"

    async def test_malformed_redirect_location_raises_validation_error_not_value_error(self) -> None:
        """A hostile Location header can make urljoin itself raise ValueError.

        Python 3.14 hardened urllib.parse to validate a bracketed IPv6
        authority and raise ValueError for a malformed one (e.g. a non-IP
        literal inside the brackets). That must surface as the same
        ValidationError every other hop refusal does, not escape as a raw
        ValueError past a caller that only catches ValidationError.
        """
        client, _calls = self._client(
            [(302, {"location": "http://[notanip]:80@evil.example.com/"})],
        )

        with (
            patch(
                "supacrawl.services.url_guard.resolve_and_pin",
                return_value=("93.184.216.34", "example.com"),
            ),
            pytest.raises(ValidationError, match="could not be parsed"),
        ):
            await guarded_request(client, "GET", "https://example.com/report.pdf")


class TestEmbeddedIPv4InIPv6:
    """IPv4-mapped/6to4/NAT64 IPv6 forms are classified by their embedded IPv4 (#152).

    The bypass this closes: an IPv6 literal is never ``in`` an IPv4 network, so
    ``::ffff:169.254.169.254`` slipped past every IPv4 blocklist entry while
    still routing to the metadata endpoint on a dual-stack host.
    """

    @pytest.mark.parametrize(
        "literal",
        [
            "::ffff:169.254.169.254",  # IPv4-mapped metadata
            "::ffff:a9fe:a9fe",  # same, hex-compressed
            "2002:a9fe:a9fe::",  # 6to4 wrapping the metadata IPv4
            "64:ff9b::a9fe:a9fe",  # NAT64 well-known prefix wrapping it
            "::169.254.169.254",  # deprecated IPv4-compatible form
        ],
    )
    def test_embedded_metadata_blocked_by_default(self, literal: str) -> None:
        import ipaddress

        assert is_blocked_address(ipaddress.ip_address(literal)) is True

    def test_ipv4_mapped_metadata_url_refused_offline(self) -> None:
        with pytest.raises(ValidationError, match="blocked"):
            assert_safe_url("http://[::ffff:169.254.169.254]/latest/meta-data/")

    def test_ipv4_compat_metadata_url_refused_offline(self) -> None:
        with pytest.raises(ValidationError, match="blocked"):
            assert_safe_url("http://[::169.254.169.254]/latest/meta-data/")

    def test_ipv6_loopback_not_misread_as_ipv4_compat(self) -> None:
        """``::1`` stays IPv6 loopback (strict-only), not IPv4-compatible 0.0.0.1."""
        import ipaddress

        assert is_blocked_address(ipaddress.ip_address("::1")) is False
        with patch.dict(os.environ, {"SUPACRAWL_BLOCK_PRIVATE_NETWORKS": "1"}):
            assert is_blocked_address(ipaddress.ip_address("::1")) is True

    def test_public_ipv4_mapped_still_allowed(self) -> None:
        """A mapped/6to4 address embedding a PUBLIC IPv4 is legitimate, not blocked."""
        assert_safe_url("http://[::ffff:8.8.8.8]/dns-query")

    def test_ipv4_mapped_loopback_is_default_allow_strict_block(self) -> None:
        import ipaddress

        addr = ipaddress.ip_address("::ffff:127.0.0.1")
        assert is_blocked_address(addr) is False
        with patch.dict(os.environ, {"SUPACRAWL_BLOCK_PRIVATE_NETWORKS": "1"}):
            assert is_blocked_address(addr) is True

    def test_resolver_answering_with_mapped_metadata_is_refused(self) -> None:
        """The rebinding form: an AAAA answer of IPv4-mapped metadata is refused at resolve."""
        infos = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::ffff:169.254.169.254", 443, 0, 0))]
        with (
            patch("supacrawl.services.url_guard.socket.getaddrinfo", return_value=infos),
            pytest.raises(ValidationError, match="blocked"),
        ):
            resolve_and_pin("http://sneaky.example.com/")

    @pytest.mark.parametrize(
        "literal",
        [
            "::ffff:169.254.169.254",  # IPv4-mapped metadata
            "::ffff:a9fe:a9fe",  # same, hex-compressed
            "2002:a9fe:a9fe::",  # 6to4 wrapping the metadata IPv4
            "64:ff9b::a9fe:a9fe",  # NAT64 well-known prefix wrapping it
            "::169.254.169.254",  # deprecated IPv4-compatible form
        ],
    )
    def test_every_embedding_form_refused_offline_as_a_url_literal(self, literal: str) -> None:
        """assert_safe_url refuses every embedding form as a bracketed URL literal.

        Each of these is a distinct spelling of the same underlying bypass; a
        guard that only catches the mapped/compat forms and misses 6to4/NAT64
        is fixing a vector, not a class.
        """
        with pytest.raises(ValidationError, match="blocked"):
            assert_safe_url(f"http://[{literal}]/latest/meta-data/")

    @pytest.mark.parametrize(
        "literal",
        [
            "::ffff:169.254.169.254",
            "::ffff:a9fe:a9fe",
            "2002:a9fe:a9fe::",
            "64:ff9b::a9fe:a9fe",
            "::169.254.169.254",
        ],
    )
    def test_every_embedding_form_refused_when_a_hostname_resolves_to_it(self, literal: str) -> None:
        """resolve_and_pin refuses every embedding form reached via DNS, not just as a literal.

        This is the shape a real attacker uses: an innocuous-looking hostname
        whose AAAA record answers with one of these forms, so the cheap
        offline assert_safe_url check (which never resolves anything) cannot
        see it at all — only the resolve-time check can.
        """
        infos = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (literal, 443, 0, 0))]
        with (
            patch("supacrawl.services.url_guard.socket.getaddrinfo", return_value=infos),
            pytest.raises(ValidationError, match="blocked"),
        ):
            resolve_and_pin("http://totally-innocent.example.com/")


class TestIdnaUnicodeHostRejected:
    """A non-ASCII host is refused rather than guessed at (#152).

    urllib.parse does no IDNA/UTS-46 normalisation of its own; a WHATWG
    browser engine does, through its own table. The two are not guaranteed to
    agree, so a Unicode hostname is the same "pre-flight validates one host,
    the engine connects to another" shape as the backslash bypass — just via
    normalisation divergence instead of authority-parsing divergence. Refusing
    it outright (rather than IDNA-encoding it ourselves and hoping that
    matches whatever engine eventually resolves it) closes the class instead
    of one browser's current table.
    """

    def test_unicode_host_rejected_offline(self) -> None:
        with pytest.raises(ValidationError, match="non-ASCII"):
            assert_safe_url("http://例え.テスト/path")

    def test_unicode_host_rejected_before_any_resolution(self) -> None:
        with (
            patch("supacrawl.services.url_guard.socket.getaddrinfo") as mock_resolve,
            pytest.raises(ValidationError, match="non-ASCII"),
        ):
            resolve_and_pin("http://例え.テスト/path")
        mock_resolve.assert_not_called()

    def test_fullwidth_digit_homoglyph_of_metadata_ip_rejected(self) -> None:
        """A fullwidth-digit spelling of the metadata address is refused as non-ASCII.

        ``ipaddress.ip_address`` never parses this as an IP literal (it wants
        ASCII digits), so without this check it would fall through as an
        ordinary hostname and reach DNS resolution.
        """
        with pytest.raises(ValidationError, match="non-ASCII"):
            assert_safe_url("http://１６９.２５４.１６９.２５４/")

    def test_punycode_form_is_unaffected(self) -> None:
        """The unambiguous ASCII/punycode encoding of an IDN still passes through."""
        assert_safe_url("http://xn--r8jz45g.xn--zckzah/path")

    def test_ascii_host_unaffected(self) -> None:
        assert_safe_url("https://docs.example.com/guide")


class TestStrictModeRanges:
    """Every declared strict-mode range is actually enforced (guards against a typo'd CIDR)."""

    @pytest.mark.parametrize(
        "ip",
        ["0.0.0.0", "0.1.2.3", "100.64.0.1", "127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.0.1"],
    )
    def test_ipv4_ranges_refused_in_strict(self, ip: str) -> None:
        import ipaddress

        with patch.dict(os.environ, {"SUPACRAWL_BLOCK_PRIVATE_NETWORKS": "1"}):
            assert is_blocked_address(ipaddress.ip_address(ip)) is True

    @pytest.mark.parametrize("ip", ["::1", "::", "fc00::1", "fd12:3456::1"])
    def test_ipv6_ranges_refused_in_strict(self, ip: str) -> None:
        import ipaddress

        with patch.dict(os.environ, {"SUPACRAWL_BLOCK_PRIVATE_NETWORKS": "1"}):
            assert is_blocked_address(ipaddress.ip_address(ip)) is True

    @pytest.mark.parametrize("ip", ["0.0.0.0", "100.64.0.1", "::1", "::", "fc00::1"])
    def test_ranges_allowed_by_default(self, ip: str) -> None:
        import ipaddress

        assert is_blocked_address(ipaddress.ip_address(ip)) is False

    def test_ipv6_unspecified_refused_in_strict(self) -> None:
        """``::`` (unspecified) is the IPv6 analogue of ``0.0.0.0``: a connect()

        to it is routed by the OS to loopback, so leaving it unblocked while
        ``0.0.0.0/8`` is blocked would defeat strict mode for exactly the
        deployment shape strict mode exists to protect.
        """
        with (
            patch.dict(os.environ, {"SUPACRAWL_BLOCK_PRIVATE_NETWORKS": "1"}),
            pytest.raises(ValidationError, match="private / loopback"),
        ):
            resolve_and_pin("http://[::]/")

    def test_ipv6_unspecified_reachable_by_default(self) -> None:
        assert_safe_url("http://[::]/")


class TestGuardedRequestRealSocket:
    """End-to-end proof against a loopback listener: the CONNECT lands on the pinned address.

    The mocked-client tests prove request construction; these drive a real
    ``httpx.AsyncClient`` so the socket layer — where the IPv4-mapped bypass
    slipped through untested — is actually exercised (#152's 'local listener').
    """

    @staticmethod
    def _listener():
        import http.server
        import threading

        received: list[str] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                received.append(self.headers.get("Host", ""))
                body = b"OK-REAL-LISTENER"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, port, received

    async def test_connects_to_pinned_loopback_with_real_host(self) -> None:
        server, port, received = self._listener()
        try:
            infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
            with patch("supacrawl.services.url_guard.socket.getaddrinfo", return_value=infos):
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await guarded_request(client, "GET", f"http://docs.internal.test:{port}/x")

            assert resp.status_code == 200
            assert resp.text == "OK-REAL-LISTENER"
            # The real connect used the pinned 127.0.0.1, and the Host header
            # carried the true hostname AND port (not the pinned IP).
            assert received == [f"docs.internal.test:{port}"]
        finally:
            server.shutdown()

    async def test_ipv4_mapped_loopback_refused_before_any_socket_in_strict_mode(self) -> None:
        """The reviewer's confirmed bypass: ::ffff:127.0.0.1 must never reach the listener."""
        server, port, received = self._listener()
        try:
            with patch.dict(os.environ, {"SUPACRAWL_BLOCK_PRIVATE_NETWORKS": "1"}):
                async with httpx.AsyncClient(timeout=5.0) as client:
                    with pytest.raises(ValidationError, match="blocked"):
                        await guarded_request(client, "GET", f"http://[::ffff:127.0.0.1]:{port}/x")

            assert received == []
        finally:
            server.shutdown()


async def _async_noop(*args, **kwargs) -> None:
    return None
