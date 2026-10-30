"""Tests for the deployer-supplied output filter (SUPACRAWL_OUTPUT_FILTER)."""

import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from supacrawl.mcp.config import SupacrawlSettings
from supacrawl.mcp.output_filter import OutputFilterError, OutputFilterMiddleware, resolve_filter
from supacrawl.mcp.server import SupacrawlServer
from supacrawl.mcp.wiring import register_all_tools

pytestmark = pytest.mark.mcp

# Fake filters live in this module and are named by its own import path.
CALLS: list[str] = []


def mask(text: str) -> str:
    CALLS.append(text)
    return text.replace("Test content", "XXXX").replace("example.com", "masked.invalid").replace("Example", "Masked")


def explode(text: str) -> str:
    raise RuntimeError(f"cannot handle {text}")


def not_a_string(text: str):
    return 42


not_callable = "just a string"

MASK = f"{__name__}:mask"


def _server(spec: str | None, api_client) -> SupacrawlServer:
    server = SupacrawlServer(settings=SupacrawlSettings(output_filter=spec))
    register_all_tools(server.mcp, api_client)
    return server


async def _call(server: SupacrawlServer, tool: str, args: dict) -> tuple[str, dict | None]:
    async with Client(server.mcp) as client:
        result = await client.call_tool(tool, args)
    return result.content[0].text, result.structured_content


class TestResolution:
    def test_unset_is_no_filter(self):
        assert resolve_filter(None) is None
        assert resolve_filter("  ") is None

    def test_resolves_callable(self):
        assert resolve_filter(MASK) is mask

    @pytest.mark.parametrize(
        "spec",
        [
            "no_colon_here",
            "no.such.module:mask",
            f"{__name__}:missing",
            f"{__name__}:not_callable",
        ],
    )
    def test_unusable_spec_names_the_setting(self, spec):
        with pytest.raises(OutputFilterError, match="SUPACRAWL_OUTPUT_FILTER"):
            resolve_filter(spec)

    def test_server_refuses_to_start_unfiltered(self):
        with pytest.raises(OutputFilterError, match="SUPACRAWL_OUTPUT_FILTER"):
            SupacrawlServer(settings=SupacrawlSettings(output_filter="no.such.module:mask"))

    def test_unset_adds_no_middleware(self):
        before = len(SupacrawlServer(settings=SupacrawlSettings()).mcp.middleware)
        after = len(SupacrawlServer(settings=SupacrawlSettings(output_filter=MASK)).mcp.middleware)
        assert after == before + 1

    def test_filter_is_outermost(self):
        server = SupacrawlServer(settings=SupacrawlSettings(output_filter=MASK))
        assert isinstance(server.mcp.middleware[0], OutputFilterMiddleware)


class TestToolFamilies:
    @pytest.mark.asyncio
    async def test_scrape_markdown_and_html_filtered(self, mock_api_client):
        text, structured = await _call(
            _server(MASK, mock_api_client), "supacrawl_scrape", {"url": "https://example.com"}
        )
        assert "XXXX" in json.dumps(structured)
        assert "Test content" not in text
        assert "Test content" not in json.dumps(structured)
        assert json.loads(text) == structured

    @pytest.mark.asyncio
    async def test_map_urls_filtered(self, mock_api_client):
        text, structured = await _call(_server(MASK, mock_api_client), "supacrawl_map", {"url": "https://example.com"})
        assert "masked.invalid" in text
        assert "example.com/page" not in text.replace("https://example.com", "")
        assert "example.com/page" not in json.dumps(structured)

    @pytest.mark.asyncio
    async def test_search_filtered(self, mock_api_client):
        text, structured = await _call(_server(MASK, mock_api_client), "supacrawl_search", {"query": "anything"})
        assert "Masked 0" in text
        assert "Example 0" not in text
        assert "Example 0" not in json.dumps(structured)

    @pytest.mark.asyncio
    async def test_crawl_filtered(self, mock_api_client):
        text, _ = await _call(_server(MASK, mock_api_client), "supacrawl_crawl", {"url": "https://example.com"})
        assert "Page 1" in text  # unaffected by this filter, but the call went through it
        assert CALLS

    @pytest.mark.asyncio
    async def test_unset_returns_untouched(self, mock_api_client):
        text, _ = await _call(_server(None, mock_api_client), "supacrawl_scrape", {"url": "https://example.com"})
        assert "Test content" in text

    @pytest.mark.asyncio
    async def test_one_call_per_text_field(self, mock_api_client):
        CALLS.clear()
        await _call(_server(MASK, mock_api_client), "supacrawl_scrape", {"url": "https://example.com"})
        assert CALLS.count("# Test Page\n\nTest content") == 1


class TestFailClosed:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("spec", [f"{__name__}:explode", f"{__name__}:not_a_string"])
    async def test_failing_filter_withholds_response(self, mock_api_client, spec):
        server = _server(spec, mock_api_client)
        async with Client(server.mcp) as client:
            with pytest.raises(ToolError) as exc:
                await client.call_tool("supacrawl_scrape", {"url": "https://example.com"})
        message = str(exc.value)
        assert "SUPACRAWL_OUTPUT_FILTER" in message
        assert "Test content" not in message
        assert "Test Page" not in message


def upper_values(text: str) -> str:
    return text if text in {"data", "screenshot", "pdf", "markdown", "n", "ok"} else text.upper()


class TestWalk:
    def test_binary_payload_skipped_text_filtered(self):
        mw = OutputFilterMiddleware(upper_values)
        b64 = "QUJD" * 100
        out = mw._walk({"data": {"screenshot": b64, "pdf": "short", "markdown": "abc", "n": 3, "ok": True}})
        assert out["data"]["screenshot"] == b64
        assert out["data"]["pdf"] == "SHORT"
        assert out["data"]["markdown"] == "ABC"
        assert out["data"]["n"] == 3 and out["data"]["ok"] is True

    def test_base64_run_under_other_key_is_filtered(self):
        mw = OutputFilterMiddleware(upper_values)
        assert mw._walk({"markdown": "abcd" * 100})["markdown"] == "ABCD" * 100


class TestSeamCoversNewTools:
    @pytest.mark.asyncio
    async def test_unregistered_in_advance_tool_and_error_text_are_filtered(self, mock_api_client):
        server = _server(MASK, mock_api_client)

        @server.mcp.tool
        async def supacrawl_future(x: str) -> str:
            """A tool added later, returning a bare string."""
            return "Test content in a future tool"

        @server.mcp.tool
        async def supacrawl_refuses(x: str) -> str:
            """A tool whose error carries page text."""
            raise ToolError("page said Test content")

        text, _ = await _call(server, "supacrawl_future", {"x": "a"})
        assert "XXXX" in text and "Test content" not in text
        async with Client(server.mcp) as client:
            with pytest.raises(ToolError) as exc:
                await client.call_tool("supacrawl_refuses", {"x": "a"})
        assert "XXXX" in str(exc.value) and "Test content" not in str(exc.value)


def mask_digits_and_name(text: str) -> str:
    return text.replace("Zqxvorn Plimbotte", "XXXX").replace("881233456677", "NNNN")


MASK_NAME = f"{__name__}:mask_digits_and_name"


class TestBypasses:
    @pytest.mark.asyncio
    async def test_real_tool_error_via_map_exception_is_filtered(self, mock_api_client):
        from supacrawl.mcp.exceptions import map_exception

        server = _server(MASK_NAME, mock_api_client)

        @server.mcp.tool
        async def supacrawl_boom(x: str) -> str:
            """Raises as the real tools do."""
            try:
                raise RuntimeError("locator resolved to <td>Zqxvorn Plimbotte</td>")
            except RuntimeError as exc:
                raise map_exception(exc, endpoint="/x", correlation_id="abcd1234") from exc

        async with Client(server.mcp) as client:
            with pytest.raises(ToolError) as exc:
                await client.call_tool("supacrawl_boom", {"x": "a"})
        assert "XXXX" in str(exc.value) and "Zqxvorn" not in str(exc.value)

    def test_keys_and_ints_are_filtered(self):
        mw = OutputFilterMiddleware(mask_digits_and_name)
        out = mw._walk({"Zqxvorn Plimbotte": "enrolled", "account": 881233456677, "n": 3, "ok": True})
        assert out == {"XXXX": "enrolled", "account": "NNNN", "n": 3, "ok": True}

    def test_key_collision_fails_closed(self):
        mw = OutputFilterMiddleware(lambda t: "k" if t.startswith("k") else t)
        with pytest.raises(ToolError):
            mw._walk({"k1": 1, "k2": 2})

    def test_words_under_binary_key_are_filtered(self):
        mw = OutputFilterMiddleware(upper_values)
        assert mw._walk({"pdf": "word " * 60})["pdf"] == "WORD " * 60

    def test_real_base64_under_binary_key_is_skipped(self):
        import base64

        blob = base64.b64encode(bytes(range(256)) * 2).decode()
        mw = OutputFilterMiddleware(upper_values)
        assert mw._walk({"screenshot": blob})["screenshot"] == blob
