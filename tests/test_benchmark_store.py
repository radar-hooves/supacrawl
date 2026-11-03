"""Unit tests for benchmark/store.py.

Uses tmp_path; no network or browser access.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from supacrawl.benchmark.models import (
    CaseResult,
    RunAggregate,
    RunResult,
)
from supacrawl.benchmark.store import (
    compare_runs,
    latest_runs,
    list_runs,
    load_run,
    write_run,
)
from tests.helpers import make_case_result as _make_case_result


def _make_run(
    run_id: str,
    cases: list[CaseResult],
    *,
    overall_quality: float | None = 80.0,
) -> RunResult:
    """Build a synthetic RunResult for testing.

    Args:
        run_id: Unique run identifier.
        cases: List of case results.
        overall_quality: Aggregate quality score.

    Returns:
        A ``RunResult`` ready for store operations.
    """
    return RunResult(
        run_id=run_id,
        started_at="2026-06-14T08:30:00+00:00",
        finished_at="2026-06-14T08:35:00+00:00",
        supacrawl_version="2026.6.0",
        git_sha="abc1234",
        suite_name="test",
        cases=cases,
        aggregate=RunAggregate(
            overall_quality=overall_quality,
            total_cases=len(cases),
            scored_cases=sum(1 for c in cases if c.scored),
            success_rate=sum(1 for c in cases if c.metrics.success) / len(cases) if cases else 0.0,
            by_category={},
        ),
    )


@pytest.mark.unit
def test_write_and_load_run(tmp_path: Path) -> None:
    case = _make_case_result("static-example")
    run = _make_run("20260614T083000-abc1234", [case])

    path = write_run(run, tmp_path)
    assert path.exists()

    loaded = load_run(tmp_path, "20260614T083000-abc1234")
    assert loaded.run_id == run.run_id
    assert len(loaded.cases) == 1
    assert loaded.cases[0].case_id == "static-example"
    assert loaded.aggregate.overall_quality == pytest.approx(80.0)


@pytest.mark.unit
def test_write_creates_metrics_jsonl(tmp_path: Path) -> None:
    cases = [
        _make_case_result("case-a", quality=70.0, category="static"),
        _make_case_result("case-b", quality=90.0, category="article"),
    ]
    run = _make_run("20260614T083000-test1", cases)
    write_run(run, tmp_path)

    metrics_path = tmp_path / "metrics.jsonl"
    assert metrics_path.exists()

    rows = [json.loads(line) for line in metrics_path.read_text().splitlines() if line.strip()]
    assert len(rows) == 2
    case_ids = {r["case_id"] for r in rows}
    assert case_ids == {"case-a", "case-b"}

    # Every row must include run-level metadata
    for row in rows:
        assert row["run_id"] == "20260614T083000-test1"
        assert row["supacrawl_version"] == "2026.6.0"
        assert row["git_sha"] == "abc1234"
        assert "quality" in row
        assert "latency_ms" in row


@pytest.mark.unit
def test_write_creates_index_jsonl(tmp_path: Path) -> None:
    case = _make_case_result("static-example")
    run = _make_run("20260614T083000-idx1", [case])
    write_run(run, tmp_path)

    index_path = tmp_path / "index.jsonl"
    assert index_path.exists()

    rows = [json.loads(line) for line in index_path.read_text().splitlines() if line.strip()]
    assert len(rows) == 1
    row = rows[0]
    assert row["run_id"] == "20260614T083000-idx1"
    assert row["overall_quality"] == pytest.approx(80.0)
    assert "success_rate" in row
    assert "scored_cases" in row


@pytest.mark.unit
def test_list_runs_newest_first(tmp_path: Path) -> None:
    for run_id in ["20260614T080000-aaa", "20260614T090000-bbb", "20260614T070000-ccc"]:
        case = _make_case_result("case-x")
        run = _make_run(run_id, [case])
        write_run(run, tmp_path)

    ids = list_runs(tmp_path)
    # Newest (lexicographically largest) should be first
    assert ids[0] == "20260614T090000-bbb"
    assert ids[-1] == "20260614T070000-ccc"


@pytest.mark.unit
def test_latest_runs_returns_n(tmp_path: Path) -> None:
    for i in range(4):
        run_id = f"2026061{i}T000000-run{i}"
        run = _make_run(run_id, [_make_case_result("c")])
        write_run(run, tmp_path)

    runs = latest_runs(tmp_path, n=2)
    assert len(runs) == 2


@pytest.mark.unit
def test_load_run_not_found_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_run(tmp_path, "nonexistent-run-id")


@pytest.mark.unit
def test_compare_runs_reports_regression(tmp_path: Path) -> None:
    old_cases = [_make_case_result("case-a", quality=80.0), _make_case_result("case-b", quality=90.0)]
    new_cases = [
        _make_case_result("case-a", quality=70.0),  # -10 → regression
        _make_case_result("case-b", quality=92.0),  # +2 → not significant
    ]
    old_run = _make_run("20260614T080000-old", old_cases, overall_quality=85.0)
    new_run = _make_run("20260614T090000-new", new_cases, overall_quality=81.0)

    cmp = compare_runs(old_run, new_run)

    assert "case-a" in cmp.regressions
    assert "case-b" not in cmp.regressions
    assert cmp.overall_quality_delta == pytest.approx(-4.0)


@pytest.mark.unit
def test_compare_runs_reports_improvement(tmp_path: Path) -> None:
    old_cases = [_make_case_result("case-x", quality=60.0)]
    new_cases = [_make_case_result("case-x", quality=75.0)]  # +15 → improvement
    old_run = _make_run("20260614T080000-old2", old_cases, overall_quality=60.0)
    new_run = _make_run("20260614T090000-new2", new_cases, overall_quality=75.0)

    cmp = compare_runs(old_run, new_run)

    assert "case-x" in cmp.improvements
    assert "case-x" not in cmp.regressions


@pytest.mark.unit
def test_compare_runs_newly_failing(tmp_path: Path) -> None:
    old_cases = [_make_case_result("case-z", quality=85.0, success=True)]
    new_cases = [_make_case_result("case-z", quality=0.0, success=False)]
    old_run = _make_run("20260614T080000-old3", old_cases, overall_quality=85.0)
    new_run = _make_run("20260614T090000-new3", new_cases, overall_quality=0.0)

    cmp = compare_runs(old_run, new_run)

    assert "case-z" in cmp.newly_failing


@pytest.mark.unit
def test_compare_runs_worst_cases(tmp_path: Path) -> None:
    cases = [_make_case_result(f"case-{i}", quality=float(i * 10)) for i in range(1, 8)]
    run_a = _make_run("20260614T080000-wca", cases[:3], overall_quality=20.0)
    run_b = _make_run("20260614T090000-wcb", cases, overall_quality=40.0)

    cmp = compare_runs(run_a, run_b)

    # worst_cases should have at most 5 entries, lowest quality first
    assert len(cmp.worst_cases) <= 5
    # The first entry should be the case with quality=10 (case-1)
    assert cmp.worst_cases[0] == "case-1"


@pytest.mark.unit
def test_metrics_jsonl_appends_across_runs(tmp_path: Path) -> None:
    for i in range(3):
        run = _make_run(f"20260614T08000{i}-r{i}", [_make_case_result(f"case-{i}")])
        write_run(run, tmp_path)

    metrics_path = tmp_path / "metrics.jsonl"
    rows = [json.loads(line) for line in metrics_path.read_text().splitlines() if line.strip()]
    # One row per case per run
    assert len(rows) == 3
