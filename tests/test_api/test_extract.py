"""Tests for extract endpoints (POST /extract, GET /extract/{id})."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from supacrawl.api.app import create_app
from supacrawl.api.dependencies import (
    get_extract_service,
    get_map_service,
    get_scrape_service,
    get_search_service,
)
from supacrawl.api.jobs import JobStore
from supacrawl.models import ExtractResult, ExtractResultItem
from supacrawl.services.extract import ExtractService


def _make_extract_result() -> ExtractResult:
    """Build a successful ``ExtractResult`` with sample data."""
    return ExtractResult(
        success=True,
        data=[
            ExtractResultItem(
                url="https://example.com",
                success=True,
                data={"name": "Widget", "price": 9.99},
            ),
        ],
    )


def _make_failed_extract_result() -> ExtractResult:
    """Build a failed ``ExtractResult``."""
    return ExtractResult(
        success=False,
        data=[
            ExtractResultItem(
                url="https://example.com",
                success=False,
                error="LLM extraction failed",
            ),
        ],
        error="Extraction failed",
    )


@pytest.fixture()
def mock_extract_service() -> AsyncMock:
    mock = AsyncMock(spec=ExtractService)
    mock.extract.return_value = _make_extract_result()
    return mock


@pytest.fixture()
def extract_app(
    mock_scrape_service: AsyncMock,
    mock_map_service: AsyncMock,
    mock_search_service: AsyncMock,
    mock_extract_service: AsyncMock,
) -> FastAPI:
    """Create the FastAPI app with mocked services and a real JobStore."""
    application = create_app()
    application.dependency_overrides[get_scrape_service] = lambda: mock_scrape_service
    application.dependency_overrides[get_map_service] = lambda: mock_map_service
    application.dependency_overrides[get_search_service] = lambda: mock_search_service
    application.dependency_overrides[get_extract_service] = lambda: mock_extract_service

    application.state.job_store = JobStore()

    return application


@pytest.fixture()
def extract_client(extract_app: FastAPI) -> TestClient:
    return TestClient(extract_app)


def _wait_for_tasks() -> None:
    """Run the event loop briefly to let background tasks complete."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
    loop.run_until_complete(asyncio.sleep(0.1))


class TestExtractCreate:
    """POST /extract creates a job and returns its ID."""

    def test_returns_job_id(self, extract_client: TestClient) -> None:
        resp = extract_client.post(
            "/extract",
            json={"urls": ["https://example.com"], "prompt": "Extract product info"},
        )
        assert resp.status_code == 200

        body = resp.json()
        assert body["success"] is True
        assert "id" in body
        assert isinstance(body["id"], str)

    def test_an_invalid_schema_is_answered_not_queued(
        self, extract_client: TestClient, mock_extract_service: AsyncMock
    ) -> None:
        resp = extract_client.post(
            "/extract",
            json={"urls": ["https://example.com"], "schema": {"type": "object", "properties": {"x": {"type": 7}}}},
        )

        body = resp.json()
        assert body["success"] is False
        assert "not valid JSON Schema" in body["error"]
        mock_extract_service.extract.assert_not_called()

    def test_missing_urls_returns_400(self, extract_client: TestClient) -> None:
        resp = extract_client.post("/extract", json={})
        assert resp.status_code == 400

    def test_unknown_fields_ignored(self, extract_client: TestClient) -> None:
        resp = extract_client.post(
            "/extract",
            json={
                "urls": ["https://example.com"],
                "enableWebSearch": True,
                "ignoreSitemap": True,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_with_schema(self, extract_client: TestClient) -> None:
        resp = extract_client.post(
            "/extract",
            json={
                "urls": ["https://example.com"],
                "schema": {"type": "object", "properties": {"name": {"type": "string"}}},
            },
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True


class TestExtractStatus:
    """GET /extract/{id} returns job status with extracted data."""

    def test_job_not_found(self, extract_client: TestClient) -> None:
        resp = extract_client.get("/extract/nonexistent")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert "not found" in body["error"]

    def test_lifecycle_post_then_get(self, extract_client: TestClient) -> None:
        """POST creates job, background task runs, GET returns completed data."""
        post_resp = extract_client.post(
            "/extract",
            json={"urls": ["https://example.com"], "prompt": "Extract product info"},
        )
        job_id = post_resp.json()["id"]

        _wait_for_tasks()

        get_resp = extract_client.get(f"/extract/{job_id}")
        assert get_resp.status_code == 200

        body = get_resp.json()
        assert body["status"] == "completed"
        assert len(body["data"]) == 1
        assert body["data"][0]["url"] == "https://example.com"
        assert body["data"][0]["data"]["name"] == "Widget"

    def test_failed_extraction(
        self,
        extract_client: TestClient,
        mock_extract_service: AsyncMock,
    ) -> None:
        """A failed extraction marks the job as failed with error."""
        mock_extract_service.extract.return_value = _make_failed_extract_result()

        post_resp = extract_client.post(
            "/extract",
            json={"urls": ["https://example.com"]},
        )
        job_id = post_resp.json()["id"]

        _wait_for_tasks()

        get_resp = extract_client.get(f"/extract/{job_id}")
        body = get_resp.json()
        assert body["status"] == "failed"
        assert body["error"] == "Extraction failed"


class TestExtractErrorHandling:
    """Extract jobs that raise exceptions are marked as failed."""

    def test_exception_marks_failed(
        self,
        extract_client: TestClient,
        mock_extract_service: AsyncMock,
    ) -> None:
        mock_extract_service.extract.side_effect = RuntimeError("LLM unavailable")

        post_resp = extract_client.post(
            "/extract",
            json={"urls": ["https://example.com"]},
        )
        job_id = post_resp.json()["id"]

        _wait_for_tasks()

        get_resp = extract_client.get(f"/extract/{job_id}")
        body = get_resp.json()
        assert body["status"] == "failed"
        assert body["error"] == "Internal error during extraction"
