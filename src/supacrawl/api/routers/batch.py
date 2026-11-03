"""Batch scrape endpoints; async job lifecycle with background processing."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from fastapi import APIRouter, Depends, Request

from supacrawl.api.auth import get_api_key
from supacrawl.api.dependencies import get_scrape_service
from supacrawl.api.jobs import JobStatus, JobStore, MaxJobsExceededError
from supacrawl.api.models.batch import (
    BatchScrapeCreateResponse,
    BatchScrapeRequest,
    BatchScrapeStatusResponse,
)
from supacrawl.api.models.common import ErrorResponse
from supacrawl.api.models.scrape import ScrapeRequest
from supacrawl.api.routers.scrape import _build_service_kwargs, _scrape_result_to_response
from supacrawl.services import ScrapeService
from supacrawl.services.batch import run_batch_scrape

logger = logging.getLogger("supacrawl.api.batch")

router = APIRouter()


def _get_job_store(request: Request) -> JobStore:
    """Retrieve the ``JobStore`` from application state."""
    store: JobStore = request.app.state.job_store
    return store


def _batch_request_to_scrape_request(req: BatchScrapeRequest, url: str) -> ScrapeRequest:
    """Translate a batch request's shared scrape options into a single-URL request.

    The two models carry identical option fields, so the batch options are
    dumped by alias and re-validated as a ``ScrapeRequest`` for ``url``. This
    keeps ``_build_service_kwargs`` the single source of truth for how v2
    options map onto ``ScrapeService.scrape``.
    """
    data = req.model_dump(by_alias=True, exclude={"urls"})
    data["url"] = url
    return ScrapeRequest.model_validate(data)


def _scrape_data_to_dict(result: Any) -> dict[str, Any]:
    """Convert a ``ScrapeResult`` to a v2-compatible camelCase dict."""
    response = _scrape_result_to_response(result)
    if response.data is None:
        return {}
    return response.data.model_dump(by_alias=True, exclude_none=True)


async def _run_batch_scrape(
    job_id: str,
    job_store: JobStore,
    scrape_service: ScrapeService,
    req: BatchScrapeRequest,
) -> None:
    """Background task that scrapes URLs concurrently and updates the job.

    Delegates to the shared ``run_batch_scrape`` service function so the API
    and CLI use identical concurrency, retry, and error-handling logic.
    """
    cancelled = asyncio.Event()

    # Watch for job cancellation in a parallel task
    async def _watch_cancel() -> None:
        while True:
            await asyncio.sleep(0.5)
            job = job_store._jobs.get(job_id)
            if job is None or job.status == JobStatus.cancelled:
                cancelled.set()
                return
            if cancelled.is_set():
                return

    watcher = asyncio.create_task(_watch_cancel())

    try:
        # Build the full v2 scrape-option set once (all URLs share the same
        # options) and forward it verbatim, so batch honours every option the
        # single-URL /v1/scrape endpoint does (include/exclude tags, actions,
        # wait_for, proxy, headers, ...).
        scrape_kwargs = _build_service_kwargs(_batch_request_to_scrape_request(req, req.urls[0])) if req.urls else {}
        batch_result = await run_batch_scrape(
            urls=req.urls,
            scrape_service=scrape_service,
            scrape_kwargs=scrape_kwargs,
            concurrency=5,
            retry=1,
            continue_on_error=True,
            cancelled=cancelled,
        )

        # Translate BatchURLResult list into the job data format
        for url_result in batch_result.results:
            job = job_store._jobs.get(job_id)
            if job is None:
                return
            if url_result.success and url_result.data is not None:
                page_dict = _scrape_data_to_dict(url_result.data)
            else:
                page_dict = {"error": f"Failed to scrape {url_result.url}"}
            job.data.append(page_dict)
            job.completed = len(job.data)

        job_store.update_job(
            job_id,
            status=JobStatus.completed,
            total=len(req.urls),
            completed=len(req.urls),
        )
        logger.info("Batch job %s completed with %d URLs", job_id, len(req.urls))

    except asyncio.CancelledError:
        logger.info("Batch job %s task cancelled", job_id)
        raise

    except Exception:
        logger.exception("Unexpected error in batch scrape job %s", job_id)
        job_store.update_job(
            job_id,
            status=JobStatus.failed,
            error="Internal error during batch scrape",
        )
    finally:
        cancelled.set()
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher


@router.post("/batch/scrape")
async def batch_scrape_create(
    req: BatchScrapeRequest,
    request: Request,
    scrape_service: ScrapeService = Depends(get_scrape_service),
    _api_key: str | None = Depends(get_api_key),
) -> BatchScrapeCreateResponse | ErrorResponse:
    """Start an async batch scrape job and return its ID."""
    job_store = _get_job_store(request)

    try:
        job = job_store.create_job(total=len(req.urls))
    except MaxJobsExceededError as exc:
        return ErrorResponse(error=str(exc))

    task = asyncio.create_task(_run_batch_scrape(job.id, job_store, scrape_service, req))
    job_store.set_task(job.id, task)

    return BatchScrapeCreateResponse(id=job.id)


@router.get("/batch/scrape/{job_id}")
async def batch_scrape_status(
    job_id: str,
    request: Request,
    offset: int = 0,
    _api_key: str | None = Depends(get_api_key),
) -> BatchScrapeStatusResponse | ErrorResponse:
    """Return the status of a batch scrape job with paginated data."""
    job_store = _get_job_store(request)
    base_url = str(request.url).split("?")[0]
    result = job_store.get_job(job_id, offset=offset, base_url=base_url)

    if result is None:
        return ErrorResponse(error=f"Job {job_id} not found")

    return BatchScrapeStatusResponse(
        status=result["status"],
        total=result["total"],
        completed=result["completed"],
        data=result["data"],
        next=result["next"],
        error=result["error"],
    )
