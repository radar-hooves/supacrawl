"""REST requests reach the services with every documented field, or are refused."""

from __future__ import annotations

from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from supacrawl.api.models.batch import BatchScrapeRequest
from supacrawl.api.models.crawl import CrawlRequest
from supacrawl.api.models.scrape import ScrapeRequest
from supacrawl.api.routers.batch import _batch_request_to_scrape_request
from supacrawl.api.routers.crawl import _build_crawl_kwargs
from supacrawl.api.routers.scrape import _build_service_kwargs
from supacrawl.models import DEFAULT_MOBILE_DEVICE


def test_scrape_forwards_mobile() -> None:
    kwargs = _build_service_kwargs(ScrapeRequest.model_validate({"url": "https://e.com", "mobile": True}))
    assert kwargs["device"] == DEFAULT_MOBILE_DEVICE


def test_scrape_refuses_location(client: TestClient) -> None:
    resp = client.post("/scrape", json={"url": "https://e.com", "location": {"country": "AU"}})
    assert resp.status_code == 400


def test_batch_carries_http_first_and_expect() -> None:
    req = BatchScrapeRequest.model_validate({"urls": ["https://e.com"], "httpFirst": False, "expect": "price"})
    kwargs = _build_service_kwargs(_batch_request_to_scrape_request(req, "https://e.com"))
    assert kwargs["http_first"] is False
    assert kwargs["expect"] == "price"


def test_crawl_forwards_scrape_options() -> None:
    req = CrawlRequest.model_validate(
        {
            "url": "https://e.com",
            "ignoreQueryParameters": True,
            "scrapeOptions": {"formats": ["html"], "onlyMainContent": False, "waitFor": 500, "includeTags": ["main"]},
        }
    )
    kwargs = _build_crawl_kwargs(req)
    assert kwargs["formats"] == ["html"]
    assert kwargs["deduplicate_similar_urls"] is True
    assert kwargs["scrape_options"]["only_main_content"] is False
    assert kwargs["scrape_options"]["wait_for"] == 500
    assert kwargs["scrape_options"]["include_tags"] == ["main"]
    assert "formats" not in kwargs["scrape_options"]


def test_crawl_refuses_what_it_cannot_do() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CrawlRequest.model_validate({"url": "https://e.com", "allowSubdomains": True})
    with pytest.raises(ValidationError):
        CrawlRequest.model_validate({"url": "https://e.com", "sitemap": "only"})
    CrawlRequest.model_validate({"url": "https://e.com", "sitemap": "include"})


def test_search_forwards_scrape_options(client: TestClient, mock_search_service: AsyncMock) -> None:
    resp = client.post(
        "/search",
        json={"query": "x", "scrapeOptions": {"formats": ["html"], "onlyMainContent": False}},
    )
    assert resp.status_code == 200
    opts = mock_search_service.search.call_args.kwargs["scrape_options"]
    assert opts.formats == ["html"]
    assert opts.only_main_content is False
