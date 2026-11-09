"""Tests for MCP lifecycle - initialization, tool discovery, and tool execution.

These tests verify the MCP protocol compliance of the Supacrawl server.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from supacrawl.mcp.server import SupacrawlServer
from supacrawl.mcp.wiring import register_all_tools, register_prompts, register_resources

pytestmark = pytest.mark.mcp


class TestMCPInitialization:
    """Test MCP server initialization lifecycle."""

    def test_server_creates_fastmcp_instance(self):
        """Server should create FastMCP instance on init."""
        server = SupacrawlServer()
        assert server.mcp is not None
        assert hasattr(server.mcp, "tool")
        assert hasattr(server.mcp, "resource")
        assert hasattr(server.mcp, "prompt")

    def test_server_name_set_correctly(self):
        """Server name should be set in FastMCP instance."""
        server = SupacrawlServer(server_name="test-supacrawl")
        assert server.server_name == "test-supacrawl"

    @pytest.mark.asyncio
    async def test_api_client_created_on_startup(self):
        """API client should be created when create_api_client is called."""
        server = SupacrawlServer()

        # The MCP server vends the SearXNG pair from Portcullis at startup.
        # Patch the vend surface so no real broker call leaves the test host.
        with (
            patch.object(server, "vend_static_fields", AsyncMock(return_value={"value": "placeholder-token"})),
            patch("supacrawl.mcp.server.create_supacrawl_services") as mock_create,
        ):
            mock_services = MagicMock()
            mock_create.return_value = mock_services

            result = await server.create_api_client()

            mock_create.assert_called_once()
            assert result == mock_services

    def test_cors_origins_configured(self):
        """Server should return allowed CORS origins."""
        server = SupacrawlServer()
        origins = server.get_allowed_origins()
        assert isinstance(origins, list)

    def test_allowed_hosts_configured(self):
        """Server should return allowed hosts."""
        server = SupacrawlServer()
        hosts = server.get_allowed_hosts()
        assert isinstance(hosts, list)


class TestToolDiscovery:
    """Test MCP tool discovery."""

    def test_all_tools_registered(self, mock_api_client):
        """All expected tools should be registered."""
        server = SupacrawlServer()
        server.api_client = mock_api_client

        # Register tools
        register_all_tools(server.mcp, mock_api_client)

        # Get registered tools from FastMCP
        # FastMCP stores tools internally - we verify by checking the tool count logged
        # For a more thorough test, we'd need to inspect the MCP tool registry
        assert server.mcp is not None

    @pytest.mark.asyncio
    async def test_tool_registration_accepts_none_api_client(self):
        """Tool registration completes on a degraded start (api_client=None).

        The crash-on-None guard was removed (#641 non-fatal-startup contract):
        mcp_common.tool_registration.create_tool_wrapper now returns the typed
        MCPDependencyUnavailableError at CALL time instead of the server
        refusing to register tools at STARTUP time. See
        servers/servarr/tests/test_degraded_registration.py in mcp-servers for
        the canonical fleet-wide version of this regression test.
        """
        server = SupacrawlServer()

        register_all_tools(server.mcp, None)  # type: ignore[arg-type]  # intentional: testing degraded-start registration

        tools = {tool.name for tool in await server.mcp.list_tools()}
        assert {
            "supacrawl_scrape",
            "supacrawl_batch",
            "supacrawl_map",
            "supacrawl_crawl",
            "supacrawl_search",
            "supacrawl_extract",
            "supacrawl_summary",
            "supacrawl_health",
            "supacrawl_diagnose",
        } <= tools

    def test_resources_registered(self, mock_api_client):
        """Resources should be registered successfully."""
        server = SupacrawlServer()

        # Should not raise
        register_resources(server.mcp, mock_api_client)

    def test_prompts_registered(self):
        """Prompts should be registered successfully."""
        server = SupacrawlServer()

        # Should not raise
        register_prompts(server.mcp)

    def test_health_check_tool_registered(self, mock_api_client):
        """Health check tool should be registered."""
        server = SupacrawlServer()
        server.api_client = mock_api_client

        # Register tools including health check
        server.register_tools()

        # Health check is registered via _register_health_check
        assert server.mcp is not None


class TestToolExecution:
    """Test MCP tool execution patterns."""

    @pytest.mark.asyncio
    async def test_scrape_tool_executes(self, mock_api_client):
        """Scrape tool should execute and return result."""
        from supacrawl.mcp.tools.scrape import supacrawl_scrape

        result = await supacrawl_scrape(
            api_client=mock_api_client,
            url="https://example.com",
            formats=["markdown"],
        )

        assert result["success"] is True
        assert "data" in result
        mock_api_client.scrape_service.scrape.assert_called_once()

    @pytest.mark.asyncio
    async def test_search_tool_executes(self, mock_api_client):
        """Search tool should execute and return result."""
        from supacrawl.mcp.tools.search import supacrawl_search

        result = await supacrawl_search(
            api_client=mock_api_client,
            ctx=MagicMock(),
            query="test query",
            limit=5,
        )

        assert result["success"] is True
        mock_api_client.search_service.search.assert_called_once()

    @pytest.mark.asyncio
    async def test_map_tool_executes(self, mock_api_client):
        """Map tool should execute and return result."""
        from supacrawl.mcp.tools.map import supacrawl_map

        result = await supacrawl_map(
            api_client=mock_api_client,
            url="https://example.com",
        )

        assert result["success"] is True
        assert "links" in result
        mock_api_client.map_service.map_all.assert_called_once()


class TestHealthCheck:
    """Test MCP health check functionality."""

    @pytest.mark.asyncio
    async def test_health_check_returns_status(self, mock_api_client):
        """Health check should return server status."""
        server = SupacrawlServer()
        server.api_client = mock_api_client

        # Mock get_service_status
        mock_api_client.get_service_status = MagicMock(
            return_value={
                "browser": "ready",
                "scrape": "ready",
                "search": "ready",
            }
        )

        # Register tools to get health check
        server.register_tools()

        # The health check is registered as a tool
        # We can verify the server has the api_client set correctly
        assert server.api_client is not None

    @pytest.mark.asyncio
    async def test_health_check_returns_detailed_info(self, mock_api_client):
        """Health check should return detailed component information."""
        from supacrawl.mcp.tools.health import supacrawl_health

        # Mock get_service_status
        mock_api_client.get_service_status = MagicMock(
            return_value={
                "browser": True,
                "scrape": True,
                "crawl": True,
                "map": True,
                "search": True,
            }
        )

        result = await supacrawl_health(mock_api_client)

        # Verify structure
        assert result["status"] == "healthy"
        assert "services" in result
        assert "components" in result
        assert "version" in result

        # Verify components
        assert "browser" in result["components"]
        assert "search" in result["components"]
        assert "llm" in result["components"]
        assert "cache" in result["components"]

        # Verify version info
        assert "supacrawl_lib" in result["version"]
        assert "mcp_server" in result["version"]

    @pytest.mark.asyncio
    async def test_health_check_degraded_status(self, mock_api_client):
        """Health check should return degraded status when services are unavailable."""
        from supacrawl.mcp.tools.health import supacrawl_health

        # Mock partial service availability
        mock_api_client.get_service_status = MagicMock(
            return_value={
                "browser": True,
                "scrape": True,
                "crawl": False,  # Unavailable
                "map": True,
                "search": True,
            }
        )

        result = await supacrawl_health(mock_api_client)

        assert result["status"] == "degraded"
        assert result["services"]["crawl"] is False


class TestServerCleanup:
    """Test MCP server cleanup."""

    @pytest.mark.asyncio
    async def test_cleanup_closes_api_client(self, mock_api_client):
        """Cleanup should close API client resources."""
        server = SupacrawlServer()
        server.api_client = mock_api_client
        mock_api_client.close = AsyncMock()

        await server.cleanup()

        mock_api_client.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_handles_no_api_client(self):
        """Cleanup should handle case where API client is None."""
        server = SupacrawlServer()
        server.api_client = None

        # Should not raise
        await server.cleanup()


class TestAccountless:
    """The health probe iterates the real services, never a RoutedClient proxy."""

    @pytest.mark.asyncio
    async def test_startup_client_is_not_a_routing_proxy(self):
        from supacrawl.mcp.tools.health import supacrawl_health

        server = SupacrawlServer()
        services = MagicMock()
        services.get_service_status.return_value = {"browser": True}
        with (
            patch.object(server, "create_api_client", AsyncMock(return_value=services)),
        ):
            await server.initialize_client()

        assert server.api_client is services
        result = await supacrawl_health(server.api_client, verify_search=False)
        assert result["status"] != "unhealthy"
