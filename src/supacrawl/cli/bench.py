"""Benchmark subcommands — measure and track scrape quality over time."""

from __future__ import annotations

import asyncio
from pathlib import Path

import click

from supacrawl.benchmark.providers import PROVIDERS
from supacrawl.benchmark.store import DEFAULT_BASE_DIR
from supacrawl.cli._common import app


@app.group("bench", help="Scrape-quality benchmark commands.")
def bench() -> None:
    """Benchmark subcommands for measuring scrape quality over time."""


@bench.command("run", help="Run the benchmark against a suite of test cases.")
@click.option(
    "--suite",
    "suite_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to a YAML suite file. Defaults to the packaged corpus.",
)
@click.option(
    "--provider",
    type=click.Choice(sorted(PROVIDERS)),
    default="supacrawl",
    show_default=True,
    help="The reader to measure. Third-party readers need their package installed (see the bench dependency group).",
)
@click.option(
    "--reference/--no-reference",
    default=True,
    show_default=True,
    help="Capture a browser reference for each HTML page. Off, cases score on anchors and structure alone and "
    "the run's time and memory are the reader's own.",
)
@click.option(
    "--searches/--no-searches",
    default=True,
    show_default=True,
    help="Run the suite's search queries through the reader's search surface, where it has one.",
)
@click.option(
    "--judge/--no-judge",
    default=False,
    show_default=True,
    help="Enable LLM-as-judge scoring (requires LLM configuration).",
)
@click.option(
    "--concurrency",
    type=int,
    default=3,
    show_default=True,
    help="Maximum concurrent cases.",
)
@click.option(
    "--only",
    "only",
    multiple=True,
    metavar="ID_OR_CATEGORY",
    help="Restrict to these case IDs or categories (repeatable).",
)
@click.option(
    "--base-dir",
    "base_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=DEFAULT_BASE_DIR,
    show_default=True,
    help="Base directory for benchmark artefacts.",
)
def bench_run(
    suite_path: Path | None,
    provider: str,
    reference: bool,
    searches: bool,
    judge: bool,
    concurrency: int,
    only: tuple[str, ...],
    base_dir: Path,
) -> None:
    """Run the benchmark suite and persist results.

    Scrapes each case in the suite with the chosen reader, captures a browser
    reference for HTML pages, scores every metric, runs the suite's search
    queries, and writes results under BASE_DIR. Prints a summary to stdout and
    the result path to stderr.

    Examples:
        supacrawl bench run
        supacrawl bench run --provider trafilatura --no-reference
        supacrawl bench run --only static --only article
        supacrawl bench run --suite ./my-suite.yaml --judge --concurrency 5
    """
    from supacrawl.benchmark.corpus import load_default_suite, load_suite
    from supacrawl.benchmark.report import render_run
    from supacrawl.benchmark.runner import run_benchmark
    from supacrawl.benchmark.store import write_run

    suite = load_suite(suite_path) if suite_path else load_default_suite()

    click.echo(
        f"Running benchmark: {suite.name} ({len(suite.cases)} cases, {len(suite.queries)} queries, "
        f"reader={provider}, concurrency={concurrency})",
        err=True,
    )

    try:
        result = asyncio.run(
            run_benchmark(
                suite,
                provider=provider,
                judge=judge,
                concurrency=concurrency,
                base_dir=base_dir,
                only=list(only) if only else None,
                reference=reference,
                searches=searches,
            )
        )
    except Exception as exc:
        click.echo(f"Error: benchmark run failed: {exc}", err=True)
        raise SystemExit(1) from exc

    try:
        run_path = write_run(result, base_dir)
        click.echo(f"Results written to: {run_path}", err=True)
    except Exception as exc:
        click.echo(f"Warning: failed to persist results: {exc}", err=True)

    click.echo(render_run(result))


@bench.command("compare", help="Compare two benchmark runs.")
@click.argument("run_a", required=False)
@click.argument("run_b", required=False)
@click.option(
    "--base-dir",
    "base_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=DEFAULT_BASE_DIR,
    show_default=True,
    help="Base directory for benchmark artefacts.",
)
def bench_compare(run_a: str | None, run_b: str | None, base_dir: Path) -> None:
    """Compare two runs (defaults to the latest two stored runs).

    RUN_A is the baseline; RUN_B is the newer run. When omitted, the two most
    recent stored runs are compared automatically.

    Examples:
        supacrawl bench compare
        supacrawl bench compare 20260614T083012-abc1234 20260614T120000-def5678
    """
    from supacrawl.benchmark.report import render_comparison
    from supacrawl.benchmark.store import compare_runs, latest_runs, load_run

    try:
        if run_a and run_b:
            old = load_run(base_dir, run_a)
            new = load_run(base_dir, run_b)
        else:
            runs = latest_runs(base_dir, n=2)
            if len(runs) < 2:
                click.echo("Error: fewer than two stored runs found. Run the benchmark at least twice.", err=True)
                raise SystemExit(1)
            # latest_runs returns newest-first; old is index 1, new is index 0
            new, old = runs[0], runs[1]
    except FileNotFoundError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc

    cmp = compare_runs(old, new)
    click.echo(render_comparison(cmp))


@bench.command("table", help="A markdown table comparing readers, one row per run.")
@click.argument("run_ids", nargs=-1)
@click.option(
    "--base-dir",
    "base_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=DEFAULT_BASE_DIR,
    show_default=True,
    help="Base directory for benchmark artefacts.",
)
def bench_table(run_ids: tuple[str, ...], base_dir: Path) -> None:
    """Render RUN_IDS side by side; with none given, the newest run of each reader.

    Examples:
        supacrawl bench table
        supacrawl bench table 20260614T083012-supacrawl-abc1234 20260614T090000-trafilatura-abc1234
    """
    from supacrawl.benchmark.report import render_table
    from supacrawl.benchmark.store import latest_run_per_provider, load_run

    try:
        runs = [load_run(base_dir, rid) for rid in run_ids] if run_ids else latest_run_per_provider(base_dir)
    except FileNotFoundError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc
    if not runs:
        click.echo("No benchmark runs found.", err=True)
        raise SystemExit(1)
    click.echo(render_table(runs))


@bench.command("list", help="List stored benchmark runs.")
@click.option(
    "--base-dir",
    "base_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=DEFAULT_BASE_DIR,
    show_default=True,
    help="Base directory for benchmark artefacts.",
)
def bench_list(base_dir: Path) -> None:
    """List past runs newest first with their headline quality score.

    Examples:
        supacrawl bench list
        supacrawl bench list --base-dir /tmp/bench
    """
    from supacrawl.benchmark.store import list_runs, load_run

    run_ids = list_runs(base_dir)
    if not run_ids:
        click.echo("No benchmark runs found.")
        return

    click.echo(f"{'Run ID':<48} {'Date':<20} {'Reader':<12} {'Quality':>8}")
    click.echo("-" * 92)
    for run_id in run_ids:
        try:
            run = load_run(base_dir, run_id)
            date_str = run.started_at[:19].replace("T", " ")
            q = run.aggregate.overall_quality
            q_str = f"{q:.1f}" if q is not None else "n/a"
            click.echo(f"{run_id:<48} {date_str:<20} {run.provider:<12} {q_str:>8}")
        except Exception as exc:
            click.echo(f"{run_id:<40} [error: {exc}]")


@bench.command("show", help="Show the summary for a specific stored run.")
@click.argument("run_id")
@click.option(
    "--base-dir",
    "base_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=DEFAULT_BASE_DIR,
    show_default=True,
    help="Base directory for benchmark artefacts.",
)
def bench_show(run_id: str, base_dir: Path) -> None:
    """Print the run summary for RUN_ID.

    Examples:
        supacrawl bench show 20260614T083012-abc1234
    """
    from supacrawl.benchmark.report import render_run
    from supacrawl.benchmark.store import load_run

    try:
        run = load_run(base_dir, run_id)
    except FileNotFoundError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc

    click.echo(render_run(run))
