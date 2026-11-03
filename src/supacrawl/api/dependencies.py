"""FastAPI dependency helpers for accessing supacrawl services."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request

from supacrawl.services import ExtractService

if TYPE_CHECKING:
    from supacrawl.services import CrawlService, MapService, ScrapeService
    from supacrawl.services.registry import SupacrawlServices
    from supacrawl.services.search.service import SearchService


def get_services(request: Request) -> "SupacrawlServices":
    """Return the shared ``SupacrawlServices`` stored on ``app.state``."""
    services: SupacrawlServices = request.app.state.services
    return services


def get_scrape_service(request: Request) -> "ScrapeService":
    """Return the shared ``ScrapeService``."""
    return get_services(request).scrape_service


def get_map_service(request: Request) -> "MapService":
    """Return the shared ``MapService``."""
    return get_services(request).map_service


def get_search_service(request: Request) -> "SearchService":
    """Return the shared ``SearchService``."""
    return get_services(request).search_service


def get_crawl_service(request: Request) -> "CrawlService":
    """Return the shared ``CrawlService``."""
    return get_services(request).crawl_service


def get_extract_service(request: Request) -> ExtractService:
    """Create a per-request ``ExtractService`` backed by the shared scrape service."""
    return ExtractService(scrape_service=get_scrape_service(request))
