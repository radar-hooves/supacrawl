"""Unit tests for benchmark/runner.py.

Runs run_benchmark over a 2-case in-memory suite using a fake provider and a
monkeypatched ReferenceRenderer. No real browser, no network access.
"""

from __future__ import annotations

from pathlib import Path
from types import TracebackType
from unittest.mock import patch

import pytest
from conftest import make_case_result as _make_case

from supacrawl.benchmark.models import BenchCase, BenchSuite, RunResult
from supacrawl.benchmark.providers.base import ProviderOutput
from supacrawl.benchmark.reference import ReferenceCapture
from supacrawl.benchmark.runner import _build_aggregate, run_benchmark

# ---------------------------------------------------------------------------
# Fake provider
# ---------------------------------------------------------------------------


class FakeProvider:
    """Minimal ScraperProvider that returns canned outputs without network I/O."""

    name = "fake"

    async def scrape(self, url: str, *, content_type: str = "html") -> ProviderOutput:
        """Return a successful output with token-rich markdown.

        Args:
            url: The URL requested (ignored; always returns a canned result).
            content_type: Content type hint (ignored for the fake).

        Returns:
            ``ProviderOutput`` with predictable content.
        """
        if "pdf" in url:
            return ProviderOutput(
                success=True,
                markdown="This is a PDF document with Attention and Transformer content.",
                status_code=200,
                latency_ms=50.0,
            )
        return ProviderOutput(
            success=True,
            markdown="# Example Domain\nThis domain is for illustrative examples in documents.",
            status_code=200,
            json_ld_found=False,
            images_count=0,
            latency_ms=42.0,
        )

    async def aclose(self) -> None:
        """No-op cleanup.

        Returns:
            None
        """


# ---------------------------------------------------------------------------
# Fake ReferenceRenderer
# ---------------------------------------------------------------------------


class FakeReferenceRenderer:
    """Fake ReferenceRenderer that returns canned captures without a browser."""

    async def __aenter__(self) -> "FakeReferenceRenderer":
        """Enter the context without launching any browser.

        Returns:
            Self.
        """
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """No-op exit.

        Args:
            exc_type: Exception type, if any.
            exc_val: Exception value, if any.
            exc_tb: Traceback, if any.
        """

    async def capture(self, url: str, *, timeout_ms: int = 30000) -> ReferenceCapture:
        """Return a canned reference capture.

        Args:
            url: The URL to capture (ignored; always returns canned content).
            timeout_ms: Timeout hint (ignored).

        Returns:
            ``ReferenceCapture`` with realistic-looking content.
        """
        return ReferenceCapture(
            main_text="Example Domain This domain is for illustrative examples in documents.",
            full_text="Example Domain This domain is for illustrative examples in documents. More page chrome.",
            dom_counts={"headings": 1, "tables": 0, "code": 0, "images": 0, "links": 1},
            status=200,
        )


# ---------------------------------------------------------------------------
# Test suite fixture
# ---------------------------------------------------------------------------


def _make_suite() -> BenchSuite:
    """Build a minimal 2-case suite for testing.

    Returns:
        ``BenchSuite`` with one HTML case (scored) and one PDF case.
    """
    return BenchSuite(
        name="test-suite",
        description="Minimal unit-test suite",
        cases=[
            BenchCase(
                id="static-example",
                url="https://example.com/",
                category="static",
                title="Example Domain",
                why="Baseline test case",
                difficulty=1,
                content_type="html",
                stable=True,
                expect=["Example Domain", "illustrative examples"],
                expect_absent=["navigation", "footer"],
            ),
            BenchCase(
                id="pdf-test",
                url="https://arxiv.org/pdf/fake.pdf",
                category="pdf",
                title="Fake PDF",
                why="PDF extraction test",
                difficulty=2,
                content_type="pdf",
                stable=True,
                expect=["Attention", "Transformer"],
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_run_benchmark_returns_run_result(tmp_path: Path) -> None:
    """run_benchmark returns a RunResult with all cases populated."""
    suite = _make_suite()

    with (
        patch("supacrawl.benchmark.runner.make_provider", lambda name: FakeProvider()),
        patch("supacrawl.benchmark.runner.ReferenceRenderer", FakeReferenceRenderer),
    ):
        result = await run_benchmark(suite, base_dir=tmp_path)

    assert isinstance(result, RunResult)
    assert result.suite_name == "test-suite"
    assert len(result.cases) == 2


@pytest.mark.unit
async def test_run_benchmark_case_ids_match(tmp_path: Path) -> None:
    """Each CaseResult carries the correct case_id."""
    suite = _make_suite()

    with (
        patch("supacrawl.benchmark.runner.make_provider", lambda name: FakeProvider()),
        patch("supacrawl.benchmark.runner.ReferenceRenderer", FakeReferenceRenderer),
    ):
        result = await run_benchmark(suite, base_dir=tmp_path)

    case_ids = {c.case_id for c in result.cases}
    assert case_ids == {"static-example", "pdf-test"}


@pytest.mark.unit
async def test_run_benchmark_html_case_has_metrics(tmp_path: Path) -> None:
    """The HTML case should have coverage and token_f1 populated."""
    suite = _make_suite()

    with (
        patch("supacrawl.benchmark.runner.make_provider", lambda name: FakeProvider()),
        patch("supacrawl.benchmark.runner.ReferenceRenderer", FakeReferenceRenderer),
    ):
        result = await run_benchmark(suite, base_dir=tmp_path)

    html_case = next(c for c in result.cases if c.case_id == "static-example")
    m = html_case.metrics

    assert m.success is True
    assert m.quality is not None
    assert m.quality > 0
    assert m.char_coverage is not None
    assert m.token_f1 is not None
    # Anchor hits: markdown contains "Example Domain" and "illustrative examples"
    assert m.expect_hit == pytest.approx(1.0)


@pytest.mark.unit
async def test_run_benchmark_pdf_case_skips_reference_metrics(tmp_path: Path) -> None:
    """PDF cases should not have char_coverage or token_f1 (reference not used)."""
    suite = _make_suite()

    with (
        patch("supacrawl.benchmark.runner.make_provider", lambda name: FakeProvider()),
        patch("supacrawl.benchmark.runner.ReferenceRenderer", FakeReferenceRenderer),
    ):
        result = await run_benchmark(suite, base_dir=tmp_path)

    pdf_case = next(c for c in result.cases if c.case_id == "pdf-test")
    m = pdf_case.metrics

    assert m.success is True
    # Reference-derived metrics must be None for PDF cases
    assert m.char_coverage is None
    assert m.token_f1 is None
    # Anchor-based metrics should still work
    assert m.expect_hit is not None
    assert m.expect_hit == pytest.approx(1.0)  # "Attention" and "Transformer" are present


@pytest.mark.unit
async def test_run_benchmark_aggregate_populated(tmp_path: Path) -> None:
    """The aggregate should have overall_quality and success_rate set."""
    suite = _make_suite()

    with (
        patch("supacrawl.benchmark.runner.make_provider", lambda name: FakeProvider()),
        patch("supacrawl.benchmark.runner.ReferenceRenderer", FakeReferenceRenderer),
    ):
        result = await run_benchmark(suite, base_dir=tmp_path)

    agg = result.aggregate
    assert agg.total_cases == 2
    assert agg.success_rate == pytest.approx(1.0)
    assert agg.overall_quality is not None
    assert 0 < agg.overall_quality <= 100


@pytest.mark.unit
async def test_run_benchmark_only_filter(tmp_path: Path) -> None:
    """The 'only' filter restricts which cases run."""
    suite = _make_suite()

    with (
        patch("supacrawl.benchmark.runner.make_provider", lambda name: FakeProvider()),
        patch("supacrawl.benchmark.runner.ReferenceRenderer", FakeReferenceRenderer),
    ):
        result = await run_benchmark(suite, base_dir=tmp_path, only=["static"])

    assert len(result.cases) == 1
    assert result.cases[0].case_id == "static-example"


@pytest.mark.unit
async def test_run_benchmark_only_filter_by_id(tmp_path: Path) -> None:
    """The 'only' filter matches by case ID as well as category."""
    suite = _make_suite()

    with (
        patch("supacrawl.benchmark.runner.make_provider", lambda name: FakeProvider()),
        patch("supacrawl.benchmark.runner.ReferenceRenderer", FakeReferenceRenderer),
    ):
        result = await run_benchmark(suite, base_dir=tmp_path, only=["pdf-test"])

    assert len(result.cases) == 1
    assert result.cases[0].case_id == "pdf-test"


@pytest.mark.unit
async def test_run_benchmark_artifacts_written(tmp_path: Path) -> None:
    """Markdown artefact files should be written under the run directory."""
    suite = _make_suite()

    with (
        patch("supacrawl.benchmark.runner.make_provider", lambda name: FakeProvider()),
        patch("supacrawl.benchmark.runner.ReferenceRenderer", FakeReferenceRenderer),
    ):
        result = await run_benchmark(suite, base_dir=tmp_path)

    html_case = next(c for c in result.cases if c.case_id == "static-example")
    assert "markdown" in html_case.artifacts
    # The artifact path should exist on disk relative to the run directory
    run_dir = tmp_path / "runs" / result.run_id
    artifact_path = run_dir / html_case.artifacts["markdown"]
    assert artifact_path.exists()


# ---------------------------------------------------------------------------
# _build_aggregate — unit tests for aggregation correctness
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_aggregate_arithmetic_mean_of_scored_cases() -> None:
    """_build_aggregate returns the plain mean of scored cases with known quality.

    Formula: overall_quality = round(sum(qualities) / count, 1)
    Scored cases: case-a (60.0) and case-b (80.0) → mean = (60+80)/2 = 70.0
    Unscored case (case-c, quality=90.0) must NOT contribute to the mean.
    """
    cases = [
        _make_case("case-a", quality=60.0, scored=True, latency_ms=100.0),
        _make_case("case-b", quality=80.0, scored=True, latency_ms=100.0),
        _make_case("case-c", quality=90.0, scored=False, latency_ms=100.0),  # excluded from mean
    ]
    agg = _build_aggregate(cases)

    # (60.0 + 80.0) / 2 = 70.0
    assert agg.overall_quality == pytest.approx(70.0)
    assert agg.scored_cases == 2
    assert agg.total_cases == 3
    assert agg.success_rate == pytest.approx(1.0)


@pytest.mark.unit
def test_build_aggregate_excludes_none_quality() -> None:
    """Cases where quality is None are excluded even when scored=True.

    Formula counts only cases where both scored=True and quality is not None.
    case-a (scored, quality=50.0) and case-b (scored, quality=None) →
    only case-a contributes → overall_quality = 50.0.
    """
    cases = [
        _make_case("case-a", quality=50.0, scored=True, latency_ms=100.0),
        _make_case("case-b", quality=None, scored=True, success=False, latency_ms=100.0),
    ]
    agg = _build_aggregate(cases)

    assert agg.overall_quality == pytest.approx(50.0)
    # scored_cases counts cases where scored=True AND quality is not None
    assert agg.scored_cases == 1


@pytest.mark.unit
def test_build_aggregate_all_unscored_gives_none() -> None:
    """When no cases are scored, overall_quality is None (not zero)."""
    cases = [
        _make_case("case-a", quality=80.0, scored=False, latency_ms=100.0),
        _make_case("case-b", quality=70.0, scored=False, latency_ms=100.0),
    ]
    agg = _build_aggregate(cases)

    assert agg.overall_quality is None
    assert agg.scored_cases == 0
