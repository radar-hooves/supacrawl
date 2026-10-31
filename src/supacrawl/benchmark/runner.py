"""Benchmark runner — orchestrates a full benchmark run over a suite.

Accepts a ``BenchSuite``, drives each case through the chosen reader and
optionally the browser reference renderer, scores every metric, runs the
suite's search queries through the reader's search surface when it has one,
and returns a ``RunResult`` with per-case detail and a run-level aggregate.

Concurrency is throttled via an ``asyncio.Semaphore`` because browser pages are
memory-heavy; the default is intentionally conservative. Peak memory is the
whole process tree (the reader's browser included), sampled through the run.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import statistics
import subprocess
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path
from urllib.parse import urlparse

from supacrawl.benchmark.judge import judge_case
from supacrawl.benchmark.metrics import (
    char_coverage,
    composite_quality,
    reference_is_degenerate,
    rouge_l,
    token_prf,
)
from supacrawl.benchmark.models import (
    CHARS_PER_TOKEN,
    BenchCase,
    BenchQuery,
    BenchSuite,
    CaseMetrics,
    CaseResult,
    RunAggregate,
    RunResult,
    SearchResult,
)
from supacrawl.benchmark.providers import make_provider, make_search_provider
from supacrawl.benchmark.providers.base import ProviderOutput, ScraperProvider, SearchProvider
from supacrawl.benchmark.reference import ReferenceCapture, ReferenceRenderer
from supacrawl.quality import (
    count_structure,
    link_density,
    strip_markdown,
    substring_absent_rate,
    substring_hit_rate,
    tokenize,
    word_spacing,
)
from supacrawl.services.renderers import process_tree_rss_bytes

LOGGER = logging.getLogger(__name__)

_RSS_SAMPLE_S = 0.5


def _git_sha() -> str | None:
    """Return the short HEAD SHA for the current repo, or None on failure.

    Returns:
        8-character SHA string, or ``None`` if git is unavailable.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _run_id(sha: str | None, provider: str) -> str:
    """Build a compact run ID from the current UTC time, the reader and the git SHA.

    Returns:
        String like ``"20260614T083012-supacrawl-a1b2c3d4"``.
    """
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return f"{ts}-{provider}-{sha}" if sha else f"{ts}-{provider}"


def _provider_version(name: str) -> str | None:
    """The installed version of the package behind a reader; None for a hosted service."""
    try:
        return pkg_version(name)
    except PackageNotFoundError:
        return None


def _platform() -> str:
    """The machine without its name: OS, architecture and core count."""
    return f"{platform.system()} {platform.machine()}, {os.cpu_count()} cores"


def _score_case(
    *,
    case: BenchCase,
    output: ProviderOutput,
    reference: ReferenceCapture | None,
    judge_score: float | None,
    judge_rationale: str | None,
) -> CaseMetrics:
    """Compute all metrics for a single case result.

    Args:
        case: The benchmark case definition.
        output: Normalised scraper output for this case.
        reference: Browser ground-truth capture, or ``None`` for PDFs/failures.
        judge_score: LLM judge score in ``[0, 100]``, or ``None``.
        judge_rationale: LLM judge rationale string, or ``None``.

    Returns:
        Fully populated ``CaseMetrics``.
    """
    markdown = output.markdown or ""
    plain_text = strip_markdown(markdown)

    md_words = len(tokenize(markdown, markdown=True))
    md_chars = len(plain_text)

    # Anchor-based metrics — always available when the case has anchors
    expect_hit = substring_hit_rate(markdown, case.expect)
    expect_absent_ok = substring_absent_rate(markdown, case.expect_absent)

    # Structure in the produced markdown
    structure = count_structure(markdown)
    ld = link_density(structure["links"], md_words)

    # Inter-word spacing sanity (reference-free): catches fused-word PDF defects.
    spacing = word_spacing(markdown)

    # Reference-based metrics — only when the reference was captured and
    # the scrape succeeded; for PDFs and failed references these stay None.
    char_cov: float | None = None
    token_f1: float | None = None
    rouge_l_val: float | None = None
    noise: float | None = None
    ref_chars: int | None = None
    ref_words: int | None = None

    if output.success and reference and not reference.error and case.content_type != "pdf":
        ref_text = reference.main_text or ""
        ref_chars = len(ref_text)
        ref_words = len(tokenize(ref_text))

        char_cov = char_coverage(md_chars, ref_chars)

        ext_tokens = tokenize(plain_text)
        gold_tokens = tokenize(ref_text)
        _, _, f1 = token_prf(ext_tokens, gold_tokens)
        token_f1 = f1
        rouge_l_val = rouge_l(ext_tokens, gold_tokens)

        # Noise = fraction of extracted tokens absent from the reference.
        # We compute 1 - precision from token_prf (precision IS the non-noise).
        precision, _, _ = token_prf(ext_tokens, gold_tokens)
        noise = 1.0 - precision if ext_tokens else None

    # When the reference renderer under-captured (a shell), its comparison
    # metrics would punish a correct, fuller extraction for content it missed.
    # Discard them and let the case score on the reference-free signals
    # (char coverage, anchors, structure, spacing).
    reference_degenerate = reference_is_degenerate(md_words, ref_words)
    if reference_degenerate:
        token_f1 = None
        rouge_l_val = None
        noise = None

    quality = composite_quality(
        success=output.success,
        char_coverage_value=char_cov,
        token_f1=token_f1,
        noise=noise,
        expect_hit=expect_hit,
        expect_absent_ok=expect_absent_ok,
        link_density_value=ld if output.success else None,
        word_spacing_value=spacing,
    )

    return CaseMetrics(
        success=output.success,
        status_code=output.status_code,
        error=output.error,
        latency_ms=output.latency_ms,
        markdown_chars=md_chars,
        markdown_words=md_words,
        approx_tokens=len(markdown) // CHARS_PER_TOKEN,
        reference_chars=ref_chars,
        reference_words=ref_words,
        char_coverage=char_cov,
        token_f1=token_f1,
        rouge_l=rouge_l_val,
        noise=noise,
        reference_degenerate=reference_degenerate,
        link_density=ld if output.success else None,
        headings=structure["headings"],
        code_blocks=structure["code_blocks"],
        tables=structure["tables"],
        images=structure["images"],
        links=structure["links"],
        json_ld_found=output.json_ld_found,
        expect_hit=expect_hit,
        expect_absent_ok=expect_absent_ok,
        word_spacing=spacing,
        judge_score=judge_score,
        judge_rationale=judge_rationale,
        quality=quality,
    )


def _save_artifacts(
    *,
    case: BenchCase,
    output: ProviderOutput,
    reference: ReferenceCapture | None,
    run_dir: Path,
) -> dict[str, str]:
    """Persist per-case artefacts to disk and return their relative paths.

    Args:
        case: The benchmark case.
        output: Scraper output for this case.
        reference: Browser reference capture, or ``None``.
        run_dir: Directory for this run's artefacts.

    Returns:
        Dict mapping artefact role to path relative to ``run_dir``.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    artifacts: dict[str, str] = {}

    if output.markdown:
        md_path = run_dir / f"{case.id}.md"
        md_path.write_text(output.markdown, encoding="utf-8")
        artifacts["markdown"] = md_path.name

    if reference:
        if reference.main_text:
            ref_path = run_dir / f"{case.id}.reference.txt"
            ref_path.write_text(reference.main_text, encoding="utf-8")
            artifacts["reference_text"] = ref_path.name

        if reference.screenshot_b64:
            import base64

            ss_path = run_dir / f"{case.id}.screenshot.jpg"
            ss_path.write_bytes(base64.b64decode(reference.screenshot_b64))
            artifacts["screenshot"] = ss_path.name

    return artifacts


async def _scrape_pdf_with_retry(
    *,
    case: BenchCase,
    provider: ScraperProvider,
    pdf_semaphore: asyncio.Semaphore,
) -> ProviderOutput:
    """Scrape a PDF case, retrying once in isolation if the first attempt yields 0 words.

    Large PDFs occasionally truncate to 0 words when scraped concurrently with
    an active browser reference renderer — a resource-contention failure, not a
    genuine extraction failure.  A single isolated retry (holding ``pdf_semaphore``
    for the full duration so no other cases run alongside) recovers reliably.
    A genuinely empty PDF is not masked: a 0-word result after the retry is
    still recorded and logged as a warning.

    Args:
        case: The PDF benchmark case.
        provider: Scraper provider to use.
        pdf_semaphore: Single-slot semaphore that serialises PDF scrapes so they
            do not contend with concurrent browser captures.

    Returns:
        ``ProviderOutput`` from the best attempt.
    """
    async with pdf_semaphore:
        output = await provider.scrape(case.url, content_type=case.content_type)

        if output.success and not _has_words(output):
            LOGGER.warning(
                "PDF case %s yielded 0 words on first attempt — retrying in isolation",
                case.id,
            )
            retry = await provider.scrape(case.url, content_type=case.content_type)
            if _has_words(retry):
                return retry
            LOGGER.warning(
                "PDF case %s still 0 words after isolated retry — recording as-is",
                case.id,
            )
            return retry

        return output


def _has_words(output: ProviderOutput) -> bool:
    """Return True when ``output`` contains at least one word.

    Args:
        output: Provider output to inspect.

    Returns:
        ``True`` when the markdown is non-empty after stripping whitespace.
    """
    return bool((output.markdown or "").split())


async def _run_case(
    *,
    case: BenchCase,
    provider: ScraperProvider,
    renderer: ReferenceRenderer | None,
    judge: bool,
    semaphore: asyncio.Semaphore,
    pdf_semaphore: asyncio.Semaphore,
    run_dir: Path,
) -> CaseResult:
    """Run the benchmark for a single case.

    HTML cases acquire ``semaphore`` (shared across all case types) and run
    reference capture concurrently.  PDF cases instead acquire ``pdf_semaphore``
    (a single-slot semaphore) so large PDF extractions do not race against
    active browser pages and cause spurious 0-word truncation.

    Args:
        case: The benchmark case to run.
        provider: Scraper provider to use.
        renderer: Browser reference renderer, or ``None`` (skips reference).
        judge: Whether to run the LLM judge.
        semaphore: Concurrency guard for HTML cases and reference captures.
        pdf_semaphore: Single-slot concurrency guard for PDF cases.
        run_dir: Directory for this run's artefacts.

    Returns:
        ``CaseResult`` with metrics and artefact paths.
    """
    output: ProviderOutput
    reference: ReferenceCapture | None

    if case.content_type == "pdf":
        LOGGER.info("Running case: %s (%s)", case.id, case.url)
        output = await _scrape_pdf_with_retry(
            case=case,
            provider=provider,
            pdf_semaphore=pdf_semaphore,
        )
        reference = None
    else:
        async with semaphore:
            LOGGER.info("Running case: %s (%s)", case.id, case.url)

            output = await provider.scrape(case.url, content_type=case.content_type)

            reference = None
            if renderer and case.content_type == "html":
                reference = await renderer.capture(case.url)

    judge_score: float | None = None
    judge_rationale: str | None = None
    if judge and output.success:
        ref_text = reference.main_text if (reference and not reference.error) else None
        judge_score, judge_rationale = await judge_case(
            case=case,
            markdown=output.markdown,
            reference_text=ref_text,
        )

    metrics = _score_case(
        case=case,
        output=output,
        reference=reference,
        judge_score=judge_score,
        judge_rationale=judge_rationale,
    )

    artifacts = _save_artifacts(
        case=case,
        output=output,
        reference=reference,
        run_dir=run_dir,
    )

    return CaseResult(
        case_id=case.id,
        category=case.category,
        url=case.url,
        difficulty=case.difficulty,
        scored=case.is_scored,
        metrics=metrics,
        artifacts=artifacts,
    )


def _domain_hit(urls: list[str], expect_domains: list[str]) -> bool | None:
    """Whether any result URL is on one of the expected domains; None when the query expects none."""
    if not expect_domains:
        return None
    hosts = [(urlparse(u).hostname or "").lower() for u in urls]
    return any(host == d or host.endswith("." + d) for host in hosts for d in expect_domains)


async def _run_query(query: BenchQuery, provider: SearchProvider) -> SearchResult:
    """Run one search query through the reader's search surface."""
    LOGGER.info("Running query: %s (%s)", query.id, query.query)
    output = await provider.search(query.query, limit=5)
    return SearchResult(
        query_id=query.id,
        query=query.query,
        success=output.success,
        latency_ms=output.latency_ms,
        results=len(output.urls),
        expect_hit=_domain_hit(output.urls, query.expect_domains) if output.success else None,
        error=output.error,
    )


def _build_aggregate(results: list[CaseResult], searches: list[SearchResult] | None = None) -> RunAggregate:
    """Build the run-level aggregate from all case results.

    Args:
        results: All case results from the run.
        searches: The run's search results, if it had a search provider.

    Returns:
        ``RunAggregate`` with overall and per-category rollups.
    """
    total = len(results)
    succeeded = [r for r in results if r.metrics.success]
    success_rate = len(succeeded) / total if total else 0.0

    latencies = [r.metrics.latency_ms for r in results if r.metrics.latency_ms is not None]
    mean_latency = sum(latencies) / len(latencies) if latencies else None
    median_latency = statistics.median(latencies) if latencies else None

    scored = [r for r in results if r.scored and r.metrics.quality is not None]
    scored_count = len(scored)
    overall_quality: float | None = None
    if scored:
        scored_qualities: list[float] = [r.metrics.quality for r in scored]  # type: ignore[misc]
        overall_quality = round(sum(scored_qualities) / scored_count, 1)

    # Per-category means over scored cases only
    by_category: dict[str, float] = {}
    categories: set[str] = {r.category for r in results}
    for cat in categories:
        cat_scored = [r for r in scored if r.category == cat]
        if cat_scored:
            cat_qualities: list[float] = [r.metrics.quality for r in cat_scored]  # type: ignore[misc]
            by_category[cat] = round(sum(cat_qualities) / len(cat_scored), 1)

    def _mean(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 3) if values else None

    hits = [r.metrics.expect_hit for r in succeeded if r.metrics.expect_hit is not None]
    absent = [r.metrics.expect_absent_ok for r in succeeded if r.metrics.expect_absent_ok is not None]
    tokens = [float(r.metrics.approx_tokens) for r in succeeded]

    searches = searches or []
    search_ok = [s for s in searches if s.success]
    judged = [s for s in search_ok if s.expect_hit is not None]

    return RunAggregate(
        overall_quality=overall_quality,
        by_category=by_category,
        total_cases=total,
        scored_cases=scored_count,
        success_rate=success_rate,
        mean_latency_ms=mean_latency,
        median_latency_ms=median_latency,
        mean_approx_tokens=_mean(tokens),
        expect_hit=_mean(hits),
        expect_absent_ok=_mean(absent),
        search_queries=len(searches),
        search_success_rate=(len(search_ok) / len(searches)) if searches else None,
        search_expect_hit=_mean([1.0 if s.expect_hit else 0.0 for s in judged]),
    )


async def _sample_peak_rss(peak: list[int]) -> None:
    """Keep ``peak[0]`` at the largest process-tree RSS seen, until cancelled."""
    while True:
        rss = process_tree_rss_bytes()
        if rss is not None and rss > peak[0]:
            peak[0] = rss
        await asyncio.sleep(_RSS_SAMPLE_S)


async def run_benchmark(
    suite: BenchSuite,
    *,
    provider: str = "supacrawl",
    judge: bool = False,
    concurrency: int = 3,
    base_dir: Path,
    only: list[str] | None = None,
    reference: bool = True,
    searches: bool = True,
) -> RunResult:
    """Run the full benchmark over ``suite`` and return results.

    Cases are filtered by ``only`` (matching case id OR category, case-
    insensitive) when provided. The browser reference renderer is constructed
    once and shared across all HTML cases.

    Args:
        suite: The benchmark suite to run.
        provider: The reader to drive, by name (``supacrawl``, ``trafilatura``, ``jina``, ...).
        judge: Whether to invoke the LLM judge on each scored case.
        concurrency: Maximum number of cases running in parallel.
        base_dir: Root directory for artefact storage; artefacts land in
            ``base_dir/runs/<run_id>/<case_id>.*``.
        only: Optional list of case IDs or category names to restrict the run.
            When ``None``, all cases are run.
        reference: Whether to capture the browser reference for HTML cases.
            Off, cases score on their anchors, structure and spacing alone, and
            the run's memory and time are the reader's own.
        searches: Whether to run the suite's queries through the reader's
            search surface (skipped for a reader without one).

    Returns:
        ``RunResult`` with all case results and the run aggregate.
    """
    started_at = datetime.now(UTC).isoformat()
    sha = _git_sha()
    run_id = _run_id(sha, provider)

    try:
        supacrawl_version = pkg_version("supacrawl")
    except Exception:
        supacrawl_version = "unknown"

    # Filter cases
    cases = suite.cases
    if only:
        only_lower = {o.lower() for o in only}
        cases = [c for c in cases if c.id.lower() in only_lower or c.category.lower() in only_lower]

    run_dir = base_dir / "runs" / run_id
    semaphore = asyncio.Semaphore(concurrency)
    # Single-slot semaphore for PDF cases: ensures large PDF scrapes run one
    # at a time and do not contend with concurrent browser reference captures.
    pdf_semaphore = asyncio.Semaphore(1)

    reader = make_provider(provider)
    search_reader = make_search_provider(provider) if searches and suite.queries else None
    peak = [process_tree_rss_bytes() or 0]
    sampler = asyncio.ensure_future(_sample_peak_rss(peak))
    search_results: list[SearchResult] = []
    try:

        async def _cases(renderer: ReferenceRenderer | None) -> list[CaseResult]:
            tasks = [
                _run_case(
                    case=case,
                    provider=reader,
                    renderer=renderer,
                    judge=judge,
                    semaphore=semaphore,
                    pdf_semaphore=pdf_semaphore,
                    run_dir=run_dir,
                )
                for case in cases
            ]
            return list(await asyncio.gather(*tasks))

        if reference:
            async with ReferenceRenderer() as renderer:
                results = await _cases(renderer)
        else:
            results = await _cases(None)
        if search_reader is not None:
            for query in suite.queries:
                search_results.append(await _run_query(query, search_reader))
    finally:
        sampler.cancel()
        await reader.aclose()
        if search_reader is not None:
            await search_reader.aclose()

    aggregate = _build_aggregate(results, search_results)

    finished_at = datetime.now(UTC).isoformat()

    return RunResult(
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        supacrawl_version=supacrawl_version,
        git_sha=sha,
        provider=reader.name,
        provider_version=supacrawl_version if provider == "supacrawl" else _provider_version(provider),
        judge_enabled=judge,
        reference_enabled=reference,
        peak_rss_mb=round(peak[0] / 1_048_576, 1) if peak[0] else None,
        platform=_platform(),
        suite_name=suite.name,
        cases=results,
        searches=search_results,
        aggregate=aggregate,
    )
