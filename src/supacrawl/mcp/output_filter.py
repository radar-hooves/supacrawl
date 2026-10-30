"""Deployer-supplied text filter over everything the MCP server returns.

``SUPACRAWL_OUTPUT_FILTER=package.module:callable`` names a plain ``str -> str``
function, resolved once at start. A FastMCP middleware passes every tool
result through it, so a tool added later is covered without touching it.

Fail closed: a filter that cannot be resolved stops the server at start; one
that raises (or returns a non-string) fails the tool call with an error that
names the setting and carries no page content.
"""

from __future__ import annotations

import importlib
import json
import re
from collections.abc import Callable
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware
from mcp.types import EmbeddedResource, ImageContent, TextContent, TextResourceContents

SETTING = "SUPACRAWL_OUTPUT_FILTER"

# Binary payloads (base64 screenshots and PDFs) are not text: passing them to a
# text filter would corrupt them. Skipped only under these keys and only when
# the value really is a long base64 run, so a short string under a key that
# happens to be named "pdf" is still filtered.
_BINARY_KEYS = frozenset({"screenshot", "screenshots", "pdf"})
_BASE64 = re.compile(r"[A-Za-z0-9+/=\s]{256,}")


class OutputFilterError(ValueError):
    """The configured output filter could not be resolved."""


def resolve_filter(spec: str | None) -> Callable[[str], str] | None:
    """Import ``module:callable``; ``None`` when unset. Raises ``OutputFilterError``."""
    if spec is None or not spec.strip():
        return None
    module_name, sep, attr_path = spec.strip().partition(":")
    if not (sep and module_name and attr_path):
        raise OutputFilterError(f"{SETTING} must be 'package.module:callable', got an unparseable value")
    try:
        target: Any = importlib.import_module(module_name)
        for attr in attr_path.split("."):
            target = getattr(target, attr)
    except Exception as exc:  # noqa: BLE001 - any import-time failure is a config failure
        raise OutputFilterError(f"{SETTING} could not be imported ({type(exc).__name__})") from exc
    if not callable(target):
        raise OutputFilterError(f"{SETTING} does not name a callable")
    return target


class OutputFilterMiddleware(Middleware):
    """Run every text field of a tool result through the deployer's filter."""

    def __init__(self, text_filter: Callable[[str], str]):
        self._filter = text_filter

    def _apply(self, text: str) -> str:
        try:
            out = self._filter(text)
        except Exception:
            raise ToolError(f"{SETTING} filter failed; response withheld") from None
        if not isinstance(out, str):
            raise ToolError(f"{SETTING} filter did not return a string; response withheld")
        return out

    def _walk(self, node: Any, key: str | None = None) -> Any:
        if isinstance(node, str):
            if key in _BINARY_KEYS and _BASE64.fullmatch(node):
                return node
            return self._apply(node) if node else node
        if isinstance(node, dict):
            return {k: self._walk(v, k if isinstance(k, str) else None) for k, v in node.items()}
        if isinstance(node, list | tuple):
            return [self._walk(v, key) for v in node]
        return node

    def _content_block(self, block: Any, original: Any, filtered: Any) -> Any:
        if isinstance(block, TextContent):
            try:
                same = original is not None and json.loads(block.text) == original
            except ValueError:
                same = False
            if same:
                return block.model_copy(update={"text": json.dumps(filtered, ensure_ascii=False)})
            return block.model_copy(update={"text": self._apply(block.text)})
        if isinstance(block, ImageContent):
            return block
        if isinstance(block, EmbeddedResource) and isinstance(block.resource, TextResourceContents):
            res = block.resource.model_copy(update={"text": self._apply(block.resource.text)})
            return block.model_copy(update={"resource": res})
        # Anything else (resource links, audio, ...) cannot be proven filtered.
        raise ToolError(f"{SETTING}: unsupported content block; response withheld")

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        try:
            result = await call_next(context)
        except ToolError as exc:
            raise ToolError(self._apply(str(exc))) from None
        original = result.structured_content
        filtered = self._walk(original) if original is not None else None
        result.content = [self._content_block(b, original, filtered) for b in result.content]
        result.structured_content = filtered
        return result
