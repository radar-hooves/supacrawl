"""The urls JSON-string argument contract, driven through a real FastMCP client.

Every other tool test in this suite calls the tool coroutine directly against a
mocked ``SupacrawlServices``. A direct call runs no pydantic validation and
builds no input schema, so it never exercises mcp_common's
``create_tool_wrapper`` argument normalisation. This test goes through
``fastmcp.Client``, the seam where both the advertised schema and the
normalisation that backs it exist.

``supacrawl_extract.urls`` and ``supacrawl_batch.urls`` are declared
``list[str] | str`` to accept a JSON-encoded string over MCP transport in
addition to a native JSON array; mcp_common's tool-registration layer parses
that string before either tool's own ``validate_urls`` ever sees it (see
radar-hooves/mcp-servers's mcp-common test_tool_registration.py for the
Annotated-unwrap coverage that backs this).

A single bare URL string (not JSON-encoded, e.g. one MCP client sent
``urls="https://example.com/a"`` rather than ``urls=["https://example.com/a"]``)
is not valid JSON, so mcp_common's parse attempt fails and the raw string
reaches ``validate_urls`` unchanged. That is the shape the schema's
``anyOf: [array of string, string]`` also admits, so ``validate_urls`` itself
treats a non-JSON string as a single URL rather than refusing it
(correlation_id 6a66d610 — 'urls must be a list, got str').
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastmcp import Client, FastMCP

from supacrawl.mcp.wiring import register_all_tools
from supacrawl.models import ScrapeData, ScrapeMetadata, ScrapeResult
from supacrawl.services.registry import SupacrawlServices

pytestmark = pytest.mark.mcp


@pytest.fixture
def surface(mock_api_client: SupacrawlServices) -> FastMCP:
    """A registered supacrawl tool surface over the suite's mocked services."""
    mcp = FastMCP("supacrawl-test")
    register_all_tools(mcp, mock_api_client)
    return mcp


@pytest.fixture
def batch_surface(mock_api_client) -> FastMCP:
    """A tool surface whose ``scrape_service.scrape`` returns a real
    ``ScrapeResult`` (not a hand-built ``MagicMock``), since ``supacrawl_batch``
    calls ``.model_dump(exclude_none=True)`` on it — the generic
    ``mock_scrape_service`` fixture's stub ``model_dump`` lambda takes no
    kwargs and only suits the single-URL scrape tool tests."""

    async def _scrape(url: str, **_kwargs: object) -> ScrapeResult:
        return ScrapeResult(
            success=True,
            data=ScrapeData(markdown=f"# {url}\n\nContent", metadata=ScrapeMetadata(source_url=url)),
        )

    mock_api_client.scrape_service.scrape = AsyncMock(side_effect=_scrape)
    mcp = FastMCP("supacrawl-test")
    register_all_tools(mcp, mock_api_client)
    return mcp


@pytest.mark.asyncio
async def test_extract_accepts_urls_as_a_json_encoded_string(surface: FastMCP) -> None:
    """A client that serialises the ``urls`` array as a JSON string (some MCP
    clients do) is accepted exactly like a native array, not refused with
    'urls must be a list, got str'."""
    async with Client(surface) as client:
        result = await client.call_tool(
            "supacrawl_extract",
            {
                "urls": '["https://example.com/a", "https://example.com/b"]',
                "prompt": "Extract the page title and domain name",
            },
        )

    payload = result.data
    assert payload["success"] is True
    assert payload["succeeded_count"] == 2
    assert [item["url"] for item in payload["data"]] == [
        "https://example.com/a",
        "https://example.com/b",
    ]


@pytest.mark.asyncio
async def test_extract_still_accepts_urls_as_a_native_array(surface: FastMCP) -> None:
    """The documented native-array shape keeps working unchanged."""
    async with Client(surface) as client:
        result = await client.call_tool(
            "supacrawl_extract",
            {
                "urls": ["https://example.com/a"],
                "prompt": "Extract the page title and domain name",
            },
        )

    payload = result.data
    assert payload["success"] is True
    assert payload["succeeded_count"] == 1


@pytest.mark.asyncio
async def test_extract_accepts_urls_as_a_single_bare_url_string(surface: FastMCP) -> None:
    """A bare single URL (not JSON-encoded) is a valid ``urls`` shape per the
    schema's ``anyOf: [array of string, string]`` and must not be refused
    with 'urls must be a list, got str' (correlation_id 6a66d610)."""
    async with Client(surface) as client:
        result = await client.call_tool(
            "supacrawl_extract",
            {
                "urls": "https://example.com/a",
                "prompt": "Extract the page title and domain name",
            },
        )

    payload = result.data
    assert payload["success"] is True
    assert payload["succeeded_count"] == 1
    assert [item["url"] for item in payload["data"]] == ["https://example.com/a"]


@pytest.mark.asyncio
async def test_batch_accepts_urls_as_a_json_encoded_string(batch_surface: FastMCP) -> None:
    """``supacrawl_batch.urls`` takes the same three shapes as extract's."""
    async with Client(batch_surface) as client:
        result = await client.call_tool(
            "supacrawl_batch",
            {"urls": '["https://example.com/a", "https://example.com/b"]'},
        )

    payload = result.data
    assert payload["success"] is True
    assert payload["succeeded"] == 2


@pytest.mark.asyncio
async def test_batch_accepts_urls_as_a_single_bare_url_string(batch_surface: FastMCP) -> None:
    """A bare single URL is accepted for batch exactly as it is for extract."""
    async with Client(batch_surface) as client:
        result = await client.call_tool(
            "supacrawl_batch",
            {"urls": "https://example.com/a"},
        )

    payload = result.data
    assert payload["success"] is True
    assert payload["succeeded"] == 1
    assert [item["url"] for item in payload["results"]] == ["https://example.com/a"]


@pytest.mark.asyncio
async def test_batch_still_accepts_urls_as_a_native_array(batch_surface: FastMCP) -> None:
    """The documented native-array shape keeps working unchanged."""
    async with Client(batch_surface) as client:
        result = await client.call_tool(
            "supacrawl_batch",
            {"urls": ["https://example.com/a", "https://example.com/b"]},
        )

    payload = result.data
    assert payload["success"] is True
    assert payload["succeeded"] == 2
