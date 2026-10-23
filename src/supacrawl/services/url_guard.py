"""Outbound URL safety guard (SSRF hardening, #152).

Supacrawl fetches whatever URL a caller — or a page it has already fetched —
hands it: scrape/crawl/map targets, PDF downloads, sitemap and robots.txt
lookups, search-result metadata probes. None of that previously checked
whether the target resolves to an internal address, so a hostname pointing at
cloud instance metadata (``169.254.169.254``) or a link-local address was
fetched without complaint, and a redirect could pivot to one mid-request.

Two failure modes are closed here:

1. **No resolved-address check at all.** A URL whose hostname resolves to a
   blocked range is refused. Checking IP literals in the URL is not enough —
   owning a DNS record that resolves to the metadata endpoint defeats that.
2. **No pinning, so a time-of-check/time-of-use gap.** Even with a check, a
   hostname validated as public could rebind to an internal address before
   the connection is made, unless the validated address is what gets
   connected to. :func:`resolve_and_pin` closes that window for the httpx
   fast path; :func:`guarded_request` / :func:`guarded_stream` are the call
   sites that actually connect to the pinned address.

Redirects get the same treatment per hop via :func:`guarded_request` /
:func:`guarded_stream`: a 3xx pivot to an internal address is the same attack
wearing a hat.

**Residual exposure — the browser engines (Playwright/Patchright/Camoufox).**
Those engines own their own network stack; there is no supported API to hand
Chromium or Firefox a pre-resolved literal address while it still presents
the original hostname for TLS SNI and virtual hosting the way httpx's
``extensions={"sni_hostname": ...}`` does. The browser call sites
(``BrowserManager.fetch_page`` / ``extract_links``,
``ScrapeService._scrape_with_captcha_solving``,
``ReferenceRenderer``) call :func:`resolve_and_pin` as a pre-flight —
refusing a URL that resolves to a blocked address before navigation starts —
and ``BrowserManager`` additionally installs a Playwright route handler
(``_install_navigation_guard``) that re-validates every request the page
makes, redirect hops and subresource loads included, aborting any that
resolve into a blocked range. What remains open is the pre-flight's own
time-of-check/time-of-use window: the engine re-resolves each hostname
itself when it actually connects, so a DNS answer that changes between a
given hop's route-handler check and the engine's connect for *that hop* is
not pinned. Closing that fully would mean routing all browser traffic
through a local proxy that itself pins addresses, which is out of scope
here. Documented rather than silently left open, per the household's "an
honest 'this path cannot be pinned, here is why' is a legitimate outcome"
standard.

**Non-ASCII hosts are refused, not IDNA-encoded.** ``urllib.parse`` performs
no IDNA/UTS-46 normalisation of its own, while a WHATWG browser engine
normalises through its own table before resolving — the same "pre-flight
checks one host, the engine connects to another" shape as the backslash /
control-character bypass this guard already refuses, just reached through
Unicode normalisation divergence instead of authority parsing. Rather than
encode a Unicode host ourselves and hope that matches whatever a given
engine's IDNA table does, :func:`assert_safe_url` refuses it outright; the
unambiguous ASCII/punycode form (``xn--...``) is unaffected and every
conformant parser reads it identically.

RFC 1918 private ranges and loopback are **not** blocked by default:
operators legitimately point supacrawl at internal documentation sites,
which is a first-class use case for a self-hosted tool. A consumer that
accepts untrusted URLs (a multi-user app letting users add their own
sources) needs the stricter posture and can set
``SUPACRAWL_BLOCK_PRIVATE_NETWORKS=1`` to refuse internal targets outright.
This mirrors ragify's ``RAGIFY_BLOCK_PRIVATE_NETWORKS`` switch (see
radar-hooves/ragify's ``url_guard.py``) so the two libraries agree on the same
policy shape. The guard is a policy layer, not a network firewall; operators
who need guaranteed SSRF containment should also use network egress
controls.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urljoin, urlparse, urlunparse

import httpx

from supacrawl.exceptions import ValidationError

type IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
type IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

# Cloud metadata / link-local ranges that must never be fetch targets. Nobody
# legitimately scrapes an instance metadata endpoint.
_BLOCKED_NETWORKS: tuple[IPNetwork, ...] = (
    ipaddress.IPv4Network("169.254.0.0/16"),  # Link-local / AWS/GCP/Azure IMDS
    ipaddress.IPv6Network("fe80::/10"),  # IPv6 link-local
)

# Additionally blocked when SUPACRAWL_BLOCK_PRIVATE_NETWORKS is set: everything
# an untrusted URL could use to reach the host or its network.
_PRIVATE_NETWORKS: tuple[IPNetwork, ...] = (
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv4Network("127.0.0.0/8"),  # Loopback
    ipaddress.IPv4Network("0.0.0.0/8"),  # "This host on this network"
    ipaddress.IPv4Network("100.64.0.0/10"),  # Carrier-grade NAT
    ipaddress.IPv6Network("::1/128"),  # IPv6 loopback
    ipaddress.IPv6Network("::/128"),  # IPv6 unspecified — a connect() to it is routed to loopback
    ipaddress.IPv6Network("fc00::/7"),  # IPv6 unique-local
)

# The well-known NAT64 prefix (RFC 6052): a 64:ff9b::/96 address embeds a
# routable IPv4 destination in its low 32 bits. IPv4-mapped and 6to4 forms have
# stdlib accessors (``.ipv4_mapped`` / ``.sixtofour``); NAT64 and the deprecated
# IPv4-compatible form (``::a.b.c.d``, RFC 4291) do not, so they are matched by
# prefix and the embedded IPv4 pulled out by hand.
_NAT64_PREFIX = ipaddress.IPv6Network("64:ff9b::/96")
_IPV4_COMPAT_PREFIX = ipaddress.IPv6Network("::/96")
# ``::`` (unspecified) and ``::1`` (loopback) sit inside ::/96 but are genuine
# IPv6 special addresses, not IPv4-compatible destinations — do not treat their
# low 32 bits as an embedded IPv4.
_IPV6_SPECIAL = frozenset({ipaddress.IPv6Address("::"), ipaddress.IPv6Address("::1")})

_ALLOWED_SCHEMES = {"http", "https"}

_STRICT_ENV = "SUPACRAWL_BLOCK_PRIVATE_NETWORKS"
_SCRIPTS_ENV = "SUPACRAWL_BLOCK_PAGE_SCRIPTS"

# Redirect hops followed by guarded_request/guarded_stream before giving up.
MAX_REDIRECTS = 10


def _strict_mode() -> bool:
    """Whether private and loopback ranges are blocked as well.

    Read at call time rather than import time so a consumer can set it during
    application startup without import-order mattering.

    Returns:
        True when ``SUPACRAWL_BLOCK_PRIVATE_NETWORKS`` is set to a truthy value.
    """
    return os.environ.get(_STRICT_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def strict_mode() -> bool:
    """Public name for :func:`_strict_mode`, for callers outside this module."""
    return _strict_mode()


def block_page_scripts() -> bool:
    """Whether browser pages run without JavaScript (``SUPACRAWL_BLOCK_PAGE_SCRIPTS``).

    The untrusted-caller posture: with scripts off, a fetched page cannot
    originate requests of its own, so it cannot use DNS rebinding to read an
    internal origin it was served from.
    """
    return os.environ.get(_SCRIPTS_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _blocked_networks() -> tuple[IPNetwork, ...]:
    """The networks refused under the current policy."""
    if _strict_mode():
        return _BLOCKED_NETWORKS + _PRIVATE_NETWORKS
    return _BLOCKED_NETWORKS


def _candidate_addresses(addr: IPAddress) -> list[IPAddress]:
    """The address plus any IPv4 destination it embeds and would route to.

    An IPv6 literal can carry a blocked IPv4 target that a dual-stack host
    connects to directly: IPv4-mapped (``::ffff:169.254.169.254``), 6to4
    (``2002::/16``), NAT64 (``64:ff9b::/96``), or the deprecated IPv4-compatible
    form (``::169.254.169.254``). Classifying only the outer IPv6 form misses the
    embedded target — an IPv6 address is never ``in`` an IPv4 network, so
    ``::ffff:169.254.169.254`` slips past every IPv4 blocklist entry (SSRF,
    #152). Checking the embedded IPv4 as well closes every destination-embedding
    form.

    Teredo (``2001::/32``) is deliberately not covered: its embedded IPv4 is the
    Teredo *server*, not the destination, so it is not a destination-embedding
    form, and no supported OS (NixOS / macOS / Linux) enables the automatic
    tunnelling that would make such a literal route to the embedded IPv4 anyway.
    It is an accepted residual rather than a silent gap.

    Args:
        addr: A parsed IP address.

    Returns:
        ``[addr]`` for a plain address; ``[addr, embedded_ipv4]`` when *addr* is
        an IPv6 form that embeds an IPv4 destination.
    """
    candidates: list[IPAddress] = [addr]
    if isinstance(addr, ipaddress.IPv6Address):
        embedded = addr.ipv4_mapped
        if embedded is None:
            embedded = addr.sixtofour
        if embedded is None and addr in _NAT64_PREFIX:
            embedded = ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
        if embedded is None and addr in _IPV4_COMPAT_PREFIX and addr not in _IPV6_SPECIAL:
            embedded = ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
        if embedded is not None:
            candidates.append(embedded)
    return candidates


def _describe(addr: IPAddress) -> str:
    """A short reason an address is refused, for the error message."""
    if any(c in net for c in _candidate_addresses(addr) for net in _BLOCKED_NETWORKS):
        return "link-local / cloud-metadata"
    return "private / loopback"


def is_blocked_address(addr: IPAddress) -> bool:
    """Whether *addr* falls in a range refused under the current policy.

    An IPv4-mapped/6to4/NAT64 IPv6 form is classified by the IPv4 destination it
    embeds as well as by its own value, so a blocked IPv4 target cannot be
    smuggled past the guard wearing an IPv6 spelling (#152).

    Args:
        addr: A parsed IP address.

    Returns:
        True when the address must not be connected to.
    """
    return any(c in net for c in _candidate_addresses(addr) for net in _blocked_networks())


def _is_blocked_ip(host: str) -> bool:
    """Whether *host* is an IP literal in a blocked range.

    Non-IP hostnames return False; they are checked after resolution by
    :func:`resolve_and_pin`.

    Args:
        host: A hostname or IP literal.

    Returns:
        True when the host is a blocked IP literal.
    """
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return is_blocked_address(addr)


def assert_safe_url(url: str) -> None:
    """Raise when *url* is not safe for outbound fetching, without resolving it.

    The cheap, offline half of the guard: scheme and IP-literal checks only. It
    cannot catch a hostname that resolves to a blocked address; use
    :func:`resolve_and_pin` where the connection is actually made.

    Args:
        url: The URL to validate.

    Raises:
        ValidationError: When the URL contains a parser-divergence character,
            the scheme is not http(s), or the host is a blocked IP literal.
    """
    # Reject characters that RFC 3986 requires to be percent-encoded but that
    # WHATWG-URL browser engines strip or remap: a backslash (Chromium treats it
    # as "/" in the authority) and raw ASCII control characters (tab/newline/CR
    # are stripped before parsing). urllib.parse and a browser then disagree on
    # which host the URL names, so the browser pre-flight would validate one host
    # while the engine navigates to another — a deterministic SSRF bypass (#152,
    # e.g. http://169.254.169.254\@allowed-host/). A well-formed URL never
    # carries these raw; a legitimately-intended one survives percent-encoded.
    bad = next((c for c in url if c == "\\" or ord(c) < 0x20 or ord(c) == 0x7F), None)
    if bad is not None:
        raise ValidationError(
            f"URL contains character {bad!r}, which browser and RFC 3986 parsers interpret "
            "differently; percent-encode it. Raw backslashes and control characters are not allowed.",
            field="url",
            value=url,
        )

    # A malformed bracketed-IPv6 authority (e.g. an unbalanced or non-IP
    # literal inside `[...]`) makes urllib.parse itself raise ValueError
    # rather than just returning an odd result. That must fail as a
    # ValidationError like every other refusal here, not escape as a raw
    # ValueError — a caller catching only ValidationError (e.g. the browser
    # route handler re-validating a redirect Location) must not see this
    # leak past it uncaught.
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise ValidationError(f"URL {url!r} could not be parsed: {exc}", field="url", value=url) from exc

    scheme = parsed.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise ValidationError(
            f"URL scheme {scheme!r} is not allowed for outbound requests; only http and https are permitted.",
            field="url",
            value=url,
        )
    host = parsed.hostname or ""

    # Reject a non-ASCII host outright rather than IDNA-encoding it ourselves.
    # urllib.parse does no IDNA/UTS-46 normalisation — it hands back the raw
    # Unicode label — while a WHATWG browser engine normalises through its own
    # IDNA table before resolving. The two are not guaranteed to agree (case
    # folding, deprecated-codepoint mappings, and homograph-adjacent forms
    # differ between IDNA2003, UTS-46 and a given engine's table version), so a
    # Unicode host is exactly the same "the pre-flight validates one host, the
    # engine connects to another" shape as the backslash bypass above — just
    # via normalisation instead of parsing. A legitimate internationalised
    # domain is still reachable via its unambiguous ASCII/punycode form
    # (``xn--...``), which every conformant parser reads identically.
    if not host.isascii():
        raise ValidationError(
            f"URL host {host!r} contains non-ASCII characters; IDNA/Unicode host normalisation can "
            "diverge between this guard and a browser engine. Use the punycode (xn--) form instead.",
            field="url",
            value=url,
        )

    if _is_blocked_ip(host):
        raise ValidationError(
            f"URL host {host!r} is in a blocked IP range ({_describe(ipaddress.ip_address(host))}).",
            field="url",
            value=url,
        )


def _resolve(host: str, port: int) -> list[IPAddress]:
    """Resolve *host* to every address it currently answers with.

    Args:
        host: Hostname or IP literal.
        port: Port, which steers getaddrinfo's service resolution.

    Returns:
        The resolved addresses, in the order the resolver returned them.

    Raises:
        ValidationError: When the host cannot be resolved.
    """
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValidationError(f"Cannot resolve host {host!r}: {exc}", field="url", value=host) from exc

    addresses: list[IPAddress] = []
    for info in infos:
        sockaddr = info[4]
        try:
            addr = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        if addr not in addresses:
            addresses.append(addr)

    if not addresses:
        raise ValidationError(f"Host {host!r} resolved to no usable address", field="url", value=host)
    return addresses


def resolve_and_pin(url: str) -> tuple[str, str]:
    """Validate *url*, resolve it, and return an address safe to connect to.

    This is the half of the guard that closes the rebinding window. It resolves
    the hostname once and refuses the fetch if **any** returned address is
    blocked, rather than hunting for one that passes: a name answering with both
    a public and an internal address is a rebinding attack, not a fallback list.
    The caller connects to the returned address literally, so the DNS answer
    cannot change between this check and the connection.

    Args:
        url: The URL about to be fetched.

    Returns:
        Tuple of (pinned address, host) — the address to connect to, and the
        original hostname the caller must still send as ``Host`` and SNI so
        virtual hosting and certificate validation keep working.

    Raises:
        ValidationError: When the scheme is disallowed, the host cannot be
            resolved, or any resolved address is in a blocked range.
    """
    assert_safe_url(url)

    parsed = urlparse(url)
    host = parsed.hostname or ""
    if not host:
        raise ValidationError(f"URL has no host: {url!r}", field="url", value=url)

    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    addresses = _resolve(host, port)

    for addr in addresses:
        if is_blocked_address(addr):
            raise ValidationError(
                f"URL host {host!r} resolves to {addr}, which is in a blocked range ({_describe(addr)}).",
                field="url",
                value=url,
            )

    return str(addresses[0]), host


def pinned_url(url: str, address: str) -> str:
    """Rewrite *url* to connect to *address* while keeping its path and query.

    An IPv6 literal is bracketed so the authority stays parseable.

    Args:
        url: The original URL.
        address: The pinned IP address to connect to.

    Returns:
        The URL with its host replaced by the pinned address.
    """
    parsed = urlparse(url)
    literal = f"[{address}]" if ":" in address else address
    netloc = f"{literal}:{parsed.port}" if parsed.port else literal
    return urlunparse(parsed._replace(netloc=netloc))


def _authority(host: str, port: int | None) -> str:
    """The value for the ``Host`` header: the real hostname, port preserved.

    Connecting to a pinned IP literal means httpx will not derive ``Host`` from
    the request URL, so it is set by hand. RFC 7230 §5.4 requires the authority
    to carry a non-default port, and an IPv6 hostname literal to be bracketed.

    Args:
        host: The original hostname (unbracketed, no port).
        port: The explicit port from the URL, or ``None`` for the scheme default.

    Returns:
        The ``Host`` header value.
    """
    literal = f"[{host}]" if ":" in host else host
    return f"{literal}:{port}" if port is not None else literal


@asynccontextmanager
async def guarded_stream(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    max_redirects: int = MAX_REDIRECTS,
) -> AsyncIterator[httpx.Response]:
    """Open a streaming request for *url*, pinning every hop to a validated address.

    Each hop is resolved once, checked, and connected to by literal address, so a
    DNS record cannot rebind to an internal target between the check and the
    socket. Redirects are followed by hand, at most *max_redirects* deep, so
    every hop passes the same guard: a 3xx pivot to the metadata endpoint is
    refused like a direct request to it.

    Args:
        client: The client to issue requests on. Its own default headers
            (User-Agent, Accept, etc. set at construction) still apply; this
            only overrides the connection target and Host/SNI per hop. Must
            not itself be configured with ``follow_redirects=True`` — that
            would let a hop through unguarded.
        method: HTTP method, e.g. "GET" or "HEAD".
        url: The URL to fetch.
        max_redirects: Maximum redirect hops to follow before giving up.

    Yields:
        The final non-redirect response, still streaming (call ``.aread()`` if
        the buffered body is needed).

    Raises:
        ValidationError: When any hop fails validation or the chain is too deep.
    """
    current_url = url
    for _ in range(max_redirects + 1):
        # resolve_and_pin does a blocking socket.getaddrinfo; run it off the
        # event loop so a slow resolver does not stall other concurrent fetches
        # (httpx's own resolution is threaded for the same reason).
        address, host = await asyncio.to_thread(resolve_and_pin, current_url)
        request_url = pinned_url(current_url, address)

        # The connection goes to the pinned address, but the server still needs
        # the real hostname: Host (with any non-default port) for virtual
        # hosting, SNI so TLS presents and verifies the right certificate.
        request = client.build_request(
            method,
            request_url,
            headers={"Host": _authority(host, urlparse(current_url).port)},
            extensions={"sni_hostname": host},
        )
        response = await client.send(request, stream=True, follow_redirects=False)

        if not response.is_redirect:
            try:
                yield response
            finally:
                await response.aclose()
            return

        location = response.headers.get("location")
        await response.aclose()
        if not location:
            raise ValidationError(f"Redirect from {current_url!r} carried no Location header", field="url", value=url)
        # Resolve against the logical URL, not the pinned one: a relative
        # Location off a pinned-IP request URL would silently drop the
        # hostname and with it the vhost/certificate identity.
        #
        # A malformed bracketed-IPv6 authority in an attacker-controlled
        # Location header makes urljoin itself raise ValueError rather than
        # returning a rejectable string; that must surface as the same
        # ValidationError every other hop refusal does.
        try:
            current_url = urljoin(current_url, location)
        except ValueError as exc:
            raise ValidationError(
                f"Redirect Location {location!r} from {current_url!r} could not be parsed: {exc}",
                field="url",
                value=location,
            ) from exc

    raise ValidationError(f"Too many redirects (>{max_redirects}) starting from {url!r}", field="url", value=url)


async def guarded_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    max_redirects: int = MAX_REDIRECTS,
) -> httpx.Response:
    """Issue a fully-read request whose every hop is pinned (see :func:`guarded_stream`).

    A thin buffering wrapper for the common case where a caller wants
    ``.text``/``.content``/``.json()`` available on the returned response,
    exactly like ``client.get()``/``client.head()``, but with every hop
    validated and pinned instead of ``follow_redirects=True`` blindly trusting
    the chain.

    Args:
        client: The client to issue requests on (see :func:`guarded_stream`).
        method: HTTP method, e.g. "GET" or "HEAD".
        url: The URL to fetch.
        max_redirects: Maximum redirect hops to follow before giving up.

    Returns:
        The final, already-read response.

    Raises:
        ValidationError: When any hop fails validation or the chain is too deep.
    """
    async with guarded_stream(client, method, url, max_redirects=max_redirects) as response:
        await response.aread()
        return response


async def guarded_hop(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    content: bytes | None = None,
) -> httpx.Response:
    """Fetch exactly one hop of *url* at a validated, pinned address; never follow a redirect.

    The browser's route handler uses this in strict mode so Chromium never
    resolves or connects itself: the bytes come from the pinned connection and
    are handed to the page, which closes the browser path's rebinding window.

    Args:
        client: The client to issue the request on (must not follow redirects).
        method: HTTP method.
        url: The URL to fetch.
        headers: Request headers to forward; ``Host`` is set from *url*.
        content: Request body, if any.

    Returns:
        The response, fully read, redirects unfollowed.

    Raises:
        ValidationError: When the URL or its resolved address is blocked.
    """
    address, host = await asyncio.to_thread(resolve_and_pin, url)
    request = client.build_request(
        method,
        pinned_url(url, address),
        headers={**(headers or {}), "Host": _authority(host, urlparse(url).port)},
        content=content,
        extensions={"sni_hostname": host},
    )
    response = await client.send(request, follow_redirects=False)
    return response
