"""POST /search router; translates v2 protocol to SearchService."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends

from supacrawl.api.auth import get_api_key
from supacrawl.api.dependencies import get_search_service
from supacrawl.api.models.search import (
    ImageResultItem,
    NewsResultItem,
    SearchDataResponse,
    SearchRequest,
    SearchResponse,
    UnresponsiveEngineItem,
    WebResultItem,
)
from supacrawl.models import SearchFilters, SearchResult, SearchSourceType
from supacrawl.services.search.service import SearchService

logger = logging.getLogger(__name__)

router = APIRouter()


def _signals(result: SearchResult) -> dict[str, Any]:
    """The provenance and health signals every response carries.

    Built once and applied to both branches: a failed or empty search is
    exactly when a caller most needs to know which provider answered and which
    engines were down, so dropping these on the failure path would leave the
    gap open where it hurts most (#166).
    """
    return {
        "provider": result.provider,
        "provider_fallback": result.provider_fallback,
        "unresponsive_engines": [
            UnresponsiveEngineItem(engine=e.engine, reason=e.reason) for e in result.unresponsive_engines
        ],
        "all_recent_empty": result.all_recent_empty,
    }


def _search_result_to_response(result: SearchResult) -> SearchResponse:
    """Map an internal ``SearchResult`` to the v2 bucketed response shape."""
    if not result.success:
        return SearchResponse(success=False, error=result.error, **_signals(result))

    web: list[WebResultItem] = []
    images: list[ImageResultItem] = []
    news: list[NewsResultItem] = []

    for item in result.data:
        if item.source_type == SearchSourceType.WEB:
            web.append(
                WebResultItem(
                    title=item.title,
                    url=item.url,
                    description=item.description,
                    markdown=item.markdown,
                )
            )
        elif item.source_type == SearchSourceType.IMAGES:
            images.append(
                ImageResultItem(
                    title=item.title,
                    url=item.url,
                    image_url=item.image_url or item.thumbnail,
                )
            )
        elif item.source_type == SearchSourceType.NEWS:
            news.append(
                NewsResultItem(
                    title=item.title,
                    url=item.url,
                    snippet=item.description,
                )
            )

    data = SearchDataResponse(web=web, images=images, news=news)
    return SearchResponse(success=True, data=data, **_signals(result))


@router.post("/search")
async def search(
    req: SearchRequest,
    service: SearchService = Depends(get_search_service),
    _api_key: str | None = Depends(get_api_key),
) -> SearchResponse:
    """Search the web (Firecrawl v2-compatible)."""
    filters = SearchFilters.model_validate(
        {
            "time_range": req.time_range,
            "start_date": req.start_date,
            "end_date": req.end_date,
            "topic": req.topic,
            "include_domains": req.include_domains,
            "exclude_domains": req.exclude_domains,
        }
    )
    result = await service.search(
        query=req.query,
        limit=req.limit,
        sources=req.sources,  # type: ignore[arg-type]
        filters=None if filters.is_empty() else filters,
    )
    return _search_result_to_response(result)
