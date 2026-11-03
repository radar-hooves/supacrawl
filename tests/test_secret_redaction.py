"""Supacrawl's own secret-redaction contract (mcp-servers rule 70 §'Tests').

The fleet suite proves ``make_masker`` works. This file proves only what a
fleet suite cannot: that supacrawl's OWN tool applies the masker, so a
credential-shaped key reaching a response envelope does not leave the process.

The binding was missing entirely until 2026-08-23. Not because anyone decided
supacrawl did not need one -- because the fleet's redaction gate could not see
an embedded server's package at all, and a ``.redaction-exempt`` dotfile
beside the container shell in mcp-servers made the blindness read as a
decision.
"""

from __future__ import annotations

import sys
from typing import Any, cast

import pytest

# The MCP layer is an optional extra, and importing it without fastmcp raises at
# COLLECTION time — before any marker can deselect the module — so a marker alone
# does not save a run that lacks the extra. This is what turned CI red from
# 2026-08-31: the public CI job cannot resolve the private mcp-common, and this
# file was the one MCP-importing module with neither guard.
pytest.importorskip("fastmcp", reason="supacrawl[mcp] extras not installed")

from supacrawl.mcp.tools import SUPACRAWL_SECRET_KEYS, mask_secrets
from supacrawl.mcp.tools.health import supacrawl_health
from supacrawl.services.registry import SupacrawlServices

pytestmark = pytest.mark.mcp

_SECRET = "sk-live-do-not-leak-0123456789"


class _StubServices:
    """The narrowest SupacrawlServices supacrawl_health actually touches."""

    browser_manager = None
    search_service = None

    def get_service_status(self) -> dict[str, bool]:
        return {"scrape": True}


def test_the_allow_list_carries_supacrawls_own_credential_fields() -> None:
    assert "captcha_api_key" in SUPACRAWL_SECRET_KEYS
    # A proxy URL routinely embeds user:pass -- a credential wearing a URL's clothes.
    assert "proxy" in SUPACRAWL_SECRET_KEYS


def test_the_masker_removes_a_credential_shaped_key() -> None:
    masked = mask_secrets({"captcha_api_key": _SECRET, "status": "healthy"})

    assert _SECRET not in str(masked)
    assert masked["status"] == "healthy"


@pytest.mark.asyncio
async def test_health_masks_a_credential_that_reaches_the_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive the real tool: a secret in a component must not reach the caller.

    Mutation check for this test: drop the ``mask_secrets(...)`` wrapper from
    ``supacrawl_health``'s success return and it fails on the ``not in``
    assertion.
    """

    def _llm_config_carrying_a_secret() -> dict[str, Any]:
        return {"configured": True, "provider": "openai", "api_key": _SECRET}

    # By sys.modules, not a dotted path: test_mcp_launch re-imports supacrawl.mcp.* and leaves the
    # package attributes pointing at its copies, which a dotted path would resolve through.
    monkeypatch.setattr(
        sys.modules["supacrawl.mcp.tools.health"],
        "_get_llm_config",
        _llm_config_carrying_a_secret,
    )

    result = await supacrawl_health(cast("SupacrawlServices", _StubServices()), verify_search=False)

    assert _SECRET not in str(result)
    # "unhealthy" is the except branch; either real verdict proves the success
    # return ran, and the search component's state is not this test's subject.
    assert result["status"] in {"healthy", "degraded"}


@pytest.mark.asyncio
async def test_health_leaves_the_non_credential_body_intact() -> None:
    """Over-redaction breaks the report; the structural fields must survive."""
    result = await supacrawl_health(cast("SupacrawlServices", _StubServices()), verify_search=False)

    assert result["services"] == {"scrape": True}
    assert "components" in result
    assert "version" in result
