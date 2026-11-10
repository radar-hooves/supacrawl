"""Crawl endpoints; async job lifecycle with background processing."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, Request

from supacrawl.api.auth import get_api_key
from supacrawl.api.dependencies import get_crawl_service
from supacrawl.api.jobs import JobStatus, JobStore, MaxJobsExceededError
from supacrawl.api.models.common import ErrorResponse
from supacrawl.api.models.crawl import (
    CrawlCreateResponse,
    CrawlRequest,
    CrawlStatusResponse,
)
from supacrawl.api.routers.scrape import _scrape_result_to_response
from supacrawl.models import ScrapeData
from supacrawl.services import CrawlService

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_job_store(request: Request) -> JobStore:
    """Retrieve the ``JobStore`` from application state."""
    store: JobStore = request.app.state.job_store
    return store


def _scrape_data_to_dict(data: ScrapeData) -> dict[str, Any]:
    """Convert ``ScrapeData`` to the v2-compatible camelCase dict.

    Re-uses the scrape router's response builder to ensure consistent
    serialisation across endpoints.
    """
    from supacrawl.models import ScrapeResult

    result = ScrapeResult(success=True, data=data)
    response = _scrape_result_to_response(result)
    if response.data is None:
        return {}
    return response.data.model_dump(by_alias=True, exclude_none=True)


def _build_crawl_kwargs(req: CrawlRequest) -> dict[str, Any]:
    """Translate a v2 crawl request into kwargs for ``CrawlService.crawl()``."""
    kwargs: dict[str, Any] = {
        "url": req.url,
        "limit": req.limit,
        "max_depth": req.max_depth,
        "include_patterns": req.include_patterns,
        "exclude_patterns": req.exclude_patterns,
        "allow_external_links": req.allow_external_links,
        "concurrency": req.concurrency,
        "respect_robots": not req.ignore_robots_txt,
        "request_delay": req.delay,
    }

    if req.scrape_options and req.scrape_options.formats:
        kwargs["formats"] = req.scrape_options.formats

    return kwargs


async def _run_crawl(
    job_id: str,
    job_store: JobStore,
    crawl_service: CrawlService,
    kwargs: dict[str, Any],
) -> None:
    """Background task that iterates the crawl generator and updates the job."""
    try:
        async for event in crawl_service.crawl(**kwargs):
            job = job_store._jobs.get(job_id)
            if job is None or job.status == JobStatus.cancelled:
                logger.info("Job %s cancelled; stopping crawl", job_id)
                return

            if event.type == "page" and event.data is not None:
                page_dict = _scrape_data_to_dict(event.data)
                job.data.append(page_dict)
                job.completed = len(job.data)
                if event.total > 0:
                    job.total = event.total

            elif event.type == "progress":
                if event.total > 0:
                    job.total = event.total
                job.completed = event.completed

            elif event.type == "complete":
                job_store.update_job(
                    job_id,
                    status=JobStatus.completed,
                    total=len(job.data),
                    completed=len(job.data),
                )
                logger.info("Job %s completed with %d pages", job_id, len(job.data))
                return

            elif event.type == "error":
                job_store.update_job(
                    job_id,
                    status=JobStatus.failed,
                    error=event.error or "Unknown crawl error",
                )
                logger.error("Job %s failed: %s", job_id, event.error)
                return

        # Generator exhausted without explicit complete event
        job = job_store._jobs.get(job_id)
        if job is not None and job.status == JobStatus.scraping:
            job_store.update_job(
                job_id,
                status=JobStatus.completed,
                total=len(job.data),
                completed=len(job.data),
            )

    except asyncio.CancelledError:
        logger.info("Job %s task cancelled", job_id)
        raise

    except Exception:
        logger.exception("Unexpected error in crawl job %s", job_id)
        job_store.update_job(
            job_id,
            status=JobStatus.failed,
            error="Internal error during crawl",
        )


@router.post("/crawl")
async def crawl_create(
    req: CrawlRequest,
    request: Request,
    crawl_service: CrawlService = Depends(get_crawl_service),
    _api_key: str | None = Depends(get_api_key),
) -> CrawlCreateResponse | ErrorResponse:
    """Start an async crawl job and return its ID."""
    job_store = _get_job_store(request)

    try:
        job = job_store.create_job(total=0)
    except MaxJobsExceededError as exc:
        return ErrorResponse(error=str(exc))

    kwargs = _build_crawl_kwargs(req)
    task = asyncio.create_task(_run_crawl(job.id, job_store, crawl_service, kwargs))
    job_store.set_task(job.id, task)

    return CrawlCreateResponse(id=job.id)


@router.get("/crawl/{job_id}")
async def crawl_status(
    job_id: str,
    request: Request,
    offset: int = 0,
    _api_key: str | None = Depends(get_api_key),
) -> CrawlStatusResponse | ErrorResponse:
    """Return the status of a crawl job with paginated data."""
    job_store = _get_job_store(request)
    base_url = str(request.url).split("?")[0]
    result = job_store.get_job(job_id, offset=offset, base_url=base_url)

    if result is None:
        return ErrorResponse(error=f"Job {job_id} not found")

    return CrawlStatusResponse(
        status=result["status"],
        total=result["total"],
        completed=result["completed"],
        data=result["data"],
        next=result["next"],
        error=result["error"],
    )


@router.delete("/crawl/{job_id}")
async def crawl_cancel(
    job_id: str,
    request: Request,
    _api_key: str | None = Depends(get_api_key),
) -> CrawlStatusResponse | ErrorResponse:
    """Cancel a running crawl job."""
    job_store = _get_job_store(request)
    job = job_store.cancel_job(job_id)

    if job is None:
        return ErrorResponse(error=f"Job {job_id} not found")

    return CrawlStatusResponse(
        status=job.status.value,
        total=job.total,
        completed=job.completed,
        data=[],
    )
