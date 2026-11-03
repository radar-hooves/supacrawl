"""Request and response models for the crawl endpoints."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from supacrawl.api.models.scrape import ScrapeRequest

# Request


class CrawlScrapeOptions(ScrapeRequest):
    """Nested scrape options within a crawl request.

    Inherits all fields from ``ScrapeRequest`` but ``url`` is not required
    (the crawl endpoint discovers URLs itself).
    """

    url: str = ""


class CrawlRequest(BaseModel):
    """Firecrawl v2-compatible crawl request body.

    Accepts camelCase field names (v2 protocol) and silently ignores
    fields that Supacrawl does not support.
    """

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
        extra="ignore",
    )

    url: str
    limit: int = 10000
    max_depth: int = Field(3, alias="maxDiscoveryDepth")
    include_patterns: list[str] | None = Field(None, alias="includePaths")
    exclude_patterns: list[str] | None = Field(None, alias="excludePaths")
    sitemap: str | None = None
    allow_external_links: bool = False
    allow_subdomains: bool = False
    concurrency: int = Field(10, alias="maxConcurrency")
    ignore_query_params: bool = Field(False, alias="ignoreQueryParameters")
    ignore_robots_txt: bool = Field(True, alias="ignoreRobotsTxt")
    delay: float = 0.0
    scrape_options: CrawlScrapeOptions | None = Field(None, alias="scrapeOptions")


# Response


class CrawlCreateResponse(BaseModel):
    """Response for POST /crawl; returns the new job ID."""

    success: bool = True
    id: str


class CrawlStatusResponse(BaseModel):
    """Response for GET /crawl/{id}; includes paginated data."""

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
