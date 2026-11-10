"""POST /scrape router; translates v2 protocol to ScrapeService."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends

from supacrawl.api.auth import get_api_key
from supacrawl.api.dependencies import get_scrape_service
from supacrawl.api.models.scrape import (
    ScrapeDataResponse,
    ScrapeMetadataResponse,
    ScrapeRequest,
    ScrapeResponse,
)
from supacrawl.models import ScrapeResult
from supacrawl.services import ScrapeService

logger = logging.getLogger(__name__)

router = APIRouter()

_PROXY_KEYWORDS = frozenset({"basic", "enhanced", "auto"})


def _translate_proxy(value: str | bool | None) -> str | None:
    """Resolve a v2 proxy field to a usable proxy URL, or ``None``.

    A proxy URL string (e.g. ``"http://user:pass@host:port"``,
    ``"socks5://host:port"``) is used as-is. The v2 managed-proxy keywords
    (``"basic"``, ``"enhanced"``, ``"auto"``) and the boolean managed-proxy flag
    request a hosted proxy pool, which a local-first scraper has no equivalent
    for, so they resolve to ``None`` (no proxy applied) rather than failing.
    """
    if isinstance(value, str) and value.lower() not in _PROXY_KEYWORDS:
        return value
    return None


def _build_service_kwargs(req: ScrapeRequest) -> dict[str, Any]:
    """Translate a v2 request into keyword arguments for ``ScrapeService.scrape()``."""
    kwargs: dict[str, Any] = {
        "formats": req.formats,
        "only_main_content": req.only_main_content,
        "wait_for": req.wait_for,
        "timeout": req.timeout,
        "include_tags": req.include_tags,
        "exclude_tags": req.exclude_tags,
        "actions": req.actions,
    }

    if req.headers:
        kwargs["headers"] = req.headers

    # maxAge: v2 sends milliseconds; service expects seconds.
    if req.max_age is not None:
        kwargs["max_age"] = req.max_age // 1000

    # storeInCache: false -> bypass cache entirely.
    if req.store_in_cache is False:
        kwargs["max_age"] = 0

    proxy = _translate_proxy(req.proxy)
    if proxy is not None:
        kwargs["proxy"] = proxy

    if req.content_mode is not None:
        kwargs["content_mode"] = req.content_mode

    if req.query is not None:
        kwargs["query"] = req.query

    kwargs["http_first"] = req.http_first

    if req.expect is not None:
        kwargs["expect"] = req.expect

    return kwargs


def _scrape_result_to_response(result: ScrapeResult) -> ScrapeResponse:
    """Map an internal ``ScrapeResult`` to the v2 response shape."""
    quality = result.quality.model_dump() if result.quality is not None else None
    if not result.success or result.data is None:
        # The failure branch carries the quality assessment too: it is the only
        # thing telling a REST caller whether the scraper or the site broke, and
        # dropping it here left that surface with the raw error string alone.
        return ScrapeResponse(success=False, error=result.error, quality=quality)

    data = result.data
    meta = data.metadata

    metadata_resp = ScrapeMetadataResponse(
        title=meta.title,
        description=meta.description,
        source_url=meta.source_url,
        url=meta.source_url,
        status_code=meta.status_code,
        language=meta.language,
    )

    actions_dict: dict[str, Any] | None = None
    if data.actions is not None:
        actions_dict = {
            "screenshots": data.actions.screenshots or [],
            "scrapes": [s.model_dump() for s in (data.actions.scrapes or [])],
        }

    branding_dict: dict[str, Any] | None = None
    if data.branding is not None:
        branding_dict = data.branding.model_dump(exclude_none=True)

    change_tracking_dict: dict[str, Any] | None = None
    if data.change_tracking is not None:
        change_tracking_dict = data.change_tracking.model_dump(exclude_none=True)

    structured_data_dict: dict[str, Any] | None = None
    if data.structured_data is not None:
        structured_data_dict = data.structured_data.model_dump(exclude_none=True)

    data_resp = ScrapeDataResponse(
        markdown=data.markdown,
        html=data.html,
        raw_html=data.raw_html,
        links=data.links,
        link_details=[d.model_dump() for d in data.link_details] if data.link_details is not None else None,
        screenshot=data.screenshot,
        images=data.images,
        image_details=[d.model_dump() for d in data.image_details] if data.image_details else None,
        pdf=data.pdf,
        summary=data.summary,
        llm_extraction=data.llm_extraction,
        structured_data=structured_data_dict,
        metadata=metadata_resp,
        actions=actions_dict,
        branding=branding_dict,
        change_tracking=change_tracking_dict,
    )

    return ScrapeResponse(success=True, data=data_resp, quality=quality)


@router.post("/scrape")
async def scrape(
    req: ScrapeRequest,
    service: ScrapeService = Depends(get_scrape_service),
    _api_key: str | None = Depends(get_api_key),
) -> ScrapeResponse:
    """Scrape a single URL (Firecrawl v2-compatible)."""
    kwargs = _build_service_kwargs(req)
    result = await service.scrape(url=req.url, **kwargs)
    return _scrape_result_to_response(result)
