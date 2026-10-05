"""Extraction follows the caller's JSON schema; a summary is a bounded summary, never the page.

The model is answered at the httpx wire (``wire_llm``), so the prompt, the
provider's structured-output setting and the reply parsing are all the real code.
"""

import json
from typing import Any, cast

import pytest
from conftest import LLMWire, wire_llm

from supacrawl.exceptions import ValidationError
from supacrawl.llm import LLMNotConfiguredError
from supacrawl.models import ScrapeData, ScrapeMetadata, ScrapeResult
from supacrawl.services.extract import ExtractService
from supacrawl.services.registry import SupacrawlServices
from supacrawl.services.scrape import ScrapeService
from supacrawl.services.summary import supacrawl_summary

URL = "https://shop.example.com/widget"
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "price": {"type": "number"}},
    "required": ["name", "price"],
    "additionalProperties": False,
}
BODY_SENTINEL = "The quarterly widget ledger reconciles every sprocket against its invoice."


class PageScrape:
    """Hands back one page's markdown, as the browser would."""

    def __init__(self, markdown: str = f"# Widget\n\nPrice: $9.50\n\n{BODY_SENTINEL}") -> None:
        self.markdown = markdown
        self.calls = 0

    async def scrape(self, url: str, **kwargs: Any) -> ScrapeResult:
        self.calls += 1
        return ScrapeResult(
            success=True,
            data=ScrapeData(markdown=self.markdown, metadata=ScrapeMetadata(title="Widget")),
        )


def _extract_service(page: PageScrape) -> ExtractService:
    return ExtractService(scrape_service=cast(ScrapeService, page))


def _services(page: PageScrape) -> SupacrawlServices:
    return cast(SupacrawlServices, type("Services", (), {"scrape_service": page})())


class TestExtractFollowsSchema:
    async def test_schema_reaches_the_openai_structured_output_setting(self, llm_wire: LLMWire) -> None:
        llm_wire.replies.append('{"name": "Widget", "price": 9.5}')

        result = await _extract_service(PageScrape()).extract(urls=[URL], schema=SCHEMA)

        assert result.data[0].data == {"name": "Widget", "price": 9.5}
        assert llm_wire.requests[0]["response_format"] == {
            "type": "json_schema",
            "json_schema": {"name": "extraction", "schema": SCHEMA},
        }

    async def test_schema_reaches_the_ollama_format(self, monkeypatch: pytest.MonkeyPatch) -> None:
        wire = wire_llm(monkeypatch, "ollama")
        wire.replies.append('{"name": "Widget", "price": 9.5}')

        result = await _extract_service(PageScrape()).extract(urls=[URL], schema=SCHEMA)

        assert result.data[0].data == {"name": "Widget", "price": 9.5}
        assert wire.requests[0]["format"] == SCHEMA

    async def test_a_nonconforming_reply_is_sent_back_once_for_repair(self, llm_wire: LLMWire) -> None:
        llm_wire.replies += ['{"name": "Widget", "price": "cheap"}', '{"name": "Widget", "price": 9.5}']

        result = await _extract_service(PageScrape()).extract(urls=[URL], schema=SCHEMA)

        assert result.success
        assert result.data[0].data == {"name": "Widget", "price": 9.5}
        repair_turn = llm_wire.requests[1]["messages"][-1]["content"]
        assert "'cheap' is not of type 'number'" in repair_turn

    async def test_a_reply_that_is_not_json_gets_the_repair_turn(self, llm_wire: LLMWire) -> None:
        llm_wire.replies += ["The widget is $9.50.", '{"name": "Widget", "price": 9.5}']

        data = await _extract_service(PageScrape()).extract_one(URL, schema=SCHEMA)

        assert data == {"name": "Widget", "price": 9.5}
        assert "not valid JSON" in llm_wire.requests[1]["messages"][-1]["content"]

    async def test_a_schema_without_a_root_type_still_needs_an_object(self, llm_wire: LLMWire) -> None:
        from supacrawl.exceptions import ExtractionSchemaError

        llm_wire.replies += ['[{"name": "Widget"}]', "null"]

        with pytest.raises(ExtractionSchemaError, match="not a JSON object"):
            await _extract_service(PageScrape()).extract_one(URL, schema={"properties": {"name": {"type": "string"}}})

    async def test_a_reply_that_never_conforms_is_a_typed_error(self, llm_wire: LLMWire) -> None:
        from supacrawl.exceptions import ExtractionSchemaError

        llm_wire.replies += ['{"name": "Widget"}', '{"name": "Widget", "colour": "red"}']

        with pytest.raises(ExtractionSchemaError) as exc_info:
            await _extract_service(PageScrape()).extract_one(URL, schema=SCHEMA)

        assert "'price' is a required property" in str(exc_info.value)
        assert exc_info.value.context["provider"] == "llm"

    async def test_extract_reports_the_nonconforming_url_as_failed(self, llm_wire: LLMWire) -> None:
        llm_wire.replies += ['{"name": "Widget"}', '{"name": "Widget"}']

        result = await _extract_service(PageScrape()).extract(urls=[URL], schema=SCHEMA)

        assert not result.success
        assert result.data[0].data is None
        assert "does not conform to the schema" in (result.data[0].error or "")

    async def test_an_invalid_schema_is_refused_before_any_fetch(self, llm_wire: LLMWire) -> None:
        page = PageScrape()

        with pytest.raises(ValidationError):
            await _extract_service(page).extract(
                urls=[URL], schema={"type": "object", "properties": {"x": {"type": 7}}}
            )

        assert page.calls == 0
        assert llm_wire.requests == []

    async def test_a_non_object_schema_is_refused(self, llm_wire: LLMWire) -> None:
        with pytest.raises(ValidationError, match="object"):
            await _extract_service(PageScrape()).extract(urls=[URL], schema={"type": "array"})


class TestSummaryIsASummary:
    async def test_returns_a_bounded_summary_with_the_source_url_and_never_the_page(self, llm_wire: LLMWire) -> None:
        llm_wire.replies.append(
            "The widget costs nine dollars fifty. It is sold by the example shop. "
            + " ".join(["More detail follows here."] * 20)
        )

        result = await supacrawl_summary(_services(PageScrape()), URL, max_length=20)

        data = result["data"]
        assert data["url"] == URL
        assert data["title"] == "Widget"
        assert len(data["summary"].split()) <= 20
        assert data["summary"].startswith("The widget costs nine dollars fifty.")
        assert "markdown" not in data
        assert BODY_SENTINEL not in json.dumps(result)

    async def test_the_bound_and_focus_reach_the_model(self, llm_wire: LLMWire) -> None:
        llm_wire.replies.append("Nine fifty.")

        await supacrawl_summary(_services(PageScrape()), URL, max_length=50, focus="pricing")

        prompt = json.dumps(llm_wire.requests[0]["messages"])
        assert "50 words" in prompt
        assert "pricing" in prompt

    async def test_ollama_is_asked_not_to_think(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A reasoning model left to think on a whole page outran the client timeout on atlas (120 s vs 6 s)."""
        wire = wire_llm(monkeypatch, "ollama")
        wire.replies.append("Nine fifty.")

        await supacrawl_summary(_services(PageScrape()), URL)

        assert wire.requests[0]["think"] is False

    async def test_without_an_llm_it_refuses_before_any_fetch(self, no_llm: None) -> None:
        page = PageScrape()

        with pytest.raises(LLMNotConfiguredError):
            await supacrawl_summary(_services(page), URL)

        assert page.calls == 0

    async def test_an_out_of_range_length_is_refused(self, llm_wire: LLMWire) -> None:
        with pytest.raises(ValidationError, match="max_length"):
            await supacrawl_summary(_services(PageScrape()), URL, max_length=0)
