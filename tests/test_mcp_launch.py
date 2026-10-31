"""``supacrawl-mcp`` on an install without the private framework says what is missing, in one sentence."""

from __future__ import annotations

import builtins
import sys
from typing import Any

import pytest

from supacrawl import mcp_launch

pytestmark = pytest.mark.unit


def _hide(monkeypatch: pytest.MonkeyPatch, module: str) -> None:
    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == module or name.startswith(module + "."):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    for loaded in [
        m for m in sys.modules if m == module or m.startswith(module + ".") or m.startswith("supacrawl.mcp")
    ]:
        monkeypatch.delitem(sys.modules, loaded, raising=False)


def test_a_missing_mcp_common_is_named_without_a_traceback(monkeypatch):
    _hide(monkeypatch, "mcp_common")
    with pytest.raises(SystemExit) as exc:
        mcp_launch.main()
    assert "mcp-common" in str(exc.value) and "not published on PyPI" in str(exc.value)


def test_a_missing_mcp_extra_names_the_extra(monkeypatch):
    _hide(monkeypatch, "fastmcp")
    with pytest.raises(SystemExit) as exc:
        mcp_launch.main()
    assert "supacrawl[mcp]" in str(exc.value)
