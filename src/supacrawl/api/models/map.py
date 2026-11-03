"""Request and response models for the POST /map endpoint."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

# Request


class MapRequest(BaseModel):
    """Firecrawl v2-compatible map request body.

    Accepts camelCase field names (v2 protocol) and silently ignores
    fields that Supacrawl does not support.
    """

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
        extra="ignore",
    )

    url: str
    limit: int = 5000
    search: str | None = None
    sitemap: Literal["skip", "include", "only"] = "include"
    include_subdomains: bool = False
    ignore_query_parameters: bool = False
    ignore_cache: bool = False
    timeout: int = 30000
    location: dict[str, Any] | None = None


# Response


class MapLinkResponse(BaseModel):
    """A single discovered link in the map response."""

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
        serialize_by_alias=True,
    )

    url: str
    title: str | None = None
    description: str | None = None


class MapResponse(BaseModel):
    """Top-level map response envelope."""

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
        serialize_by_alias=True,
    )

    success: bool
    links: list[MapLinkResponse] | None = None
    error: str | None = None
