"""Tests for the batch scrape feature.

Fast unit tests — no network, no browser.  All external I/O is mocked.

Coverage:
- URL-list parsing (file, stdin, comments, blank lines, whitespace)
- ``run_batch_scrape`` logic with a mocked ``ScrapeService``
- Manifest-writing helper (``_write_directory``)
- CLI exit-code contract via CliRunner (all-success → 0, any-failure → 1)
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from supacrawl.cli._common import app
from supacrawl.cli.batch import _parse_urls_from_text, _write_directory
from supacrawl.models import ScrapeData, ScrapeMetadata, ScrapeResult
from supacrawl.services.batch import BatchScrapeResult, BatchURLResult, run_batch_scrape

# Helpers


def _make_scrape_result(url: str, success: bool = True) -> ScrapeResult:
    """Build a minimal ScrapeResult for use in mocks."""
    if not success:
        return ScrapeResult(success=False, error=f"simulated failure for {url}")
    return ScrapeResult(
        success=True,
        data=ScrapeData(
            markdown=f"# {url}\n\nContent",
            metadata=ScrapeMetadata(source_url=url),
        ),
    )


def _make_mock_scrape_service(results: dict[str, ScrapeResult]) -> MagicMock:
    """Return a ScrapeService mock where ``scrape()`` looks up results by URL.

    Args:
        results: Mapping of URL → ScrapeResult to return for each call.
    """
    mock = MagicMock()

    async def _scrape(url: str, **kwargs: object) -> ScrapeResult:
        return results[url]

    mock.scrape = AsyncMock(side_effect=_scrape)
    return mock


# Task 1 — URL-list parsing


class TestParseUrlsFromText:
    """Tests for ``_parse_urls_from_text``."""

    def test_simple_list(self) -> None:
        """Parses plain URL lines in order."""
        text = "https://a.com\nhttps://b.com\nhttps://c.com"
        assert _parse_urls_from_text(text) == [
            "https://a.com",
            "https://b.com",
            "https://c.com",
        ]

    def test_blank_lines_ignored(self) -> None:
        """Blank lines between URLs are discarded."""
        text = "\nhttps://a.com\n\nhttps://b.com\n\n"
        assert _parse_urls_from_text(text) == ["https://a.com", "https://b.com"]

    def test_comment_lines_ignored(self) -> None:
        """Lines starting with ``#`` are discarded."""
        text = "# this is a comment\nhttps://a.com\n# another comment\nhttps://b.com"
        assert _parse_urls_from_text(text) == ["https://a.com", "https://b.com"]

    def test_whitespace_trimmed(self) -> None:
        """Leading and trailing whitespace is stripped from every URL."""
        text = "  https://a.com  \n\thttps://b.com\t"
        assert _parse_urls_from_text(text) == ["https://a.com", "https://b.com"]

    def test_empty_input_returns_empty_list(self) -> None:
        """Completely empty text yields an empty list."""
        assert _parse_urls_from_text("") == []

    def test_all_comments_returns_empty_list(self) -> None:
        """Input that is all comments yields an empty list."""
        text = "# comment 1\n# comment 2\n"
        assert _parse_urls_from_text(text) == []

    def test_mixed_blank_and_comments(self) -> None:
        """Only real URLs survive when mixed with blanks and comments."""
        text = "\n# header\n\nhttps://a.com\n\n# skip\nhttps://b.com\n"
        assert _parse_urls_from_text(text) == ["https://a.com", "https://b.com"]


# Task 2 — run_batch_scrape with mocked ScrapeService


class TestRunBatchScrapeAllSucceed:
    """All URLs scrape successfully."""

    async def test_all_succeed_counts(self) -> None:
        """Succeeded count equals the number of input URLs."""
        urls = ["https://a.com", "https://b.com", "https://c.com"]
        service = _make_mock_scrape_service({u: _make_scrape_result(u) for u in urls})

        result = await run_batch_scrape(urls=urls, scrape_service=service)

        assert result.succeeded == 3
        assert result.failed == 0
        assert result.partial is False

    async def test_all_succeed_results_in_order(self) -> None:
        """Results list preserves input URL order."""
        urls = ["https://a.com", "https://b.com", "https://c.com"]
        service = _make_mock_scrape_service({u: _make_scrape_result(u) for u in urls})

        result = await run_batch_scrape(urls=urls, scrape_service=service)

        for i, url in enumerate(urls):
            assert result.results[i].url == url
            assert result.results[i].success is True

    async def test_all_succeed_data_attached(self) -> None:
        """Successful results carry a ScrapeResult in the data field."""
        urls = ["https://a.com"]
        service = _make_mock_scrape_service({u: _make_scrape_result(u) for u in urls})

        result = await run_batch_scrape(urls=urls, scrape_service=service)

        assert result.results[0].data is not None
        assert result.results[0].data.success is True


class TestRunBatchScrapeMixedResults:
    """Some URLs succeed, some fail, continue_on_error=True."""

    async def test_partial_flag_set(self) -> None:
        """``partial`` is True when any URL failed with continue_on_error."""
        urls = ["https://a.com", "https://bad.com"]
        service = _make_mock_scrape_service(
            {
                "https://a.com": _make_scrape_result("https://a.com", success=True),
                "https://bad.com": _make_scrape_result("https://bad.com", success=False),
            }
        )

        result = await run_batch_scrape(urls=urls, scrape_service=service, continue_on_error=True)

        assert result.succeeded == 1
        assert result.failed == 1
        assert result.partial is True

    async def test_failed_result_has_error_field(self) -> None:
        """A failed URLResult records an error string."""
        urls = ["https://bad.com"]
        service = _make_mock_scrape_service({"https://bad.com": _make_scrape_result("https://bad.com", success=False)})

        result = await run_batch_scrape(urls=urls, scrape_service=service, continue_on_error=True)

        assert result.results[0].success is False
        assert result.results[0].error is not None
        assert len(result.results[0].error) > 0

    async def test_all_results_present_despite_failures(self) -> None:
        """All URLs get a result entry even when some fail."""
        urls = ["https://a.com", "https://bad.com", "https://b.com"]
        service = _make_mock_scrape_service(
            {
                "https://a.com": _make_scrape_result("https://a.com", success=True),
                "https://bad.com": _make_scrape_result("https://bad.com", success=False),
                "https://b.com": _make_scrape_result("https://b.com", success=True),
            }
        )

        result = await run_batch_scrape(urls=urls, scrape_service=service, continue_on_error=True)

        assert len(result.results) == 3


class TestRunBatchScrapeNoContineOnError:
    """continue_on_error=False raises RuntimeError on first failure."""

    async def test_raises_on_failure(self) -> None:
        """RuntimeError is raised when a URL fails and continue_on_error is False."""
        urls = ["https://bad.com"]
        service = _make_mock_scrape_service({"https://bad.com": _make_scrape_result("https://bad.com", success=False)})

        with pytest.raises(RuntimeError, match="1 URL"):
            await run_batch_scrape(urls=urls, scrape_service=service, continue_on_error=False)


class TestRunBatchScrapeRetry:
    """Retry logic: a failing URL is attempted up to ``retry`` times."""

    async def test_retry_re_attempts_failing_url(self) -> None:
        """A URL that fails is retried up to the ``retry`` budget."""
        call_counts: dict[str, int] = {"https://flaky.com": 0}

        async def _flaky_scrape(url: str, **kwargs: object) -> ScrapeResult:
            call_counts[url] = call_counts.get(url, 0) + 1
            return _make_scrape_result(url, success=False)

        service = MagicMock()
        service.scrape = AsyncMock(side_effect=_flaky_scrape)

        result = await run_batch_scrape(
            urls=["https://flaky.com"],
            scrape_service=service,
            retry=3,
            continue_on_error=True,
        )

        # Exactly 3 attempts should have been made
        assert call_counts["https://flaky.com"] == 3
        assert result.results[0].attempts == 3
        assert result.results[0].success is False

    async def test_succeeds_after_retry(self) -> None:
        """A URL that fails twice then succeeds on the third attempt records success."""
        attempt: dict[str, int] = {"n": 0}

        async def _eventually_ok(url: str, **kwargs: object) -> ScrapeResult:
            attempt["n"] += 1
            if attempt["n"] < 3:
                return _make_scrape_result(url, success=False)
            return _make_scrape_result(url, success=True)

        service = MagicMock()
        service.scrape = AsyncMock(side_effect=_eventually_ok)

        result = await run_batch_scrape(
            urls=["https://flaky.com"],
            scrape_service=service,
            retry=3,
            continue_on_error=True,
        )

        assert result.results[0].success is True
        assert result.results[0].attempts == 3


class TestRunBatchScrapeConcurrencyLimit:
    """Concurrency semaphore prevents more than ``concurrency`` tasks in flight."""

    async def test_concurrency_never_exceeded(self) -> None:
        """Track peak concurrent-in-flight count; must not exceed the limit."""
        concurrency_limit = 2
        peak: dict[str, int] = {"current": 0, "max": 0}
        lock = asyncio.Lock()

        async def _slow_scrape(url: str, **kwargs: object) -> ScrapeResult:
            async with lock:
                peak["current"] += 1
                if peak["current"] > peak["max"]:
                    peak["max"] = peak["current"]
            # Yield to allow other tasks to start if the semaphore is leaking
            await asyncio.sleep(0)
            async with lock:
                peak["current"] -= 1
            return _make_scrape_result(url, success=True)

        urls = [f"https://example.com/page{i}" for i in range(10)]
        service = MagicMock()
        service.scrape = AsyncMock(side_effect=_slow_scrape)

        await run_batch_scrape(urls=urls, scrape_service=service, concurrency=concurrency_limit)

        # Peak concurrency must not exceed the limit
        assert peak["max"] <= concurrency_limit


class TestRunBatchScrapeEmptyInput:
    """Empty URL list returns an empty BatchScrapeResult immediately."""

    async def test_empty_returns_empty_result(self) -> None:
        """No service calls made; result has zero counts."""
        service = MagicMock()
        service.scrape = AsyncMock()

        result = await run_batch_scrape(urls=[], scrape_service=service)

        assert result.succeeded == 0
        assert result.failed == 0
        assert result.results == []
        service.scrape.assert_not_called()


# Task 3 — Directory mode manifest helper


class TestWriteDirectory:
    """Tests for ``_write_directory``."""

    def test_manifest_created(self, tmp_path: Path) -> None:
        """manifest.json is created in the output directory."""
        batch_result = BatchScrapeResult(
            results=[
                BatchURLResult(url="https://a.com", success=True, data=_make_scrape_result("https://a.com")),
            ],
            succeeded=1,
            failed=0,
            partial=False,
        )
        _write_directory(tmp_path, batch_result, ["markdown"])
        assert (tmp_path / "manifest.json").exists()

    def test_manifest_structure(self, tmp_path: Path) -> None:
        """manifest.json contains top-level counts and a per-URL list."""
        success_result = _make_scrape_result("https://a.com")
        batch_result = BatchScrapeResult(
            results=[
                BatchURLResult(url="https://a.com", success=True, data=success_result),
                BatchURLResult(url="https://bad.com", success=False, error="timeout"),
            ],
            succeeded=1,
            failed=1,
            partial=True,
        )
        _write_directory(tmp_path, batch_result, ["markdown"])

        manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["succeeded"] == 1
        assert manifest["failed"] == 1
        assert manifest["partial"] is True
        assert len(manifest["urls"]) == 2

    def test_failed_url_in_manifest(self, tmp_path: Path) -> None:
        """Failed URLs are present in the manifest with success=False and error."""
        batch_result = BatchScrapeResult(
            results=[
                BatchURLResult(url="https://bad.com", success=False, error="connection refused"),
            ],
            succeeded=0,
            failed=1,
            partial=False,
        )
        _write_directory(tmp_path, batch_result, ["markdown"])

        manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
        entry = manifest["urls"][0]
        assert entry["success"] is False
        assert entry["error"] == "connection refused"

    def test_markdown_file_written(self, tmp_path: Path) -> None:
        """A ``.md`` file is written for each successful URL in markdown format."""
        batch_result = BatchScrapeResult(
            results=[
                BatchURLResult(url="https://a.com", success=True, data=_make_scrape_result("https://a.com")),
            ],
            succeeded=1,
            failed=0,
            partial=False,
        )
        _write_directory(tmp_path, batch_result, ["markdown"])

        md_files = list(tmp_path.glob("*.md"))
        assert len(md_files) == 1
        assert "a.com" in md_files[0].name

    def test_no_markdown_written_on_failure(self, tmp_path: Path) -> None:
        """No ``.md`` file is written for a failed URL."""
        batch_result = BatchScrapeResult(
            results=[
                BatchURLResult(url="https://bad.com", success=False, error="timeout"),
            ],
            succeeded=0,
            failed=1,
            partial=False,
        )
        _write_directory(tmp_path, batch_result, ["markdown"])

        md_files = list(tmp_path.glob("*.md"))
        assert len(md_files) == 0


# Task 4 — CLI exit-code contract via CliRunner


class TestBatchCliExitCodes:
    """Exit code is 0 iff every URL succeeded; 1 otherwise."""

    def _invoke_with_mock_result(self, batch_result: BatchScrapeResult) -> int:
        """Invoke the ``batch`` CLI command with a patched ``run_batch_scrape``.

        Feeds three dummy URLs via stdin.

        Args:
            batch_result: The ``BatchScrapeResult`` that ``run_batch_scrape`` will return.

        Returns:
            The Click exit code.
        """
        runner = CliRunner()

        with patch("supacrawl.services.batch.run_batch_scrape", new=AsyncMock(return_value=batch_result)):
            result = runner.invoke(
                app,
                ["batch", "-"],
                input="https://a.com\nhttps://b.com\nhttps://c.com\n",
            )
        return result.exit_code

    def test_all_success_exits_zero(self) -> None:
        """Exit code is 0 when all URLs succeed."""
        batch_result = BatchScrapeResult(
            results=[
                BatchURLResult(url="https://a.com", success=True, data=_make_scrape_result("https://a.com")),
                BatchURLResult(url="https://b.com", success=True, data=_make_scrape_result("https://b.com")),
                BatchURLResult(url="https://c.com", success=True, data=_make_scrape_result("https://c.com")),
            ],
            succeeded=3,
            failed=0,
            partial=False,
        )
        assert self._invoke_with_mock_result(batch_result) == 0

    def test_any_failure_exits_one(self) -> None:
        """Exit code is 1 when at least one URL failed."""
        batch_result = BatchScrapeResult(
            results=[
                BatchURLResult(url="https://a.com", success=True, data=_make_scrape_result("https://a.com")),
                BatchURLResult(url="https://b.com", success=False, error="timeout"),
                BatchURLResult(url="https://c.com", success=True, data=_make_scrape_result("https://c.com")),
            ],
            succeeded=2,
            failed=1,
            partial=True,
        )
        assert self._invoke_with_mock_result(batch_result) == 1

    def test_all_failure_exits_one(self) -> None:
        """Exit code is 1 when every URL failed."""
        batch_result = BatchScrapeResult(
            results=[
                BatchURLResult(url="https://a.com", success=False, error="timeout"),
                BatchURLResult(url="https://b.com", success=False, error="timeout"),
                BatchURLResult(url="https://c.com", success=False, error="timeout"),
            ],
            succeeded=0,
            failed=3,
            partial=False,
        )
        assert self._invoke_with_mock_result(batch_result) == 1

    def test_empty_input_exits_zero(self) -> None:
        """An empty URL list exits 0 after printing a warning (nothing to do)."""
        runner = CliRunner()
        result = runner.invoke(app, ["batch", "-"], input="# only comments\n\n")
        assert result.exit_code == 0

    def test_missing_url_file_exits_one(self, tmp_path: Path) -> None:
        """A non-existent file path exits 1 with an error message."""
        runner = CliRunner()
        result = runner.invoke(app, ["batch", str(tmp_path / "nonexistent.txt")])
        assert result.exit_code == 1
