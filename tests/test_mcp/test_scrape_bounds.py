"""``max_chars`` bounds a scrape response and reports what it cut; health reports the extras."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from supacrawl.mcp.tools.health import supacrawl_health
from supacrawl.mcp.tools.scrape import LARGE_RESPONSE_CHARS, supacrawl_scrape
from supacrawl.models import ScrapeData, ScrapeMetadata, ScrapeResult

pytestmark = [pytest.mark.mcp, pytest.mark.asyncio]


def _client(markdown: str) -> MagicMock:
    client = MagicMock()
    result = ScrapeResult(success=True, data=ScrapeData(markdown=markdown, metadata=ScrapeMetadata()))
    client.scrape_service.scrape = AsyncMock(return_value=result)
    return client


async def test_max_chars_cuts_markdown_and_reports_the_full_length():
    text = "\n".join(f"line {i}" for i in range(1000))
    out = await supacrawl_scrape(_client(text), url="https://example.com", max_chars=500)

    assert len(out["data"]["markdown"]) <= 500
    assert out["truncation"] == {
        "max_chars": 500,
        "truncated": True,
        "fields": {
            "markdown": {"total_chars": len(text), "returned_chars": len(out["data"]["markdown"]), "strategy": "head"}
        },
    }


async def test_a_page_within_the_cap_is_untouched_and_says_so():
    out = await supacrawl_scrape(_client("short"), url="https://example.com", max_chars=500)

    assert out["data"]["markdown"] == "short"
    assert out["truncation"]["truncated"] is False


async def test_no_cap_leaves_the_page_whole_but_warns_when_it_is_huge():
    text = "x" * (LARGE_RESPONSE_CHARS + 1)
    out = await supacrawl_scrape(_client(text), url="https://example.com")

    assert out["data"]["markdown"] == text
    assert "truncation" not in out
    assert any("max_chars" in w for w in out["warnings"])


async def test_no_cap_and_a_normal_page_carry_no_warning():
    out = await supacrawl_scrape(_client("fine"), url="https://example.com")

    assert not out["warnings"]


async def test_health_reports_whether_query_filtering_is_live():
    class _Services:
        browser_manager = None
        search_service = None

        def get_service_status(self) -> dict[str, bool]:
            return {"scrape": True}

    with patch("supacrawl.mcp.tools.health.available_extras", return_value={"query_filter": False}):
        out = await supacrawl_health(_Services(), verify_search=False)  # type: ignore[arg-type]

    assert out["components"]["extraction"]["query_filter"] is False


async def test_max_chars_sent_as_a_string_is_accepted():
    out = await supacrawl_scrape(_client("x" * 600), url="https://example.com", max_chars="500")  # type: ignore[arg-type]

    assert out["truncation"]["max_chars"] == 500


async def test_a_non_positive_max_chars_is_refused():
    from supacrawl.mcp.exceptions import SupacrawlValidationError

    with pytest.raises(SupacrawlValidationError):
        await supacrawl_scrape(_client("x"), url="https://example.com", max_chars=0)


async def test_section_returns_one_heading_and_reports_the_match():
    text = "# Doc\n\n## One\n\nfirst\n\n## Two\n\nsecond"
    out = await supacrawl_scrape(_client(text), url="https://example.com", section="#two")
    assert out["data"]["markdown"] == "## Two\n\nsecond"
    assert out["section"] == {"requested": "#two", "found": True, "total_chars": len(text)}


async def test_an_unmatched_section_returns_the_page_whole_and_says_so():
    text = "# Doc\n\n## One\n\nfirst"
    out = await supacrawl_scrape(_client(text), url="https://example.com", section="nope")
    assert out["data"]["markdown"] == text
    assert out["section"]["found"] is False
