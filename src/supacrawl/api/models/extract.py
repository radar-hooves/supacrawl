"""Request and response models for the extract endpoints."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from supacrawl.api.models.scrape import ScrapeRequest

# Request


class ExtractScrapeOptions(ScrapeRequest):
    """Nested scrape options within an extract request.

    Inherits all fields from ``ScrapeRequest`` but ``url`` is not required
    (the extract endpoint receives URLs separately).
    """

    url: str = ""


class ExtractRequest(BaseModel):
    """Firecrawl v2-compatible extract request body.

    Accepts camelCase field names (v2 protocol) and silently ignores
    fields that Supacrawl does not support.
    """

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
        extra="ignore",
    )

    urls: list[str]
    prompt: str | None = None
    schema_: dict[str, Any] | None = Field(None, alias="schema")
    scrape_options: ExtractScrapeOptions | None = Field(None, alias="scrapeOptions")


# Response


class ExtractCreateResponse(BaseModel):
    """Response for POST /extract; returns the new job ID."""

    success: bool = True
    id: str


class ExtractStatusResponse(BaseModel):
    """Response for GET /extract/{id}; includes extracted data."""

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
        serialize_by_alias=True,
    )

    success: bool = True
    status: str
    data: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
