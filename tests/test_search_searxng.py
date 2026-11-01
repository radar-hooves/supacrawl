"""Unit tests for SearXNGProvider (#156).

SearXNGProvider had zero test coverage before #156: the multi-word-query
regression that prompted it could not have been caught by CI. These tests
exercise request building (query, params, domain/time filters) and response
mapping via a fake httpx client, mirroring the style used for the other
providers in test_search_providers.py / test_search_provider_filters.py.

The HTTP Basic auth tests deliberately drive a REAL ``httpx.AsyncClient`` over a
recording ``MockTransport`` rather than the fake client: the defect being
guarded against is a credential landing on the wrong request, which is only
observable on the actual outgoing headers.

Every credential in this file is an obvious placeholder. Nothing here resembles
a real value, and nothing in the provider is expected to print one.
"""

import base64
import logging
import os
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from supacrawl.exceptions import ProviderError
from supacrawl.models import SearchFilters, SearchSourceType
from supacrawl.services.registry import create_supacrawl_services
from supacrawl.services.search.duckduckgo import DuckDuckGoProvider
from supacrawl.services.search.registry import build_provider_chain
from supacrawl.services.search.searxng import SearXNGProvider, _parse_unresponsive_engines
from supacrawl.services.search.service import SearchService

CORRELATION_ID = "test-1234"

# Obvious placeholders — never a real credential (rules-library core/21-secret-handling.md).
PLACEHOLDER_USERNAME = "searxng-user-placeholder"
PLACEHOLDER_PASSWORD = "searxng-password-placeholder"
URL_USERINFO_USERNAME = "url-user-placeholder"
URL_USERINFO_PASSWORD = "url-password-placeholder"


class _FakeResponse:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data
        self.status_code = 200

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return self._data


class _FakeClient:
    """Captures the last outgoing request and returns a canned JSON body."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data
        self.last: dict[str, Any] = {}

    async def get(self, url: str, *, params: Any = None, auth: Any = None) -> _FakeResponse:
        self.last = {"url": url, "params": params, "auth": auth}
        return _FakeResponse(self._data)

    async def aclose(self) -> None:
        pass


def _recording_client(sink: list[httpx.Request], body: str = '{"results": []}') -> httpx.AsyncClient:
    """A real httpx client whose transport records every request it is given."""

    def handler(request: httpx.Request) -> httpx.Response:
        sink.append(request)
        return httpx.Response(200, text=body, headers={"content-type": "application/json"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


WEB_RESPONSE = {
    "results": [
        {"url": "https://example.com/1", "title": "Result One", "content": "First snippet."},
        {"url": "https://example.com/2", "title": "Result Two", "content": "Second snippet."},
    ]
}

IMAGE_RESPONSE = {
    "results": [
        {
            "url": "https://example.com/gallery/photo",
            "img_src": "https://example.com/photo-full.jpg",
            "title": "A Photo",
            "thumbnail_src": "https://example.com/thumb.jpg",
            "resolution": "800\u00d7600",
            "img_format": "JPG",
        }
    ]
}

NEWS_RESPONSE = {
    "results": [
        {
            "url": "https://news.example.com/story",
            "title": "Breaking Story",
            "content": "Story snippet.",
            "publishedDate": "2026-06-13",
            "engine": "bing news",
        }
    ]
}


class TestSearXNGProvider:
    """Request-building and response-mapping tests for SearXNGProvider."""

    @pytest.mark.asyncio
    async def test_search_web_field_mapping(self) -> None:
        provider = SearXNGProvider(url="http://searxng.invalid")
        try:
            fake = _FakeClient(WEB_RESPONSE)
            provider._http_client = fake  # type: ignore[assignment]

            results = await provider.search_web("prometheus alertmanager grouping", 5, CORRELATION_ID)

            assert len(results) == 2
            assert results[0].url == "https://example.com/1"
            assert results[0].title == "Result One"
            assert results[0].description == "First snippet."
            assert results[0].source_type == SearchSourceType.WEB
            assert fake.last["params"]["q"] == "prometheus alertmanager grouping"
            assert fake.last["params"]["categories"] == "general"
            assert fake.last["params"]["format"] == "json"
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_search_web_respects_limit(self) -> None:
        provider = SearXNGProvider(url="http://searxng.invalid")
        try:
            fake = _FakeClient(WEB_RESPONSE)
            provider._http_client = fake  # type: ignore[assignment]

            results = await provider.search_web("query", 1, CORRELATION_ID)

            assert len(results) == 1
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_search_web_missing_optional_fields(self) -> None:
        provider = SearXNGProvider(url="http://searxng.invalid")
        try:
            fake = _FakeClient({"results": [{"url": "https://x.com"}]})
            provider._http_client = fake  # type: ignore[assignment]

            results = await provider.search_web("q", 5, CORRELATION_ID)

            assert len(results) == 1
            assert results[0].title == ""
            assert results[0].description == ""
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_search_web_empty_results(self) -> None:
        """Regression guard for #156: an empty upstream result set must map to []."""
        provider = SearXNGProvider(url="http://searxng.invalid")
        try:
            fake = _FakeClient({"results": []})
            provider._http_client = fake  # type: ignore[assignment]

            results = await provider.search_web("prometheus alertmanager grouping", 5, CORRELATION_ID)

            assert results == []
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_search_images_field_mapping(self) -> None:
        provider = SearXNGProvider(url="http://searxng.invalid")
        try:
            fake = _FakeClient(IMAGE_RESPONSE)
            provider._http_client = fake  # type: ignore[assignment]

            results = await provider.search_images("photo", 5, CORRELATION_ID)

            assert len(results) == 1
            item = results[0]
            assert item.url == "https://example.com/gallery/photo"
            assert item.image_url == "https://example.com/photo-full.jpg"
            assert item.source_type == SearchSourceType.IMAGES
            assert item.thumbnail == "https://example.com/thumb.jpg"
            assert item.image_width == 800
            assert item.image_height == 600
            assert fake.last["params"]["categories"] == "images"
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_search_images_ascii_resolution_and_missing_resolution(self) -> None:
        provider = SearXNGProvider(url="http://searxng.invalid")
        try:
            body = {
                "results": [
                    {"img_src": "https://a.test/a.jpg", "url": "https://a.test/p", "resolution": "1920x1080"},
                    {"img_src": "https://b.test/b.jpg", "url": "https://b.test/p", "img_format": "PNG"},
                ]
            }
            provider._http_client = _FakeClient(body)  # type: ignore[assignment]

            first, second = await provider.search_images("q", 5, CORRELATION_ID)

            assert (first.image_width, first.image_height) == (1920, 1080)
            assert (second.image_width, second.image_height) == (None, None)
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_search_images_falls_back_to_img_src(self) -> None:
        provider = SearXNGProvider(url="http://searxng.invalid")
        try:
            body = {"results": [{"img_src": "https://example.com/only-img.jpg", "title": "X"}]}
            fake = _FakeClient(body)
            provider._http_client = fake  # type: ignore[assignment]

            results = await provider.search_images("q", 5, CORRELATION_ID)

            assert results[0].image_url == "https://example.com/only-img.jpg"
            assert results[0].thumbnail == "https://example.com/only-img.jpg"
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_search_news_field_mapping(self) -> None:
        provider = SearXNGProvider(url="http://searxng.invalid")
        try:
            fake = _FakeClient(NEWS_RESPONSE)
            provider._http_client = fake  # type: ignore[assignment]

            results = await provider.search_news("news query", 5, CORRELATION_ID)

            assert len(results) == 1
            item = results[0]
            assert item.url == "https://news.example.com/story"
            assert item.source_type == SearchSourceType.NEWS
            assert item.published_at == "2026-06-13"
            assert item.source_name == "bing news"
            assert fake.last["params"]["categories"] == "news"
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_domain_filters_rewrite_query(self) -> None:
        provider = SearXNGProvider(url="http://searxng.invalid")
        try:
            fake = _FakeClient({"results": []})
            provider._http_client = fake  # type: ignore[assignment]

            await provider.search_web(
                "ai",
                5,
                CORRELATION_ID,
                SearchFilters(include_domains=["a.com"], exclude_domains=["b.com"]),
            )

            assert fake.last["params"]["q"] == "ai site:a.com -site:b.com"
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_supported_time_range_is_forwarded(self) -> None:
        provider = SearXNGProvider(url="http://searxng.invalid")
        try:
            fake = _FakeClient({"results": []})
            provider._http_client = fake  # type: ignore[assignment]

            await provider.search_web("ai", 5, CORRELATION_ID, SearchFilters(time_range="month"))

            assert fake.last["params"]["time_range"] == "month"
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_unsupported_time_range_is_dropped(self) -> None:
        """SearXNG has no 'week' bucket; the filter must not be forwarded verbatim."""
        provider = SearXNGProvider(url="http://searxng.invalid")
        try:
            fake = _FakeClient({"results": []})
            provider._http_client = fake  # type: ignore[assignment]

            await provider.search_web("ai", 5, CORRELATION_ID, SearchFilters(time_range="week"))

            assert "time_range" not in fake.last["params"]
        finally:
            await provider.close()

    def test_is_available_false_without_url(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            provider = SearXNGProvider(url=None)
        assert not provider.is_available()

    def test_is_available_true_with_url(self) -> None:
        provider = SearXNGProvider(url="http://searxng.invalid")
        assert provider.is_available()

    def test_reads_url_from_env(self) -> None:
        with patch.dict(os.environ, {"SEARXNG_URL": "http://env-searxng.invalid"}):
            provider = SearXNGProvider()
        assert provider.is_available()

    def test_url_is_stripped_of_trailing_slash(self) -> None:
        provider = SearXNGProvider(url="http://searxng.invalid/")
        assert provider._url == "http://searxng.invalid"


EMPTY_WITH_FAILURES = {
    "results": [],
    "unresponsive_engines": [["brave", "timeout"], ["wikibooks", "SSL error"]],
}


class TestUpstreamEngineFailure:
    """An empty result set caused by dead engines must say so, not read as no-match (#161)."""

    @pytest.mark.asyncio
    async def test_empty_with_unresponsive_engines_raises_provider_error(self) -> None:
        provider = SearXNGProvider(url="http://searxng.invalid")
        try:
            provider._http_client = _FakeClient(EMPTY_WITH_FAILURES)  # type: ignore[assignment]

            with pytest.raises(ProviderError) as excinfo:
                await provider.search_web("prometheus alertmanager grouping", 5, CORRELATION_ID)

            message = str(excinfo.value)
            assert "brave" in message and "wikibooks" in message, f"the failure must name the dead engines: {message}"
            assert excinfo.value.context["unresponsive_engines"] == [
                {"engine": "brave", "reason": "timeout"},
                {"engine": "wikibooks", "reason": "SSL error"},
            ]
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_results_present_with_unresponsive_does_not_raise(self) -> None:
        """Partial degradation still returns what it got — only a *total* empty raises."""
        body = {"results": WEB_RESPONSE["results"], "unresponsive_engines": [["brave", "timeout"]]}
        provider = SearXNGProvider(url="http://searxng.invalid")
        try:
            provider._http_client = _FakeClient(body)  # type: ignore[assignment]

            results = await provider.search_web("q", 5, CORRELATION_ID)

            assert len(results) == 2
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_empty_without_unresponsive_returns_empty(self) -> None:
        """A genuine no-match must NOT be dressed up as an upstream failure."""
        provider = SearXNGProvider(url="http://searxng.invalid")
        try:
            provider._http_client = _FakeClient({"results": []})  # type: ignore[assignment]

            assert await provider.search_web("no such thing anywhere", 5, CORRELATION_ID) == []
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_news_empty_with_unresponsive_also_raises(self) -> None:
        provider = SearXNGProvider(url="http://searxng.invalid")
        try:
            provider._http_client = _FakeClient(EMPTY_WITH_FAILURES)  # type: ignore[assignment]

            with pytest.raises(ProviderError):
                await provider.search_news("q", 5, CORRELATION_ID)
        finally:
            await provider.close()

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ([["brave", "timeout"]], [{"engine": "brave", "reason": "timeout"}]),
            ([["brave"]], [{"engine": "brave", "reason": "unresponsive"}]),
            (["brave"], [{"engine": "brave", "reason": "unresponsive"}]),
            ([["brave", ""]], [{"engine": "brave", "reason": "unresponsive"}]),
            ([["a", "x"], ["b", "y"]], [{"engine": "a", "reason": "x"}, {"engine": "b", "reason": "y"}]),
            ([], []),
            (None, []),
            ("garbage", []),
            ([[""]], []),
        ],
    )
    def test_parse_unresponsive_engines_shapes(self, raw: Any, expected: list[dict[str, str]]) -> None:
        assert _parse_unresponsive_engines(raw) == expected


def _basic_credential(request: httpx.Request) -> tuple[str, str] | None:
    """Decode the Basic credential a request carried, or None when it carried none."""
    header = request.headers.get("authorization")
    if header is None:
        return None
    scheme, _, encoded = header.partition(" ")
    assert scheme == "Basic", f"expected Basic auth, got {scheme!r}"
    username, _, password = base64.b64decode(encoded).decode().partition(":")
    return username, password


class TestBasicAuth:
    """HTTP Basic auth for a gated instance.

    A self-hosted instance typically sits behind a vhost with ``satisfy any``:
    forward-auth for browsers, HTTP Basic for machines. supacrawl is the machine.
    """

    @pytest.mark.asyncio
    async def test_explicit_credentials_are_sent_on_the_request(self) -> None:
        requests: list[httpx.Request] = []
        client = _recording_client(requests)
        provider = SearXNGProvider(
            url="http://searxng.invalid",
            http_client=client,
            username=PLACEHOLDER_USERNAME,
            password=PLACEHOLDER_PASSWORD,
        )
        try:
            await provider.search_web("q", 5, CORRELATION_ID)
        finally:
            await client.aclose()

        assert _basic_credential(requests[0]) == (PLACEHOLDER_USERNAME, PLACEHOLDER_PASSWORD)

    @pytest.mark.asyncio
    async def test_shared_client_does_not_leak_the_credential_to_another_provider(self) -> None:
        """The regression that would hurt most.

        ``create_provider`` hands every provider the SAME ``httpx.AsyncClient``.
        Setting ``client.auth`` would send the household's SearXNG credential to
        Brave, Tavily, Serper and every other host that client talks to — a
        worse leak than the credential-in-URL shape this change removes. So the
        credential must ride on the individual request and nowhere else.
        """
        requests: list[httpx.Request] = []
        shared = _recording_client(requests)
        searxng = SearXNGProvider(
            url="http://searxng.invalid",
            http_client=shared,
            username=PLACEHOLDER_USERNAME,
            password=PLACEHOLDER_PASSWORD,
        )
        # A real co-tenant of the shared client, not a stub: DuckDuckGo is the
        # other provider that accepts the shared client today.
        duckduckgo = DuckDuckGoProvider(http_client=shared)
        try:
            await searxng.search_web("q", 5, CORRELATION_ID)
            await duckduckgo.search_web("q", 5, CORRELATION_ID)
        finally:
            await shared.aclose()

        searxng_request, duckduckgo_request = requests
        assert "searxng.invalid" in str(searxng_request.url)
        assert "duckduckgo.com" in str(duckduckgo_request.url)

        assert _basic_credential(searxng_request) == (PLACEHOLDER_USERNAME, PLACEHOLDER_PASSWORD)
        assert _basic_credential(duckduckgo_request) is None, (
            "SearXNG's credential leaked onto another provider's request via the shared client"
        )
        assert shared.auth is None, "the credential was mutated onto the shared client instead of the request"

    @pytest.mark.asyncio
    async def test_credentials_are_read_from_the_environment(self) -> None:
        with patch.dict(
            os.environ,
            {
                "SEARXNG_URL": "http://searxng.invalid",
                "SEARXNG_USERNAME": PLACEHOLDER_USERNAME,
                "SEARXNG_PASSWORD": PLACEHOLDER_PASSWORD,
            },
        ):
            provider = SearXNGProvider()

        requests: list[httpx.Request] = []
        client = _recording_client(requests)
        provider._http_client = client
        try:
            await provider.search_web("q", 5, CORRELATION_ID)
        finally:
            await client.aclose()

        assert _basic_credential(requests[0]) == (PLACEHOLDER_USERNAME, PLACEHOLDER_PASSWORD)

    @pytest.mark.asyncio
    async def test_explicit_credentials_beat_url_userinfo(self) -> None:
        """Both shapes configured: the discrete pair wins, and only it is sent."""
        with patch.dict(
            os.environ,
            {
                "SEARXNG_URL": f"http://{URL_USERINFO_USERNAME}:{URL_USERINFO_PASSWORD}@searxng.invalid",
                "SEARXNG_USERNAME": PLACEHOLDER_USERNAME,
                "SEARXNG_PASSWORD": PLACEHOLDER_PASSWORD,
            },
        ):
            provider = SearXNGProvider()

        requests: list[httpx.Request] = []
        client = _recording_client(requests)
        provider._http_client = client
        try:
            await provider.search_web("q", 5, CORRELATION_ID)
        finally:
            await client.aclose()

        assert _basic_credential(requests[0]) == (PLACEHOLDER_USERNAME, PLACEHOLDER_PASSWORD)

    @pytest.mark.asyncio
    async def test_url_userinfo_alone_still_authenticates(self) -> None:
        """The deprecated shape keeps working for an installation still on it."""
        requests: list[httpx.Request] = []
        client = _recording_client(requests)
        provider = SearXNGProvider(
            url=f"http://{URL_USERINFO_USERNAME}:{URL_USERINFO_PASSWORD}@searxng.invalid",
            http_client=client,
        )
        try:
            await provider.search_web("q", 5, CORRELATION_ID)
        finally:
            await client.aclose()

        assert _basic_credential(requests[0]) == (URL_USERINFO_USERNAME, URL_USERINFO_PASSWORD)

    def test_url_userinfo_is_stripped_from_the_stored_url(self) -> None:
        """The stored URL must be safe to log; the credential moves to the auth."""
        provider = SearXNGProvider(
            url=f"https://{URL_USERINFO_USERNAME}:{URL_USERINFO_PASSWORD}@searxng.invalid:8443/base/"
        )
        assert provider._url == "https://searxng.invalid:8443/base"
        assert URL_USERINFO_PASSWORD not in provider._url

    @pytest.mark.parametrize(
        ("host", "expected"),
        [
            ("[::1]:8080", "http://[::1]:8080"),
            ("[2001:db8::1]", "http://[2001:db8::1]"),
            ("searxng.invalid:8080", "http://searxng.invalid:8080"),
            ("192.0.2.10:8080", "http://192.0.2.10:8080"),
        ],
    )
    def test_stripping_userinfo_preserves_the_authority(self, host: str, expected: str) -> None:
        """Removing the credential must not also mangle the host.

        An IPv6 literal is the trap: ``urlsplit`` reports ``hostname`` with its
        brackets removed, so a naive rebuild yields ``http://::1:8080`` and every
        later request dies on an opaque port-parsing error rather than anything
        that points at the URL.
        """
        provider = SearXNGProvider(url=f"http://{URL_USERINFO_USERNAME}:{URL_USERINFO_PASSWORD}@{host}")

        assert provider._url == expected
        # The rebuilt URL must still be one httpx will accept.
        httpx.URL(f"{provider._url}/search")

    @pytest.mark.asyncio
    async def test_ipv6_instance_still_authenticates_from_url_userinfo(self) -> None:
        """The deprecated shape must keep working on an IPv6 host, not just a name."""
        requests: list[httpx.Request] = []
        client = _recording_client(requests)
        provider = SearXNGProvider(
            url=f"http://{URL_USERINFO_USERNAME}:{URL_USERINFO_PASSWORD}@[::1]:8080",
            http_client=client,
        )
        try:
            await provider.search_web("q", 5, CORRELATION_ID)
        finally:
            await client.aclose()

        assert _basic_credential(requests[0]) == (URL_USERINFO_USERNAME, URL_USERINFO_PASSWORD)
        assert requests[0].url.host == "::1"

    @pytest.mark.asyncio
    async def test_url_userinfo_never_reaches_the_request_url(self) -> None:
        requests: list[httpx.Request] = []
        client = _recording_client(requests)
        provider = SearXNGProvider(
            url=f"http://{URL_USERINFO_USERNAME}:{URL_USERINFO_PASSWORD}@searxng.invalid",
            http_client=client,
        )
        try:
            await provider.search_web("q", 5, CORRELATION_ID)
        finally:
            await client.aclose()

        assert URL_USERINFO_PASSWORD not in str(requests[0].url)

    @pytest.mark.asyncio
    async def test_no_credentials_configured_sends_no_authorization(self) -> None:
        """An ungated vhost is a supported configuration, not a misconfiguration."""
        requests: list[httpx.Request] = []
        client = _recording_client(requests)
        provider = SearXNGProvider(url="http://searxng.invalid", http_client=client)
        try:
            results = await provider.search_web("q", 5, CORRELATION_ID)
        finally:
            await client.aclose()

        assert results == []
        assert _basic_credential(requests[0]) is None

    @pytest.mark.asyncio
    async def test_half_configured_credential_warns_and_sends_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        """Half a credential is a misconfiguration that must be loud, not silent."""
        with caplog.at_level(logging.WARNING, logger="supacrawl.services.search.searxng"):
            provider = SearXNGProvider(url="http://searxng.invalid", username=PLACEHOLDER_USERNAME)

        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "a half-configured credential was dropped silently"
        assert "SEARXNG_PASSWORD" in " ".join(warnings)

        requests: list[httpx.Request] = []
        client = _recording_client(requests)
        provider._http_client = client
        try:
            await provider.search_web("q", 5, CORRELATION_ID)
        finally:
            await client.aclose()

        assert _basic_credential(requests[0]) is None

    @pytest.mark.asyncio
    async def test_half_configured_warning_names_the_url_credential_taking_over(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The warning must describe what actually happens, not what usually happens.

        With half a discrete pair AND a URL still carrying userinfo, the URL
        credential is what goes out. A warning saying "no credential will be
        sent" would send an operator debugging this exact config hunting the
        wrong problem.
        """
        with caplog.at_level(logging.WARNING, logger="supacrawl.services.search.searxng"):
            provider = SearXNGProvider(
                url=f"http://{URL_USERINFO_USERNAME}:{URL_USERINFO_PASSWORD}@searxng.invalid",
                username=PLACEHOLDER_USERNAME,
            )

        joined = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
        assert "SEARXNG_PASSWORD" in joined
        assert "SEARXNG_URL" in joined, f"the warning does not say the URL credential took over: {joined}"
        assert "no credential will be sent" not in joined

        requests: list[httpx.Request] = []
        client = _recording_client(requests)
        provider._http_client = client
        try:
            await provider.search_web("q", 5, CORRELATION_ID)
        finally:
            await client.aclose()

        assert _basic_credential(requests[0]) == (URL_USERINFO_USERNAME, URL_USERINFO_PASSWORD)

    @pytest.mark.asyncio
    async def test_auth_failure_does_not_expose_the_password(self, caplog: pytest.LogCaptureFixture) -> None:
        """A 401 must not put the credential in the exception message or the logs.

        httpx quotes the request URL verbatim into ``HTTPStatusError``, and
        ProviderChain logs ``str(error)`` on fallback — so a URL still carrying
        ``user:pass@`` would leak the password into the log on the very failure
        an operator is most likely to hit.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="Unauthorized")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = SearXNGProvider(
            url=f"http://{URL_USERINFO_USERNAME}:{URL_USERINFO_PASSWORD}@searxng.invalid",
            http_client=client,
        )
        try:
            with caplog.at_level(logging.DEBUG):
                with pytest.raises(httpx.HTTPStatusError) as excinfo:
                    await provider.search_web("q", 5, CORRELATION_ID)
        finally:
            await client.aclose()

        assert URL_USERINFO_PASSWORD not in str(excinfo.value)
        assert URL_USERINFO_PASSWORD not in repr(excinfo.value)
        assert URL_USERINFO_PASSWORD not in caplog.text

    @pytest.mark.asyncio
    async def test_configured_password_is_never_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """The discrete-credential path must be just as quiet on failure."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="Unauthorized")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = SearXNGProvider(
            url="http://searxng.invalid",
            http_client=client,
            username=PLACEHOLDER_USERNAME,
            password=PLACEHOLDER_PASSWORD,
        )
        try:
            with caplog.at_level(logging.DEBUG):
                with pytest.raises(httpx.HTTPStatusError) as excinfo:
                    await provider.search_web("q", 5, CORRELATION_ID)
        finally:
            await client.aclose()

        assert PLACEHOLDER_PASSWORD not in str(excinfo.value)
        assert PLACEHOLDER_PASSWORD not in caplog.text

    def test_availability_does_not_depend_on_credentials(self) -> None:
        """A URL with no credential is still a usable configuration."""
        assert SearXNGProvider(url="http://searxng.invalid").is_available()


async def _credential_on_the_wire(provider: SearXNGProvider) -> tuple[str, str] | None:
    """Drive one search through a recording client and decode what it carried.

    Asserting on the outgoing header rather than the provider's stored ``_auth``
    keeps these tests measuring the thing that matters: what actually reaches
    the instance.
    """
    requests: list[httpx.Request] = []
    client = _recording_client(requests)
    provider._http_client = client
    try:
        await provider.search_web("q", 5, CORRELATION_ID)
    finally:
        await client.aclose()
    return _basic_credential(requests[0])


def _searxng_from(chain: Any) -> SearXNGProvider:
    """Pull the SearXNG provider out of a built chain."""
    for provider in chain.providers:
        if isinstance(provider, SearXNGProvider):
            return provider
    raise AssertionError(f"no SearXNG provider in the chain: {[p.name for p in chain.providers]}")


class TestBrokeredCredentialThreading:
    """A credential supplied by the caller reaches the provider (#164).

    The MCP server vends the pair from the household secrets broker at startup
    precisely so it never has to be an environment variable. That is only worth
    anything if the vended value survives every hop between the server and the
    outgoing request, so each hop is asserted rather than assumed.
    """

    @pytest.mark.asyncio
    async def test_chain_forwards_the_supplied_credential(self) -> None:
        with patch.dict(os.environ, {"SEARXNG_URL": "http://searxng.invalid"}):
            chain = build_provider_chain(
                ["searxng"],
                searxng_username=PLACEHOLDER_USERNAME,
                searxng_password=PLACEHOLDER_PASSWORD,
            )

        assert await _credential_on_the_wire(_searxng_from(chain)) == (PLACEHOLDER_USERNAME, PLACEHOLDER_PASSWORD)

    @pytest.mark.asyncio
    async def test_supplied_credential_beats_the_environment(self) -> None:
        """The broker is the stronger source: a stale env pair must not win."""
        with patch.dict(
            os.environ,
            {
                "SEARXNG_URL": "http://searxng.invalid",
                "SEARXNG_USERNAME": URL_USERINFO_USERNAME,
                "SEARXNG_PASSWORD": URL_USERINFO_PASSWORD,
            },
        ):
            chain = build_provider_chain(
                ["searxng"],
                searxng_username=PLACEHOLDER_USERNAME,
                searxng_password=PLACEHOLDER_PASSWORD,
            )

        assert await _credential_on_the_wire(_searxng_from(chain)) == (PLACEHOLDER_USERNAME, PLACEHOLDER_PASSWORD)

    @pytest.mark.asyncio
    async def test_supplied_credential_beats_url_userinfo(self) -> None:
        with patch.dict(
            os.environ,
            {"SEARXNG_URL": f"http://{URL_USERINFO_USERNAME}:{URL_USERINFO_PASSWORD}@searxng.invalid"},
        ):
            chain = build_provider_chain(
                ["searxng"],
                searxng_username=PLACEHOLDER_USERNAME,
                searxng_password=PLACEHOLDER_PASSWORD,
            )

        assert await _credential_on_the_wire(_searxng_from(chain)) == (PLACEHOLDER_USERNAME, PLACEHOLDER_PASSWORD)

    @pytest.mark.asyncio
    async def test_no_supplied_credential_leaves_the_environment_in_charge(self) -> None:
        """The unset case must be byte-for-byte what it was before the vend existed."""
        with patch.dict(
            os.environ,
            {
                "SEARXNG_URL": "http://searxng.invalid",
                "SEARXNG_USERNAME": PLACEHOLDER_USERNAME,
                "SEARXNG_PASSWORD": PLACEHOLDER_PASSWORD,
            },
        ):
            chain = build_provider_chain(["searxng"])

        assert await _credential_on_the_wire(_searxng_from(chain)) == (PLACEHOLDER_USERNAME, PLACEHOLDER_PASSWORD)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "kwargs",
        [
            pytest.param({"providers": "searxng"}, id="explicit-chain"),
            pytest.param({"provider": "searxng"}, id="legacy-single-provider"),
            pytest.param({}, id="auto-detected-from-env"),
        ],
    )
    async def test_search_service_forwards_the_credential_on_every_construction_path(
        self, kwargs: dict[str, Any]
    ) -> None:
        """SearchService builds its chain at three separate call sites.

        A forward missed at any one of them silently drops the vended credential
        for that configuration only, which is exactly the kind of gap that is
        invisible until an operator hits the one path nobody covered.
        """
        with patch.dict(
            os.environ,
            {"SEARXNG_URL": "http://searxng.invalid", "SUPACRAWL_SEARCH_PROVIDERS": "searxng"},
        ):
            service = SearchService(
                searxng_username=PLACEHOLDER_USERNAME,
                searxng_password=PLACEHOLDER_PASSWORD,
                **kwargs,
            )

        try:
            provider = _searxng_from(service.provider_chain)
            assert await _credential_on_the_wire(provider) == (PLACEHOLDER_USERNAME, PLACEHOLDER_PASSWORD)
        finally:
            await service.close()

    @pytest.mark.asyncio
    async def test_services_factory_hands_the_credential_to_the_search_service(self) -> None:
        """The seam the MCP server actually calls.

        The browser and search service are stubbed: this asserts the wiring
        between ``create_supacrawl_services`` and ``SearchService``, not the
        behaviour of either.
        """
        from unittest.mock import AsyncMock, MagicMock

        browser = MagicMock()
        browser.start = AsyncMock()
        search_service = MagicMock()

        with (
            patch("supacrawl.services.BrowserManager", return_value=browser),
            patch("supacrawl.services.SearchService", return_value=search_service) as search_cls,
        ):
            await create_supacrawl_services(
                searxng_username=PLACEHOLDER_USERNAME,
                searxng_password=PLACEHOLDER_PASSWORD,
            )

        assert search_cls.call_args.kwargs["searxng_username"] == PLACEHOLDER_USERNAME
        assert search_cls.call_args.kwargs["searxng_password"] == PLACEHOLDER_PASSWORD

    @pytest.mark.asyncio
    async def test_services_factory_defaults_to_no_supplied_credential(self) -> None:
        """The REST API calls this factory with no arguments and must stay unchanged."""
        from unittest.mock import AsyncMock, MagicMock

        browser = MagicMock()
        browser.start = AsyncMock()

        with (
            patch("supacrawl.services.BrowserManager", return_value=browser),
            patch("supacrawl.services.SearchService", return_value=MagicMock()) as search_cls,
        ):
            await create_supacrawl_services()

        assert search_cls.call_args.kwargs["searxng_username"] is None
        assert search_cls.call_args.kwargs["searxng_password"] is None
