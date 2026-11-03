"""Field telemetry sink (#137): persist per-scrape and per-search events.

The runtime quality signal (#128) and per-domain memory (#130) are computed on
every request but, until now, discarded. This module persists a small, durable
event per scrape/search so quality, escalation, and usage can be tracked *over
time* — the data foundation for a future, separate observability/control-plane
GUI.

Design seam (deliberate, for the coming front end):
- **Append-only JSONL** under ``~/.supacrawl/metrics/`` — the idiomatic
  zero-infrastructure emission format for a local-first tool (cf. dbt's run
  artefacts on disk; Prometheus/Grafana keep the data layer and the dashboard
  layer separate on purpose). No daemon, no DB, crash-safe append.
- **A versioned event schema** (every line carries ``schema``) so a downstream
  dashboard never breaks when the shape evolves — the single most important
  forward-compatibility guard.
- **Privacy-first**: only the registrable domain is logged by default (the
  operator scrapes finance/trading sites); full URLs and search-query text are
  opt-in. Search queries are otherwise reduced to a non-reversible hash.
- **A read/aggregate API** (``MetricsReader``) the dashboard backend imports,
  rather than re-parsing the file — the clean contract between the CLI that
  emits and any GUI that consumes. The GUI is a separate tool; this writes only.

The sink is injected into the services (like the strategy store) and enabled by
default only at the CLI and MCP boundaries; embedding the library directly emits
nothing unless a sink is passed. Disable everywhere with ``SUPACRAWL_METRICS=0``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

if TYPE_CHECKING:
    from supacrawl.models import ScrapeResult

LOGGER = logging.getLogger(__name__)

# The named logger every field-telemetry event is also emitted through (the
# household telemetry contract, platform/telemetry.md): one log record per
# event, fields in `extra`, shipped over OTLP or to stdout/stderr by whatever
# api_common.telemetry.configure() call the process made at startup. This
# module never pushes to a log store's own API or carries a credential for one.
_TELEMETRY_LOGGER = logging.getLogger("supacrawl.telemetry")

# Bump only on a breaking change to the event shape; readers branch on it.
SCHEMA_VERSION = 1


def _registrable_domain(url: str) -> str | None:
    """Registrable-domain key (hostname sans leading www); None when absent."""
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return None
    return host[4:] if host.startswith("www.") else host


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _query_hash(query: str) -> str:
    """Non-reversible short hash of a search query, for distinct-count without
    storing the (potentially sensitive) query text."""
    return hashlib.sha256(query.encode("utf-8")).hexdigest()[:12]


class MetricsSink:
    """Append-only writer of scrape/search telemetry events.

    One JSON object per line under ``metrics_dir/events.jsonl`` — the durable
    local record (#137). Cheap enough to open-append-close per event (a scrape
    takes seconds; the write is bytes), which keeps concurrent CLI + MCP
    writers safe via the OS's atomic ``O_APPEND``.

    Each event is also emitted as one log record on ``supacrawl.telemetry``
    (the household telemetry contract): the process's own
    ``api_common.telemetry.configure()`` call, made once at startup, decides
    where that record goes — OTLP when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set,
    stdout/stderr otherwise. This module never pushes to a log store's own API
    and carries no credential for one.
    """

    DEFAULT_METRICS_DIR = Path.home() / ".supacrawl" / "metrics"

    def __init__(
        self,
        metrics_dir: Path | None = None,
        *,
        full_url: bool = False,
    ) -> None:
        """Initialise the sink.

        Args:
            metrics_dir: Directory for the event log. Defaults to
                ``~/.supacrawl/metrics`` or ``SUPACRAWL_METRICS_DIR``.
            full_url: When True, log full URLs and full search-query text instead
                of just the registrable domain / a query hash.
        """
        if metrics_dir is not None:
            self.metrics_dir = metrics_dir
        else:
            env_dir = os.environ.get("SUPACRAWL_METRICS_DIR")
            self.metrics_dir = Path(env_dir) if env_dir else self.DEFAULT_METRICS_DIR
        self.path = self.metrics_dir / "events.jsonl"
        self._full_url = full_url
        self.metrics_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def default(cls) -> "MetricsSink | None":
        """Return a default sink, or None when telemetry is disabled.

        ``SUPACRAWL_METRICS=0`` (or ``false``/``off``/``no``) disables it, as does
        ``metrics = false`` in the config store (env wins over the store);
        ``SUPACRAWL_METRICS_FULL_URL=1`` / ``metrics_full_url = true`` opts into
        full URLs/queries. Used by the CLI and MCP wiring so field telemetry is on
        out of the box for the primary entry points while remaining opt-out and local.
        """
        from supacrawl.config import load_config

        config = load_config()
        if not config.metrics:
            return None
        try:
            return cls(full_url=config.metrics_full_url)
        except OSError as exc:  # unwritable home — degrade silently to no telemetry
            LOGGER.debug("Telemetry unavailable (%s); not recording events", exc)
            return None

    def _append(self, event: dict[str, Any]) -> None:
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, separators=(",", ":")) + "\n")
        except OSError as exc:
            LOGGER.debug("Failed to write telemetry event: %s", exc)
        _TELEMETRY_LOGGER.info(event["kind"], extra=event)

    def record_scrape(self, *, url: str, result: "ScrapeResult", latency_ms: int) -> None:
        """Append one scrape event derived from the final result.

        Args:
            url: The scraped URL.
            result: The final ScrapeResult (post-escalation).
            latency_ms: End-to-end wall time for the scrape, in milliseconds.
        """
        quality = result.quality
        meta = result.data.metadata if result.data is not None else None
        event: dict[str, Any] = {
            "schema": SCHEMA_VERSION,
            "kind": "scrape",
            "ts": _now_iso(),
            "domain": _registrable_domain(url),
            "success": result.success,
            "verdict": quality.verdict.value if quality is not None else None,
            "score": quality.score if quality is not None else None,
            "attempts": quality.attempts if quality is not None else None,
            "escalated": quality.escalated if quality is not None else None,
            "word_count": meta.word_count if meta is not None else None,
            "status_code": meta.status_code if meta is not None else None,
            "cache_hit": meta.cache_hit if meta is not None else False,
            "latency_ms": latency_ms,
        }
        if self._full_url:
            event["url"] = url
        self._append(event)

    def record_search(self, *, query: str, provider: str, result_count: int, success: bool, latency_ms: int) -> None:
        """Append one search event.

        The query text is stored only when full logging is enabled; otherwise a
        short non-reversible hash captures distinctness without the text.
        """
        event: dict[str, Any] = {
            "schema": SCHEMA_VERSION,
            "kind": "search",
            "ts": _now_iso(),
            "provider": provider,
            "result_count": result_count,
            "success": success,
            "latency_ms": latency_ms,
            "query_hash": _query_hash(query),
        }
        if self._full_url:
            event["query"] = query
        self._append(event)


class MetricsReader:
    """Read and aggregate telemetry events.

    The clean contract for any downstream consumer (the future dashboard backend
    imports this rather than re-parsing the JSONL). All methods tolerate a missing
    or partially-corrupt log: a bad line is skipped, never fatal.
    """

    def __init__(self, metrics_dir: Path | None = None) -> None:
        if metrics_dir is not None:
            self.metrics_dir = metrics_dir
        else:
            env_dir = os.environ.get("SUPACRAWL_METRICS_DIR")
            self.metrics_dir = Path(env_dir) if env_dir else MetricsSink.DEFAULT_METRICS_DIR
        self.path = self.metrics_dir / "events.jsonl"

    def events(self, *, kind: str | None = None, since: datetime | None = None) -> Iterator[dict[str, Any]]:
        """Yield events, optionally filtered by ``kind`` ("scrape"/"search") and a
        ``since`` cutoff (inclusive). Malformed lines are skipped."""
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if kind is not None and event.get("kind") != kind:
                    continue
                if since is not None:
                    ts = event.get("ts")
                    if not ts or _parse_ts(ts) < since:
                        continue
                yield event

    def summary(self, *, since: datetime | None = None) -> dict[str, Any]:
        """Aggregate the log into headline numbers a dashboard would surface."""
        scrapes = list(self.events(kind="scrape", since=since))
        searches = list(self.events(kind="search", since=since))

        by_verdict: dict[str, int] = {}
        by_domain: dict[str, int] = {}
        escalated = 0
        succeeded = 0
        for ev in scrapes:
            verdict = ev.get("verdict") or "unknown"
            by_verdict[verdict] = by_verdict.get(verdict, 0) + 1
            domain = ev.get("domain") or "unknown"
            by_domain[domain] = by_domain.get(domain, 0) + 1
            if ev.get("escalated"):
                escalated += 1
            if ev.get("success"):
                succeeded += 1

        n = len(scrapes)
        return {
            "scrapes": n,
            "searches": len(searches),
            "success_rate": round(succeeded / n, 3) if n else None,
            "escalation_rate": round(escalated / n, 3) if n else None,
            "by_verdict": dict(sorted(by_verdict.items(), key=lambda kv: -kv[1])),
            "top_domains": dict(sorted(by_domain.items(), key=lambda kv: -kv[1])[:10]),
        }

    def prune(self, *, keep_days: int | None = None, keep_last: int | None = None) -> int:
        """Bound the log to a recent window. Returns the number of events removed.

        Args:
            keep_days: Keep only events newer than this many days.
            keep_last: Keep only the most recent N events (applied after keep_days).
        """
        if not self.path.exists():
            return 0
        kept: list[dict[str, Any]] = list(self.events())
        original = len(kept)
        if keep_days is not None:
            cutoff = datetime.now(UTC) - timedelta(days=keep_days)
            kept = [e for e in kept if e.get("ts") and _parse_ts(e["ts"]) >= cutoff]
        if keep_last is not None and len(kept) > keep_last:
            kept = kept[-keep_last:]
        removed = original - len(kept)
        if removed:
            tmp = self.path.with_suffix(".jsonl.tmp")
            tmp.write_text("".join(json.dumps(e, separators=(",", ":")) + "\n" for e in kept), encoding="utf-8")
            os.replace(tmp, self.path)
        return removed


def _parse_ts(ts: str) -> datetime:
    """Parse an ISO timestamp, returning the UTC epoch on failure (sorts first)."""
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return datetime.fromtimestamp(0, tz=UTC)
