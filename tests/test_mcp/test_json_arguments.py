"""The urls JSON-string argument contract, driven through a real FastMCP client.

Every other tool test in this suite calls the tool coroutine directly against a
mocked ``SupacrawlServices``. A direct call runs no pydantic validation and
builds no input schema, so it never exercises mcp_common's
``create_tool_wrapper`` argument normalisation. This test goes through
``fastmcp.Client``, the seam where both the advertised schema and the
normalisation that backs it exist.

``supacrawl_extract.urls`` is declared ``list[str] | str`` to accept a
JSON-encoded string over MCP transport in addition to a native JSON array;
mcp_common's tool-registration layer parses the string before this tool's own
``validate_urls`` ever sees it (see radar-hooves/mcp-servers's mcp-common
test_tool_registration.py for the Annotated-unwrap coverage that backs this).
"""

from __future__ import annotations

import pytest
from fastmcp import Client, FastMCP

from supacrawl.mcp.wiring import register_all_tools
from supacrawl.services.registry import SupacrawlServices

pytestmark = pytest.mark.mcp


@pytest.fixture
def surface(mock_api_client: SupacrawlServices) -> FastMCP:
    """A registered supacrawl tool surface over the suite's mocked services."""
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
