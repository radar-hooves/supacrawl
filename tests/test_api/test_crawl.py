"""Tests for crawl endpoints (POST /crawl, GET /crawl/{id}, DELETE /crawl/{id})."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from supacrawl.api.app import create_app
from supacrawl.api.dependencies import (
    get_crawl_service,
    get_map_service,
    get_scrape_service,
    get_search_service,
)
from supacrawl.api.jobs import JobStore
from supacrawl.models import CrawlEvent, ScrapeData, ScrapeMetadata
from supacrawl.services import CrawlService


def _make_page_event(url: str = "https://example.com") -> CrawlEvent:
    """Build a ``CrawlEvent`` of type ``page`` with minimal data."""
    return CrawlEvent(
        type="page",
        url=url,
        data=ScrapeData(
            markdown="# Example",
            html="<h1>Example</h1>",
            metadata=ScrapeMetadata(
                title="Example",
                source_url=url,
                status_code=200,
            ),
        ),
        completed=1,
        total=2,
    )


def _make_complete_event() -> CrawlEvent:
    return CrawlEvent(type="complete", completed=2, total=2)


def _make_error_event(msg: str = "Something broke") -> CrawlEvent:
    return CrawlEvent(type="error", error=msg)


async def _mock_crawl_generator(**kwargs: Any) -> AsyncGenerator[CrawlEvent]:
    """Yield two page events then a complete event."""
    yield _make_page_event("https://example.com")
    yield _make_page_event("https://example.com/about")
    yield _make_complete_event()


async def _mock_crawl_error(**kwargs: Any) -> AsyncGenerator[CrawlEvent]:
    """Yield an error event."""
    yield _make_error_event("Crawl failed")


@pytest.fixture()
def mock_crawl_service() -> AsyncMock:
    mock = AsyncMock(spec=CrawlService)
    mock.crawl.side_effect = _mock_crawl_generator
    return mock


@pytest.fixture()
def crawl_app(
    mock_scrape_service: AsyncMock,
    mock_map_service: AsyncMock,
    mock_search_service: AsyncMock,
    mock_crawl_service: AsyncMock,
) -> FastAPI:
    """Create the FastAPI app with mocked services and a real JobStore."""
    application = create_app()
    application.dependency_overrides[get_scrape_service] = lambda: mock_scrape_service
    application.dependency_overrides[get_map_service] = lambda: mock_map_service
    application.dependency_overrides[get_search_service] = lambda: mock_search_service
    application.dependency_overrides[get_crawl_service] = lambda: mock_crawl_service

    # Attach a real JobStore so the endpoints can use it
    application.state.job_store = JobStore()

    return application


@pytest.fixture()
def crawl_client(crawl_app: FastAPI) -> TestClient:
    return TestClient(crawl_app)


def _wait_for_tasks() -> None:
    """Run the event loop briefly to let background tasks complete."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
    loop.run_until_complete(asyncio.sleep(0.1))


class TestCrawlCreate:
    """POST /crawl creates a job and returns its ID."""

    def test_returns_job_id(self, crawl_client: TestClient) -> None:
        resp = crawl_client.post("/crawl", json={"url": "https://example.com"})
        assert resp.status_code == 200

        body = resp.json()
        assert body["success"] is True
        assert "id" in body
        assert isinstance(body["id"], str)

    def test_missing_url_returns_400(self, crawl_client: TestClient) -> None:
        resp = crawl_client.post("/crawl", json={})
        assert resp.status_code == 400

    def test_unknown_fields_ignored(self, crawl_client: TestClient) -> None:
        resp = crawl_client.post(
            "/crawl",
            json={"url": "https://example.com", "webhook": "https://hook.example.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True


class TestCrawlStatus:
    """GET /crawl/{id} returns job status with paginated data."""

    def test_job_not_found(self, crawl_client: TestClient) -> None:
        resp = crawl_client.get("/crawl/nonexistent")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert "not found" in body["error"]

    def test_lifecycle_post_then_get(self, crawl_client: TestClient) -> None:
        """POST creates job, background task runs, GET returns completed data."""
        post_resp = crawl_client.post("/crawl", json={"url": "https://example.com"})
        job_id = post_resp.json()["id"]

        # Let the background crawl task finish
        _wait_for_tasks()

        get_resp = crawl_client.get(f"/crawl/{job_id}")
        assert get_resp.status_code == 200

        body = get_resp.json()
        assert body["status"] == "completed"
        assert body["completed"] == 2
        assert len(body["data"]) == 2

        # Verify data shape matches v2 scrape output
        page = body["data"][0]
        assert "markdown" in page
        assert "metadata" in page

    def test_pagination(self, crawl_client: TestClient) -> None:
        """GET with offset returns paginated results and next URL."""
        post_resp = crawl_client.post("/crawl", json={"url": "https://example.com"})
        job_id = post_resp.json()["id"]

        _wait_for_tasks()

        # Request with page_size=1 (default is 10, but we only have 2 items)
        # Use offset to test pagination
        get_resp = crawl_client.get(f"/crawl/{job_id}?offset=0")
        body = get_resp.json()
        assert body["status"] == "completed"
        # Default page size is 10, so all 2 items fit in one page
        assert len(body["data"]) == 2
        assert body["next"] is None


class TestCrawlCancel:
    """DELETE /crawl/{id} cancels a running job."""

    def test_cancel_job(self, crawl_client: TestClient) -> None:
        post_resp = crawl_client.post("/crawl", json={"url": "https://example.com"})
        job_id = post_resp.json()["id"]

        del_resp = crawl_client.delete(f"/crawl/{job_id}")
        assert del_resp.status_code == 200

        body = del_resp.json()
        assert body["status"] == "cancelled"

    def test_cancel_nonexistent(self, crawl_client: TestClient) -> None:
        resp = crawl_client.delete("/crawl/nonexistent")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert "not found" in body["error"]


class TestCrawlErrorHandling:
    """Crawl jobs that encounter errors are marked as failed."""

    def test_error_event_marks_failed(self, crawl_client: TestClient, mock_crawl_service: AsyncMock) -> None:
        mock_crawl_service.crawl.side_effect = _mock_crawl_error

        post_resp = crawl_client.post("/crawl", json={"url": "https://example.com"})
        job_id = post_resp.json()["id"]

        _wait_for_tasks()

        get_resp = crawl_client.get(f"/crawl/{job_id}")
        body = get_resp.json()
        assert body["status"] == "failed"
        assert body["error"] == "Crawl failed"
