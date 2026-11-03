"""Request and response models for the POST /search endpoint."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

# Known source types that supacrawl supports.
_KNOWN_SOURCES = frozenset({"web", "images", "news"})

# Request


class SearchRequest(BaseModel):
    """Firecrawl v2-compatible search request body.

    Accepts camelCase field names (v2 protocol) and silently ignores
    fields that Supacrawl does not support (``tbs``, ``location``,
    ``country``, ``categories``, ``ignoreInvalidURLs``, ``enterprise``).

    The v2 protocol sends ``sources`` as ``[{"type": "web"}]`` objects;
    we normalise these to ``["web"]`` strings and drop unknown types.
    """

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
        extra="ignore",
    )

    query: str
    limit: int = 5
    sources: list[str] = Field(default_factory=lambda: ["web"])
    timeout: int = 30000
    scrape_options: dict[str, Any] | None = None
    # Recency / topic / domain filters (mapped onto each provider's native API).
    time_range: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    topic: str | None = None
    include_domains: list[str] | None = None
    exclude_domains: list[str] | None = None

    @field_validator("sources", mode="before")
    @classmethod
    def _normalise_sources(cls, v: Any) -> list[str]:
        """Translate v2 ``[{type: "web"}]`` objects to ``["web"]`` strings.

        Unknown source types are silently dropped. If nothing remains,
        defaults to ``["web"]``.
        """
        if not isinstance(v, list):
            return ["web"]

        result: list[str] = []
        for item in v:
            if isinstance(item, dict):
                raw = item.get("type", "")
            elif isinstance(item, str):
                raw = item
            else:
                continue

            if raw in _KNOWN_SOURCES:
                result.append(raw)

        return result or ["web"]


# Response


class WebResultItem(BaseModel):
    """A single web search result (camelCase output)."""

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
        serialize_by_alias=True,
    )

    title: str
    url: str
    description: str | None = None
    markdown: str | None = None


class ImageResultItem(BaseModel):
    """A single image search result (camelCase output)."""

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
        serialize_by_alias=True,
    )

    title: str
    url: str
    image_url: str | None = Field(None, serialization_alias="imageUrl")


class NewsResultItem(BaseModel):
    """A single news search result (camelCase output)."""

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
        serialize_by_alias=True,
    )

    title: str
    url: str
    snippet: str | None = None


class SearchDataResponse(BaseModel):
    """Bucketed search results grouped by source type."""

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
        serialize_by_alias=True,
    )

    web: list[WebResultItem] = Field(default_factory=list)
    images: list[ImageResultItem] = Field(default_factory=list)
    news: list[NewsResultItem] = Field(default_factory=list)


class UnresponsiveEngineItem(BaseModel):
    """An upstream engine that failed during the query (camelCase output)."""

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
        serialize_by_alias=True,
    )

    engine: str
    reason: str


class SearchResponse(BaseModel):
    """Top-level search response envelope.

    The bucketed `data` shape is Firecrawl v2-compatible. The provenance and
    health signals below are additive supacrawl fields: a v2 client ignores
    keys it does not know, so carrying them costs that compatibility nothing,
    while withholding them leaves a REST caller unable to tell "no matches"
    from "the backend has stopped answering" — the silent-failure class the
    MCP surface has been protected from since #158/#161 (#166).

    They are deliberately flat rather than nested under an extension object,
    so the REST and MCP surfaces describe a search the same way.
    """

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
        serialize_by_alias=True,
    )

    success: bool
    data: SearchDataResponse | None = None
    error: str | None = None
    # Which provider actually answered, and whether it was one the operator
    # configured rather than a silent fallback (#158).
    provider: str | None = None
    provider_fallback: bool = False
    # Upstream engines that failed on this query, so an empty `data` is never
    # mistaken for "no such thing exists" (#161).
    unresponsive_engines: list[UnresponsiveEngineItem] = Field(default_factory=list)
    # True once a full window of searches has come back empty in a row — a
    # backend that has stopped answering rather than a genuine no-match.
    all_recent_empty: bool = False
