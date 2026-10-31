"""Persistence and comparison layer for benchmark run results.

Writes run results to a flat file layout under a base directory:

- ``runs/<run_id>.json``    — full ``RunResult`` verbatim.
- ``metrics.jsonl``         — one flat row per case per run (dashboard-ingestible).
- ``index.jsonl``           — one row per run (headline numbers for trend views).

The JSONL files use the tidy long-form format: every row is self-contained so a
dashboard can ingest incrementally without reprocessing old runs.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from supacrawl.benchmark.models import CaseResult, RunResult

DEFAULT_BASE_DIR = Path(".supacrawl/bench")


class RunComparison(BaseModel):
    """Comparison between two benchmark runs.

    Attributes:
        old_run_id: ID of the baseline run.
        new_run_id: ID of the current run.
        overall_quality_delta: Signed change in overall quality score.
        case_deltas: Per-case quality delta keyed by case_id.
        regressions: Cases where quality dropped by 5 or more points.
        improvements: Cases where quality improved by 5 or more points.
        newly_failing: Case IDs that were successful before but failed now.
        worst_cases: The five lowest-quality scored cases in the new run.
    """

    old_run_id: str
    new_run_id: str
    overall_quality_delta: float | None = None
    case_deltas: dict[str, float] = {}
    regressions: list[str] = []
    improvements: list[str] = []
    newly_failing: list[str] = []
    worst_cases: list[str] = []


def write_run(run: RunResult, base_dir: Path) -> Path:
    """Persist a run result and update the rolling JSONL indexes.

    Creates ``base_dir`` and its children when they do not exist. The full
    result is written atomically via a write-then-rename approach is NOT used
    here for simplicity; the JSONL files are append-only and naturally
    idempotent for re-runs with distinct run_ids.

    Args:
        run: The completed run result.
        base_dir: Root benchmark directory (defaults to ``.supacrawl/bench``).

    Returns:
        Path to the written ``runs/<run_id>.json`` file.
    """
    runs_dir = base_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)

    # Full run JSON
    run_path = runs_dir / f"{run.run_id}.json"
    run_path.write_text(run.model_dump_json(indent=2), encoding="utf-8")

    # Tidy per-case rows
    metrics_path = base_dir / "metrics.jsonl"
    with metrics_path.open("a", encoding="utf-8") as fh:
        for case in run.cases:
            row = _case_row(run, case)
            fh.write(json.dumps(row) + "\n")

    # Run-level index row
    index_path = base_dir / "index.jsonl"
    with index_path.open("a", encoding="utf-8") as fh:
        index_row = {
            "run_id": run.run_id,
            "started_at": run.started_at,
            "provider": run.provider,
            "overall_quality": run.aggregate.overall_quality,
            "success_rate": run.aggregate.success_rate,
            "scored_cases": run.aggregate.scored_cases,
            "by_category": run.aggregate.by_category,
            "median_latency_ms": run.aggregate.median_latency_ms,
            "peak_rss_mb": run.peak_rss_mb,
        }
        fh.write(json.dumps(index_row) + "\n")

    return run_path


def _case_row(run: RunResult, case: CaseResult) -> dict:
    """Build the flat metrics row for a single case in a run.

    Args:
        run: The parent run.
        case: The individual case result.

    Returns:
        Dict with run metadata and all scalar case metrics.
    """
    m = case.metrics
    return {
        "run_id": run.run_id,
        "started_at": run.started_at,
        "supacrawl_version": run.supacrawl_version,
        "git_sha": run.git_sha,
        "case_id": case.case_id,
        "category": case.category,
        "difficulty": case.difficulty,
        "scored": case.scored,
        "success": m.success,
        "status_code": m.status_code,
        "latency_ms": m.latency_ms,
        "markdown_chars": m.markdown_chars,
        "markdown_words": m.markdown_words,
        "approx_tokens": m.approx_tokens,
        "reference_chars": m.reference_chars,
        "reference_words": m.reference_words,
        "char_coverage": m.char_coverage,
        "token_f1": m.token_f1,
        "rouge_l": m.rouge_l,
        "noise": m.noise,
        "reference_degenerate": m.reference_degenerate,
        "link_density": m.link_density,
        "headings": m.headings,
        "code_blocks": m.code_blocks,
        "tables": m.tables,
        "images": m.images,
        "links": m.links,
        "json_ld_found": m.json_ld_found,
        "expect_hit": m.expect_hit,
        "expect_absent_ok": m.expect_absent_ok,
        "word_spacing": m.word_spacing,
        "judge_score": m.judge_score,
        "quality": m.quality,
    }


def list_runs(base_dir: Path) -> list[str]:
    """Return all known run IDs, newest first.

    Args:
        base_dir: Root benchmark directory.

    Returns:
        Sorted list of run ID strings (descending by name, which sorts by
        timestamp since the ID starts with ``YYYYMMDDTHHMMSS``).
    """
    runs_dir = base_dir / "runs"
    if not runs_dir.exists():
        return []
    ids = [p.stem for p in sorted(runs_dir.glob("*.json"), reverse=True)]
    return ids


def load_run(base_dir: Path, run_id: str) -> RunResult:
    """Load a previously stored run by its ID.

    Args:
        base_dir: Root benchmark directory.
        run_id: The run ID to load.

    Returns:
        ``RunResult`` parsed from the stored JSON.

    Raises:
        FileNotFoundError: If the run does not exist on disk.
        ValueError: If the stored JSON is malformed.
    """
    run_path = base_dir / "runs" / f"{run_id}.json"
    if not run_path.exists():
        raise FileNotFoundError(f"Run not found: {run_path}")
    return RunResult.model_validate_json(run_path.read_text(encoding="utf-8"))


def latest_run_per_provider(base_dir: Path) -> list[RunResult]:
    """The newest stored run of each reader, in the order the readers were first seen (newest first)."""
    seen: dict[str, RunResult] = {}
    for run_id in list_runs(base_dir):
        run = load_run(base_dir, run_id)
        seen.setdefault(run.provider, run)
    return list(seen.values())


def latest_runs(base_dir: Path, n: int = 2) -> list[RunResult]:
    """Return the ``n`` most recent stored runs, newest first.

    Args:
        base_dir: Root benchmark directory.
        n: Number of runs to return.

    Returns:
        List of ``RunResult`` objects, up to ``n``, newest first.
    """
    ids = list_runs(base_dir)[:n]
    return [load_run(base_dir, run_id) for run_id in ids]


def compare_runs(old: RunResult, new: RunResult) -> RunComparison:
    """Compare two runs and report regressions, improvements, and worst cases.

    Quality delta is signed (positive = improvement, negative = regression).
    Regressions are cases where quality dropped by 5 or more points.
    Improvements are cases where quality improved by 5 or more points.
    Newly failing cases were successful in ``old`` but are not in ``new``.

    Args:
        old: The baseline run.
        new: The current run to compare against the baseline.

    Returns:
        ``RunComparison`` with all deltas and lists.
    """
    # Build lookup maps by case_id
    old_by_id = {c.case_id: c for c in old.cases}
    new_by_id = {c.case_id: c for c in new.cases}

    overall_delta: float | None = None
    if old.aggregate.overall_quality is not None and new.aggregate.overall_quality is not None:
        overall_delta = round(new.aggregate.overall_quality - old.aggregate.overall_quality, 1)

    case_deltas: dict[str, float] = {}
    regressions: list[str] = []
    improvements: list[str] = []
    newly_failing: list[str] = []

    for case_id, new_case in new_by_id.items():
        old_case = old_by_id.get(case_id)

        # Newly failing
        if old_case and old_case.metrics.success and not new_case.metrics.success:
            newly_failing.append(case_id)

        # Quality delta
        old_q = old_case.metrics.quality if old_case else None
        new_q = new_case.metrics.quality
        if old_q is not None and new_q is not None:
            delta = round(new_q - old_q, 1)
            case_deltas[case_id] = delta
            if delta <= -5:
                regressions.append(case_id)
            elif delta >= 5:
                improvements.append(case_id)

    # Worst 5 scored cases in the new run by quality (ascending)
    scored_new = [c for c in new.cases if c.scored and c.metrics.quality is not None]
    scored_new.sort(key=lambda c: c.metrics.quality or 0.0)
    worst_cases = [c.case_id for c in scored_new[:5]]

    return RunComparison(
        old_run_id=old.run_id,
        new_run_id=new.run_id,
        overall_quality_delta=overall_delta,
        case_deltas=case_deltas,
        regressions=regressions,
        improvements=improvements,
        newly_failing=newly_failing,
        worst_cases=worst_cases,
    )
