"""Tests for PDF concurrency hardening in benchmark/runner.py.

Verifies that:
1. ``_scrape_pdf_with_retry`` retries once when a PDF yields 0 words
   (contention-induced truncation), and records the non-zero retry result.
2. Genuinely empty PDFs are not masked — a 0-word result after the retry
   is still recorded (with a warning).
3. A bench run including a PDF case yields stable non-zero extraction
   across repeated simulated runs (mocked provider).

No real browser or network access is used; all provider calls are mocked.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import TracebackType
from unittest.mock import patch

import pytest

from supacrawl.benchmark.models import BenchCase, BenchSuite
from supacrawl.benchmark.providers.base import ProviderOutput
from supacrawl.benchmark.reference import ReferenceCapture
from supacrawl.benchmark.runner import _has_words, _scrape_pdf_with_retry, run_benchmark

# Helpers

_PDF_CASE = BenchCase(
    id="pdf-arxiv-attention",
    url="https://arxiv.org/pdf/1706.03762",
    category="pdf",
    title="Attention is All You Need",
    why="Large PDF concurrency test",
    difficulty=2,
    content_type="pdf",
    stable=True,
    expect=["Attention", "Transformer"],
)


def _pdf_semaphore() -> asyncio.Semaphore:
    """Return a fresh single-slot PDF semaphore."""
    return asyncio.Semaphore(1)


# _has_words


@pytest.mark.unit
def test_has_words_true_for_non_empty() -> None:
    """_has_words returns True when markdown has at least one word."""
    output = ProviderOutput(success=True, markdown="Attention is all you need", status_code=200)
    assert _has_words(output) is True


@pytest.mark.unit
def test_has_words_false_for_empty() -> None:
    """_has_words returns False when markdown is empty or whitespace-only."""
    for md in ("", "   ", "\n\t"):
        output = ProviderOutput(success=True, markdown=md, status_code=200)
        assert _has_words(output) is False, f"Expected False for {md!r}"


@pytest.mark.unit
def test_has_words_false_for_failed_output() -> None:
    """_has_words returns False when a failed scrape yields empty markdown."""
    output = ProviderOutput(success=False, markdown="", status_code=0)
    assert _has_words(output) is False


# _scrape_pdf_with_retry


@pytest.mark.unit
async def test_retry_returns_non_zero_on_second_attempt() -> None:
    """_scrape_pdf_with_retry returns the non-zero retry when first attempt is 0 words.

    Simulates the contention case: first attempt yields empty markdown (0 words),
    retry yields real content.  The retry result should be returned.
    """
    empty_output = ProviderOutput(success=True, markdown="", status_code=200, latency_ms=30.0)
    real_output = ProviderOutput(
        success=True,
        markdown="Attention is all you need. Transformer architecture.",
        status_code=200,
        latency_ms=35.0,
    )

    class _FakeProvider:
        name = "fake"
        call_count = 0

        async def scrape(self, url: str, *, content_type: str = "html") -> ProviderOutput:
            self.call_count += 1
            return empty_output if self.call_count == 1 else real_output

        async def aclose(self) -> None:
            pass

    provider = _FakeProvider()
    result = await _scrape_pdf_with_retry(
        case=_PDF_CASE,
        provider=provider,  # type: ignore[arg-type]
        pdf_semaphore=_pdf_semaphore(),
    )

    assert provider.call_count == 2, "Expected exactly two scrape calls (first + retry)"
    assert _has_words(result), "Retry result must contain words"
    assert "Attention" in (result.markdown or "")


@pytest.mark.unit
async def test_retry_records_zero_words_when_retry_also_empty() -> None:
    """_scrape_pdf_with_retry records 0-word result when retry also yields empty.

    A genuinely empty PDF (or a persistent failure) must not be masked.
    The retry result is returned even though it is 0 words.
    """
    empty_output = ProviderOutput(success=True, markdown="", status_code=200, latency_ms=30.0)

    class _EmptyProvider:
        name = "fake"

        async def scrape(self, url: str, *, content_type: str = "html") -> ProviderOutput:
            return empty_output

        async def aclose(self) -> None:
            pass

    result = await _scrape_pdf_with_retry(
        case=_PDF_CASE,
        provider=_EmptyProvider(),  # type: ignore[arg-type]
        pdf_semaphore=_pdf_semaphore(),
    )

    assert not _has_words(result), "0-word genuinely-empty PDF must still be 0 words"
    assert result.success is True


@pytest.mark.unit
async def test_no_retry_when_first_attempt_has_words() -> None:
    """_scrape_pdf_with_retry must NOT retry when the first attempt has content.

    Only the contention-induced 0-word case triggers a retry.
    """
    real_output = ProviderOutput(
        success=True,
        markdown="Attention is all you need.",
        status_code=200,
        latency_ms=30.0,
    )

    class _FakeProvider:
        name = "fake"
        call_count = 0

        async def scrape(self, url: str, *, content_type: str = "html") -> ProviderOutput:
            self.call_count += 1
            return real_output

        async def aclose(self) -> None:
            pass

    provider = _FakeProvider()
    result = await _scrape_pdf_with_retry(
        case=_PDF_CASE,
        provider=provider,  # type: ignore[arg-type]
        pdf_semaphore=_pdf_semaphore(),
    )

    assert provider.call_count == 1, "No retry expected when first attempt has words"
    assert _has_words(result)


# Full run_benchmark — stable non-zero extraction across repeated runs


class _FakeRendererForPdf:
    """Fake ReferenceRenderer that returns no-op captures."""

    async def __aenter__(self) -> _FakeRendererForPdf:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        pass

    async def capture(self, url: str, *, timeout_ms: int = 30000) -> ReferenceCapture:
        """Return a generic capture for any HTML URL.

        Args:
            url: The URL requested.
            timeout_ms: Navigation timeout hint.

        Returns:
            ``ReferenceCapture`` with placeholder content.
        """
        return ReferenceCapture(
            main_text="placeholder",
            full_text="placeholder",
            dom_counts={},
            status=200,
        )


class _FakeProviderForConcurrency:
    """Provider that returns substantial PDF content on every call.

    Simulates a PDF scrape that works reliably once resource contention is
    eliminated by the separate PDF concurrency lane.
    """

    name = "fake"
    _call_count: int = 0

    async def scrape(self, url: str, *, content_type: str = "html") -> ProviderOutput:
        """Return non-empty PDF or HTML content.

        Args:
            url: The URL requested.
            content_type: ``"pdf"`` or ``"html"``.

        Returns:
            ``ProviderOutput`` with realistic-looking content.
        """
        self._call_count += 1
        if content_type == "pdf":
            return ProviderOutput(
                success=True,
                markdown="Attention is all you need. The Transformer model architecture.",
                status_code=200,
                latency_ms=50.0,
            )
        return ProviderOutput(
            success=True,
            markdown="# Example\nThis domain is for illustrative examples.",
            status_code=200,
            latency_ms=30.0,
        )

    async def aclose(self) -> None:
        """No-op cleanup."""


def _make_pdf_suite() -> BenchSuite:
    """Build a suite with one HTML case and one large PDF case.

    Returns:
        ``BenchSuite`` suitable for concurrency testing.
    """
    return BenchSuite(
        name="pdf-concurrency-test",
        description="Tests that PDF cases run deterministically under concurrency",
        cases=[
            BenchCase(
                id="static-example",
                url="https://example.com/",
                category="static",
                title="Example Domain",
                why="HTML baseline",
                difficulty=1,
                content_type="html",
                stable=True,
                expect=["Example Domain"],
            ),
            _PDF_CASE,
        ],
    )


@pytest.mark.unit
async def test_pdf_case_stable_non_zero_in_bench_run(tmp_path: Path) -> None:
    """PDF case must yield non-zero word count in a bench run with concurrency.

    Runs the benchmark three times in sequence (simulating repeated bench
    invocations) and asserts that the PDF case always has non-zero markdown
    words — i.e. no spurious 0-word truncation.
    """
    suite = _make_pdf_suite()

    for run_idx in range(3):
        with (
            patch("supacrawl.benchmark.runner.make_provider", lambda name: _FakeProviderForConcurrency()),
            patch("supacrawl.benchmark.runner.ReferenceRenderer", _FakeRendererForPdf),
        ):
            result = await run_benchmark(suite, base_dir=tmp_path, concurrency=3)

        pdf_result = next(c for c in result.cases if c.case_id == "pdf-arxiv-attention")
        assert pdf_result.metrics.success is True, f"Run {run_idx}: PDF case must succeed"
        assert (pdf_result.metrics.markdown_words or 0) > 0, f"Run {run_idx}: PDF case must have non-zero word count"
