"""Batch scrape command — scrape a list of URLs concurrently."""

from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from supacrawl.cli._common import app, parse_header_string, parse_headers_env

if TYPE_CHECKING:
    from supacrawl.services.batch import BatchScrapeResult


def _parse_urls_from_text(text: str) -> list[str]:
    """Parse a URL list from raw text.

    Blank lines and lines starting with ``#`` are ignored.  Leading/trailing
    whitespace is stripped from each line.

    Args:
        text: Raw text content, one URL per line.

    Returns:
        Ordered list of non-empty, non-comment URL strings.
    """
    urls: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        urls.append(stripped)
    return urls


def _slug_for_url(url: str) -> str:
    """Derive a filesystem-safe slug from a URL.

    Mirrors the logic in ``CrawlService._save_page``: strips scheme and host,
    converts path separators to underscores, and falls back to ``"index"`` for
    root URLs.

    Args:
        url: Absolute URL.

    Returns:
        Filesystem-safe slug string (no path separators).
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    # Use netloc + path so different domains don't collide in the same directory
    combined = (parsed.netloc + parsed.path).strip("/").replace("/", "_")
    return combined or "index"


def _output_path_for_url(output_dir: Path, url: str, ext: str, existing: set[str]) -> Path:
    """Choose a collision-free output path for a URL.

    When the base slug already exists in ``existing``, a short SHA-256 suffix
    is appended, matching the crawl service's deduplication pattern.

    Args:
        output_dir: Target directory.
        url: Source URL (used to derive the slug and collision suffix).
        ext: File extension including the leading dot (e.g. ``".md"``).
        existing: Set of slugs already committed in this batch run.

    Returns:
        ``Path`` that does not collide with existing entries.
    """
    slug = _slug_for_url(url)
    if slug not in existing:
        existing.add(slug)
        return output_dir / f"{slug}{ext}"

    # Collision: add URL hash suffix
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:8]
    unique_slug = f"{slug}_{url_hash}"
    existing.add(unique_slug)
    return output_dir / f"{unique_slug}{ext}"


@app.command("batch", help="Scrape a list of URLs concurrently.")
@click.argument(
    "url_file",
    default="-",
    required=False,
    metavar="URL_FILE",
)
@click.option(
    "--output",
    "-o",
    "output_dir",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="Output directory. One file per URL plus manifest.json.",
)
@click.option(
    "--format",
    "-f",
    "formats",
    multiple=True,
    type=click.Choice(
        [
            "markdown",
            "html",
            "rawHtml",
            "links",
            "images",
            "screenshot",
            "pdf",
            "json",
            "branding",
            "summary",
        ],
        case_sensitive=False,
    ),
    default=["markdown"],
    show_default=True,
    help="Output format(s). Repeatable.",
)
@click.option(
    "--concurrency",
    type=int,
    default=5,
    show_default=True,
    help="Maximum concurrent scrape tasks.",
)
@click.option(
    "--timeout",
    type=int,
    default=30000,
    show_default=True,
    help="Per-page load timeout in ms.",
)
@click.option(
    "--retry",
    type=int,
    default=1,
    show_default=True,
    help="Maximum attempts per URL (1 = one try, no retry).",
)
@click.option(
    "--continue-on-error/--no-continue-on-error",
    default=True,
    show_default=True,
    help="Continue processing remaining URLs after a failure.",
)
@click.option(
    "--stealth/--no-stealth",
    default=False,
    help="Enable Patchright stealth mode (requires supacrawl[stealth]).",
)
@click.option(
    "--proxy",
    type=str,
    default=None,
    help="Proxy URL (e.g. http://user:pass@host:port, socks5://host:port).",
)
@click.option(
    "--engine",
    type=click.Choice(["playwright", "patchright", "camoufox"], case_sensitive=False),
    default=None,
    help="Browser engine override. Overrides --stealth.",
)
@click.option(
    "--header",
    "-H",
    "headers",
    multiple=True,
    metavar="KEY: VALUE",
    help=("Custom HTTP header in 'Key: Value' format. Repeatable. Also reads SUPACRAWL_HEADERS env."),
)
@click.option(
    "--max-age",
    type=int,
    default=0,
    show_default=True,
    help="Cache freshness in seconds (0 = no cache).",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Emit a JSON array of results to stdout instead of writing files.",
)
def batch(
    url_file: str,
    output_dir: Path | None,
    formats: tuple[str, ...],
    concurrency: int,
    timeout: int,
    retry: int,
    continue_on_error: bool,
    stealth: bool,
    proxy: str | None,
    engine: str | None,
    headers: tuple[str, ...],
    max_age: int,
    output_json: bool,
) -> None:
    """Scrape a list of URLs concurrently.

    URL_FILE is a path to a plain-text file containing one URL per line, or
    ``-`` to read from stdin.  Blank lines and lines beginning with ``#`` are
    ignored.

    In directory mode (``-o``), one file is written per URL plus a
    ``manifest.json`` recording per-URL success/failure.  In JSON mode
    (``--json``), a single JSON array is printed to stdout.

    Progress feedback is always written to stderr so stdout stays clean for
    piping.

    Exit code is 0 when all URLs succeeded, 1 when any failed.

    Examples:
        supacrawl batch urls.txt
        supacrawl batch urls.txt -o ./output
        supacrawl batch - < urls.txt --json
        supacrawl batch urls.txt --concurrency 10 --timeout 60000
        supacrawl batch urls.txt --retry 2 --no-continue-on-error
        cat urls.txt | supacrawl batch --json
    """
    import asyncio

    # Read URL list
    if url_file == "-":
        raw_text = sys.stdin.read()
    else:
        path = Path(url_file)
        if not path.exists():
            click.echo(f"Error: URL file not found: {url_file}", err=True)
            raise SystemExit(1)
        raw_text = path.read_text(encoding="utf-8")

    urls = _parse_urls_from_text(raw_text)

    if not urls:
        click.echo("No URLs to scrape (input was empty or all lines were comments).", err=True)
        raise SystemExit(0)

    # Resolve headers
    resolved_headers: dict[str, str] | None = parse_headers_env()
    if headers:
        if resolved_headers is None:
            resolved_headers = {}
        for raw in headers:
            name, value = parse_header_string(raw)
            resolved_headers[name] = value

    formats_list = list(formats)

    # Validate: cannot use both directory output and JSON mode
    if output_dir and output_json:
        click.echo("Error: --output and --json are mutually exclusive.", err=True)
        raise SystemExit(1)

    click.echo(
        f"Scraping {len(urls)} URL(s) with concurrency={concurrency}, retry={retry}",
        err=True,
    )
    start_time = time.monotonic()

    async def run() -> BatchScrapeResult:
        from supacrawl.services.batch import run_batch_scrape
        from supacrawl.services.strategy_memory import StrategyStore
        from supacrawl.telemetry import MetricsSink

        result = await run_batch_scrape(
            urls=urls,
            formats=formats_list,
            timeout=timeout,
            max_age=max_age,
            concurrency=concurrency,
            retry=retry,
            continue_on_error=continue_on_error,
            headers=resolved_headers,
            proxy=proxy,
            engine=engine,
            stealth=stealth,
            strategy_store=StrategyStore.default(),
            telemetry=MetricsSink.default(),
        )
        return result

    batch_result: BatchScrapeResult | None
    try:
        batch_result = asyncio.run(run())
    except Exception as exc:
        click.echo(f"Error: batch scrape failed: {exc}", err=True)
        raise SystemExit(1) from exc

    if batch_result is None:
        click.echo("Error: batch scrape returned no result.", err=True)
        raise SystemExit(1)

    elapsed = time.monotonic() - start_time
    click.echo(
        f"Completed {batch_result.succeeded}/{len(urls)} succeeded, {batch_result.failed} failed in {elapsed:.1f}s",
        err=True,
    )

    if output_json:
        _emit_json(batch_result)
    elif output_dir:
        _write_directory(output_dir, batch_result, formats_list)
    else:
        # No output destination: print markdown to stdout for single-URL convenience
        # or a summary when multiple URLs were given.
        _emit_stdout(batch_result)

    # Exit 1 if any URL failed
    if batch_result.failed > 0:
        raise SystemExit(1)


def _emit_json(batch_result: object) -> None:
    """Print batch results as a JSON array to stdout.

    Args:
        batch_result: ``BatchScrapeResult`` from ``run_batch_scrape``.
    """
    from supacrawl.services.batch import BatchScrapeResult

    assert isinstance(batch_result, BatchScrapeResult)

    output: list[dict[str, Any]] = []
    for r in batch_result.results:
        entry: dict[str, Any] = {"url": r.url, "success": r.success, "attempts": r.attempts}
        if r.success and r.data is not None:
            entry["data"] = r.data.model_dump(exclude_none=True)
        if r.error:
            entry["error"] = r.error
        output.append(entry)

    # succeeded/failed counts and partial flag at top level
    payload = {
        "succeeded": batch_result.succeeded,
        "failed": batch_result.failed,
        "partial": batch_result.partial,
        "results": output,
    }
    click.echo(json.dumps(payload, indent=2))


def _write_directory(output_dir: Path, batch_result: object, formats: list[str]) -> None:
    """Write one file per URL plus a manifest into ``output_dir``.

    Args:
        output_dir: Target directory (created if absent).
        batch_result: ``BatchScrapeResult`` from ``run_batch_scrape``.
        formats: Output formats requested by the caller.
    """
    from supacrawl.services.batch import BatchScrapeResult

    assert isinstance(batch_result, BatchScrapeResult)

    output_dir.mkdir(parents=True, exist_ok=True)
    existing_slugs: set[str] = set()

    manifest_entries: list[dict[str, Any]] = []

    for r in batch_result.results:
        entry: dict[str, Any] = {"url": r.url, "success": r.success, "attempts": r.attempts}
        if r.error:
            entry["error"] = r.error

        if r.success and r.data is not None and r.data.data is not None:
            data = r.data.data
            files_written: list[str] = []

            if "markdown" in formats and data.markdown:
                out_path = _output_path_for_url(output_dir, r.url, ".md", existing_slugs)
                frontmatter = ""
                if data.metadata:
                    frontmatter = f"---\nsource_url: {r.url}\n"
                    if data.metadata.title:
                        frontmatter += f"title: {data.metadata.title}\n"
                    frontmatter += "---\n\n"
                out_path.write_text(frontmatter + data.markdown, encoding="utf-8")
                files_written.append(str(out_path))

            if "html" in formats and data.html:
                out_path = _output_path_for_url(output_dir, r.url, ".html", existing_slugs)
                out_path.write_text(data.html, encoding="utf-8")
                files_written.append(str(out_path))

            if "json" in formats:
                out_path = _output_path_for_url(output_dir, r.url, ".json", existing_slugs)
                out_path.write_text(
                    json.dumps(r.data.model_dump(exclude_none=True), indent=2),
                    encoding="utf-8",
                )
                files_written.append(str(out_path))

            entry["files"] = files_written

        manifest_entries.append(entry)

    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "succeeded": batch_result.succeeded,
                "failed": batch_result.failed,
                "partial": batch_result.partial,
                "urls": manifest_entries,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    click.echo(f"Wrote {output_dir}/manifest.json", err=True)


def _emit_stdout(batch_result: object) -> None:
    """Write batch results to stdout when no output destination is given.

    Prints each URL's markdown (or an error note) separated by a header line.

    Args:
        batch_result: ``BatchScrapeResult`` from ``run_batch_scrape``.
    """
    from supacrawl.services.batch import BatchScrapeResult

    assert isinstance(batch_result, BatchScrapeResult)

    for r in batch_result.results:
        click.echo(f"\n--- {r.url} ---")
        if r.success and r.data is not None and r.data.data is not None:
            if r.data.data.markdown:
                click.echo(r.data.data.markdown)
            else:
                click.echo("[no markdown content]")
        else:
            click.echo(f"[failed: {r.error}]", err=True)
