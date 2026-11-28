"""Tests for deterministic embedded structured-data extraction (#120)."""

from unittest.mock import AsyncMock

import pytest

from supacrawl.services.browser import BrowserManager, PageContent
from supacrawl.services.scrape import ScrapeService
from supacrawl.services.structured_data import extract_structured_data

JSON_LD_HTML = """
<html><head>
<script type="application/ld+json">
{"@context": "https://schema.org", "@type": "Product", "name": "Widget", "offers": {"@type": "Offer", "price": "19.99", "priceCurrency": "USD"}}
</script>
<meta property="og:title" content="Widget — Buy Now">
<meta property="og:type" content="product">
</head><body><h1>Widget</h1></body></html>
"""

GRAPH_HTML = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@graph":[{"@type":"Organization","name":"Acme"},{"@type":"WebSite","name":"Acme Site"}]}
</script>
</head><body>x</body></html>
"""

NEXT_DATA_HTML = """
<html><head></head><body>
<script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"title":"Hydrated"}},"page":"/p"}</script>
</body></html>
"""

MICRODATA_HTML = """
<html><body>
<div itemscope itemtype="https://schema.org/Person">
  <span itemprop="name">Ada Lovelace</span>
  <a itemprop="url" href="/ada">profile</a>
  <div itemprop="address" itemscope itemtype="https://schema.org/PostalAddress">
    <span itemprop="addressLocality">London</span>
  </div>
</div>
</body></html>
"""


class TestExtractStructuredData:
    def test_json_ld_object(self) -> None:
        sd = extract_structured_data(JSON_LD_HTML)
        assert sd.json_ld is not None and len(sd.json_ld) == 1
        assert sd.json_ld[0]["@type"] == "Product"
        assert sd.json_ld[0]["offers"]["price"] == "19.99"

    def test_json_ld_graph_is_flattened(self) -> None:
        sd = extract_structured_data(GRAPH_HTML)
        assert sd.json_ld is not None
        types = {obj["@type"] for obj in sd.json_ld}
        assert types == {"Organization", "WebSite"}

    def test_nested_graph_is_flattened(self) -> None:
        html = (
            '<html><head><script type="application/ld+json">'
            '{"@context":"x","@graph":[{"@graph":[{"@type":"A"},{"@type":"B"}]},{"@type":"C"}]}'
            "</script></head><body>x</body></html>"
        )
        sd = extract_structured_data(html)
        assert sd.json_ld is not None
        assert {obj.get("@type") for obj in sd.json_ld} == {"A", "B", "C"}

    def test_opengraph(self) -> None:
        sd = extract_structured_data(JSON_LD_HTML)
        assert sd.opengraph == {"og:title": "Widget — Buy Now", "og:type": "product"}

    def test_next_data(self) -> None:
        sd = extract_structured_data(NEXT_DATA_HTML)
        assert sd.next_data is not None
        assert sd.next_data["props"]["pageProps"]["title"] == "Hydrated"

    def test_microdata_nested(self) -> None:
        sd = extract_structured_data(MICRODATA_HTML, base_url="https://example.com")
        assert sd.microdata is not None and len(sd.microdata) == 1
        item = sd.microdata[0]
        assert item["type"] == "https://schema.org/Person"
        props = item["properties"]
        assert props["name"] == "Ada Lovelace"
        assert props["url"] == "https://example.com/ada"  # resolved against base_url
        # Nested itemscope becomes a nested item, not flattened into the parent.
        assert props["address"]["type"] == "https://schema.org/PostalAddress"
        assert props["address"]["properties"]["addressLocality"] == "London"

    def test_absent_sources_are_none(self) -> None:
        sd = extract_structured_data("<html><body><p>nothing here</p></body></html>")
        assert sd.json_ld is None
        assert sd.microdata is None
        assert sd.opengraph is None
        assert sd.next_data is None

    def test_malformed_json_ld_is_skipped(self) -> None:
        html = '<html><head><script type="application/ld+json">{not valid json,,}</script></head><body>x</body></html>'
        sd = extract_structured_data(html)
        assert sd.json_ld is None  # malformed block skipped, nothing collected


@pytest.mark.asyncio
class TestScrapeStructuredDataFormat:
    """The structuredData format flows through the shared assembler (HTTP-first path)."""

    async def test_http_first_populates_structured_data(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from supacrawl.services.http_fetch import HttpFetchResult

        # A substantial body so the HTTP-first path serves it (rather than
        # escalating as a JS shell), letting the shared assembler run the extractor.
        rich_html = (
            '<html><head><script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"Product","name":"Widget"}'
            "</script></head><body><main><h1>Widget</h1><p>" + ("word " * 80) + "</p></main></body></html>"
        )

        async def fake_fetch(url: str, **kwargs: object) -> HttpFetchResult:
            return HttpFetchResult(url=url, html=rich_html, status_code=200, content_type="text/html", headers={})

        monkeypatch.setattr("supacrawl.services.scrape.fetch_static", fake_fetch)
        result = await ScrapeService().scrape("https://example.com", formats=["structuredData"])
        assert result.success
        assert result.data is not None
        assert result.data.structured_data is not None
        assert result.data.structured_data.json_ld is not None
        assert result.data.structured_data.json_ld[0]["name"] == "Widget"


@pytest.mark.parametrize("offer_url, success", [("/product/widget", True), ("/product/other", False)])
async def test_throttled_scrape_requires_a_priced_offer_for_the_fetched_page(monkeypatch, offer_url, success):
    url = "https://example.com/product/widget"
    html = JSON_LD_HTML.replace('"price": "19.99"', f'"url": "{offer_url}", "price": "19.99"')
    html = html.replace("</h1>", "</h1><p>" + ("A glass container with a durable airtight lid. " * 12) + "</p>")
    browser = BrowserManager()
    monkeypatch.setattr(browser, "fetch_page", AsyncMock(return_value=PageContent(url, html, "Widget", 429)))

    result = await ScrapeService(browser=browser).scrape(
        url, formats=["markdown", "structuredData"], http_first=False, escalate=False
    )

    assert result.success is success
    assert (result.error is None) is success
    assert result.data.metadata.status_code == 429
    assert result.data.structured_data.json_ld[0]["offers"]["price"] == "19.99"
