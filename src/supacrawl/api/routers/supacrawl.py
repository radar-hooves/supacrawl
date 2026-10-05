"""Native supacrawl endpoints (health, diagnose, summary, config, metrics)."""

from __future__ import annotations

import time
from importlib.metadata import version
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from supacrawl.api.auth import get_api_key
from supacrawl.api.dependencies import get_services
from supacrawl.services.registry import SupacrawlServices

router = APIRouter(prefix="/supacrawl", tags=["supacrawl"])

# Captured at import time; close enough to app start for uptime tracking.
_START_TIME = time.monotonic()


# --- Request / response models -------------------------------------------


class DiagnoseRequest(BaseModel):
    """Body for POST /supacrawl/diagnose."""

    url: str


class SummaryRequest(BaseModel):
    """Body for POST /supacrawl/summary."""

    url: str
    max_length: int | None = Field(default=None, alias="maxLength", ge=1)
    focus: str | None = None

    model_config = {"populate_by_name": True}


# --- Endpoints ------------------------------------------------------------


@router.get("/health")
async def health() -> dict[str, Any]:
    """Return service version, uptime, and status. No auth required."""
    uptime_seconds = round(time.monotonic() - _START_TIME)
    return {
        "success": True,
        "version": version("supacrawl"),
        "status": "healthy",
        "uptime_seconds": uptime_seconds,
    }


@router.get("/config/schema")
async def config_schema_endpoint(
    _api_key: str | None = Depends(get_api_key),
) -> dict[str, Any]:
    """Return the JSON schema a GUI uses to render the settings form.

    The schema carries ``x-ui`` metadata on each field (group, widget hint,
    help text, and optional conditional-visibility rules). No secret ever
    appears here — credentials live only in ``SupacrawlSecrets`` and are
    structurally absent from this schema.
    """
    from supacrawl.config import config_schema

    return config_schema()


@router.get("/config")
async def config_effective(
    _api_key: str | None = Depends(get_api_key),
) -> dict[str, Any]:
    """Return the effective non-secret config and a secrets presence map.

    The ``config`` object mirrors ``supacrawl config get --json``: the resolved
    effective values (default < stored TOML < environment). The ``secrets``
    object reports only *whether* each credential is configured — never its
    value — so a GUI can show "configured" / "not set" indicators safely.
    """
    from supacrawl.config import SupacrawlSecrets, load_config

    config = load_config()
    secrets = SupacrawlSecrets.from_env()
    config_dump = config.model_dump(mode="json")
    return {
        "config": config_dump,
        "secrets": secrets.configured(),
    }


@router.get("/metrics/summary")
async def metrics_summary(
    days: int = Query(default=7, ge=1, description="Summarise only events from the last N days."),
    _api_key: str | None = Depends(get_api_key),
) -> dict[str, Any]:
    """Return a headline telemetry rollup for the last ``days`` days.

    Aggregates the local ``events.jsonl`` into scrape/search counts, success
    and escalation rates, verdict mix, and the top domains. A separate GUI
    plane can consume this endpoint without touching the raw JSONL.
    """
    from datetime import datetime, timedelta, timezone

    from supacrawl.telemetry import MetricsReader

    since = datetime.now(timezone.utc) - timedelta(days=days)
    return MetricsReader().summary(since=since)


@router.post("/diagnose")
async def diagnose(
    req: DiagnoseRequest,
    services: SupacrawlServices = Depends(get_services),
    _api_key: str | None = Depends(get_api_key),
) -> dict[str, Any]:
    """Run pre-scrape diagnostics on a URL."""
    from supacrawl.services.diagnose import supacrawl_diagnose

    return await supacrawl_diagnose(api_client=services, url=req.url)


@router.post("/summary")
async def summary(
    req: SummaryRequest,
    services: SupacrawlServices = Depends(get_services),
    _api_key: str | None = Depends(get_api_key),
) -> dict[str, Any]:
    """Scrape a URL and return a word-bounded summary from the configured LLM."""
    from supacrawl.services.summary import supacrawl_summary

    return await supacrawl_summary(
        api_client=services,
        url=req.url,
        max_length=req.max_length,
        focus=req.focus,
    )
