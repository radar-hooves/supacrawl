"""Tests for scrape service."""

import inspect

import pytest

from supacrawl.exceptions import ProviderError, ValidationError
from supacrawl.models import ScrapeResult
from supacrawl.services.browser import PageContent, PageMetadata
from supacrawl.services.scrape import ScrapeService


def _fake_metadata() -> PageMetadata:
    return PageMetadata(
        title="T",
        description=None,
        language=None,
        keywords=None,
        robots=None,
        canonical_url=None,
        og_title=None,
        og_description=None,
        og_image=None,
        og_url=None,
        og_site_name=None,
    )


class TestScrapeServiceSignature:
    """Contract tests for the public ``ScrapeService.scrape()`` signature."""

    def test_scrape_accepts_per_request_proxy(self) -> None:
        """Regression for #112: the REST API passes ``proxy`` per request, so the
        method must accept it (a non-autospec mock previously hid its absence)."""
        params = inspect.signature(ScrapeService.scrape).parameters
        assert "proxy" in params
        assert params["proxy"].default is None


class TestJsonFormatRefusal:
    """formats=["json"] refuses cleanly instead of quietly degrading to a
    markdown-only result with no extraction (master-project#350).
    """

    @pytest.mark.asyncio
    async def test_missing_schema_and_prompt_raises_before_any_fetch(self) -> None:
        """No schema or prompt means there is nothing to tell the LLM to pull
        out, so the call is refused before any network or browser activity —
        no BrowserManager is patched here because none should be constructed."""
        service = ScrapeService()
        with pytest.raises(ValidationError, match="json_schema or json_prompt"):
            await service.scrape("https://example.com", formats=["json"])

    @pytest.mark.asyncio
    async def test_extract_json_strict_raises_when_llm_not_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SUPACRAWL_LLM_PROVIDER", raising=False)
        monkeypatch.delenv("SUPACRAWL_LLM_MODEL", raising=False)
        service = ScrapeService()

        with pytest.raises(ProviderError) as exc_info:
            await service._extract_json("some page content", None, "extract stuff", strict=True)

        assert exc_info.value.context.get("provider") == "llm"

    @pytest.mark.asyncio
    async def test_extract_json_non_strict_still_returns_none_on_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The changeTracking json-comparison path calls _extract_json without
        strict=True and must keep its existing best-effort, non-raising
        contract — only the primary "json" format path is made strict."""
        monkeypatch.delenv("SUPACRAWL_LLM_PROVIDER", raising=False)
        monkeypatch.delenv("SUPACRAWL_LLM_MODEL", raising=False)
        service = ScrapeService()

        result = await service._extract_json("some page content", None, "extract stuff")

        assert result is None

    @pytest.mark.asyncio
    async def test_scrape_refuses_without_escalating_when_llm_not_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A real end-to-end scrape (fake browser, no network) with a well-formed
        json request but no LLM configured raises ProviderError directly out of
        scrape() rather than being swallowed into a success=False result and
        retried through the anti-bot escalation ladder, which cannot fix a
        missing LLM configuration."""
        monkeypatch.delenv("SUPACRAWL_LLM_PROVIDER", raising=False)
        monkeypatch.delenv("SUPACRAWL_LLM_MODEL", raising=False)

        class FakeBrowser:
            def __init__(self, **_kwargs: object) -> None:
                pass

            async def __aenter__(self) -> "FakeBrowser":
                return self

            async def __aexit__(self, *_args: object) -> bool:
                return False

            async def fetch_page(self, url: str, **_kwargs: object) -> PageContent:
                return PageContent(
                    url=url,
                    html="<html><body><main><p>"
                    + " ".join(f"word{i}" for i in range(80))
                    + "</p></main></body></html>",
                    title="T",
                    status_code=200,
                )

            async def extract_metadata(self, _html: str) -> PageMetadata:
                return _fake_metadata()

        monkeypatch.setattr("supacrawl.services.scrape.BrowserManager", FakeBrowser)

        service = ScrapeService()
        with pytest.raises(ProviderError) as exc_info:
            await service.scrape(
                "https://example.com",
                formats=["json"],
                json_prompt="extract the title",
                http_first=False,
            )

        assert exc_info.value.context.get("provider") == "llm"


@pytest.mark.e2e
class TestScrapeService:
    """Tests for ScrapeService (E2E - require browser/network)."""

    @pytest.mark.asyncio
    async def test_scrape_returns_markdown(self):
        """Test that scrape returns markdown content."""
        service = ScrapeService()
        result = await service.scrape("https://example.com")
        assert isinstance(result, ScrapeResult)
        assert result.success
        assert result.data is not None
        assert result.data.markdown is not None
        assert len(result.data.markdown) > 0

    @pytest.mark.asyncio
    async def test_scrape_extracts_metadata(self):
        """Test that scrape extracts page metadata."""
        service = ScrapeService()
        result = await service.scrape("https://example.com")
        assert result.success
        assert result.data is not None
        assert result.data.metadata is not None
        assert result.data.metadata.title is not None
        assert result.data.metadata.source_url == "https://example.com"

    @pytest.mark.asyncio
    async def test_scrape_returns_html_when_requested(self):
        """Test that scrape returns HTML when requested."""
        service = ScrapeService()
        result = await service.scrape("https://example.com", formats=["html"])
        assert result.success
        assert result.data is not None
        assert result.data.html is not None

    @pytest.mark.asyncio
    async def test_scrape_returns_raw_html_when_requested(self):
        """Test that scrape returns raw HTML when requested."""
        service = ScrapeService()
        result = await service.scrape("https://example.com", formats=["rawHtml"])
        assert result.success
        assert result.data is not None
        assert result.data.raw_html is not None

    @pytest.mark.asyncio
    async def test_scrape_returns_links_when_requested(self):
        """Test that scrape returns links when requested."""
        service = ScrapeService()
        result = await service.scrape("https://example.com", formats=["links"])
        assert result.success
        assert result.data is not None
        assert result.data.links is not None
        assert isinstance(result.data.links, list)

    @pytest.mark.asyncio
    async def test_scrape_returns_multiple_formats(self):
        """Test that scrape can return multiple formats."""
        service = ScrapeService()
        result = await service.scrape("https://example.com", formats=["markdown", "html", "links"])
        assert result.success
        assert result.data is not None
        assert result.data.markdown is not None
        assert result.data.html is not None
        assert result.data.links is not None

    @pytest.mark.asyncio
    async def test_scrape_handles_error(self):
        """Test that scrape handles errors gracefully."""
        service = ScrapeService()
        result = await service.scrape("https://invalid-url-that-does-not-exist.example")
        assert not result.success
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_scrape_returns_json_with_prompt(self):
        """When the LLM is configured, json format returns extracted data;
        when it is not, scrape refuses with a typed ProviderError rather than
        a silent markdown-only success (master-project#350)."""
        service = ScrapeService()
        try:
            result = await service.scrape(
                "https://example.com",
                formats=["json"],
                json_prompt="Extract the page title and domain name",
            )
        except ProviderError as e:
            assert e.context.get("provider") == "llm"
            return
        assert result.success
        assert result.data is not None
        assert isinstance(result.data.llm_extraction, dict)

    @pytest.mark.asyncio
    async def test_scrape_returns_json_with_schema(self):
        """When the LLM is configured, json format returns extracted data;
        when it is not, scrape refuses with a typed ProviderError rather than
        a silent markdown-only success (master-project#350)."""
        schema = {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "domain": {"type": "string"},
            },
            "required": ["title", "domain"],
        }
        service = ScrapeService()
        try:
            result = await service.scrape(
                "https://example.com",
                formats=["json"],
                json_schema=schema,
            )
        except ProviderError as e:
            assert e.context.get("provider") == "llm"
            return
        assert result.success
        assert result.data is not None
        assert isinstance(result.data.llm_extraction, dict)

    @pytest.mark.asyncio
    async def test_scrape_returns_multiple_formats_including_json(self):
        """When the LLM is configured, markdown and json both come back;
        when it is not, scrape refuses with a typed ProviderError rather than
        a silent markdown-only success (master-project#350)."""
        service = ScrapeService()
        try:
            result = await service.scrape(
                "https://example.com",
                formats=["markdown", "json"],
                json_prompt="Extract page info",
            )
        except ProviderError as e:
            assert e.context.get("provider") == "llm"
            return
        assert result.success
        assert result.data is not None
        assert result.data.markdown is not None
        assert isinstance(result.data.llm_extraction, dict)

    @pytest.mark.asyncio
    async def test_scrape_returns_images_when_requested(self):
        """Test that scrape returns images when requested."""
        service = ScrapeService()
        result = await service.scrape("https://example.com", formats=["images"])
        assert result.success
        assert result.data is not None
        assert result.data.images is not None
        assert isinstance(result.data.images, list)

    @pytest.mark.asyncio
    async def test_scrape_returns_images_with_other_formats(self):
        """Test that scrape can return images alongside other formats."""
        service = ScrapeService()
        result = await service.scrape("https://example.com", formats=["markdown", "images"])
        assert result.success
        assert result.data is not None
        assert result.data.markdown is not None
        assert result.data.images is not None
        assert isinstance(result.data.images, list)

    @pytest.mark.asyncio
    async def test_scrape_returns_branding_when_requested(self):
        """Test that scrape returns branding information when requested."""
        service = ScrapeService()
        result = await service.scrape("https://example.com", formats=["branding"])
        assert result.success
        assert result.data is not None
        assert result.data.branding is not None
        # Branding should have at least color_scheme
        assert result.data.branding.color_scheme is not None

    @pytest.mark.asyncio
    async def test_scrape_returns_summary_when_requested(self):
        """Test that scrape returns LLM-generated summary when requested."""
        service = ScrapeService()
        result = await service.scrape("https://example.com", formats=["summary"])
        assert result.success
        assert result.data is not None
        # Summary may be None if Ollama is not running, but should not crash
        if result.data.summary is not None:
            assert isinstance(result.data.summary, str)
            assert len(result.data.summary) <= 500  # Max 500 chars per spec

    @pytest.mark.asyncio
    async def test_scrape_returns_summary_with_other_formats(self):
        """Test that scrape can return summary alongside other formats."""
        service = ScrapeService()
        result = await service.scrape("https://example.com", formats=["markdown", "summary"])
        assert result.success
        assert result.data is not None
        assert result.data.markdown is not None
        # Summary may be None if Ollama is not running
        if result.data.summary is not None:
            assert isinstance(result.data.summary, str)


class TestCaptchaSolvingInstallsNavigationGuard:
    """The CAPTCHA-solving browser path installs the same per-request
    re-validation as ``BrowserManager`` (#152), not just a pre-flight.

    ``_scrape_with_captcha_solving`` drives its own ad-hoc page rather than
    going through ``BrowserManager.fetch_page``, so unlike that method it has
    to install ``_install_navigation_guard`` itself. Driven with a fake
    ``BrowserManager`` (patched at the point ``scrape.py`` constructs it) so
    no real Chromium is needed.
    """

    class _FakePage:
        def __init__(self) -> None:
            self.route_installed = False
            self.route_installed_before_goto = False

        async def route(self, _pattern: str, _handler) -> None:
            self.route_installed = True

        async def goto(self, _url: str, **_kwargs):
            self.route_installed_before_goto = self.route_installed
            raise RuntimeError("simulated navigation failure — test stops here")

        async def close(self) -> None:
            return None

    class _FakeInnerBrowser:
        def __init__(self, page: object) -> None:
            self._page = page

        async def new_page(self) -> object:
            return self._page

    class _FakeBrowserManager:
        """Stands in for supacrawl.services.scrape.BrowserManager.

        engine="camoufox" so ``_open_page`` takes the no-separate-context branch
        and just creates a page — the minimum surface needed to reach page.goto.
        """

        instances: "list[TestCaptchaSolvingInstallsNavigationGuard._FakeBrowserManager]" = []

        def __init__(self, **_kwargs: object) -> None:
            self.engine = "camoufox"
            self.page = TestCaptchaSolvingInstallsNavigationGuard._FakePage()
            self._browser = TestCaptchaSolvingInstallsNavigationGuard._FakeInnerBrowser(self.page)
            TestCaptchaSolvingInstallsNavigationGuard._FakeBrowserManager.instances.append(self)

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

        async def _open_page(self, *, owns_context: bool, **_: object) -> tuple[None, object]:
            assert owns_context is False, "camoufox owns no separate context"
            return None, await self._browser.new_page()

    @pytest.mark.asyncio
    async def test_route_handler_installed_before_navigation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from supacrawl.services import scrape as scrape_mod

        self._FakeBrowserManager.instances.clear()
        monkeypatch.setattr(
            "supacrawl.services.url_guard.resolve_and_pin", lambda url: ("93.184.216.34", "example.com")
        )
        monkeypatch.setattr(scrape_mod, "BrowserManager", self._FakeBrowserManager)

        service = ScrapeService()

        with pytest.raises(RuntimeError, match="simulated navigation failure"):
            await service._scrape_with_captcha_solving(
                "https://example.com/",
                formats=["markdown"],
                only_main_content=True,
                wait_for=0,
                timeout=5000,
                screenshot_full_page=False,
                actions=None,
                json_schema=None,
                json_prompt=None,
                include_tags=None,
                exclude_tags=None,
            )

        assert len(self._FakeBrowserManager.instances) == 1
        page = self._FakeBrowserManager.instances[0].page
        assert page.route_installed is True
        assert page.route_installed_before_goto is True
