"""Tests for adaptive auto-escalation (#129).

The escalation ladder spawns fresh ScrapeService instances internally, each of
which builds its own BrowserManager. These tests patch the BrowserManager the
scrape module constructs so a whole ladder can be driven offline, with each
attempt's response chosen by the engine/stealth the rung used.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from supacrawl.models import QualityVerdict
from supacrawl.services.browser import PageContent, PageMetadata
from supacrawl.services.scrape import ScrapeService

pytestmark = pytest.mark.asyncio

_BLOCKED_HTML = "<html><body><h1>Access Denied</h1><p>You have been blocked.</p></body></html>"
_GOOD_HTML = "<html><body><main><p>" + " ".join(f"word{i}" for i in range(200)) + "</p></main></body></html>"
# A bare third-party CAPTCHA wall: a reCAPTCHA widget, no CDN interstitial text,
# no usable content. A stronger engine cannot solve it (#153).
_CAPTCHA_HTML = '<html><body><div class="g-recaptcha" data-sitekey="abc"></div></body></html>'
# A CAPTCHA widget wrapped in a Cloudflare managed-challenge interstitial: a
# stealth engine's job, so this must keep escalating.
_CF_INTERSTITIAL_HTML = '<html><body><h1>Just a moment...</h1><div class="cf-turnstile"></div></body></html>'


def _meta() -> PageMetadata:
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


def _patch_engine_availability(monkeypatch: pytest.MonkeyPatch, *, patchright: bool, camoufox: bool) -> None:
    """Pin every engine-availability surface the escalation code consults.

    The ladder reads the binary-aware gate, so patching only the import checks
    left it reading the developer's own machine: the suite passed or failed on
    whether ``camoufox fetch`` had ever been run there.
    """
    usable = {"playwright": True, "patchright": patchright, "camoufox": camoufox}
    monkeypatch.setattr(
        "supacrawl.services.scrape._engine_available",
        lambda engine: usable.get(engine or "playwright", False),
    )


def _patch_ladder(monkeypatch: pytest.MonkeyPatch, respond, *, patchright=True, camoufox=True) -> list:
    """Patch the scrape module's BrowserManager + engine availability.

    ``respond(engine, stealth)`` returns ``(html, status_code)`` for an attempt.
    Returns a list that records each constructed fake browser (for counting).
    """
    _patch_engine_availability(monkeypatch, patchright=patchright, camoufox=camoufox)
    created: list = []

    class FakeBrowser:
        def __init__(self, **kwargs: object) -> None:
            self.engine = kwargs.get("engine")
            self.stealth = bool(kwargs.get("stealth", False))
            self.proxy = kwargs.get("proxy")
            created.append(self)

        async def __aenter__(self) -> "FakeBrowser":
            return self

        async def __aexit__(self, *_: object) -> bool:
            return False

        async def fetch_page(self, url: str, **_: object) -> PageContent:
            html, status = respond(self.engine, self.stealth)
            return PageContent(url=url, html=html, title="T", status_code=status)

        async def extract_metadata(self, _html: str) -> PageMetadata:
            return _meta()

    monkeypatch.setattr("supacrawl.services.scrape.BrowserManager", FakeBrowser)
    return created


async def test_auto_escalation_recovers_blocked_page(monkeypatch: pytest.MonkeyPatch) -> None:
    # Cheap playwright attempt is blocked; the stealth rung returns real content.
    def respond(engine: str | None, stealth: bool) -> tuple[str, int]:
        if stealth or engine == "camoufox":
            return _GOOD_HTML, 200
        return _BLOCKED_HTML, 403

    created = _patch_ladder(monkeypatch, respond)
    result = await ScrapeService().scrape("https://x.example", formats=["markdown"], http_first=False)

    assert result.success is True
    assert result.data is not None and result.data.markdown and "word10" in result.data.markdown
    assert result.quality is not None
    assert result.quality.verdict == QualityVerdict.OK
    assert result.quality.escalated is True
    assert result.quality.attempts >= 2
    assert len(created) >= 2  # at least one escalation happened


async def test_escalation_budget_bounds_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every rung stays blocked: the ladder must stop at the bounded budget.
    created = _patch_ladder(monkeypatch, lambda engine, stealth: (_BLOCKED_HTML, 403))
    result = await ScrapeService().scrape("https://x.example", formats=["markdown"], http_first=False)

    assert result.success is False
    assert result.quality is not None
    # playwright -> patchright -> camoufox -> camoufox+HTTP/1.1 == 4 attempts, no more.
    assert result.quality.attempts == 4
    assert len(created) == 4


async def test_exhausted_ladder_reports_each_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    # The caller must see which strategies ran and how each failed, in order.
    _patch_ladder(monkeypatch, lambda engine, stealth: (_BLOCKED_HTML, 403))
    result = await ScrapeService().scrape("https://x.example", formats=["markdown"], http_first=False)

    assert result.quality is not None
    tried = result.quality.tried
    assert [t.strategy for t in tried] == ["playwright", "patchright", "camoufox", "camoufox (HTTP/1.1)"]
    assert all(t.verdict == QualityVerdict.BOT_CHALLENGE and t.status_code == 403 for t in tried)
    assert all(t.reason for t in tried)
    assert len(tried) == result.quality.attempts
    assert result.model_dump()["quality"]["tried"][0]["strategy"] == "playwright"


async def test_captcha_wall_fails_fast_after_one_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    # A bare CAPTCHA wall is terminal for the engine ladder (#153): stop after a
    # single attempt rather than burning three doomed escalations, and hand the
    # caller the CAPTCHA suggestion immediately.
    monkeypatch.delenv("SUPACRAWL_CAPTCHA_FAIL_FAST", raising=False)
    created = _patch_ladder(monkeypatch, lambda engine, stealth: (_CAPTCHA_HTML, 200))
    result = await ScrapeService().scrape("https://x.example", formats=["markdown"], http_first=False)

    assert result.success is False
    assert result.quality is not None
    assert result.quality.verdict == QualityVerdict.CAPTCHA
    assert result.quality.attempts == 1  # was 4 before fail-fast
    assert result.quality.escalated is False
    assert len(created) == 1  # only the first attempt ran
    # The suggestion the escalation used to arrive at only after four attempts is
    # present on the very first result.
    assert result.quality.suggestion is not None and "captcha" in result.quality.suggestion.lower()
    # The fail-fast is an audible decision, not a silent skip.
    assert any("fail-fast" in r for r in result.quality.reasons)


async def test_captcha_fail_fast_override_walks_the_ladder(monkeypatch: pytest.MonkeyPatch) -> None:
    # Falsifiability: SUPACRAWL_CAPTCHA_FAIL_FAST=0 restores the full ladder walk,
    # so a caller who disagrees with the short-circuit can see it run.
    monkeypatch.setenv("SUPACRAWL_CAPTCHA_FAIL_FAST", "0")
    created = _patch_ladder(monkeypatch, lambda engine, stealth: (_CAPTCHA_HTML, 200))
    result = await ScrapeService().scrape("https://x.example", formats=["markdown"], http_first=False)

    assert result.quality is not None
    assert result.quality.verdict == QualityVerdict.CAPTCHA
    assert result.quality.attempts == 4  # ladder walked to exhaustion, as before
    assert len(created) == 4


async def test_cloudflare_interstitial_with_captcha_still_escalates(monkeypatch: pytest.MonkeyPatch) -> None:
    # A CAPTCHA widget inside a Cloudflare "just a moment" interstitial is a
    # stealth engine's job — it must NOT be misread as a hard CAPTCHA wall and
    # fail fast. It classifies BOT_CHALLENGE and keeps escalating (#153 guard).
    monkeypatch.delenv("SUPACRAWL_CAPTCHA_FAIL_FAST", raising=False)

    def respond(engine: str | None, stealth: bool) -> tuple[str, int]:
        if stealth or engine == "camoufox":
            return _GOOD_HTML, 200
        return _CF_INTERSTITIAL_HTML, 200

    created = _patch_ladder(monkeypatch, respond)
    result = await ScrapeService().scrape("https://x.example", formats=["markdown"], http_first=False)

    assert result.success is True
    assert result.quality is not None and result.quality.verdict == QualityVerdict.OK
    assert result.quality.escalated is True
    assert len(created) >= 2  # the interstitial escalated to a stealth engine


async def test_escalate_false_takes_a_single_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    created = _patch_ladder(monkeypatch, lambda engine, stealth: (_BLOCKED_HTML, 403))
    result = await ScrapeService().scrape("https://x.example", formats=["markdown"], http_first=False, escalate=False)

    assert result.success is False
    assert result.quality is not None and result.quality.attempts == 1
    assert result.quality.escalated is False
    assert len(created) == 1


async def test_no_escalation_when_no_stealth_engine_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Honest dead-end: blocked, and nothing stronger is installed to escalate to.
    created = _patch_ladder(monkeypatch, lambda engine, stealth: (_BLOCKED_HTML, 403), patchright=False, camoufox=False)
    result = await ScrapeService().scrape("https://x.example", formats=["markdown"], http_first=False)

    assert result.success is False
    assert result.quality is not None and result.quality.verdict == QualityVerdict.BOT_CHALLENGE
    assert len(created) == 1


async def test_thin_main_content_falls_back_to_full_page() -> None:
    # When only_main_content extraction is anomalously sparse, the fuller page is
    # recovered rather than silently dropping the real content.
    service = ScrapeService()
    rich_body = " ".join(f"word{i}" for i in range(200))
    html = f"<html><body><p>{rich_body}</p></body></html>"
    recovered = service._recover_thin_main_content(
        html=html,
        main_markdown="tiny bit",
        url="https://x.example",
        exclude_tags=None,
        content_mode=0.5,
        query=None,
    )
    assert len(recovered.split()) >= 50


async def test_strategy_memory_seeds_the_ladder(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # The first hit climbs the ladder; the second hit to the same domain is
    # seeded straight to the winning strategy, taking a single attempt (#130).
    from supacrawl.services.strategy_memory import StrategyStore

    def respond(engine: str | None, stealth: bool) -> tuple[str, int]:
        if stealth or engine == "camoufox":
            return _GOOD_HTML, 200
        return _BLOCKED_HTML, 403

    created = _patch_ladder(monkeypatch, respond)
    store = StrategyStore(strategy_dir=tmp_path, explore_rate=0.0)

    r1 = await ScrapeService(strategy_store=store).scrape(
        "https://qantas.example/a", formats=["markdown"], http_first=False
    )
    assert r1.success is True
    assert len(created) >= 2  # the first hit had to escalate to find the winner

    created.clear()
    r2 = await ScrapeService(strategy_store=store).scrape(
        "https://qantas.example/b", formats=["markdown"], http_first=False
    )
    assert r2.success is True
    assert len(created) == 1  # seeded straight to the champion — no ladder walk
    assert r2.quality is not None
    assert r2.quality.attempts == 1
    assert r2.quality.escalated is False


async def test_strategy_memory_disabled_is_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    # With no store, behaviour is the plain stateless ladder (no seeding/recording).
    created = _patch_ladder(monkeypatch, lambda engine, stealth: (_GOOD_HTML, 200))
    result = await ScrapeService(strategy_store=None).scrape(
        "https://x.example", formats=["markdown"], http_first=False
    )
    assert result.success is True
    assert len(created) == 1


async def test_thin_result_on_known_platform_escalates(monkeypatch: pytest.MonkeyPatch) -> None:
    # A thin result on a recognised site-builder (Foleon) escalates to that
    # platform's tuned engine, even though THIN is not in the generic escalatable
    # set — a regression the review caught after the ladder refactor.
    foleon_thin = '<html><body data-foleon="1"><p>short teaser only</p></body></html>'

    def respond(engine: str | None, stealth: bool) -> tuple[str, int]:
        if engine == "camoufox":
            return _GOOD_HTML, 200
        return foleon_thin, 200

    created = _patch_ladder(monkeypatch, respond)
    result = await ScrapeService().scrape("https://x.example", formats=["markdown"], http_first=False)

    assert result.success is True
    assert result.quality is not None and result.quality.verdict == QualityVerdict.OK
    assert result.quality.escalated is True
    assert any(b.engine == "camoufox" for b in created)


async def test_http_first_escalatable_verdict_falls_through_to_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    # An HTTP-first result with an escalatable verdict (EMPTY) must NOT be returned
    # directly — it falls through to the browser ladder, which recovers the page.
    from supacrawl.models import ScrapeData, ScrapeMetadata, ScrapeResult
    from supacrawl.quality import assess_quality

    empty_quality = assess_quality(status_code=200, html="<html><body></body></html>", markdown="")

    async def fake_http_first(self: ScrapeService, **_: object) -> ScrapeResult:
        return ScrapeResult(
            success=False,
            error="empty",
            quality=empty_quality,
            data=ScrapeData(metadata=ScrapeMetadata(source_url="https://x.example")),
        )

    monkeypatch.setattr(ScrapeService, "_try_http_first", fake_http_first)
    created = _patch_ladder(monkeypatch, lambda engine, stealth: (_GOOD_HTML, 200))

    result = await ScrapeService().scrape("https://x.example", formats=["markdown"], http_first=True)

    assert result.success is True
    assert result.data is not None and result.data.markdown and "word10" in result.data.markdown
    assert len(created) >= 1  # the browser path actually ran


def _patch_faithful_browser(monkeypatch: pytest.MonkeyPatch) -> list:
    """Patch BrowserManager with a fake that resolves engine/stealth EXACTLY like
    the real one (engine wins; else stealth→patchright; stealth = patchright/
    camoufox). Records every construction so per-page temporary browsers can be
    counted. The real BrowserManager's resolution is the crux of #139: a fake that
    stores the raw kwargs would hide the very bug this exercises.
    """
    _patch_engine_availability(monkeypatch, patchright=True, camoufox=True)
    created: list = []

    class FaithfulBrowser:
        def __init__(self, **kwargs: object) -> None:
            engine = kwargs.get("engine")
            stealth = bool(kwargs.get("stealth", False))
            self.engine = engine if engine is not None else ("patchright" if stealth else "playwright")
            self.stealth = self.engine in ("patchright", "camoufox")
            self.proxy = kwargs.get("proxy")
            created.append(self)

        async def __aenter__(self) -> "FaithfulBrowser":
            return self

        async def __aexit__(self, *_: object) -> bool:
            return False

        async def fetch_page(self, url: str, **_: object) -> PageContent:
            return PageContent(url=url, html=_GOOD_HTML, title="T", status_code=200)

        async def extract_metadata(self, _html: str) -> PageMetadata:
            return _meta()

    monkeypatch.setattr("supacrawl.services.scrape.BrowserManager", FaithfulBrowser)
    return created


async def test_shared_champion_browser_needs_no_per_page_override(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # #139 crux: when the crawl's shared browser already IS the domain's champion
    # engine, a same-domain page must reuse it — NOT build a temporary browser.
    # This exercises the real needs_seed_override comparison against a faithfully
    # resolved browser (camoufox resolves stealth=True), the exact path the
    # earlier ScrapeService-mocking wiring tests never touched.
    from supacrawl.services.scrape import ScrapeService
    from supacrawl.services.strategy_memory import StrategyStore

    store = StrategyStore(strategy_dir=tmp_path, explore_rate=0.0)
    assert await _record_camoufox_champion(store, "hard.example")  # sanity

    created = _patch_faithful_browser(monkeypatch)
    from supacrawl.services.scrape import BrowserManager  # the patched FaithfulBrowser

    shared_browser = BrowserManager(engine="camoufox", stealth=False)  # crawl's shared browser
    created.clear()  # count only per-page constructions

    service = ScrapeService(browser=shared_browser, strategy_store=store)
    result = await service.scrape("https://hard.example/page", formats=["markdown"], http_first=False)

    assert result.success is True
    assert len(created) == 0, "a per-page temporary browser was built despite the shared browser matching the champion"


async def test_shared_weaker_browser_does_build_per_page_override(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # The contrast: a shared playwright browser with a camoufox champion still
    # builds a per-page temporary camoufox (the pre-#139 waste), proving the test
    # above is measuring something real, not a resolution that never overrides.
    from supacrawl.services.scrape import ScrapeService
    from supacrawl.services.strategy_memory import StrategyStore

    store = StrategyStore(strategy_dir=tmp_path, explore_rate=0.0)
    await _record_camoufox_champion(store, "hard.example")

    created = _patch_faithful_browser(monkeypatch)
    from supacrawl.services.scrape import BrowserManager

    shared_browser = BrowserManager(engine="playwright")  # weaker than the champion
    created.clear()

    service = ScrapeService(browser=shared_browser, strategy_store=store)
    result = await service.scrape("https://hard.example/page", formats=["markdown"], http_first=False)

    assert result.success is True
    assert len(created) == 1 and created[0].engine == "camoufox"


async def _record_camoufox_champion(store, domain: str) -> bool:
    from supacrawl.models import QualityAssessment, QualityVerdict, ScrapeData, ScrapeMetadata, ScrapeResult

    good = ScrapeResult(
        success=True,
        quality=QualityAssessment(verdict=QualityVerdict.OK, score=90),
        data=ScrapeData(markdown="ok", metadata=ScrapeMetadata(source_url=f"https://{domain}/")),
    )
    store.record(domain, engine="camoufox", stealth=False, wait_for=5000, only_main_content=True, result=good)
    return store.get(domain) is not None


async def test_fetch_exception_yields_clean_failure_not_crash() -> None:
    # A mid-fetch exception must become success=False with a hint, never a crash.
    browser = MagicMock()
    browser.engine = "playwright"
    browser.fetch_page = AsyncMock(side_effect=TimeoutError("Page.goto: Timeout 30000ms exceeded"))
    service = ScrapeService(browser=browser)

    result = await service.scrape("https://x.example", formats=["markdown"], http_first=False)

    assert result.success is False
    assert result.error is not None and "timeout" in result.error.lower()
    assert result.quality is not None  # a structured verdict accompanies the failure
