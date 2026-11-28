"""``max_chars`` bounds a scrape response and reports what it cut; health reports the extras."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from supacrawl.mcp.tools.health import supacrawl_health
from supacrawl.mcp.tools.scrape import LARGE_RESPONSE_CHARS, supacrawl_scrape
from supacrawl.models import ScrapeData, ScrapeMetadata, ScrapeResult, StructuredData

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


async def test_max_chars_bounds_structured_data_and_keeps_the_small_product_node():
    product = {"@type": "Product", "name": "Glass set", "offers": {"price": "29.00"}}
    sd = StructuredData(
        json_ld=[product, {"@type": "WebPage", "blob": "y" * 5000}],
        next_data={"props": "x" * 20000},
        opengraph={"og:title": "Glass set"},
    )
    client = MagicMock()
    result = ScrapeResult(success=True, data=ScrapeData(markdown="m", structured_data=sd, metadata=ScrapeMetadata()))
    client.scrape_service.scrape = AsyncMock(return_value=result)

    out = await supacrawl_scrape(client, url="https://example.com", formats=["structuredData"], max_chars=1000)

    kept = out["data"]["structured_data"]
    assert "next_data" not in kept
    assert kept["json_ld"][0] == product
    assert out["truncation"]["fields"]["structured_data"]["returned_chars"] <= 1000


async def test_a_product_node_heavy_with_reviews_is_slimmed_not_dropped():
    product = {"@type": "Product", "name": "Glass set", "offers": {"price": "29.00"}, "review": [{"t": "z" * 900}] * 30}
    client = MagicMock()
    sd = StructuredData(json_ld=[product])
    result = ScrapeResult(success=True, data=ScrapeData(markdown="m", structured_data=sd, metadata=ScrapeMetadata()))
    client.scrape_service.scrape = AsyncMock(return_value=result)

    out = await supacrawl_scrape(client, url="https://example.com", formats=["structuredData"], max_chars=1500)

    node = out["data"]["structured_data"]["json_ld"][0]
    assert node["offers"] == {"price": "29.00"} and len(node["review"]) <= 3


async def test_largest_product_is_compacted_before_losing_its_offer():
    from supacrawl.mcp.tools.scrape import _bound_structured_data

    product = {
        "@type": "Product",
        "name": "Kitchen Pro Airlock Glass Container Set 10pc",
        "offers": {"@type": "Offer", "price": 99.95, "priceCurrency": "AUD"},
        "additionalProperty": {f"prop{i}": "x" * 500 for i in range(500)},
    }
    offer = {"@type": "Offer", "price": 59.95, "priceCurrency": "AUD"}
    data = {"structured_data": {"json_ld": [{"@type": "WebSite"}, {"@type": "ItemList"}, product, offer]}}

    report = _bound_structured_data(data, 450)

    assert report["returned_chars"] <= 450
    kept_product, kept_offer = data["structured_data"]["json_ld"]
    assert kept_product["offers"] == product["offers"]
    assert kept_offer == offer
