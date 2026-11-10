"""FastAPI application factory for supacrawl REST API."""

from __future__ import annotations

import contextlib
import importlib
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from importlib.metadata import version
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from supacrawl.api.models.common import ErrorResponse

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Create shared services on startup, tear down on shutdown."""
    from supacrawl.api.jobs import JobStore
    from supacrawl.services.registry import create_supacrawl_services

    services = await create_supacrawl_services()
    app.state.services = services
    app.state.job_store = JobStore()
    logger.info("Supacrawl API services initialised")

    yield

    await services.close()
    logger.info("Supacrawl API services shut down")


def create_app() -> FastAPI:
    """Build and return the FastAPI application.

    The app includes:
    - CORS middleware (all origins)
    - Global exception handler mapping errors to ``ErrorResponse``
    - 422 -> 400 remapping for validation errors

    Configures this process's telemetry (the household contract): one
    ``api_common.telemetry.configure()`` call, before anything else logs,
    owning the log format, the OTLP bootstrap, and nothing else in the
    process configuring logging of its own. A plain PyPI install carries no
    api-common (it is the ``telemetry`` extra) and skips both calls.
    """
    telemetry: Any = None
    with contextlib.suppress(ImportError):
        telemetry = importlib.import_module("api_common.telemetry")

    if telemetry is not None:
        telemetry.configure(service_name="supacrawl", service_version=version("supacrawl"))

    app = FastAPI(
        title="Supacrawl API",
        description="Firecrawl v2-compatible REST API for supacrawl.",
        lifespan=_lifespan,
    )
    if telemetry is not None:
        telemetry.instrument_app(app)

    # --- CORS ----------------------------------------------------------
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # --- Exception handlers --------------------------------------------

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Remap FastAPI's 422 validation errors to 400 Bad Request."""
        return JSONResponse(
            status_code=400,
            content=ErrorResponse(error=str(exc)).model_dump(),
        )

    @app.exception_handler(Exception)
    async def _generic_error(request: Request, exc: Exception) -> JSONResponse:
        """Catch-all handler; returns a consistent error envelope."""
        logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(error=str(exc)).model_dump(),
        )

    # --- Routers -----------------------------------------------------------
    from supacrawl.api.routers.batch import router as batch_router
    from supacrawl.api.routers.crawl import router as crawl_router
    from supacrawl.api.routers.extract import router as extract_router
    from supacrawl.api.routers.map import router as map_router
    from supacrawl.api.routers.scrape import router as scrape_router
    from supacrawl.api.routers.search import router as search_router
    from supacrawl.api.routers.supacrawl import router as supacrawl_router
    from supacrawl.api.routers.team import router as team_router

    app.include_router(scrape_router)
    app.include_router(map_router)
    app.include_router(search_router)
    app.include_router(crawl_router)
    app.include_router(extract_router)
    app.include_router(batch_router)
    app.include_router(team_router)
    # supacrawl router: /health has no auth dependency on the endpoint itself
    app.include_router(supacrawl_router)

    return app
