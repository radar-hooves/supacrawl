"""The ``supacrawl-mcp`` console script.

The MCP server is built on mcp-common, a framework that is not published on
PyPI, so a plain ``pip install supacrawl[mcp]`` can start the CLI and the REST
API but not the server. This launcher turns the resulting ImportError into one
sentence saying so, instead of a traceback from inside the package.
"""

from __future__ import annotations

import sys

_PRIVATE = {"mcp_common", "api_common"}
_EXTRA = {"fastmcp", "mcp", "pydantic_settings", "dotenv", "anyio", "starlette"}


def main() -> None:
    try:
        from supacrawl.mcp.server import main as serve
    except ImportError as exc:
        root = (exc.name or "").split(".")[0]
        if root in _PRIVATE:
            sys.exit(
                "supacrawl-mcp needs mcp-common, the MCP framework the server is built on, which is not "
                "published on PyPI. The server is deployed from the container image described in the README; "
                "the CLI, the Python library and the REST API work without it."
            )
        if root in _EXTRA or "supacrawl[mcp]" in str(exc):
            sys.exit("supacrawl-mcp needs the mcp extra: pip install 'supacrawl[mcp]'")
        raise
    serve()
