"""Async job store for tracking long-running API operations."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger("supacrawl.api.jobs")

DEFAULT_MAX_JOBS = 3
DEFAULT_JOB_TTL = 86_400  # 24 hours in seconds
DEFAULT_PAGE_SIZE = 10


class JobStatus(str, Enum):
    """Possible states for an async job."""

    scraping = "scraping"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class Job(BaseModel):
    """Internal representation of an async job."""

    id: str
    status: JobStatus = JobStatus.scraping
    total: int = 0
    completed: int = 0
    data: list[Any] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None
    error: str | None = None


class MaxJobsExceededError(Exception):
    """Raised when the maximum number of concurrent jobs is exceeded."""


class JobStore:
    """In-memory store for async jobs.

    The store tracks active jobs, enforces a concurrency limit, and drops a
    job past its TTL when the next one is created.

    Configuration via environment variables:
        ``SUPACRAWL_API_MAX_JOBS`` - maximum concurrent active jobs (default 3).
        ``SUPACRAWL_API_JOB_TTL`` - job TTL in seconds (default 86400).
    """

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._max_jobs = int(os.environ.get("SUPACRAWL_API_MAX_JOBS", DEFAULT_MAX_JOBS))
        self._ttl = int(os.environ.get("SUPACRAWL_API_JOB_TTL", DEFAULT_JOB_TTL))

    def _active_count(self) -> int:
        """Count jobs that are still in progress."""
        return sum(1 for j in self._jobs.values() if j.status == JobStatus.scraping)

    def create_job(self, total: int = 0) -> Job:
        """Create a new job, returning the ``Job`` instance.

        Raises ``MaxJobsExceededError`` if the active job limit is reached.
        """
        self.cleanup_expired()
        if self._active_count() >= self._max_jobs:
            raise MaxJobsExceededError(f"Maximum concurrent jobs ({self._max_jobs}) exceeded. Try again later.")

        job_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc)
        job = Job(
            id=job_id,
            total=total,
            created_at=now,
            expires_at=now + timedelta(seconds=self._ttl),
        )
        self._jobs[job_id] = job
        logger.info("Created job %s (total=%d)", job_id, total)
        return job

    def get_job(
        self,
        job_id: str,
        offset: int = 0,
        page_size: int = DEFAULT_PAGE_SIZE,
        base_url: str | None = None,
    ) -> dict[str, Any] | None:
        """Return a job's status with paginated data.

        Returns ``None`` if the job does not exist.

        The returned dict includes a ``next`` key with a URL for the next
        page, or ``None`` if there are no more results.
        """
        job = self._jobs.get(job_id)
        if job is None:
            return None

        page = job.data[offset : offset + page_size]
        has_more = offset + page_size < len(job.data)

        next_url: str | None = None
        if has_more and base_url is not None:
            next_url = f"{base_url}?offset={offset + page_size}"

        return {
            "id": job.id,
            "status": job.status.value,
            "total": job.total,
            "completed": job.completed,
            "data": page,
            "created_at": job.created_at.isoformat(),
            "expires_at": job.expires_at.isoformat() if job.expires_at else None,
            "error": job.error,
            "next": next_url,
        }

    def update_job(
        self,
        job_id: str,
        *,
        status: JobStatus | None = None,
        completed: int | None = None,
        total: int | None = None,
        data: list[Any] | None = None,
        error: str | None = None,
    ) -> Job | None:
        """Update fields on an existing job. Returns ``None`` if not found."""
        job = self._jobs.get(job_id)
        if job is None:
            return None

        if status is not None:
            job.status = status
        if completed is not None:
            job.completed = completed
        if total is not None:
            job.total = total
        if data is not None:
            job.data = data
        if error is not None:
            job.error = error

        return job

    def cancel_job(self, job_id: str) -> Job | None:
        """Cancel a job and its associated asyncio task if present.

        Returns the cancelled ``Job``, or ``None`` if not found.
        """
        job = self._jobs.get(job_id)
        if job is None:
            return None

        job.status = JobStatus.cancelled

        task = self._tasks.pop(job_id, None)
        if task is not None and not task.done():
            task.cancel()
            logger.info("Cancelled asyncio task for job %s", job_id)

        logger.info("Job %s cancelled", job_id)
        return job

    def set_task(self, job_id: str, task: asyncio.Task[Any]) -> None:
        """Associate an asyncio task with a job for cancellation support."""
        self._tasks[job_id] = task

    def cleanup_expired(self) -> int:
        """Remove jobs past their ``expires_at`` timestamp.

        Returns the number of removed jobs.
        """
        now = datetime.now(timezone.utc)
        expired_ids = [jid for jid, job in self._jobs.items() if job.expires_at is not None and job.expires_at <= now]
        for jid in expired_ids:
            del self._jobs[jid]
            self._tasks.pop(jid, None)

        if expired_ids:
            logger.info("Cleaned up %d expired job(s)", len(expired_ids))
        return len(expired_ids)
