"""A scrape that carries a ``query`` must say whether the filter ran, and why not."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from supacrawl.models import ScrapeData, ScrapeMetadata, ScrapeResult
from supacrawl.services.content_filter import QUERY_OUTCOME, available_extras
from supacrawl.services.converter import MarkdownConverter
from supacrawl.services.scrape import ScrapeService

pytestmark = pytest.mark.unit

SECTIONED = (
    "<html><body><main>"
    + "".join(f"<h2>{t}</h2><p>{t} {' '.join([t] * 30)}</p>" for t in ("alpha", "beta", "gamma", "delta"))
    + "</main></body></html>"
)
FLAT = "<html><body><main><p>" + "just words " * 40 + "</p></main></body></html>"


def _convert(html: str, **kwargs):
    QUERY_OUTCOME.set(None)
    MarkdownConverter().convert(html, base_url="https://example.com", **kwargs)
    return QUERY_OUTCOME.get()


def test_sectioned_page_reports_applied_with_section_counts():
    pytest.importorskip("rank_bm25")
    outcome = _convert(SECTIONED, query="alpha", content_mode=1.0)
    assert outcome is not None and outcome.applied is True
    assert outcome.sections_total == 4
    assert outcome.sections_kept is not None and outcome.sections_kept < 4


def test_flat_page_reports_why_it_was_not_filtered():
    pytest.importorskip("rank_bm25")
    outcome = _convert(FLAT, query="anything")
    assert outcome is not None
    assert (outcome.applied, outcome.reason) == (False, "flat_page")


def test_missing_rank_bm25_is_reported_not_silent():
    with patch("supacrawl.services.content_filter._is_rank_bm25_available", return_value=False):
        outcome = _convert(SECTIONED, query="alpha")
    assert outcome is not None
    assert (outcome.applied, outcome.reason) == (False, "rank_bm25_not_installed")


def test_include_tags_bypasses_the_filter_and_says_so():
    outcome = _convert(SECTIONED, query="alpha", include_tags=["main"])
    assert outcome is not None
    assert (outcome.applied, outcome.reason) == (False, "include_tags_set")


def test_full_page_extraction_bypasses_the_filter_and_says_so():
    outcome = _convert(SECTIONED, query="alpha", only_main_content=False)
    assert outcome is not None
    assert (outcome.applied, outcome.reason) == (False, "only_main_content_false")


def test_no_query_leaves_no_outcome():
    assert _convert(SECTIONED) is None


def _finish(result: ScrapeResult, query: str | None) -> ScrapeResult:
    service = ScrapeService.__new__(ScrapeService)
    service._telemetry = None
    return service._finish(result, "https://example.com", None, query)


def _result() -> ScrapeResult:
    return ScrapeResult(success=True, data=ScrapeData(markdown="x", metadata=ScrapeMetadata()))


def test_finish_without_a_query_adds_nothing():
    QUERY_OUTCOME.set(None)
    assert _finish(_result(), None).query_filter is None


def test_finish_with_a_query_that_never_reached_extraction_says_so():
    QUERY_OUTCOME.set(None)
    qf = _finish(_result(), "q").query_filter
    assert qf is not None and (qf.applied, qf.reason) == (False, "no_html_extraction")


def test_available_extras_agrees_with_the_filter_guard():
    with patch("supacrawl.services.content_filter._is_rank_bm25_available", return_value=False):
        assert available_extras()["query_filter"] is False
