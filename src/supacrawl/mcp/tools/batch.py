"""
Batch scrape tool for Supacrawl MCP server.

Scrapes a list of URLs concurrently using a single shared browser, with a
semaphore to bound parallelism.  Mirrors the CLI ``batch`` command and the
API ``/v1/batch/scrape`` endpoint; all three delegate to the shared
``run_batch_scrape`` service function.
"""

from typing import Annotated, Any, Literal

from api_common.correlation import generate_correlation_id
from pydantic import Field

from supacrawl.mcp.exceptions import SupacrawlValidationError, log_tool_exception, map_exception
from supacrawl.mcp.validators import validate_urls
from supacrawl.services.batch import run_batch_scrape
from supacrawl.services.registry import SupacrawlServices


async def supacrawl_batch(
    api_client: SupacrawlServices,
    urls: Annotated[
        list[str] | str, Field(description="Ordered list of URLs to scrape (1–100). Also accepts a single URL string.")
    ],
    formats: Annotated[
        list[
            Literal[
                "markdown",
                "html",
                "rawHtml",
                "links",
                "screenshot",
                "pdf",
                "json",
                "images",
                "branding",
                "summary",
            ]
        ]
        | None,
        Field(
            description="Output formats for every URL. Options: - markdown: Clean markdown with resolved URLs (default) - html: Cleaned HTML with boilerplate removed - rawHtml: Full unprocessed HTML - links: All extracted links - images: All image URLs - screenshot: Base64-encoded PNG - pdf: Base64-encoded PDF - json: LLM-extracted structured data - branding: Brand identity (colours, fonts, logo) - summary: LLM-generated 2–3 sentence summary"
        ),
    ] = None,
    only_main_content: Annotated[
        bool, Field(description="Extract only main content, excluding nav/footer/sidebars.")
    ] = True,
    timeout: Annotated[int, Field(description="Per-page load timeout in ms (default: 30000).")] = 30000,
    max_age: Annotated[int, Field(description="Cache freshness in seconds; 0 = always fetch fresh (default).")] = 0,
    concurrency: Annotated[int, Field(description="Maximum number of pages scraped simultaneously (default: 5).")] = 5,
    retry: Annotated[int, Field(description="Maximum attempts per URL; 1 = one try with no retry (default).")] = 1,
    continue_on_error: Annotated[
        bool,
        Field(
            description="When True (default), failures are recorded but the remaining URLs continue. When False, the first failure aborts the entire batch."
        ),
    ] = True,
    headers: Annotated[
        dict[str, str] | None,
        Field(
            description='Custom HTTP headers sent with every request (e.g. ``{"Authorization": "Bearer token"}``). Header values are never persisted or written to logs.'
        ),
    ] = None,
) -> dict[str, Any]:
    """
    Scrape a list of URLs concurrently using a single shared browser.

    This is the batch equivalent of ``supacrawl_scrape``.  All URLs are scraped
    in parallel (up to ``concurrency`` at a time) via one shared browser
    instance, which is far more efficient than calling ``supacrawl_scrape``
    repeatedly in a loop.

    Use this tool when you have several known URLs to fetch at once:
    - You have a list of known URLs and need content from all of them
    - You want parallel scraping with a controlled concurrency cap
    - You need per-URL success/failure tracking with optional retries

    **Prefer other tools when:**
    - You only have one URL → use ``supacrawl_scrape``
    - You need to discover URLs first → use ``supacrawl_map`` then ``supacrawl_batch``
    - You want to follow links recursively → use ``supacrawl_crawl``

    Args:
        api_client: Injected SupacrawlServices instance.
        urls: Ordered list of URLs to scrape (1–100).
        formats: Output formats for every URL. Options:
            - markdown: Clean markdown with resolved URLs (default)
            - html: Cleaned HTML with boilerplate removed
            - rawHtml: Full unprocessed HTML
            - links: All extracted links
            - images: All image URLs
            - screenshot: Base64-encoded PNG
            - pdf: Base64-encoded PDF
            - json: LLM-extracted structured data
            - branding: Brand identity (colours, fonts, logo)
            - summary: LLM-generated 2–3 sentence summary
        only_main_content: Extract only main content, excluding nav/footer/sidebars.
        timeout: Per-page load timeout in ms (default: 30000).
        max_age: Cache freshness in seconds; 0 = always fetch fresh (default).
        concurrency: Maximum number of pages scraped simultaneously (default: 5).
        retry: Maximum attempts per URL; 1 = one try with no retry (default).
        continue_on_error: When True (default), failures are recorded but the
            remaining URLs continue.  When False, the first failure aborts the
            entire batch.
        headers: Custom HTTP headers sent with every request
            (e.g. ``{"Authorization": "Bearer token"}``).  Header values are
            never persisted or written to logs.

    Returns:
        Batch result dict:
        {
            "success": true,          # True when at least one URL succeeded
            "partial": false,         # True when some URLs succeeded and some failed
            "succeeded": 3,           # Count of successful URLs
            "failed": 1,              # Count of failed URLs
            "results": [
                {
                    "url": "https://example.com",
                    "success": true,
                    "attempts": 1,
                    "data": { ... }   # ScrapeResult fields (same as supacrawl_scrape)
                },
                {
                    "url": "https://broken.example",
                    "success": false,
                    "attempts": 1,
                    "error": "..."
                }
            ],
            "correlation_id": "..."
        }

    Raises:
        SupacrawlValidationError: `urls` is empty, over 100 entries, or not a
            list of valid URLs.
        SupacrawlMCPError: any other whole-call failure, mapped from the
            underlying exception by `map_exception`.
    """
    correlation_id = generate_correlation_id()

    try:
        validated_urls = validate_urls(urls, "urls", min_count=1, max_count=100)

        resolved_formats: list[str] = list(formats) if formats else ["markdown"]

        batch_result = await run_batch_scrape(
            urls=validated_urls,
            scrape_service=api_client.scrape_service,
            formats=resolved_formats,
            only_main_content=only_main_content,
            timeout=timeout,
            max_age=max_age,
            concurrency=concurrency,
            retry=retry,
            continue_on_error=continue_on_error,
            headers=headers,
        )

        results: list[dict[str, Any]] = []
        for url_result in batch_result.results:
            entry: dict[str, Any] = {
                "url": url_result.url,
                "success": url_result.success,
                "attempts": url_result.attempts,
            }
            if url_result.success and url_result.data is not None:
                entry["data"] = url_result.data.model_dump(exclude_none=True)
            if url_result.error:
                entry["error"] = url_result.error
            results.append(entry)

        return {
            "success": batch_result.succeeded > 0,
            "partial": batch_result.partial,
            "succeeded": batch_result.succeeded,
            "failed": batch_result.failed,
            "results": results,
            "correlation_id": correlation_id,
        }

    except SupacrawlValidationError:
        raise
    except Exception as exc:
        log_tool_exception("supacrawl_batch", exc)
        raise map_exception(exc, endpoint="/batch/scrape", correlation_id=correlation_id) from exc
