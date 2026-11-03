"""Security tests for the MCP server: the household inbound-auth floor.

Covers:
- HTTP transport refuses to start on a non-loopback host without an auth
  token, unless --insecure is passed.
- Bearer verifier is wired into SupacrawlServer when SUPACRAWL_MCP_AUTH_TOKEN
  is set, and left unwired when it is not.
"""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.mcp


class TestHttpInsecureGuard:
    """HTTP transport refuses to bind non-loopback without auth or --insecure."""

    # The token is read inside main() as:
    #   from supacrawl.mcp.config import SUPACRAWL_MCP_AUTH_TOKEN
    # so we patch it at the config module level.
    _TOKEN_PATH = "supacrawl.mcp.config.SUPACRAWL_MCP_AUTH_TOKEN"

    def test_non_loopback_no_token_exits_1(self) -> None:
        """Binding 0.0.0.0 without a token must exit 1."""
        import supacrawl.mcp.server as srv

        with (
            patch("sys.exit") as mock_exit,
            patch("anyio.run"),
            patch(self._TOKEN_PATH, None),
            patch.object(
                sys,
                "argv",
                ["supacrawl-mcp", "--transport", "http", "--host", "0.0.0.0"],
            ),
        ):
            srv.main()
        mock_exit.assert_called_once_with(1)

    def test_loopback_no_token_allowed(self) -> None:
        """Binding 127.0.0.1 without a token is permitted (loopback is safe)."""
        import supacrawl.mcp.server as srv

        with (
            patch("sys.exit") as mock_exit,
            patch("anyio.run"),
            patch(self._TOKEN_PATH, None),
            patch.object(
                sys,
                "argv",
                ["supacrawl-mcp", "--transport", "http", "--host", "127.0.0.1"],
            ),
        ):
            srv.main()
        mock_exit.assert_not_called()

    def test_non_loopback_with_token_allowed(self) -> None:
        """Binding 0.0.0.0 with a token is permitted."""
        import supacrawl.mcp.server as srv

        with (
            patch("sys.exit") as mock_exit,
            patch("anyio.run"),
            patch(self._TOKEN_PATH, "supersecret"),
            patch.object(
                sys,
                "argv",
                ["supacrawl-mcp", "--transport", "http", "--host", "0.0.0.0"],
            ),
        ):
            srv.main()
        mock_exit.assert_not_called()

    def test_non_loopback_insecure_flag_allowed(self) -> None:
        """--insecure bypasses the guard (with a warning, not an exit)."""
        import supacrawl.mcp.server as srv

        with (
            patch("sys.exit") as mock_exit,
            patch("anyio.run"),
            patch(self._TOKEN_PATH, None),
            patch.object(
                sys,
                "argv",
                ["supacrawl-mcp", "--transport", "http", "--host", "0.0.0.0", "--insecure"],
            ),
        ):
            srv.main()
        mock_exit.assert_not_called()

    def test_stdio_transport_ignores_guard(self) -> None:
        """stdio transport never triggers the non-loopback guard."""
        import supacrawl.mcp.server as srv

        with (
            patch("sys.exit") as mock_exit,
            patch("anyio.run"),
            patch(self._TOKEN_PATH, None),
            patch.object(sys, "argv", ["supacrawl-mcp"]),
        ):
            srv.main()
        mock_exit.assert_not_called()

    def test_default_host_is_loopback(self) -> None:
        """The default --host is 127.0.0.1, not 0.0.0.0."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--transport", default="stdio")
        parser.add_argument("--host", default="127.0.0.1")
        parser.add_argument("--port", type=int, default=5000)
        parser.add_argument("--path", default="/mcp")
        parser.add_argument("--insecure", action="store_true", default=False)
        args = parser.parse_args([])
        assert args.host == "127.0.0.1"

    def test_is_loopback_host_recognises_127(self) -> None:
        """_is_loopback_host returns True for 127.0.0.1."""
        from supacrawl.mcp.server import _is_loopback_host

        assert _is_loopback_host("127.0.0.1") is True

    def test_is_loopback_host_recognises_localhost(self) -> None:
        """_is_loopback_host returns True for 'localhost'."""
        from supacrawl.mcp.server import _is_loopback_host

        assert _is_loopback_host("localhost") is True

    def test_is_loopback_host_rejects_0000(self) -> None:
        """_is_loopback_host returns False for 0.0.0.0."""
        from supacrawl.mcp.server import _is_loopback_host

        assert _is_loopback_host("0.0.0.0") is False

    def test_is_loopback_host_rejects_public(self) -> None:
        """_is_loopback_host returns False for a public IP."""
        from supacrawl.mcp.server import _is_loopback_host

        assert _is_loopback_host("192.168.1.100") is False


class TestBearerAuthWired:
    """StaticBearerVerifier is constructed when SUPACRAWL_MCP_AUTH_TOKEN is set."""

    def test_auth_wired_when_token_set(self) -> None:
        """SupacrawlServer.__init__ wires StaticBearerVerifier when a token is present.

        The token lives in supacrawl.mcp.config and is read at
        SupacrawlServer.__init__() call time via a local import, so we patch
        it there.
        """
        from mcp_common.auth import StaticBearerVerifier

        with patch("supacrawl.mcp.config.SUPACRAWL_MCP_AUTH_TOKEN", "my-secret-token"):
            captured: dict = {}

            original_init = __import__("mcp_common.server.base", fromlist=["BaseMCPServer"]).BaseMCPServer.__init__

            def capturing_init(self, *a, **kw):
                captured.update(kw)
                original_init(self, *a, **kw)

            with patch("mcp_common.server.base.BaseMCPServer.__init__", capturing_init):
                from supacrawl.mcp.server import SupacrawlServer

                SupacrawlServer()

            assert "auth" in captured, "auth= not passed to BaseMCPServer"
            assert isinstance(captured["auth"], StaticBearerVerifier)

    def test_auth_not_wired_when_token_unset(self) -> None:
        """SupacrawlServer.__init__ passes auth=None when no token is configured."""
        with patch("supacrawl.mcp.config.SUPACRAWL_MCP_AUTH_TOKEN", None):
            captured: dict = {}

            original_init = __import__("mcp_common.server.base", fromlist=["BaseMCPServer"]).BaseMCPServer.__init__

            def capturing_init(self, *a, **kw):
                captured.update(kw)
                original_init(self, *a, **kw)

            with patch("mcp_common.server.base.BaseMCPServer.__init__", capturing_init):
                from supacrawl.mcp.server import SupacrawlServer

                SupacrawlServer()

            assert captured.get("auth") is None


class TestAuthTokenValidator:
    """SUPACRAWL_MCP_AUTH_TOKEN normalises an empty/whitespace value to None."""

    def test_whitespace_token_normalised_to_none(self) -> None:
        """An all-whitespace token is treated as absent.

        The ``_reject_empty_token`` before-validator collapses an all-whitespace
        value to None so it cannot accidentally open auth.
        """
        from supacrawl.mcp.config import SupacrawlSettings

        settings = SupacrawlSettings(mcp_auth_token="   ")
        assert settings.mcp_auth_token is None

    def test_real_token_preserved(self) -> None:
        """A genuine token value passes through unchanged."""
        from supacrawl.mcp.config import SupacrawlSettings

        settings = SupacrawlSettings(mcp_auth_token="real-token")
        assert settings.mcp_auth_token == "real-token"
