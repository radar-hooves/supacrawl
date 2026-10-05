"""supacrawl_extract returns schema-conforming data; supacrawl_summary returns a summary, never the page.

Driven through the FastMCP surface a client calls, with the model answered at
the httpx wire (``llm_wire``).
"""

import json
from typing import Any

import pytest
from conftest import LLMWire
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from supacrawl.mcp.wiring import register_all_tools

pytestmark = pytest.mark.mcp

URL = "https://shop.example.com/widget"
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "price": {"type": "number"}},
    "required": ["name", "price"],
}


@pytest.fixture
def surface(mock_api_client) -> FastMCP:
    mcp = FastMCP("supacrawl-test")
    register_all_tools(mcp, mock_api_client)
    return mcp


async def _call(surface: FastMCP, tool: str, arguments: dict[str, Any]) -> Any:
    async with Client(surface) as client:
        return (await client.call_tool(tool, arguments)).data


class TestExtract:
    async def test_result_is_the_schema_shaped_data(self, surface: FastMCP, llm_wire: LLMWire) -> None:
        llm_wire.replies.append('{"name": "Widget", "price": 9.5}')

        payload = await _call(surface, "supacrawl_extract", {"urls": [URL], "schema": SCHEMA})

        assert payload["data"] == [{"url": URL, "success": True, "data": {"name": "Widget", "price": 9.5}}]
        assert "markdown" not in json.dumps(payload)

    async def test_a_schema_sent_as_a_json_string_is_honoured(self, surface: FastMCP, llm_wire: LLMWire) -> None:
        llm_wire.replies.append('{"name": "Widget", "price": 9.5}')

        await _call(surface, "supacrawl_extract", {"urls": URL, "schema": json.dumps(SCHEMA)})

        assert llm_wire.requests[0]["response_format"]["json_schema"]["schema"] == SCHEMA

    async def test_a_reply_that_never_conforms_fails_the_call(self, surface: FastMCP, llm_wire: LLMWire) -> None:
        llm_wire.replies += ['{"name": "Widget"}', '{"name": "Widget"}']

        with pytest.raises(ToolError, match="does not conform to the schema"):
            await _call(surface, "supacrawl_extract", {"urls": [URL], "schema": SCHEMA})

    async def test_every_url_failing_names_each_url(self, surface: FastMCP, llm_wire: LLMWire) -> None:
        llm_wire.replies += ['{"name": "Widget"}'] * 4
        other = "https://shop.example.com/gadget"

        with pytest.raises(ToolError) as exc_info:
            await _call(surface, "supacrawl_extract", {"urls": [URL, other], "schema": SCHEMA})

        assert URL in str(exc_info.value)
        assert other in str(exc_info.value)

    async def test_without_an_llm_the_call_fails_before_any_fetch(
        self, surface: FastMCP, mock_api_client, no_llm: None
    ) -> None:
        with pytest.raises(ToolError, match="SUPACRAWL_LLM_PROVIDER"):
            await _call(surface, "supacrawl_extract", {"urls": [URL], "schema": SCHEMA})

        mock_api_client.scrape_service.scrape.assert_not_called()

    async def test_neither_prompt_nor_schema_is_refused(self, surface: FastMCP, llm_wire: LLMWire) -> None:
        with pytest.raises(ToolError, match="prompt or schema"):
            await _call(surface, "supacrawl_extract", {"urls": [URL]})


class TestSummary:
    async def test_result_is_a_bounded_summary_with_the_source_url(self, surface: FastMCP, llm_wire: LLMWire) -> None:
        llm_wire.replies.append("A test page. " + "It says very little. " * 30)

        payload = await _call(surface, "supacrawl_summary", {"url": URL, "max_length": 25})

        assert payload["data"]["url"] == URL
        assert len(payload["data"]["summary"].split()) <= 25
        assert "Test content" not in json.dumps(payload)
        assert "markdown" not in payload["data"]

    async def test_without_an_llm_the_call_fails(self, surface: FastMCP, no_llm: None) -> None:
        with pytest.raises(ToolError, match="SUPACRAWL_LLM_PROVIDER"):
            await _call(surface, "supacrawl_summary", {"url": URL})
