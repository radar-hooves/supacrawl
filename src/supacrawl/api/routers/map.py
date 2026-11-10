"""POST /map router; translates v2 protocol to MapService."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from supacrawl.api.auth import get_api_key
from supacrawl.api.dependencies import get_map_service
from supacrawl.api.models.map import (
    MapLinkResponse,
    MapRequest,
    MapResponse,
)
from supacrawl.services import MapService

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/map")
async def map_url(
    req: MapRequest,
    service: MapService = Depends(get_map_service),
    _api_key: str | None = Depends(get_api_key),
) -> MapResponse:
    """Discover URLs on a website (Firecrawl v2-compatible)."""
    try:
        links: list[MapLinkResponse] = []
        async for event in service.map(
            url=req.url,
            limit=req.limit,
            search=req.search,
            sitemap=req.sitemap,
            include_subdomains=req.include_subdomains,
            ignore_query_params=req.ignore_query_parameters,
            ignore_cache=req.ignore_cache,
        ):
            if event.type == "complete" and event.result is not None:
                links = [
                    MapLinkResponse(
                        url=link.url,
                        title=link.title,
                        description=link.description,
                    )
                    for link in event.result.links
                ]
            elif event.type == "error":
                return MapResponse(
                    success=False,
                    error=event.message or "Map operation failed",
                )

        return MapResponse(success=True, links=links)
    except Exception:
        logger.exception("Map operation failed for %s", req.url)
        raise
