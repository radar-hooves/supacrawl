"""FastMCP-facing wrapper around ``supacrawl.services.summary``."""

from typing import Annotated, Any

from api_common.correlation import generate_correlation_id
from pydantic import Field

from supacrawl.mcp.exceptions import log_tool_exception, map_exception
from supacrawl.services.registry import SupacrawlServices
from supacrawl.services.summary import supacrawl_summary as _supacrawl_summary


async def supacrawl_summary(
    api_client: SupacrawlServices,
    url: Annotated[str, Field(description="The URL to summarise")],
    max_length: Annotated[
        int | None,
        Field(description="Most words the summary may run to (default 120, capped at 1000)"),
    ] = None,
    focus: Annotated[
        str | None,
        Field(
            description='Optional focus area for the summary (e.g., "technical details", "pricing information", "key findings")'
        ),
    ] = None,
) -> dict[str, Any]:
    """
    Generate a summary of a web page.

    This tool scrapes the specified URL and has the server's LLM summarise
    it in at most `max_length` words. The page body is never returned.

    Use this tool when you want the gist of a page rather than its full text:
    - You need a quick overview of a page without reading all content
    - You're triaging multiple pages to find relevant ones
    - You want to understand what a page is about before deeper analysis
    - You're aggregating information from multiple sources

    **Best for:**
    - Quick overviews of long articles
    - Understanding page content before deeper analysis
    - Research and content aggregation
    - Getting the gist of documentation pages

    **Common patterns:**
    - Use focus parameter to target specific aspects ("pricing", "features", "requirements")
    - Use max_length for consistent summary sizes when comparing pages
    - Chain with search: search first, then summarise top results
    - For technical content: use focus="technical details" or focus="API usage"

    **Prefer other tools when:**
    - You need the full content → use supacrawl_scrape
    - You need structured data → use supacrawl_extract
    - You need multiple pages summarised → loop over supacrawl_summary or use supacrawl_crawl

    Args:
        api_client: Injected SupacrawlServices instance
        url: The URL to summarise
        max_length: Most words the summary may run to (default 120, capped at 1000)
        focus: Optional focus area for the summary (e.g., "technical details",
            "pricing information", "key findings")

    Returns:
        {
            "success": true,
            "data": {"url": "...", "title": "...", "summary": "..."},
            "correlation_id": "..."
        }

    Raises:
        SupacrawlValidationError: `url` or `max_length` failed validation.
        SupacrawlMCPError: no LLM is configured on the server, the page
            could not be fetched, or the model call failed.
    """
    correlation_id = generate_correlation_id()
    try:
        return await _supacrawl_summary(api_client, url, max_length=max_length, focus=focus)
    except Exception as e:
        log_tool_exception("supacrawl_summary", e)
        raise map_exception(e, endpoint="/summary", correlation_id=correlation_id) from e
