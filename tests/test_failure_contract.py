"""The scrape failure contract (#160).

``supacrawl_scrape``'s own documentation makes two promises a calling agent acts
on, and both were broken by the reported outage:

* **a non-ok verdict always carries a concrete ``quality.suggestion``** — the
  browser-crash path returned ``verdict="empty"`` with ``suggestion=null`` and a
  raw Playwright string as the only signal;
* **an infrastructure fault is distinguishable from a site fault** — without
  that, "the site is a problem, escalate differently" and "the scraper is
  broken, restart it" look identical, and the agent in the reported session
  burned a retry when only a restart would have helped.

The first promise is stated generally, so it is asserted over the whole verdict
taxonomy rather than the one case that failed.
"""

from __future__ import annotations

from typing import Any

import pytest

from supacrawl.models import (
    HARD_FAIL_VERDICTS,
    QualityAssessment,
    QualityVerdict,
    ScrapeResult,
)
from supacrawl.quality import assess_quality
from supacrawl.services.browser import BrowserUnavailableError, PageContent, PageMetadata
from supacrawl.services.scrape import ScrapeService

NON_OK_VERDICTS = [v for v in QualityVerdict if v is not QualityVerdict.OK]


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


class _RaisingBrowser:
    """A shared browser whose fetch fails the way the test names."""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.engine = "playwright"
        self.stealth = False
        self.proxy = None

    async def fetch_page(self, url: str, **_: Any) -> PageContent:
        raise self.error

    async def extract_metadata(self, _html: str) -> PageMetadata:
        return _meta()


async def _scrape_failure(error: BaseException) -> ScrapeResult:
    """Drive a real ScrapeService through a failing fetch, browser path only."""
    service = ScrapeService(browser=_RaisingBrowser(error))  # type: ignore[arg-type]
    return await service.scrape("https://support.claude.com/en/", http_first=False, escalate=False)


# ---------------------------------------------------------------------------
# The contract itself, across the whole taxonomy
# ---------------------------------------------------------------------------


class TestSuggestionAlwaysPresent:
    @pytest.mark.parametrize("verdict", NON_OK_VERDICTS, ids=lambda v: v.value)
    def test_every_non_ok_verdict_carries_a_concrete_next_step(self, verdict: QualityVerdict) -> None:
        quality = QualityAssessment(verdict=verdict, score=0)

        assert quality.suggestion is not None, f"{verdict.value} left the caller with nothing to do"
        assert len(quality.suggestion) > 30, f"{verdict.value}'s suggestion is not a concrete next step"

    def test_a_clean_result_carries_no_suggestion(self) -> None:
        assert QualityAssessment(verdict=QualityVerdict.OK, score=95).suggestion is None

    def test_an_explicit_suggestion_is_never_overwritten(self) -> None:
        quality = QualityAssessment(verdict=QualityVerdict.EMPTY, score=0, suggestion="do this specific thing")

        assert quality.suggestion == "do this specific thing"

    def test_an_http_error_status_now_carries_one(self) -> None:
        """ERROR_STATUS had no mapping at all, so every 4xx/5xx shipped a null suggestion."""
        quality = assess_quality(status_code=404, html="<html><body>Not found</body></html>", markdown="Not found")

        assert quality.verdict is QualityVerdict.ERROR_STATUS
        assert quality.suggestion is not None


# ---------------------------------------------------------------------------
# Infrastructure vs site
# ---------------------------------------------------------------------------


class TestInfrastructureIsDistinguishable:
    def test_infrastructure_is_a_hard_fail(self) -> None:
        assert QualityVerdict.INFRASTRUCTURE in HARD_FAIL_VERDICTS
        assert QualityAssessment(verdict=QualityVerdict.INFRASTRUCTURE, score=0).is_usable is False

    @pytest.mark.asyncio
    async def test_a_dead_engine_reports_an_infrastructure_fault(self) -> None:
        """The reported failure, end to end: what the caller now gets back."""
        result = await _scrape_failure(BrowserUnavailableError("Browser engine relaunch failed: no chromium"))

        assert result.success is False
        assert result.quality is not None
        assert result.quality.verdict is QualityVerdict.INFRASTRUCTURE
        assert result.quality.suggestion is not None
        # The next step has to be the RIGHT one: restart the server, not retry
        # the site with different options.
        assert "restart" in result.quality.suggestion.lower()

    @pytest.mark.asyncio
    async def test_a_dead_engine_is_not_blamed_on_the_site(self) -> None:
        """A stealth or wait hint would send the caller after a site that was never contacted."""
        result = await _scrape_failure(BrowserUnavailableError("Browser engine died mid-fetch"))

        assert result.error is not None
        assert "stealth" not in result.error.lower()
        assert result.quality is not None
        assert result.quality.verdict is not QualityVerdict.BOT_CHALLENGE

    @pytest.mark.asyncio
    async def test_a_genuine_site_failure_still_reads_as_a_site_failure(self) -> None:
        result = await _scrape_failure(TimeoutError("Timeout 30000ms exceeded navigating to https://example.com/"))

        assert result.success is False
        assert result.quality is not None
        assert result.quality.verdict is QualityVerdict.EMPTY
        assert result.quality.suggestion is not None

    @pytest.mark.asyncio
    async def test_a_blocked_site_still_reads_as_a_block(self) -> None:
        result = await _scrape_failure(RuntimeError("Navigation failed: HTTP 403 blocked by the origin"))

        assert result.quality is not None
        assert result.quality.verdict is QualityVerdict.BOT_CHALLENGE
        assert result.quality.suggestion is not None


# ---------------------------------------------------------------------------
# The remaining paths that could reach a caller with nothing to act on
# ---------------------------------------------------------------------------


class TestOtherFailurePaths:
    @pytest.mark.asyncio
    async def test_a_missing_pdf_dependency_is_a_scraper_fault_with_a_next_step(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """This path returned no ``quality`` object at all, not merely a null suggestion."""

        async def missing(**_: Any) -> Any:
            raise ImportError("PDF parsing requires pypdf. Install with: pip install supacrawl[pdf]")

        monkeypatch.setattr("supacrawl.services.pdf.parse_pdf", missing)
        result = await ScrapeService()._scrape_pdf("https://example.com/x.pdf", "fast", ["markdown"])

        assert result.quality is not None
        assert result.quality.verdict is QualityVerdict.INFRASTRUCTURE
        assert result.quality.suggestion is not None
        assert "supacrawl[pdf]" in result.quality.suggestion

    @pytest.mark.asyncio
    async def test_an_unreadable_pdf_is_a_document_fault_with_a_next_step(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def broken(**_: Any) -> Any:
            raise ValueError("EOF marker not found")

        monkeypatch.setattr("supacrawl.services.pdf.parse_pdf", broken)
        result = await ScrapeService()._scrape_pdf("https://example.com/x.pdf", "fast", ["markdown"])

        assert result.quality is not None
        assert result.quality.verdict is QualityVerdict.EMPTY
        assert result.quality.suggestion is not None

    def test_an_unmet_expect_assertion_carries_a_next_step(self) -> None:
        """The verdict stays honest (the content was fine) but the caller still needs an action."""
        result = ScrapeResult(
            success=True,
            quality=QualityAssessment(verdict=QualityVerdict.OK, score=90),
        )

        overlaid = ScrapeService._overlay_expect(result, expect="Pricing", expect_met=False)

        assert overlaid.success is False
        assert overlaid.quality is not None
        assert overlaid.quality.suggestion is not None
        assert "Pricing" in overlaid.quality.suggestion
