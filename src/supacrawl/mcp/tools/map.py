"""
Map tool for Supacrawl MCP server.

Wraps supacrawl library's MapService for MCP consumption.
"""

from typing import Annotated, Any, Literal

from api_common.correlation import generate_correlation_id
from pydantic import Field

from supacrawl.mcp.exceptions import SupacrawlValidationError, log_tool_exception, map_exception
from supacrawl.mcp.validators import validate_limit, validate_url
from supacrawl.services.registry import SupacrawlServices


async def supacrawl_map(
    api_client: SupacrawlServices,
    url: Annotated[str, Field(description="Starting URL for mapping")],
    limit: Annotated[int, Field(description="Maximum number of URLs to discover (default: 200)")] = 200,
    max_depth: Annotated[int, Field(description="Maximum BFS depth for link discovery (default: 3)")] = 3,
    sitemap: Annotated[
        Literal["include", "skip", "only"],
        Field(
            description='Sitemap handling mode: - "include": Use sitemaps + link following (default) - "skip": Link following only, ignore sitemaps - "only": Sitemaps only, no link following'
        ),
    ] = "include",
    include_subdomains: Annotated[
        bool, Field(description="Include URLs from subdomains (e.g., blog.example.com)")
    ] = False,
    search: Annotated[str | None, Field(description="Filter URLs to only those containing this string")] = None,
    ignore_query_params: Annotated[
        bool,
        Field(
            description="Remove query parameters from URLs for deduplication (e.g., treat /page?utm_source=x and /page as the same URL)"
        ),
    ] = False,
    allow_external_links: Annotated[bool, Field(description="Follow and include links to external domains")] = False,
    ignore_cache: Annotated[bool, Field(description="Bypass cached results and perform fresh URL discovery")] = False,
    headers: Annotated[
        dict[str, str] | None,
        Field(
            description='Custom HTTP headers sent with every request (e.g. {"Authorization": "Bearer token"}). Only header KEYS are logged; values are never persisted or written to logs.'
        ),
    ] = None,
) -> dict[str, Any]:
    """
    Map a website to discover all URLs without scraping content.

    Use this tool to explore a website's structure and find all available pages
    before deciding what to scrape. This is faster than crawling since it only
    discovers URLs without fetching full page content.

    **When to use this tool:**
    - You want to understand a website's structure before scraping
    - You need a list of all pages on a site (or matching a pattern)
    - You're planning selective scraping and need to choose which pages
    - You want to check if specific content exists on a site

    **Common patterns:**
    - For quick site overview: use sitemap="only" (fastest, uses sitemap.xml)
    - For comprehensive discovery: use sitemap="include" (default, combines both)
    - For JS-rendered links only: use sitemap="skip"
    - To find specific sections: use search="/blog/" or search="/docs/"
    - For large sites: start with low limit, then increase if needed
    - For sites with query param clutter: use ignore_query_params=True

    **Prefer other tools when:**
    - You need actual page content → use supacrawl_scrape or supacrawl_crawl
    - You don't know the website → use supacrawl_search first
    - You want to scrape all discovered pages → use supacrawl_crawl instead

    Args:
        api_client: Injected SupacrawlServices instance
        url: Starting URL for mapping
        limit: Maximum number of URLs to discover (default: 200)
        max_depth: Maximum BFS depth for link discovery (default: 3)
        sitemap: Sitemap handling mode:
            - "include": Use sitemaps + link following (default)
            - "skip": Link following only, ignore sitemaps
            - "only": Sitemaps only, no link following
        include_subdomains: Include URLs from subdomains (e.g., blog.example.com)
        search: Filter URLs to only those containing this string
        ignore_query_params: Remove query parameters from URLs for deduplication
            (e.g., treat /page?utm_source=x and /page as the same URL)
        allow_external_links: Follow and include links to external domains
        ignore_cache: Bypass cached results and perform fresh URL discovery
        headers: Custom HTTP headers sent with every request (e.g.
            {"Authorization": "Bearer token"}). Only header KEYS are logged;
            values are never persisted or written to logs.

    Returns:
        Firecrawl-compatible map result:
        {
            "success": true,
            "links": [
                {
                    "url": "https://example.com/page",
                    "title": "Page Title",
                    "description": "Page description if available"
                }
            ]
        }

    Example:
        # Map a website to find all pages
        result = await supacrawl_map(url="https://example.com", limit=100)

        # Find only blog posts
        result = await supacrawl_map(
            url="https://example.com",
            search="/blog/",
            limit=50
        )

        # Use sitemap only (faster for large sites)
        result = await supacrawl_map(
            url="https://example.com",
            sitemap="only",
            limit=500
        )
    """
    # Generate correlation ID for request tracking
    correlation_id = generate_correlation_id()

    try:
        # Validate inputs
        validated_url = validate_url(url)
        assert validated_url is not None  # validate_url raises on None
        validated_limit = validate_limit(limit, "limit", min_value=1, max_value=10000, default=200)
        assert validated_limit is not None  # default=200 ensures non-None

        result = await api_client.map_service.map_all(
            url=validated_url,
            limit=validated_limit,
            max_depth=max_depth,
            sitemap=sitemap,
            include_subdomains=include_subdomains,
            search=search,
            ignore_query_params=ignore_query_params,
            allow_external_links=allow_external_links,
            ignore_cache=ignore_cache,
            headers=headers,
        )

        response = result.model_dump()
        response["correlation_id"] = correlation_id
        return response

    except SupacrawlValidationError:
        raise
    except Exception as e:
        log_tool_exception("supacrawl_map", e)
        raise map_exception(e, endpoint="/map", correlation_id=correlation_id) from e
