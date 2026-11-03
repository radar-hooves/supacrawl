"""
Search tool for Supacrawl MCP server.

Provides web search functionality with optional result scraping.
"""

import asyncio
import contextlib
from typing import Annotated, Any, Literal

import httpx
from api_common.correlation import generate_correlation_id
from fastmcp import Context
from pydantic import Field

from supacrawl.mcp.config import logger
from supacrawl.mcp.exceptions import SupacrawlValidationError, log_tool_exception, map_exception
from supacrawl.mcp.validators import (
    enhance_query_with_current_year,
    validate_formats,
    validate_limit,
    validate_query,
    validate_sources,
)
from supacrawl.models import SearchFilters
from supacrawl.services.registry import SupacrawlServices
from supacrawl.services.url_guard import guarded_request, guarded_stream


async def _fetch_url_metadata(url: str, timeout: float = 5.0) -> dict[str, Any]:
    """
    Fetch lightweight metadata for a URL using HEAD request.

    Falls back to GET with stream if HEAD is not allowed.

    Args:
        url: URL to fetch metadata for
        timeout: Request timeout in seconds

    Returns:
        Dictionary with metadata fields (content_type, content_length, last_modified)
    """
    metadata: dict[str, Any] = {}

    # follow_redirects=False: redirects are followed by hand inside
    # guarded_request/guarded_stream so every hop is validated and pinned
    # (#152) — these URLs come straight from search results, i.e. external
    # and not directly operator-typed.
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        headers={"User-Agent": "Mozilla/5.0 (compatible; Supacrawl/1.0)"},
    ) as client:
        try:
            # Try HEAD request first (lightweight)
            response = await guarded_request(client, "HEAD", url)

            # Some servers don't support HEAD, fall back to GET with stream
            if response.status_code == 405:
                async with guarded_stream(client, "GET", url) as response:
                    headers = response.headers
            else:
                headers = response.headers

            # Extract useful headers
            if "content-type" in headers:
                metadata["content_type"] = headers["content-type"].split(";")[0].strip()

            if "content-length" in headers:
                with contextlib.suppress(ValueError):
                    metadata["content_length"] = int(headers["content-length"])

            if "last-modified" in headers:
                metadata["last_modified"] = headers["last-modified"]

        except httpx.TimeoutException:
            metadata["error"] = "timeout"
        except httpx.RequestError as e:
            metadata["error"] = str(e)
        except Exception as e:
            logger.debug(f"Failed to fetch metadata for {url}: {e}")
            metadata["error"] = "fetch_failed"

    return metadata


async def _enrich_results_with_metadata(
    results: list[dict[str, Any]],
    timeout: float = 5.0,
) -> list[dict[str, Any]]:
    """
    Enrich search results with lightweight metadata from HEAD requests.

    Only processes web results (not images/news).

    Args:
        results: List of search result dictionaries
        timeout: Per-request timeout in seconds

    Returns:
        Results with metadata field added
    """
    # Filter to web results only (images/news don't need metadata fetch)
    web_results = [(i, r) for i, r in enumerate(results) if r.get("source_type") == "web"]

    if not web_results:
        return results

    # Fetch metadata in parallel
    tasks = [_fetch_url_metadata(r["url"], timeout) for _, r in web_results]
    metadata_list = await asyncio.gather(*tasks)

    # Merge metadata into results
    for (idx, _), metadata in zip(web_results, metadata_list, strict=True):
        if metadata and not metadata.get("error"):
            results[idx]["metadata"] = metadata

    return results


async def supacrawl_search(
    api_client: SupacrawlServices,
    ctx: Context,
    query: Annotated[
        str,
        Field(
            description='Search query string. Supports search operators: - "quotes" for exact match - -word to exclude - site:example.com to limit to domain - intitle:word for title matches - inurl:word for URL matches'
        ),
    ],
    limit: Annotated[int, Field(description="Maximum number of results per source type (1-10, default 5)")] = 5,
    sources: Annotated[
        list[Literal["web", "images", "news"]] | None,
        Field(
            description='Source types to search. Defaults to ["web"]. - "web": Standard web pages - "images": Photographs: each row has the page (url), the image file itself (image_url) and its pixel size (image_width/image_height) when the engine reports it - "news": News articles'
        ),
    ] = None,
    scrape_results: Annotated[bool, Field(description="Whether to scrape content from web result pages")] = False,
    formats: Annotated[
        list[Literal["markdown", "html"]] | None, Field(description="Output formats when scraping (markdown, html)")
    ] = None,
    only_main_content: Annotated[bool, Field(description="Extract only main content when scraping")] = True,
    include_metadata: Annotated[
        bool,
        Field(
            description="Fetch lightweight metadata (content_type, content_length, last_modified) for web results via HEAD requests. Useful for deciding which results to scrape without loading full pages."
        ),
    ] = False,
    time_range: Annotated[
        Literal["day", "week", "month", "year"] | None,
        Field(description='Restrict results to the past "day"/"week"/"month"/"year".'),
    ] = None,
    start_date: Annotated[
        str | None, Field(description="Earliest result date (YYYY-MM-DD); pairs with end_date.")
    ] = None,
    end_date: Annotated[str | None, Field(description="Latest result date (YYYY-MM-DD).")] = None,
    topic: Annotated[
        Literal["general", "news", "finance"] | None,
        Field(
            description='Topic vertical "general"/"news"/"finance" (honoured natively by Tavily and Exa; other providers ignore it — use sources=["news"] for news).'
        ),
    ] = None,
    include_domains: Annotated[list[str] | None, Field(description="Restrict results to these domains.")] = None,
    exclude_domains: Annotated[
        list[str] | None,
        Field(
            description="Exclude results from these domains. Filters map onto each provider's native API, or are synthesised as ``site:`` query operators for providers without native support."
        ),
    ] = None,
) -> dict[str, Any]:
    """
    Search the web and optionally scrape result pages.

    This is the most powerful web search tool available for finding information
    across the web. Use it when you need to find specific information and
    don't know which website contains it.

    Supports multiple source types (web, images, news) and can optionally
    scrape the content from web result pages.

    Use this tool when you need to find something and do not know its URL:
    - You need to find information but don't know which website has it
    - You want to discover URLs related to a topic
    - You need recent news articles on a subject
    - You're looking for images matching a description

    **Common patterns:**
    - For current/recent info: the query is automatically enhanced with current year
    - For specific sites: use "site:example.com" in query
    - For exact phrases: wrap in quotes ("exact phrase")
    - To exclude terms: use minus (-unwanted)
    - For deep content: set scrape_results=True to get full page content
    - For research: search first (scrape_results=False), then scrape relevant URLs
    - To preview before scraping: use include_metadata=True to get content type/size

    **Prefer other tools when:**
    - You already have the URL → use supacrawl_scrape
    - You want all pages from a known site → use supacrawl_map or supacrawl_crawl
    - You need structured data extraction → use supacrawl_extract

    Args:
        api_client: Injected SupacrawlServices instance
        query: Search query string. Supports search operators:
            - "quotes" for exact match
            - -word to exclude
            - site:example.com to limit to domain
            - intitle:word for title matches
            - inurl:word for URL matches
        limit: Maximum number of results per source type (1-10, default 5)
        sources: Source types to search. Defaults to ["web"].
            - "web": Standard web pages
            - "images": Image search results; ``image_url`` is the image file itself (download
              this), ``url`` the page it sits on, ``image_width``/``image_height`` its size when known
            - "news": News articles
        scrape_results: Whether to scrape content from web result pages
        formats: Output formats when scraping (markdown, html)
        only_main_content: Extract only main content when scraping
        include_metadata: Fetch lightweight metadata (content_type, content_length,
            last_modified) for web results via HEAD requests. Useful for deciding
            which results to scrape without loading full pages.
        time_range: Restrict results to the past "day"/"week"/"month"/"year".
        start_date: Earliest result date (YYYY-MM-DD); pairs with end_date.
        end_date: Latest result date (YYYY-MM-DD).
        topic: Topic vertical "general"/"news"/"finance" (honoured natively by
            Tavily and Exa; other providers ignore it — use sources=["news"] for news).
        include_domains: Restrict results to these domains.
        exclude_domains: Exclude results from these domains.
        Filters map onto each provider's native API, or are synthesised as
        ``site:`` query operators for providers without native support.

    Returns:
        Firecrawl-compatible search result:
        {
            "success": true,
            "data": [
                {
                    "url": "https://...",
                    "title": "...",
                    "description": "...",
                    "source_type": "web",  // or "images", "news"
                    "markdown": "...",  // If scrape_results=true (web only)
                    "html": "...",  // If scrape_results=true
                    "metadata": {  // If include_metadata=true (web only)
                        "content_type": "text/html",
                        "content_length": 45000,
                        "last_modified": "Sat, 01 Jan 2025 00:00:00 GMT"
                    },
                    // Image-specific:
                    "image_url": "https://.../full.jpg",  // the image itself; url is its page
                    "thumbnail": "...",
                    "image_width": 1920,
                    "image_height": 1080,
                    // News-specific:
                    "published_at": "2024-01-15T...",
                    "source_name": "..."
                }
            ]
        }

    Example:
        # Basic web search
        result = await supacrawl_search(query="python web scraping 2024", limit=5)

        # Image search
        result = await supacrawl_search(
            query="python logo",
            sources=["images"],
            limit=10
        )

        # News search with content scraping
        result = await supacrawl_search(
            query="AI breakthroughs",
            sources=["news", "web"],
            scrape_results=True,
            formats=["markdown"]
        )

        # Search with metadata to preview before scraping
        result = await supacrawl_search(
            query="python documentation",
            include_metadata=True
        )

    Note:
        - Search requires a provider API key. Brave is recommended (free tier,
          get a key at https://brave.com/search/api/). Set BRAVE_API_KEY — see
          .env.example. Without any key, supacrawl falls back to DuckDuckGo,
          which is aggressively bot-walled; a keyless search that returns nothing
          raises an actionable error rather than returning an empty list.
        - Multiple providers are tried in order. Providers without API keys are
          skipped. Configure the chain with SUPACRAWL_SEARCH_PROVIDERS.
        - Scraping is only applied to "web" source type results.
        - Metadata fetch uses lightweight HEAD requests (fast, no page content).
    """
    # Mint a fresh correlation ID per request. Reading the contextvar first
    # (get_correlation_id()) reused the previous call's ID: the var is
    # process-wide and the server task is long-lived, so the first tool call to
    # set it pinned that value onto every later response — a correlation ID that
    # never varies cannot correlate anything (#161).
    correlation_id = generate_correlation_id()

    try:
        # Validate inputs - provides helpful error messages
        validated_query = validate_query(query)
        # Enhance query with current year if time-sensitive (LLMs often use training cutoff year)
        validated_query = enhance_query_with_current_year(validated_query)
        validated_limit = validate_limit(limit, "limit", min_value=1, max_value=10, default=5)
        assert validated_limit is not None  # default=5 ensures non-None
        validated_sources = validate_sources(sources)
        validated_formats = validate_formats(formats, allowed_formats=["markdown", "html"])

        # Default to web search
        if validated_sources is None:
            validated_sources = ["web"]

        from supacrawl.services.search import ScrapeOptions, SourceType

        typed_sources: list[SourceType] = validated_sources  # type: ignore[assignment]

        scrape_options = None
        if scrape_results:
            typed_formats: list[Literal["markdown", "html"]] = validated_formats or ["markdown"]  # type: ignore[assignment]
            scrape_options = ScrapeOptions(
                formats=typed_formats,
                only_main_content=only_main_content,
            )

        # Create progress callback for gateway keepalive
        async def on_scrape_progress(completed: int, total: int, url: str) -> None:
            await ctx.report_progress(
                progress=completed,
                total=total,
                message=f"Scraped {completed}/{total}: {url}",
            )
            await ctx.info(f"Scrape progress: {completed}/{total} pages completed")

        progress_cb = on_scrape_progress if scrape_results else None

        filters = SearchFilters.model_validate(
            {
                "time_range": time_range,
                "start_date": start_date,
                "end_date": end_date,
                "topic": topic,
                "include_domains": include_domains,
                "exclude_domains": exclude_domains,
            }
        )

        result = await api_client.search_service.search(
            query=validated_query,
            limit=validated_limit,
            sources=typed_sources,
            scrape_options=scrape_options,
            progress_callback=progress_cb,
            filters=None if filters.is_empty() else filters,
        )

        response = result.model_dump()

        # Enrich web results with lightweight metadata if requested
        if include_metadata and not scrape_results:
            # Only fetch metadata if we're not already scraping (scraping includes richer metadata)
            response["data"] = await _enrich_results_with_metadata(response.get("data", []))

        # Include the actual query used (may differ from input if year was corrected)
        if validated_query != query:
            response["query_used"] = validated_query
            response["query_original"] = query

        # Surface correlation ID for request tracing by MCP consumers
        response["correlation_id"] = correlation_id

        return response

    except SupacrawlValidationError:
        raise
    except Exception as e:
        log_tool_exception("supacrawl_search", e)
        raise map_exception(e, endpoint="/search", correlation_id=correlation_id) from e
