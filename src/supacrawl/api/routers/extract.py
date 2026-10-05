"""Extract endpoints; async job lifecycle with background processing."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, Request

from supacrawl.api.auth import get_api_key
from supacrawl.api.dependencies import get_extract_service
from supacrawl.api.jobs import JobStatus, JobStore, MaxJobsExceededError
from supacrawl.api.models.common import ErrorResponse
from supacrawl.api.models.extract import (
    ExtractCreateResponse,
    ExtractRequest,
    ExtractStatusResponse,
)
from supacrawl.exceptions import ValidationError
from supacrawl.llm.schema import schema_validator
from supacrawl.services.extract import ExtractService

logger = logging.getLogger("supacrawl.api.extract")

router = APIRouter()


def _get_job_store(request: Request) -> JobStore:
    """Retrieve the ``JobStore`` from application state."""
    return request.app.state.job_store


def _build_extract_kwargs(req: ExtractRequest) -> dict[str, Any]:
    """Translate a v2 extract request into kwargs for ``ExtractService.extract()``."""
    return {
        "urls": req.urls,
        "prompt": req.prompt,
        "schema": req.schema_,
    }


async def _run_extract(
    job_id: str,
    job_store: JobStore,
    extract_service: ExtractService,
    kwargs: dict[str, Any],
) -> None:
    """Background task that runs the extract and updates the job."""
    try:
        result = await extract_service.extract(**kwargs)

        items = [item.model_dump(exclude_none=True) for item in result.data]

        if result.success:
            job_store.update_job(
                job_id,
                status=JobStatus.completed,
                data=items,
                total=len(items),
                completed=len(items),
            )
            logger.info("Job %s completed with %d items", job_id, len(items))
        else:
            job_store.update_job(
                job_id,
                status=JobStatus.failed,
                data=items,
                error=result.error or "Extraction failed",
            )
            logger.error("Job %s failed: %s", job_id, result.error)

    except asyncio.CancelledError:
        logger.info("Job %s task cancelled", job_id)
        raise

    except Exception:
        logger.exception("Unexpected error in extract job %s", job_id)
        job_store.update_job(
            job_id,
            status=JobStatus.failed,
            error="Internal error during extraction",
        )


@router.post("/extract")
async def extract_create(
    req: ExtractRequest,
    request: Request,
    extract_service: ExtractService = Depends(get_extract_service),
    _api_key: str | None = Depends(get_api_key),
) -> ExtractCreateResponse | ErrorResponse:
    """Start an async extract job and return its ID."""
    job_store = _get_job_store(request)

    # A bad schema is the caller's to fix, so it is answered here, not buried in a failed job
    if req.schema_ is not None:
        try:
            schema_validator(req.schema_)
        except ValidationError as exc:
            return ErrorResponse(error=exc.message)

    try:
        job = job_store.create_job(total=0)
    except MaxJobsExceededError as exc:
        return ErrorResponse(error=str(exc))

    kwargs = _build_extract_kwargs(req)
    task = asyncio.create_task(_run_extract(job.id, job_store, extract_service, kwargs))
    job_store.set_task(job.id, task)

    return ExtractCreateResponse(id=job.id)


@router.get("/extract/{job_id}")
async def extract_status(
    job_id: str,
    request: Request,
    _api_key: str | None = Depends(get_api_key),
) -> ExtractStatusResponse | ErrorResponse:
    """Return the status of an extract job."""
    job_store = _get_job_store(request)
    job = job_store._jobs.get(job_id)

    if job is None:
        return ErrorResponse(error=f"Job {job_id} not found")

    return ExtractStatusResponse(
        status=job.status.value,
        data=job.data,
        error=job.error,
    )
