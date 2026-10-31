"""Tests for the HTTP-first fast path (#119).

Covers the eligibility gate, the escalation decision (when a cheap GET is not
enough and the browser must take over), and the offline assembly of a result
from static HTML. The fast path's network call is monkeypatched throughout so
these stay unit tests.
"""

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from supacrawl.services.detection import (
    _JS_SHELL_MAX_VISIBLE_TEXT,
    _MIN_BODY_TEXT_LENGTH,
    estimate_js_requirement,
)
from supacrawl.services.http_fetch import HttpFetchResult
from supacrawl.services.scrape import ScrapeService

# A well-formed static article: real <title>, a heading, and dense body text.
GOOD_HTML = (
    "<html><head><title>Example Domain</title>"
    '<meta name="description" content="An illustrative page."></head>'
    "<body><main><h1>Hello</h1><p>" + ("word " * 80) + "</p></main></body></html>"
)
# A React/Next.js shell: framework markers, empty root, no real body text.
JS_SHELL = (
    "<html><head><title>App</title></head><body>"
    '<div id="root"></div><script src="/_next/static/chunks/app.js"></script>'
    "</body></html>"
)
# A Cloudflare-style interstitial.
BOT_HTML = "<html><body><h1>Just a moment...</h1><p>Checking your browser before accessing.</p></body></html>"


def _fetched(html: str, status: int = 200) -> HttpFetchResult:
    return HttpFetchResult(
        url="https://example.com",
        html=html,
        status_code=status,
        content_type="text/html",
        headers={},
    )


# A shell that ships all content as inline JSON data without any framework
# marker — mimics quotes.toscrape.com/js/ pattern.  Visible text (~194 chars)
# clears Guard 2's 100-char floor but stays below Guard 3's 500-char ceiling;
# the large inline <script> payload (>3x visible text) triggers Guard 3.
_NAV_LINKS = "".join(f"<a href='/{i}'>Category{i}</a>" for i in range(10))
_INLINE_SCRIPT_PAYLOAD = (
    '{"quotes": [' + ", ".join(f'{{"text": "quote{i}", "author": "author{i}"}}' for i in range(60)) + "]}"
)
JS_DATA_SHELL = (
    "<html><head><title>Quotes</title></head><body>"
    f"<nav><a href='/'>Home</a>{_NAV_LINKS}"
    "<a href='/login'>Sign In</a><a href='/register'>Register</a>"
    "<a href='/about'>About</a><a href='/contact'>Contact Us</a>"
    "<a href='/help'>Help Centre</a></nav>"
    f"<script>var data = {_INLINE_SCRIPT_PAYLOAD};</script>"
    "<footer><p>Copyright 2024 Quotes Inc. All rights reserved.</p>"
    "<a href='/privacy'>Privacy</a><a href='/terms'>Terms</a></footer>"
    "</body></html>"
)
# A content-rich static page that also carries a large analytics blob.
# Guard 3 must NOT escalate: visible text (~763 chars) exceeds the 500-char ceiling.
_ANALYTICS_BLOB = "var _analytics = {" + ", ".join(f"k{i}: {i}" for i in range(200)) + "};"
STATIC_WITH_ANALYTICS = (
    "<html><head><title>Article</title></head><body>"
    "<main><h1>A real article</h1><p>" + ("word " * 150) + "</p></main>"
    f"<script>{_ANALYTICS_BLOB}</script>"
    "</body></html>"
)
# A static product page with thin visible text (~149 chars) and a sizeable
# schema.org ld+json block in the body (~4.1x the visible text, i.e. 609/149).
# Before the HIGH 1 fix, Guard 3 would have falsely escalated this because
# the ld+json chars were counted as script_chars (old ratio: 4.1x > 3x).
# After the fix, application/ld+json blocks are excluded from script_chars
# so Guard 3 sees 0 executable-JS chars and correctly returns False.
_LD_JSON_BLOCK = (
    '{"@context": "https://schema.org", "@type": "Product", "name": "Widget Pro 9000",'
    ' "description": "A professional-grade widget for industrial use. Featuring advanced'
    ' torque calibration and precision engineering.", "brand": {"@type": "Brand", "name":'
    ' "Widget Corp"}, "offers": {"@type": "Offer", "price": "299.99", "priceCurrency":'
    ' "USD", "availability": "https://schema.org/InStock", "seller": {"@type":'
    ' "Organization", "name": "Shop Inc."}}, "aggregateRating": {"@type":'
    ' "AggregateRating", "ratingValue": "4.8", "reviewCount": "247"},'
    ' "image": ["https://example.com/img1.jpg", "https://example.com/img2.jpg"]}'
)
STATIC_PRODUCT_WITH_LD_JSON = (
    "<html><head><title>Widget Pro 9000</title></head><body>"
    "<nav><a href='/'>Home</a><a href='/products'>Products</a><a href='/cart'>Cart</a></nav>"
    "<main><h1>Widget Pro 9000</h1><p>In stock. Free shipping on orders over fifty dollars.</p>"
    "<p>Trusted by professionals worldwide.</p></main>"
    f'<script type="application/ld+json">{_LD_JSON_BLOCK}</script>'
    "<footer><p>Shop Inc. All rights reserved.</p></footer>"
    "</body></html>"
)


# Guard that the JS_DATA_SHELL fixture's visible text stays within Guard 3's
# target window — if a future edit pushes it below 100 (Guard 2 fires instead)
# or at/above 500 (Guard 3 cannot fire), the positive test loses its meaning.
def _extract_visible_text(html: str) -> str:
    body_match = re.search(r"<body[^>]*>(.*?)</body>", html, re.DOTALL | re.IGNORECASE)
    if not body_match:
        return ""
    body = body_match.group(1)
    body = re.sub(r"<script[^>]*>.*?</script>", "", body, flags=re.DOTALL | re.IGNORECASE)
    body = re.sub(r"<style[^>]*>.*?</style>", "", body, flags=re.DOTALL | re.IGNORECASE)
    body = re.sub(r"<[^>]+>", "", body)
    return body.strip()


_js_data_shell_visible = _extract_visible_text(JS_DATA_SHELL)
assert len(_js_data_shell_visible) >= _MIN_BODY_TEXT_LENGTH, (
    f"JS_DATA_SHELL visible text ({len(_js_data_shell_visible)} chars) fell below "
    f"_MIN_BODY_TEXT_LENGTH ({_MIN_BODY_TEXT_LENGTH}); Guard 2 fires before Guard 3 "
    "and the positive test no longer targets the right branch."
)
assert len(_js_data_shell_visible) < _JS_SHELL_MAX_VISIBLE_TEXT, (
    f"JS_DATA_SHELL visible text ({len(_js_data_shell_visible)} chars) reached "
    f"_JS_SHELL_MAX_VISIBLE_TEXT ({_JS_SHELL_MAX_VISIBLE_TEXT}); Guard 3 can no "
    "longer fire and the positive test always passes vacuously."
)


class TestEstimateJsRequirement:
    """The render-needed heuristic that gates escalation."""

    def test_static_article_does_not_need_js(self) -> None:
        assert estimate_js_requirement(GOOD_HTML, len(GOOD_HTML)) is False

    def test_framework_shell_needs_js(self) -> None:
        assert estimate_js_requirement(JS_SHELL, len(JS_SHELL)) is True

    def test_empty_body_needs_js(self) -> None:
        html = "<html><body>   </body></html>"
        assert estimate_js_requirement(html, len(html)) is True

    def test_inline_data_shell_needs_js(self) -> None:
        """Guard 3: nav+footer text (~194 chars) clears Guard 2 but sits below
        Guard 3's 500-char ceiling; the inline JS data payload is >3x the
        visible text, so Guard 3 must escalate.

        Covers the quotes.toscrape.com/js/ pattern: no framework marker, body
        is just a nav/footer wrapper with real content injected at runtime.
        This fixture is designed so Guard 2 does NOT fire (visible text >= 100)
        and Guard 3 is the branch under test.
        """
        assert estimate_js_requirement(JS_DATA_SHELL, len(JS_DATA_SHELL)) is True

    def test_content_rich_page_with_analytics_does_not_need_js(self) -> None:
        """False-positive control: a page with ~763 chars of visible text must NOT
        escalate even when it carries a large inline analytics or config script blob.
        Visible text comfortably exceeds Guard 3's 500-char ceiling (~263 chars margin).
        """
        assert estimate_js_requirement(STATIC_WITH_ANALYTICS, len(STATIC_WITH_ANALYTICS)) is False

    def test_static_product_with_ld_json_does_not_need_js(self) -> None:
        """False-positive control for non-JS script types (HIGH 1 regression guard).

        A static product page with ~149 chars visible text and a schema.org
        application/ld+json block totalling ~4.1x the visible text must NOT
        escalate. Before the fix, ld+json chars were counted as script_chars
        and Guard 3 would have fired (old ratio 4.1x > 3x threshold); after
        the fix, application/ld+json is excluded so script_chars is 0.
        """
        assert estimate_js_requirement(STATIC_PRODUCT_WITH_LD_JSON, len(STATIC_PRODUCT_WITH_LD_JSON)) is False

    def test_thin_static_page_without_scripts_does_not_need_js(self) -> None:
        """Regression guard: Guard 3 must not fire when there are no inline scripts.

        Confirms that a page clearing Guard 2's 100-char floor (visible text ~150
        chars) but carrying zero script content does not escalate — the ratio
        condition (0 >= 3x) is false regardless of the visible-text ceiling.
        Does not exercise Guard 3's positive path; use test_inline_data_shell_needs_js
        for that.
        """
        # ~150 chars of visible text, no <script> blocks — Guard 3 ratio is 0.
        visible = "word " * 30  # 150 chars
        html = f"<html><body><nav><a href='/'>Home</a></nav><p>{visible}</p></body></html>"
        assert estimate_js_requirement(html, len(html)) is False


class TestHttpFirstEligible:
    """The cheap-path gate on ScrapeService."""

    def test_eligible_for_plain_markdown(self) -> None:
        assert ScrapeService()._http_first_eligible(["markdown"], None, None, None) is True

    def test_eligible_with_explicit_playwright_engine(self) -> None:
        assert ScrapeService()._http_first_eligible(["markdown"], None, "playwright", None) is True

    def test_ineligible_for_screenshot(self) -> None:
        assert ScrapeService()._http_first_eligible(["screenshot"], None, None, None) is False

    def test_ineligible_for_pdf_capture(self) -> None:
        assert ScrapeService()._http_first_eligible(["pdf"], None, None, None) is False

    def test_ineligible_with_actions(self) -> None:
        assert ScrapeService()._http_first_eligible(["markdown"], [{"type": "wait"}], None, None) is False

    def test_ineligible_with_device(self) -> None:
        assert ScrapeService()._http_first_eligible(["markdown"], None, None, "iPhone 14") is False

    def test_ineligible_with_stealth(self) -> None:
        assert ScrapeService(stealth=True)._http_first_eligible(["markdown"], None, None, None) is False

    def test_ineligible_with_camoufox_engine(self) -> None:
        assert ScrapeService()._http_first_eligible(["markdown"], None, "camoufox", None) is False

    def test_ineligible_with_service_default_engine(self) -> None:
        assert ScrapeService(engine="patchright")._http_first_eligible(["markdown"], None, None, None) is False


@pytest.mark.asyncio
class TestTryHttpFirst:
    """The escalate-or-serve decision after a static fetch."""

    async def _run(self, monkeypatch: pytest.MonkeyPatch, fetched: HttpFetchResult | None, **overrides: object):
        async def fake_fetch(url: str, **kwargs: object) -> HttpFetchResult | None:
            return fetched

        monkeypatch.setattr("supacrawl.services.scrape.fetch_static", fake_fetch)
        service = ScrapeService()
        kwargs: dict[str, object] = {
            "url": "https://example.com",
            "formats": ["markdown"],
            "timeout": 30000,
            "only_main_content": True,
            "include_tags": None,
            "exclude_tags": None,
            "content_mode": 0.5,
            "query": None,
            "expand_iframes": "same-origin",
            "headers": None,
            "proxy": None,
            "json_schema": None,
            "json_prompt": None,
            "wants_change_tracking": False,
            "previous_entry": None,
            "change_tracking_modes": None,
            "max_age": 0,
            "cache_variant": None,
            "expect": None,
            "parse_pdf": None,
        }
        kwargs.update(overrides)
        return await service._try_http_first(**kwargs)  # type: ignore[arg-type]

    async def test_page_with_robots_meta_is_served(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Regression: a normal page carrying ``<meta name="robots">`` (matched by
        the bot-detection regex via the substring "robot") must still be served —
        the density heuristic must use the real word count, not a hard zero."""
        html = (
            '<html><head><title>Article</title><meta name="robots" content="index,follow">'
            "</head><body><main><h1>Headline</h1><p>" + ("word " * 80) + "</p></main></body></html>"
        )
        result = await self._run(monkeypatch, _fetched(html))
        assert result is not None
        assert result.success
        assert result.data is not None
        assert result.data.markdown and "Headline" in result.data.markdown

    async def test_links_only_page_with_robots_meta_is_served(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Same regression for a non-markdown format, where markdown is not computed
        and the visible-text fallback must supply the word count."""
        html = (
            '<html><head><title>Links</title><meta name="robots" content="all"></head>'
            "<body><main><a href='/a'>A</a><p>" + ("word " * 80) + "</p></main></body></html>"
        )
        result = await self._run(monkeypatch, _fetched(html), formats=["links"])
        assert result is not None
        assert result.success

    async def test_static_page_served_without_browser(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = await self._run(monkeypatch, _fetched(GOOD_HTML))
        assert result is not None
        assert result.success
        assert result.data is not None
        assert result.data.markdown is not None
        assert "Hello" in result.data.markdown
        assert result.data.metadata.title == "Example Domain"
        assert result.data.metadata.source_url == "https://example.com"
        assert result.data.metadata.status_code == 200

    async def test_js_shell_escalates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert await self._run(monkeypatch, _fetched(JS_SHELL)) is None

    async def test_bot_challenge_escalates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert await self._run(monkeypatch, _fetched(BOT_HTML)) is None

    async def test_prose_about_captchas_and_blocking_is_served(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An article that mentions a CAPTCHA, being blocked and access denied is content, not a wall."""
        html = (
            "<html><head><title>Web scraping</title></head><body><main><h1>Web scraping</h1>"
            "<p>Sites deploy a CAPTCHA, and a crawler that is blocked sees access denied; please wait "
            "is a common interstitial. " + ("word " * 120) + "</p></main></body></html>"
        )
        result = await self._run(monkeypatch, _fetched(html))
        assert result is not None and result.success

    async def test_an_over_pruned_catalogue_page_is_recovered_without_a_browser(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A static grid whose main-content prune leaves a few words is served from the full page."""
        products = "".join(
            f"<article class='product'><h3><a href='/p{i}'>Book {i}</a></h3><p class='price'>£{i}.00</p></article>"
            for i in range(60)
        )
        html = (
            "<html><head><title>Books</title><meta name='robots' content='all'></head><body>"
            "<main><p>All products</p></main>"
            f"<section>{products}</section></body></html>"
        )
        result = await self._run(monkeypatch, _fetched(html))
        assert result is not None and result.success
        assert result.data is not None and result.data.markdown and "Book 59" in result.data.markdown

    async def test_a_challenge_widget_escalates_whatever_the_word_count(self, monkeypatch: pytest.MonkeyPatch) -> None:
        html = (
            "<html><head><title>Checking</title></head><body><main><p>" + ("word " * 120) + "</p>"
            '<div class="g-recaptcha" data-sitekey="x"></div></main></body></html>'
        )
        assert await self._run(monkeypatch, _fetched(html)) is None

    async def test_a_static_catalogue_page_is_served_with_its_listing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A product grid prunes to a few words under only_main_content; the fast path recovers the full page
        the way the browser path does, instead of launching a browser over the same HTML."""
        items = "".join(
            f"<article class='product_pod'><h3><a href='/b{i}'>Title {i}</a></h3><p class='price'>£{i}.00</p></article>"
            for i in range(40)
        )
        html = (
            '<html><head><title>Books</title><meta name="robots" content="index"></head><body>'
            "<nav><a href='/'>Home</a><a href='/books'>Books</a></nav>"
            f"<main><section>{items}</section></main>"
            "<footer><p>A footer.</p></footer></body></html>"
        )
        result = await self._run(monkeypatch, _fetched(html))
        assert result is not None and result.success
        assert result.data is not None and result.data.markdown and "Title 39" in result.data.markdown

    async def test_blocking_status_escalates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert await self._run(monkeypatch, _fetched(GOOD_HTML, status=403)) is None

    async def test_fetch_failure_escalates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert await self._run(monkeypatch, None) is None

    async def test_iframe_page_escalates_when_expansion_requested(self, monkeypatch: pytest.MonkeyPatch) -> None:
        html = "<html><body><iframe src='https://x.example'></iframe><p>" + ("word " * 80) + "</p></body></html>"
        assert await self._run(monkeypatch, _fetched(html), expand_iframes="same-origin") is None

    async def test_disclosure_gated_page_escalates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """#142: gated content is invisible to quality scoring, so nothing else catches it.

        The page has no iframe, no bot marker and plenty of prose — it would
        score "ok" and be served straight from HTTP, silently dropping the
        hidden panel the browser would have opened.
        """
        html = (
            "<html><head><title>Rates</title></head><body><main><h1>Rates</h1>"
            "<p>" + ("word " * 80) + "</p>"
            '<button aria-expanded="false" aria-controls="p1">Fee schedule</button>'
            '<div id="p1" hidden><p>Band A: $412 per quarter.</p></div>'
            "</main></body></html>"
        )
        assert await self._run(monkeypatch, _fetched(html)) is None

    async def test_click_injected_panel_escalates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`aria-controls` names an id nothing matches: the panel arrives on click."""
        html = (
            "<html><head><title>FAQ</title></head><body><main><h1>FAQ</h1>"
            "<p>" + ("word " * 80) + "</p>"
            '<button aria-expanded="false" aria-controls="answer-1">Why is my bill estimated?</button>'
            "</main></body></html>"
        )
        assert await self._run(monkeypatch, _fetched(html)) is None

    async def test_bootstrap_accordion_page_is_still_served(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The fast path must survive the commonest accordion markup on the web.

        Bootstrap hides via its own stylesheet, so the converter emits the panel
        text regardless — escalating would cost a browser render for nothing.
        """
        html = (
            "<html><head><title>FAQ</title></head><body><main><h1>FAQ</h1>"
            "<p>" + ("word " * 80) + "</p>"
            '<button aria-expanded="false" aria-controls="p1">Question</button>'
            '<div id="p1" class="accordion-collapse collapse"><p>Answer text.</p></div>'
            "</main></body></html>"
        )
        result = await self._run(monkeypatch, _fetched(html))
        assert result is not None
        assert result.success
        assert result.data is not None
        assert result.data.markdown and "Answer text." in result.data.markdown

    async def test_closed_details_page_is_still_served(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A closed <details> is emitted by the converter, so it is not gated."""
        html = (
            "<html><head><title>Rates</title></head><body><main><h1>Rates</h1>"
            "<p>" + ("word " * 80) + "</p>"
            "<details><summary>Fee schedule</summary><p>Band A: $412.</p></details>"
            "</main></body></html>"
        )
        result = await self._run(monkeypatch, _fetched(html))
        assert result is not None
        assert result.success
        assert result.data is not None
        assert result.data.markdown and "Band A: $412." in result.data.markdown

    async def test_page_with_only_a_nav_menu_is_still_served(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The fast path must survive the hamburger button every mobile site has."""
        html = (
            "<html><head><title>Article</title></head><body>"
            '<nav><button aria-expanded="false" aria-controls="menu">Menu</button>'
            '<ul id="menu" hidden><li><a href="/a">A</a></li></ul></nav>'
            "<main><h1>Headline</h1><p>" + ("word " * 80) + "</p></main></body></html>"
        )
        result = await self._run(monkeypatch, _fetched(html))
        assert result is not None
        assert result.success
        assert result.data is not None
        assert result.data.markdown and "Headline" in result.data.markdown

    async def test_iframe_page_served_when_expansion_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        html = (
            "<html><head><title>Embed</title></head><body><iframe src='https://x.example'></iframe>"
            "<main><p>" + ("word " * 80) + "</p></main></body></html>"
        )
        result = await self._run(monkeypatch, _fetched(html), expand_iframes="none")
        assert result is not None
        assert result.success

    async def test_links_format_served_without_markdown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        html = (
            "<html><head><title>Links</title></head><body><main>"
            '<a href="/a">A</a><a href="https://other.example/b">B</a>'
            "<p>" + ("word " * 80) + "</p></main></body></html>"
        )
        result = await self._run(monkeypatch, _fetched(html), formats=["links"])
        assert result is not None
        assert result.data is not None
        assert result.data.links is not None
        assert "https://example.com/a" in result.data.links


@pytest.mark.asyncio
class TestFetchStatic:
    """Unit tests for fetch_static() content-type routing and size guards.

    Each test injects a fake httpx response so no real network calls are made.
    """

    def _make_fake_response(
        self,
        *,
        content_type: str,
        body: bytes,
        content_length: str | None = None,
        url: str = "https://example.com/doc",
        status_code: int = 200,
    ) -> MagicMock:
        """Build a minimal fake httpx response."""
        resp = MagicMock()
        headers: dict[str, str] = {}
        if content_type:
            headers["content-type"] = content_type
        if content_length is not None:
            headers["content-length"] = content_length
        resp.headers = headers
        resp.content = body
        resp.text = body.decode("utf-8", errors="replace")
        resp.url = url
        resp.status_code = status_code
        return resp

    async def _call(self, fake_response: MagicMock) -> HttpFetchResult | None:
        """Call fetch_static with a patched guarded_request (#152).

        fetch_static routes its GET through url_guard.guarded_request rather
        than calling client.get() directly, so the fake response is injected
        there; the real httpx.AsyncClient is left to construct normally since
        it never actually opens a socket in this path.
        """
        from supacrawl.services.http_fetch import fetch_static

        with patch("supacrawl.services.http_fetch.guarded_request", AsyncMock(return_value=fake_response)):
            return await fetch_static("https://example.com/doc", timeout_ms=5000)

    async def test_missing_content_type_with_pdf_magic_routed_to_pdf(self) -> None:
        """A response with no Content-Type header whose body starts with %PDF
        must be returned with raw_bytes set (routed to the PDF extractor).

        This is strictly better than pre-patch behaviour, which would have
        decoded the raw bytes as text and returned them on the HTML path.
        """
        pdf_body = b"%PDF-1.4 test content"
        resp = self._make_fake_response(content_type="", body=pdf_body)
        result = await self._call(resp)
        assert result is not None
        assert result.raw_bytes == pdf_body

    async def test_missing_content_type_with_html_body_falls_through_to_html_path(self) -> None:
        """A response with no Content-Type header whose body is plain HTML must
        be processed on the HTML path, not discarded.

        Pre-patch behaviour: the `if content_type and ...` guard was falsy for
        an empty string, so the body fell through to HTML decoding and was
        returned as html=response.text.  This test documents and locks in that
        behaviour — a missing content-type non-PDF response is a valid HTML
        result, not a browser-fallback None.
        """
        html_body = b"<html><body><p>Hello world</p></body></html>"
        resp = self._make_fake_response(content_type="", body=html_body)
        result = await self._call(resp)
        # Must return a result (not None) so the HTML-first path can proceed.
        assert result is not None
        # Must not have been mistaken for a PDF.
        assert result.raw_bytes is None
        assert "Hello world" in result.html

    async def test_content_length_pre_check_skips_oversized_pdf(self) -> None:
        """When Content-Length declares a body exceeding MAX_PDF_SIZE, fetch_static
        must return None WITHOUT reading the body — the expensive buffer is never
        allocated.

        The body here is tiny (to prove the body was not read); only the header
        is consulted for the early-exit decision.
        """
        from supacrawl.services._pdf_sniff import MAX_PDF_SIZE

        # Declare a body larger than the cap via header; actual body is tiny.
        oversized_length = str(MAX_PDF_SIZE + 1)
        resp = self._make_fake_response(
            content_type="application/pdf",
            body=b"%PDF-1.4 tiny",
            content_length=oversized_length,
        )
        result = await self._call(resp)
        # The body is tiny, so the post-read size guard (len(body) > max_bytes)
        # can never produce None for this input.  A None result can therefore
        # only originate from the Content-Length pre-read check, proving that
        # path fired before any body was consumed.
        assert result is None

    async def test_content_length_pre_check_allows_within_limit(self) -> None:
        """A Content-Length within the PDF cap must not trigger the early exit."""
        from supacrawl.services._pdf_sniff import MAX_PDF_SIZE

        within_limit = str(MAX_PDF_SIZE - 1)
        pdf_body = b"%PDF-1.4 small document"
        resp = self._make_fake_response(
            content_type="application/pdf",
            body=pdf_body,
            content_length=within_limit,
        )
        result = await self._call(resp)
        assert result is not None
        assert result.raw_bytes == pdf_body

    async def test_post_read_size_guard_still_rejects_oversized_body(self) -> None:
        """When Content-Length is absent (server omits it), the post-read len()
        check must still reject a body that exceeds MAX_PDF_SIZE."""
        from supacrawl.services._pdf_sniff import MAX_PDF_SIZE

        oversized_body = b"%PDF-1.4 " + b"x" * (MAX_PDF_SIZE + 1)
        resp = self._make_fake_response(content_type="application/pdf", body=oversized_body)
        result = await self._call(resp)
        assert result is None
