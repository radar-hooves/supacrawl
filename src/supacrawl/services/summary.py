"""Summarise one web page with the configured LLM; shared by the MCP tool and the REST endpoint."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from supacrawl.exceptions import ProviderError, ValidationError, generate_correlation_id
from supacrawl.llm import LLMClient, load_llm_config
from supacrawl.services.validation import validate_url

if TYPE_CHECKING:
    from supacrawl.services.registry import SupacrawlServices

DEFAULT_SUMMARY_WORDS = 120
MAX_SUMMARY_WORDS = 1000


def _summary_words(max_length: Any) -> int:
    """The word bound: the default when unset, refused below one, capped at ``MAX_SUMMARY_WORDS``."""
    if max_length is None:
        return DEFAULT_SUMMARY_WORDS
    try:
        words = int(max_length)
    except (TypeError, ValueError) as e:
        raise ValidationError("max_length must be a whole number of words", field="max_length", value=max_length) from e
    if words < 1:
        raise ValidationError(f"max_length must be at least 1 word, got {words}", field="max_length", value=max_length)
    return min(words, MAX_SUMMARY_WORDS)


async def supacrawl_summary(
    api_client: SupacrawlServices,
    url: str,
    max_length: int | None = None,
    focus: str | None = None,
) -> dict[str, Any]:
    """
    Scrape ``url`` and summarise it in at most ``max_length`` words.

    Returns ``{"success": True, "data": {"url", "title", "summary"}, "correlation_id"}``;
    the page body is never returned.

    Raises:
        ValidationError: ``url`` or ``max_length`` is invalid.
        LLMNotConfiguredError: no LLM is configured; raised before the page is fetched.
        ProviderError: the page could not be fetched or had no content, or the model call failed.
    """
    correlation_id = generate_correlation_id()
    validated_url = validate_url(url)
    assert validated_url is not None  # validate_url raises on None
    max_words = _summary_words(max_length)
    client = LLMClient(load_llm_config())

    try:
        scrape_result = await api_client.scrape_service.scrape(
            url=validated_url,
            formats=["markdown"],
            only_main_content=True,
        )
        page = scrape_result.data if scrape_result.success else None
        if page is None or not (page.markdown or "").strip():
            raise ProviderError(
                scrape_result.error or "No content scraped from page",
                provider="scrape",
                correlation_id=correlation_id,
            )
        summary = await client.summarize(page.markdown or "", max_words, focus)
    finally:
        await client.close()

    return {
        "success": True,
        "data": {
            "url": validated_url,
            "title": page.metadata.title if page.metadata else None,
            "summary": summary,
        },
        "correlation_id": correlation_id,
    }
