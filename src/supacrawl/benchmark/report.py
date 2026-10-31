"""Human-readable report rendering for benchmark runs.

Produces terminal-friendly summaries of a ``RunResult`` or a ``RunComparison``.
Uses ``rich`` (already a project dependency) for table formatting, rendered to
a plain string via ``Console(file=...)`` so callers can print or pipe the
output without a live terminal.
"""

from __future__ import annotations

import io

from rich.console import Console
from rich.table import Table

from supacrawl.benchmark.models import RunResult
from supacrawl.benchmark.store import RunComparison


def _console() -> Console:
    """Create a Rich Console that writes to a string buffer.

    Returns:
        ``Console`` with ``highlight=False`` and a fixed 100-column width so
        the output is readable in a standard terminal without reflowing.
    """
    return Console(file=io.StringIO(), width=100, highlight=False)


def render_run(run: RunResult) -> str:
    """Render a human-readable summary of a benchmark run.

    Includes overall quality, a per-category breakdown table, and the five
    worst scored cases.

    Args:
        run: The run result to summarise.

    Returns:
        Multi-line string suitable for printing to a terminal.
    """
    con = _console()
    agg = run.aggregate

    con.print(f"[bold]Run:[/bold] {run.run_id}")
    reader = f"{run.provider} {run.provider_version}" if run.provider_version else run.provider
    con.print(f"[bold]Suite:[/bold] {run.suite_name}   [bold]Reader:[/bold] {reader}")
    con.print(f"[bold]supacrawl:[/bold] {run.supacrawl_version}   [bold]SHA:[/bold] {run.git_sha or 'n/a'}")
    con.print(f"[bold]Started:[/bold] {run.started_at[:19].replace('T', ' ')} UTC   {run.platform or ''}")
    con.print(f"[bold]Reference renderer:[/bold] {'on' if run.reference_enabled else 'off'}")
    con.print()

    # Headline numbers
    quality_str = f"{agg.overall_quality:.1f}" if agg.overall_quality is not None else "n/a"
    con.print(f"[bold]Overall quality:[/bold] {quality_str} / 100")
    con.print(
        f"[bold]Success rate:[/bold] {agg.success_rate * 100:.1f}%  ({agg.total_cases} cases, {agg.scored_cases} scored)"
    )
    if agg.mean_latency_ms is not None and agg.median_latency_ms is not None:
        con.print(
            f"[bold]Latency:[/bold] median {agg.median_latency_ms / 1000:.1f} s, mean {agg.mean_latency_ms / 1000:.1f} s"
        )
    if agg.expect_hit is not None or agg.expect_absent_ok is not None:
        hit = f"{agg.expect_hit * 100:.0f}%" if agg.expect_hit is not None else "n/a"
        gone = f"{agg.expect_absent_ok * 100:.0f}%" if agg.expect_absent_ok is not None else "n/a"
        con.print(f"[bold]Content kept:[/bold] {hit}   [bold]Boilerplate gone:[/bold] {gone}")
    if agg.mean_approx_tokens is not None:
        con.print(f"[bold]Output:[/bold] {agg.mean_approx_tokens:,.0f} tokens per page (chars/4)")
    if run.peak_rss_mb is not None:
        con.print(f"[bold]Peak memory:[/bold] {run.peak_rss_mb:,.0f} MB (process tree)")
    if agg.search_queries:
        ok = f"{(agg.search_success_rate or 0) * 100:.0f}%"
        hit = f"{agg.search_expect_hit * 100:.0f}%" if agg.search_expect_hit is not None else "n/a"
        con.print(f"[bold]Search:[/bold] {agg.search_queries} queries, {ok} answered, expected domain in {hit}")
    con.print()

    # Per-category table
    cat_table = Table(title="By category", show_header=True, header_style="bold")
    cat_table.add_column("Category", style="cyan")
    cat_table.add_column("N", justify="right")
    cat_table.add_column("Scored", justify="right")
    cat_table.add_column("Quality", justify="right")
    cat_table.add_column("Success %", justify="right")

    categories = sorted({c.category for c in run.cases})
    for cat in categories:
        cat_cases = [c for c in run.cases if c.category == cat]
        scored = [c for c in cat_cases if c.scored and c.metrics.quality is not None]
        successes = sum(1 for c in cat_cases if c.metrics.success)
        q_str = f"{agg.by_category[cat]:.1f}" if cat in agg.by_category else "n/a"
        succ_str = f"{successes / len(cat_cases) * 100:.0f}%" if cat_cases else "n/a"
        cat_table.add_row(cat, str(len(cat_cases)), str(len(scored)), q_str, succ_str)

    con.print(cat_table)
    con.print()

    # Worst 5 scored cases
    scored_cases = [c for c in run.cases if c.scored and c.metrics.quality is not None]
    scored_cases.sort(key=lambda c: c.metrics.quality or 0.0)
    worst = scored_cases[:5]

    if worst:
        worst_table = Table(title="Worst 5 scored cases", show_header=True, header_style="bold")
        worst_table.add_column("Case ID", style="cyan", no_wrap=True)
        worst_table.add_column("Quality", justify="right")
        worst_table.add_column("F1", justify="right")
        worst_table.add_column("Expect hit", justify="right")
        worst_table.add_column("Success")

        for c in worst:
            m = c.metrics
            q = f"{m.quality:.1f}" if m.quality is not None else "n/a"
            f1 = f"{m.token_f1:.2f}" if m.token_f1 is not None else "n/a"
            eh = f"{m.expect_hit:.2f}" if m.expect_hit is not None else "n/a"
            worst_table.add_row(c.case_id, q, f1, eh, "yes" if m.success else "[red]no[/red]")

        con.print(worst_table)

    assert isinstance(con.file, io.StringIO)
    return con.file.getvalue()


def _pct(value: float | None) -> str:
    return f"{value * 100:.0f}%" if value is not None else "n/a"


def render_table(runs: list[RunResult]) -> str:
    """A markdown table, one row per run, for comparing readers side by side.

    Columns are the figures the decision needs: whether pages came back,
    whether the main content survived and the boilerplate went, how long a
    page took, how much a model would read, and how much memory the reader's
    process tree peaked at. Every run states its reader version.
    """
    header = (
        "| Reader | Pages | Content kept | Boilerplate gone | Median s | Mean s | Tokens/page | Peak MB | Search |\n"
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"
    )
    rows = []
    for run in runs:
        agg = run.aggregate
        reader = f"{run.provider} {run.provider_version}" if run.provider_version else run.provider
        search = (
            f"{_pct(agg.search_expect_hit)} of {agg.search_queries}"
            if agg.search_queries and agg.search_expect_hit is not None
            else ("failed" if agg.search_queries else "none")
        )
        rows.append(
            f"| {reader} | {_pct(agg.success_rate)} of {agg.total_cases} | {_pct(agg.expect_hit)} | "
            f"{_pct(agg.expect_absent_ok)} | "
            f"{(agg.median_latency_ms or 0) / 1000:.1f} | {(agg.mean_latency_ms or 0) / 1000:.1f} | "
            f"{agg.mean_approx_tokens or 0:,.0f} | {run.peak_rss_mb or 0:,.0f} | {search} |"
        )
    return "\n".join([header, *rows])


def render_comparison(cmp: RunComparison) -> str:
    """Render a human-readable comparison between two runs.

    Args:
        cmp: The comparison result from ``compare_runs``.

    Returns:
        Multi-line string suitable for printing to a terminal.
    """
    con = _console()

    con.print(f"[bold]Comparison:[/bold] {cmp.old_run_id}  →  {cmp.new_run_id}")
    con.print()

    if cmp.overall_quality_delta is not None:
        sign = "+" if cmp.overall_quality_delta >= 0 else ""
        colour = "green" if cmp.overall_quality_delta >= 0 else "red"
        con.print(f"[bold]Overall quality delta:[/bold] [{colour}]{sign}{cmp.overall_quality_delta:.1f}[/{colour}]")
    else:
        con.print("[bold]Overall quality delta:[/bold] n/a (one run has no scored cases)")
    con.print()

    if cmp.regressions:
        con.print(f"[bold red]Regressions (delta ≤ -5):[/bold red] {len(cmp.regressions)}")
        for cid in cmp.regressions:
            delta = cmp.case_deltas.get(cid, 0.0)
            con.print(f"  [red]{cid}[/red]  ({delta:+.1f})")
        con.print()

    if cmp.improvements:
        con.print(f"[bold green]Improvements (delta ≥ +5):[/bold green] {len(cmp.improvements)}")
        for cid in cmp.improvements:
            delta = cmp.case_deltas.get(cid, 0.0)
            con.print(f"  [green]{cid}[/green]  ({delta:+.1f})")
        con.print()

    if cmp.newly_failing:
        con.print("[bold red]Newly failing:[/bold red]")
        for cid in cmp.newly_failing:
            con.print(f"  [red]{cid}[/red]")
        con.print()

    if cmp.worst_cases:
        con.print("[bold]Worst cases in new run:[/bold]")
        for cid in cmp.worst_cases:
            delta_str = ""
            if cid in cmp.case_deltas:
                d = cmp.case_deltas[cid]
                sign = "+" if d >= 0 else ""
                delta_str = f"  (delta {sign}{d:.1f})"
            con.print(f"  {cid}{delta_str}")

    if not cmp.regressions and not cmp.improvements and not cmp.newly_failing:
        con.print("[green]No significant changes between runs.[/green]")

    assert isinstance(con.file, io.StringIO)
    return con.file.getvalue()
