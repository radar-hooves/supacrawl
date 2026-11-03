"""Tests for POST /map endpoint."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from supacrawl.models import MapEvent


class TestMapEndpoint:
    """POST /map returns a v2-compatible response with discovered links."""

    def test_basic_map(self, client: TestClient) -> None:
        """A minimal request returns success with links."""
        resp = client.post("/map", json={"url": "https://example.com"})
        assert resp.status_code == 200

        body = resp.json()
        assert body["success"] is True
        assert len(body["links"]) == 2
        assert body["links"][0]["url"] == "https://example.com"
        assert body["links"][0]["title"] == "Example"
        assert body["links"][1]["url"] == "https://example.com/about"

    def test_service_called_with_url(self, client: TestClient, mock_map_service: AsyncMock) -> None:
        """The service receives the requested URL."""
        client.post("/map", json={"url": "https://example.com"})
        mock_map_service.map.assert_called_once()
        call_kwargs = mock_map_service.map.call_args.kwargs
        assert call_kwargs["url"] == "https://example.com"

    def test_service_called_with_options(self, client: TestClient, mock_map_service: AsyncMock) -> None:
        """Optional fields are forwarded to the service."""
        client.post(
            "/map",
            json={
                "url": "https://example.com",
                "limit": 100,
                "search": "blog",
                "includeSubdomains": True,
                "ignoreQueryParameters": True,
            },
        )
        call_kwargs = mock_map_service.map.call_args.kwargs
        assert call_kwargs["limit"] == 100
        assert call_kwargs["search"] == "blog"
        assert call_kwargs["include_subdomains"] is True
        assert call_kwargs["ignore_query_params"] is True

    def test_missing_url_returns_400(self, client: TestClient) -> None:
        """Omitting ``url`` triggers a 400 Bad Request."""
        resp = client.post("/map", json={})
        assert resp.status_code == 400

    def test_error_event_returns_failure(self, client: TestClient, mock_map_service: AsyncMock) -> None:
        """An error event from the service returns success=false."""

        async def _error_generator(**kwargs: Any) -> AsyncGenerator[MapEvent]:
            yield MapEvent(type="error", message="Something went wrong")

        mock_map_service.map.side_effect = _error_generator

        resp = client.post("/map", json={"url": "https://example.com"})
        assert resp.status_code == 200

        body = resp.json()
        assert body["success"] is False
        assert body["error"] == "Something went wrong"

    def test_camel_case_request_accepted(self, client: TestClient) -> None:
        """camelCase request fields are accepted via alias."""
        resp = client.post(
            "/map",
            json={
                "url": "https://example.com",
                "includeSubdomains": True,
                "ignoreQueryParameters": True,
                "ignoreCache": True,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_unknown_fields_ignored(self, client: TestClient) -> None:
        """Unknown request fields are silently ignored."""
        resp = client.post(
            "/map",
            json={"url": "https://example.com", "unknownField": "value"},
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True
