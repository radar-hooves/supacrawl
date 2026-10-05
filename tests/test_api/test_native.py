"""Tests for native supacrawl endpoints and team credential stub."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from supacrawl.api.app import create_app
from supacrawl.api.dependencies import get_services


@pytest.fixture()
def app() -> FastAPI:
    """Create the FastAPI app without triggering lifespan services."""
    return create_app()


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    """Return a ``TestClient`` wired to the app."""
    return TestClient(app)


# --- GET /team/credit-usage ------------------------------------------------


class TestTeamCreditUsage:
    """n8n credential-test stub."""

    def test_returns_200(self, client: TestClient) -> None:
        resp = client.get("/team/credit-usage")
        assert resp.status_code == 200

    def test_returns_valid_json(self, client: TestClient) -> None:
        data = client.get("/team/credit-usage").json()
        assert data["success"] is True
        assert data["data"]["credits"] == 0


# --- GET /supacrawl/health -------------------------------------------------


class TestHealth:
    """Health endpoint; no auth required."""

    def test_returns_200(self, client: TestClient) -> None:
        resp = client.get("/supacrawl/health")
        assert resp.status_code == 200

    def test_response_shape(self, client: TestClient) -> None:
        data = client.get("/supacrawl/health").json()
        assert data["success"] is True
        assert "version" in data
        assert data["status"] == "healthy"
        assert "uptime_seconds" in data

    def test_no_auth_required(self, client: TestClient) -> None:
        """Health must succeed even when an API key is configured."""
        with patch.dict("os.environ", {"SUPACRAWL_API_KEY": "secret"}):
            resp = client.get("/supacrawl/health")
        assert resp.status_code == 200


# --- POST /supacrawl/diagnose ----------------------------------------------


class TestDiagnose:
    """Diagnose endpoint; verify shape with mocked diagnostics."""

    def test_returns_diagnosis(self, app: FastAPI) -> None:
        mock_services = AsyncMock()
        app.dependency_overrides[get_services] = lambda: mock_services

        fake_result: dict[str, Any] = {
            "success": True,
            "diagnosis": {"url": "https://example.com", "reachable": True},
        }
        with patch(
            "supacrawl.services.diagnose.supacrawl_diagnose",
            new_callable=AsyncMock,
            return_value=fake_result,
        ) as mock_diag:
            client = TestClient(app)
            resp = client.post("/supacrawl/diagnose", json={"url": "https://example.com"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "diagnosis" in data
        mock_diag.assert_awaited_once()


# --- POST /supacrawl/summary -----------------------------------------------


class TestSummary:
    """Summary endpoint; verify shape with mocked summary logic."""

    def test_returns_summary_shape(self, app: FastAPI) -> None:
        mock_services = AsyncMock()
        app.dependency_overrides[get_services] = lambda: mock_services

        fake_result: dict[str, Any] = {
            "success": True,
            "data": {"url": "https://example.com", "title": "Hello", "summary": "A greeting."},
        }
        with patch(
            "supacrawl.services.summary.supacrawl_summary",
            new_callable=AsyncMock,
            return_value=fake_result,
        ) as mock_sum:
            client = TestClient(app)
            resp = client.post(
                "/supacrawl/summary",
                json={"url": "https://example.com", "maxLength": 100, "focus": "pricing"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["data"]["summary"] == "A greeting."
        mock_sum.assert_awaited_once()
