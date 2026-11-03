"""Request and response models for the batch scrape endpoints."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

# Request


class BatchScrapeRequest(BaseModel):
    """Firecrawl v2-compatible batch scrape request body.

    Scrape options are flat at the top level (not nested), with an
    additional ``urls`` array.  Accepts camelCase field names (v2 protocol)
    and silently ignores unsupported fields.
    """

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
        extra="ignore",
    )

    urls: list[str]
    formats: list[str] | None = None
    only_main_content: bool = True
    wait_for: int = 0
    timeout: int = 30000
    include_tags: list[str] | None = None
    exclude_tags: list[str] | None = None
    mobile: bool | None = None
    actions: list[Any] | None = None
    location: dict[str, Any] | None = None
    headers: dict[str, str] | None = None
    max_age: int | None = Field(None, alias="maxAge")
    proxy: str | bool | None = None
    store_in_cache: bool | None = Field(None, alias="storeInCache")
    content_mode: float | None = Field(None, ge=0.0, le=1.0)
    query: str | None = None


# Response


class BatchScrapeCreateResponse(BaseModel):
    """Response for POST /batch/scrape; returns the new job ID."""

    success: bool = True
    id: str


class BatchScrapeStatusResponse(BaseModel):
    """Response for GET /batch/scrape/{id}; includes paginated data."""

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
        serialize_by_alias=True,
    )

    status: str
    total: int = 0
    completed: int = 0
    data: list[dict[str, Any]] = Field(default_factory=list)
    next: str | None = None
    error: str | None = None
