"""
Extract tool for Supacrawl MCP server.

Scrapes web pages and has the server's configured LLM pull structured data out
of them, conforming to the caller's JSON schema when one is given.
"""

from typing import Annotated, Any

from api_common.correlation import generate_correlation_id
from pydantic import Field

from supacrawl.exceptions import ConfigurationError, ValidationError
from supacrawl.mcp.config import logger
from supacrawl.mcp.exceptions import SupacrawlValidationError, log_tool_exception, map_exception
from supacrawl.mcp.validators import validate_json_object, validate_prompt, validate_urls
from supacrawl.services.extract import ExtractService
from supacrawl.services.registry import SupacrawlServices


async def supacrawl_extract(
    api_client: SupacrawlServices,
    urls: Annotated[
        list[str] | str, Field(description="URLs to extract data from (1-10 URLs). Also accepts a single URL string.")
    ],
    prompt: Annotated[
        str | None,
        Field(
            description='Natural language description of what to extract. Example: "Extract the product name, price, and availability"'
        ),
    ] = None,
    schema: Annotated[
        dict[str, Any] | str | None,
        Field(
            description='JSON schema each URL\'s extracted data conforms to; the root must be an object. Example: {"type": "object", "properties": {"name": {"type": "string"}}}'
        ),
    ] = None,
    allow_external_links: Annotated[
        bool, Field(description="Whether to follow and extract from external links")
    ] = False,
) -> dict[str, Any]:
    """
    Extract structured information from web pages using LLM.

    This tool scrapes the specified URLs and has the server's LLM extract
    structured data according to your prompt and/or schema. With a schema,
    each URL's `data` conforms to it: the schema goes to the model's
    structured-output setting, the reply is validated against it, and a reply
    that still breaks it after one repair turn fails that URL.

    Use this tool when you need structured data out of one or more pages:
    - You need structured data (JSON) from web pages
    - You want to extract specific fields from multiple URLs at once
    - You have a schema defining what data you need
    - You're building datasets from web content

    **Best for extracting:**
    - Product information (name, price, description, availability)
    - Contact details (emails, phone numbers, addresses)
    - Article metadata (author, date, categories, tags)
    - Event information (dates, venues, speakers)
    - Any custom data matching your schema

    **Common patterns:**
    - Provide both prompt AND schema for best results
    - Keep schemas simple and flat when possible; the root must be an object
    - Use descriptive field names in schema (LLM uses them as hints)
    - For e-commerce: extract from product listing pages, not search results
    - Batch related URLs together (same site/structure) for consistency

    **Prefer other tools when:**
    - You just need the page content → use supacrawl_scrape with formats=["markdown"]
    - You need a summary, not structured data → use supacrawl_summary
    - The site already publishes the facts (prices, ratings, dates) → use
      supacrawl_scrape with formats=["structuredData"], which needs no LLM

    Args:
        api_client: Injected SupacrawlServices instance
        urls: URLs to extract data from (1-10 URLs)
        prompt: Natural language description of what to extract.
            Example: "Extract the product name, price, and availability"
        schema: JSON schema each URL's data must conform to (an object, or
            that object JSON-encoded as a string).
            Example: {"type": "object", "properties": {"name": {"type": "string"}}}
        allow_external_links: Whether to follow and extract from external links

    Returns:
        {
            "success": true,          # True when at least one URL succeeded
            "partial": false,         # True when some URLs succeeded and some failed
            "succeeded_count": 1,
            "failed_count": 0,
            "data": [
                {"url": "...", "success": true, "data": {...}},   # conforms to schema
                {"url": "...", "success": false, "error": "..."}
            ],
            "correlation_id": "..."
        }

    Raises:
        SupacrawlValidationError: `urls`, `prompt` or `schema` failed
            validation, or neither `prompt` nor `schema` was given.
        SupacrawlMCPError: no LLM is configured on the server, or every URL
            failed (the page could not be fetched, or the model's output
            does not conform to the schema).
    """
    correlation_id = generate_correlation_id()

    try:
        validated_urls = validate_urls(urls, "urls", min_count=1, max_count=10)
        validated_prompt = validate_prompt(prompt, "prompt", allow_none=True)
        validated_schema = validate_json_object(schema, "schema")
        if validated_prompt is None and validated_schema is None:
            raise SupacrawlValidationError(
                "Provide a prompt or schema (or both) saying what to extract",
                field="prompt",
                value=None,
            )

        results: list[dict[str, Any]] = []
        failures: list[Exception] = []
        async with ExtractService(scrape_service=api_client.scrape_service) as service:
            for url in validated_urls:
                try:
                    data = await service.extract_one(url, validated_prompt, validated_schema)
                    results.append({"url": url, "success": True, "data": data})
                except ConfigurationError, ValidationError:
                    # No LLM, or a schema no URL could satisfy: the whole call fails
                    raise
                except Exception as e:
                    logger.warning(f"Failed to extract from {url}: {e}")
                    failures.append(e)
                    results.append({"url": url, "success": False, "error": str(e)})

        if len(failures) == len(results):
            raise failures[0]

        succeeded_count = len(results) - len(failures)
        return {
            "success": True,
            "partial": bool(failures),
            "succeeded_count": succeeded_count,
            "failed_count": len(failures),
            "data": results,
            "correlation_id": correlation_id,
        }

    except SupacrawlValidationError:
        raise
    except Exception as e:
        log_tool_exception("supacrawl_extract", e)
        raise map_exception(e, endpoint="/extract", correlation_id=correlation_id) from e
