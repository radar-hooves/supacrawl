"""Field telemetry commands (#137).

supacrawl records a small event per scrape and search so quality, escalation, and
usage can be tracked over time. These commands inspect and bound that local event
log. The log is the seam a separate dashboard would consume; this is the
terminal-native view of the same data. Each event is also emitted as a log
record (the household telemetry contract) shipped over OTLP or to stdout/stderr
by the process's own ``api_common.telemetry.configure()`` call.
"""

import json
from datetime import UTC

import click

from supacrawl.cli._common import app
from supacrawl.telemetry import MetricsReader


@app.group()
def metrics() -> None:
    """Inspect the local field telemetry log.

    supacrawl appends one event per scrape/search to ~/.supacrawl/metrics/events.jsonl
    (on by default for the CLI and MCP server; disable with SUPACRAWL_METRICS=0,
    log full URLs with SUPACRAWL_METRICS_FULL_URL=1). It is local, append-only, and
    the data foundation for a future dashboard.

    Examples:
        supacrawl metrics summary            # headline quality/usage rollup
        supacrawl metrics summary --days 7   # last 7 days only
        supacrawl metrics tail -n 20         # the most recent events
        supacrawl metrics path               # where the log lives
        supacrawl metrics prune --keep-days 90
    """


@metrics.command("path")
def metrics_path() -> None:
    """Print the path to the event log."""
    click.echo(MetricsReader().path)


@metrics.command("summary")
@click.option("--days", type=int, default=None, help="Only summarise events from the last N days.")
def metrics_summary(days: int | None) -> None:
    """Show a headline rollup: scrape/search counts, success and escalation rates,
    verdict mix, and the busiest domains."""
    from datetime import datetime, timedelta

    since = datetime.now(UTC) - timedelta(days=days) if days else None
    s = MetricsReader().summary(since=since)
    if not s["scrapes"] and not s["searches"]:
        click.echo("No telemetry recorded yet. Scrape or search and supacrawl will start tracking quality over time.")
        return
    window = f" (last {days}d)" if days else ""
    click.echo(f"Field telemetry{window}:")
    click.echo(f"  scrapes:         {s['scrapes']}")
    click.echo(f"  searches:        {s['searches']}")
    if s["success_rate"] is not None:
        click.echo(f"  success rate:    {s['success_rate']:.0%}")
    if s["escalation_rate"] is not None:
        click.echo(f"  escalation rate: {s['escalation_rate']:.0%}")
    if s["by_verdict"]:
        click.echo("  verdicts:        " + ", ".join(f"{k}={v}" for k, v in s["by_verdict"].items()))
    if s["top_domains"]:
        click.echo("  top domains:")
        for domain, count in s["top_domains"].items():
            click.echo(f"    {domain}: {count}")


@metrics.command("tail")
@click.option("-n", "--count", type=int, default=10, show_default=True, help="Number of recent events to show.")
@click.option(
    "--kind",
    type=click.Choice(["scrape", "search"], case_sensitive=False),
    default=None,
    help="Only show events of this kind.",
)
def metrics_tail(count: int, kind: str | None) -> None:
    """Print the most recent events as JSON lines."""
    events = list(MetricsReader().events(kind=kind))
    if not events:
        click.echo("No telemetry recorded yet.")
        return
    for event in events[-count:]:
        click.echo(json.dumps(event))


@metrics.command("prune")
@click.option("--keep-days", type=int, default=None, help="Keep only events newer than this many days.")
@click.option("--keep-last", type=int, default=None, help="Keep only the most recent N events.")
def metrics_prune(keep_days: int | None, keep_last: int | None) -> None:
    """Bound the event log by age and/or count."""
    if keep_days is None and keep_last is None:
        click.echo("Specify --keep-days and/or --keep-last.", err=True)
        raise SystemExit(1)
    removed = MetricsReader().prune(keep_days=keep_days, keep_last=keep_last)
    click.echo(f"Pruned {removed} event{'s' if removed != 1 else ''}.")
