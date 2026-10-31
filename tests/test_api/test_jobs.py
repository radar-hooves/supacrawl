"""Tests for the async job store."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from supacrawl.api.jobs import (
    Job,
    JobStatus,
    JobStore,
    MaxJobsExceededError,
)


@pytest.fixture()
def store() -> JobStore:
    """Return a fresh JobStore with default settings."""
    return JobStore()


# --- create / get --------------------------------------------------------


def test_create_job_returns_job(store: JobStore) -> None:
    job = store.create_job(total=5)
    assert isinstance(job, Job)
    assert job.status == JobStatus.scraping
    assert job.total == 5
    assert job.completed == 0
    assert job.data == []
    assert job.expires_at is not None


def test_create_job_generates_unique_ids(store: JobStore) -> None:
    ids = {store.create_job().id for _ in range(3)}
    assert len(ids) == 3


def test_get_job_returns_dict(store: JobStore) -> None:
    job = store.create_job(total=1)
    result = store.get_job(job.id)
    assert result is not None
    assert result["id"] == job.id
    assert result["status"] == "scraping"
    assert result["total"] == 1
    assert result["next"] is None


def test_get_nonexistent_job_returns_none(store: JobStore) -> None:
    assert store.get_job("nonexistent") is None


# --- update / complete ---------------------------------------------------


def test_update_job_status(store: JobStore) -> None:
    job = store.create_job(total=2)
    updated = store.update_job(
        job.id,
        status=JobStatus.completed,
        completed=2,
        data=[{"url": "https://example.com"}],
    )
    assert updated is not None
    assert updated.status == JobStatus.completed
    assert updated.completed == 2
    assert len(updated.data) == 1


def test_update_nonexistent_returns_none(store: JobStore) -> None:
    assert store.update_job("missing", status=JobStatus.failed) is None


def test_update_job_error(store: JobStore) -> None:
    job = store.create_job()
    store.update_job(job.id, status=JobStatus.failed, error="Timed out")
    result = store.get_job(job.id)
    assert result is not None
    assert result["status"] == "failed"
    assert result["error"] == "Timed out"


# --- cancel ---------------------------------------------------------------


def test_cancel_job(store: JobStore) -> None:
    job = store.create_job()
    cancelled = store.cancel_job(job.id)
    assert cancelled is not None
    assert cancelled.status == JobStatus.cancelled


def test_cancel_nonexistent_returns_none(store: JobStore) -> None:
    assert store.cancel_job("nope") is None


def test_cancel_cancels_asyncio_task(store: JobStore) -> None:
    """Cancelling a job should also cancel its associated asyncio task."""

    async def _run() -> None:
        task: asyncio.Task[None] = asyncio.get_event_loop().create_task(asyncio.sleep(999))
        job = store.create_job()
        store.set_task(job.id, task)
        store.cancel_job(job.id)
        # Allow one iteration for the cancellation to propagate.
        await asyncio.sleep(0)
        assert task.cancelled()

    asyncio.run(_run())


# --- expiration -----------------------------------------------------------


def test_cleanup_expired_removes_old_jobs(store: JobStore) -> None:
    job = store.create_job()
    # Force expiry in the past.
    store._jobs[job.id].expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    removed = store.cleanup_expired()
    assert removed == 1
    assert store.get_job(job.id) is None


def test_cleanup_preserves_non_expired(store: JobStore) -> None:
    job = store.create_job()
    removed = store.cleanup_expired()
    assert removed == 0
    assert store.get_job(job.id) is not None


def test_an_expired_job_is_dropped_when_the_next_job_is_created(store: JobStore) -> None:
    old = store.create_job()
    store._jobs[old.id].expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    store.create_job()
    assert store.get_job(old.id) is None


# --- max jobs enforcement -------------------------------------------------


def test_max_jobs_enforcement(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPACRAWL_API_MAX_JOBS", "2")
    store = JobStore()
    store.create_job()
    store.create_job()
    with pytest.raises(MaxJobsExceededError):
        store.create_job()


def test_completed_jobs_do_not_count_toward_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPACRAWL_API_MAX_JOBS", "1")
    store = JobStore()
    first = store.create_job()
    store.update_job(first.id, status=JobStatus.completed)
    # Should succeed because the first job is no longer active.
    second = store.create_job()
    assert second.id != first.id


def test_cancelled_jobs_do_not_count_toward_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPACRAWL_API_MAX_JOBS", "1")
    store = JobStore()
    first = store.create_job()
    store.cancel_job(first.id)
    second = store.create_job()
    assert second.id != first.id


# --- pagination -----------------------------------------------------------


def test_pagination_basic(store: JobStore) -> None:
    job = store.create_job(total=5)
    store.update_job(job.id, data=list(range(25)))

    page1 = store.get_job(job.id, offset=0, page_size=10, base_url="/v1/crawl/abc")
    assert page1 is not None
    assert len(page1["data"]) == 10
    assert page1["next"] == "/v1/crawl/abc?offset=10"


def test_pagination_last_page(store: JobStore) -> None:
    job = store.create_job()
    store.update_job(job.id, data=list(range(5)))

    page = store.get_job(job.id, offset=0, page_size=10, base_url="/v1/crawl/abc")
    assert page is not None
    assert len(page["data"]) == 5
    assert page["next"] is None


def test_pagination_middle_page(store: JobStore) -> None:
    job = store.create_job()
    store.update_job(job.id, data=list(range(30)))

    page = store.get_job(job.id, offset=10, page_size=10, base_url="/v1/crawl/abc")
    assert page is not None
    assert page["data"] == list(range(10, 20))
    assert page["next"] == "/v1/crawl/abc?offset=20"


def test_pagination_no_base_url(store: JobStore) -> None:
    """When no base_url is provided, next should always be None."""
    job = store.create_job()
    store.update_job(job.id, data=list(range(20)))
    page = store.get_job(job.id, offset=0, page_size=5)
    assert page is not None
    assert page["next"] is None


# --- TTL config -----------------------------------------------------------


def test_custom_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPACRAWL_API_JOB_TTL", "60")
    store = JobStore()
    job = store.create_job()
    assert job.expires_at is not None
    delta = job.expires_at - job.created_at
    assert 59 <= delta.total_seconds() <= 61
