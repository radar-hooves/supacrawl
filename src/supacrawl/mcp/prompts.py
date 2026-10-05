"""MCP prompts for Supacrawl server.

Provides workflow guidance prompts for AI agents to effectively use
supacrawl tools for web scraping and data extraction tasks.
"""


async def get_scrape_page_prompt() -> str:
    """Guide for basic page scraping."""
    return """# Scraping a Web Page with Supacrawl

## Quick Start
Use `supacrawl_scrape` for single page content extraction.

## Basic Usage
```
supacrawl_scrape(
    url="https://example.com/page",
    formats=["markdown"]  # Default, best for text
)
```

## Format Selection Guide
- **markdown**: Clean text content (default, recommended for most cases)
- **html**: Structured HTML with boilerplate removed
- **screenshot**: Visual capture of the page
- **links**: Extract all hyperlinks
- **json**: Structured data extraction (requires LLM + schema/prompt)
- **summary**: AI-generated page summary (requires LLM)

## Handling Dynamic Content
For JavaScript-heavy pages, add wait time:
```
supacrawl_scrape(
    url="https://spa-example.com",
    wait_for=3000,  # Wait 3 seconds for JS
    timeout=60000   # Allow more time
)
```

## Page Interactions
Execute actions before capturing:
```
supacrawl_scrape(
    url="https://example.com",
    actions=[
        {"type": "click", "selector": "button.load-more"},
        {"type": "wait", "milliseconds": 2000},
        {"type": "scroll", "direction": "down"}
    ]
)
```

## Filtering Content
Focus on specific elements:
```
supacrawl_scrape(
    url="https://example.com",
    include_tags=["article", "main"],
    exclude_tags=["nav", "footer", ".ads"]
)
```

## Best Practices
1. Start with `formats=["markdown"]` for text extraction
2. Use `screenshot` for visual verification
3. Add `wait_for` for dynamic/SPA content
4. Check response `success` field before processing
5. Handle empty content gracefully
"""


async def get_extract_data_prompt() -> str:
    """Guide for structured data extraction."""
    return """# Extracting Structured Data with Supacrawl

## Overview
`supacrawl_extract` scrapes each URL and has the server's LLM extract what you
ask for. It needs an LLM configured on the server (`SUPACRAWL_LLM_PROVIDER`,
`SUPACRAWL_LLM_MODEL`); without one the call fails and names what to set.

## Schema-Based Extraction
With a schema, each URL's `data` conforms to it. The schema goes to the
model's structured-output setting, the reply is validated, and a reply that
still breaks it after one repair turn fails that URL. The root must be an object.
```
result = supacrawl_extract(
    urls=["https://example.com/product"],
    prompt="Extract the product name, price, and availability",
    schema={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "price": {"type": "number"},
            "in_stock": {"type": "boolean"}
        },
        "required": ["name", "price"]
    }
)
# result["data"] == [{"url": ..., "success": true, "data": {"name": ..., "price": ...}}]
```

## Multi-URL Extraction
Up to 10 URLs. A URL that fails carries `success: false` and its `error`;
`partial` is true when some succeeded. If every URL fails the call fails.

## When to Use Extract vs Scrape
- **extract**: structured fields from one or more pages
- **scrape** with `formats=["structuredData"]`: facts the site already publishes
  (schema.org, OpenGraph), no LLM needed
- **scrape** with `formats=["markdown"]`: the page itself

## Best Practices
1. Give both a prompt and a schema
2. Keep schemas flat, with descriptive field names
3. Mark the fields you need as `required`
"""


async def get_summary_prompt() -> str:
    """Guide for page summarisation."""
    return """# Summarising Web Pages with Supacrawl

## Overview
`supacrawl_summary` scrapes a page and has the server's LLM summarise it. It
returns the summary, never the page body. It needs an LLM configured on the
server (`SUPACRAWL_LLM_PROVIDER`, `SUPACRAWL_LLM_MODEL`).

```
result = supacrawl_summary(
    url="https://example.com/article",
    max_length=100,                       # most words; default 120, cap 1000
    focus="technical implementation details"
)
# result["data"] == {"url": ..., "title": ..., "summary": "..."}
```

## When to Use Summary vs Scrape
- **summary**: the gist of a page without its text in your context
- **scrape**: the full content
"""


async def get_crawl_website_prompt() -> str:
    """Guide for multi-page website crawling."""
    return """# Crawling Websites with Supacrawl

## Workflow
1. **Discover** - Use `supacrawl_map` to find URLs first
2. **Crawl** - Use `supacrawl_crawl` to extract content

## Step 1: Discover Site Structure
```
supacrawl_map(
    url="https://example.com",
    limit=100,
    max_depth=3
)
```

## Step 2: Crawl with Content Extraction
```
supacrawl_crawl(
    url="https://example.com",
    limit=50,
    max_depth=2,
    formats=["markdown"]
)
```

## Filtering URLs
Include or exclude patterns:
```
supacrawl_crawl(
    url="https://blog.example.com",
    include_patterns=["*/posts/*", "*/articles/*"],
    exclude_patterns=["*/tag/*", "*/author/*"],
    limit=100
)
```

## Sitemap-Based Crawling
Use sitemap for comprehensive discovery:
```
supacrawl_map(
    url="https://example.com",
    sitemap="only"  # Use only sitemap URLs
)
```

## Large Site Considerations
- Start with `supacrawl_map` to understand size
- Set appropriate `limit` to control scope
- Use `include_patterns` to focus on relevant sections
- Get user confirmation before large crawls

## Best Practices
1. Always map before crawling unknown sites
2. Start with small limits, increase as needed
3. Use patterns to focus on relevant content
4. Respect robots.txt and site ToS
5. Add delays for large crawls (be considerate)
"""


async def get_research_topic_prompt() -> str:
    """Guide for multi-step web research using primitives."""
    return """# Web Research with Supacrawl

## Overview
When researching a topic, YOU (the LLM) orchestrate the primitives.
There is no separate "agent" - you ARE the agent.

## Research Workflow

### Step 1: Search for Sources
```
supacrawl_search(
    query="your research topic",
    limit=5
)
```

### Step 2: Scrape Promising Results
```
supacrawl_scrape(
    url="https://promising-result.com/article",
    formats=["markdown"]
)
```

### Step 3: Reason About What You Learned
- What information did you get?
- What gaps remain?
- What should you search for next?

### Step 4: Iterate
Repeat steps 1-3 until you have enough information.

## Example Research Session

```
# 1. Initial search
supacrawl_search(query="Python web frameworks comparison 2024", limit=5)

# 2. Scrape the most relevant result
supacrawl_scrape(url="https://example.com/python-frameworks", formats=["markdown"])

# 3. You read the content and identify gaps
# "This covers Django and Flask, but I need more on FastAPI..."

# 4. Follow-up search
supacrawl_search(query="FastAPI vs Django performance benchmarks", limit=3)

# 5. Scrape and synthesize
supacrawl_scrape(url="https://benchmark-site.com/fastapi", formats=["markdown"])
```

## When to Use Each Tool

| Need | Tool |
|------|------|
| Find sources | `supacrawl_search` |
| Get page content | `supacrawl_scrape` |
| Explore a site | `supacrawl_map` → `supacrawl_scrape` |
| Bulk content | `supacrawl_crawl` |
| Structured data | `supacrawl_extract` |

## Best Practices
1. Start broad, then narrow
2. Verify information across multiple sources
3. Be explicit about what you're looking for
4. Don't over-scrape - get what you need
5. Synthesize as you go, don't just collect
"""


async def get_select_tool_prompt() -> str:
    """Guide for choosing the right supacrawl tool."""
    return """# Choosing the Right Supacrawl Tool

## Decision Tree

```
Do you know the URL(s)?
├─ YES: How many?
│   ├─ One URL → supacrawl_scrape
│   └─ Multiple URLs → supacrawl_crawl
│
└─ NO: What do you need?
    ├─ Find relevant pages → supacrawl_search
    ├─ Discover site structure → supacrawl_map
    └─ Structured data → supacrawl_extract
```

## Quick Reference

| Tool | Use When |
|------|----------|
| `supacrawl_scrape` | You have ONE URL and want its content |
| `supacrawl_map` | You want to discover what URLs exist on a site |
| `supacrawl_crawl` | You want content from MULTIPLE pages on a site |
| `supacrawl_search` | You don't know which site has the information |
| `supacrawl_extract` | You need structured data (JSON) from pages |
| `supacrawl_summary` | You need to summarise a page's content |
| `supacrawl_health` | Check if the service is running |

## Common Patterns

### "Research this topic"
1. `supacrawl_search` to find sources
2. `supacrawl_scrape` on promising results
3. Reason → repeat if needed

### "Get all docs from this site"
1. `supacrawl_map` to discover URLs
2. `supacrawl_crawl` with patterns to filter

### "Extract product info from these pages"
1. `supacrawl_extract` with schema

### "What's on this page?"
1. `supacrawl_scrape` with `formats=["markdown"]`

## Anti-Patterns

❌ Using `crawl` for a single page (use `scrape`)
❌ Using `search` when you already have the URL
❌ Using `extract` without LLM configured
❌ Scraping without checking `success` in response
"""


async def get_search_web_prompt() -> str:
    """Guide for web search with optional scraping."""
    return """# Web Search with Supacrawl

## Overview
Use `supacrawl_search` when you don't know the source URL.
Searches the web and optionally scrapes results.

## Basic Search
```
supacrawl_search(
    query="best python testing frameworks 2024",
    limit=5
)
```

## Search with Result Scraping
Get full content from search results:
```
supacrawl_search(
    query="machine learning tutorials",
    limit=3,
    scrape_results=True,
    formats=["markdown"]
)
```

## Source Types
- **web**: Standard web pages (default)
- **images**: Image search results
- **news**: Recent news articles

```
supacrawl_search(
    query="tech news today",
    sources=["news"],
    limit=10
)
```

## Search Providers
- **brave**: Recommended default. Requires BRAVE_API_KEY (free tier: ~1,000 searches/month)
- **duckduckgo**: Deprecated fallback. Free but unreliable due to bot detection

## Best Practices
1. Use specific, focused queries
2. Start with limit=5, increase if needed
3. Use `scrape_results=True` when you need full content
4. Filter by source type for specific content
5. Consider news source for current events
"""


async def get_handle_errors_prompt() -> str:
    """Guide for error handling and troubleshooting."""
    return """# Error Handling in Supacrawl

## Response Structure
All tools return a response with `success` field:
```json
{
    "success": true,
    "data": { ... }
}
```

Always check `success` before processing:
```
result = supacrawl_scrape(url="...")
if not result.get("success"):
    # Handle error
    error = result.get("error", "Unknown error")
```

## Common Errors and Solutions

### Timeout Errors
**Symptom**: Operation timed out
**Solutions**:
- Increase `timeout` parameter (up to 300000ms)
- Add `wait_for` for slow-loading pages
- Check if site is accessible

### Connection Errors
**Symptom**: Connection failed
**Solutions**:
- Verify URL is correct and accessible
- Check network connectivity
- Site may be blocking automated access

### Validation Errors
**Symptom**: Invalid parameter
**Solutions**:
- Check URL format (must start with http:// or https://)
- Verify parameter types (e.g., limit must be integer)
- Check parameter ranges

### LLM Errors (json/summary formats in scrape)
**Symptom**: LLM operation failed when using `formats=["json"]` or `formats=["summary"]`
**Solutions**:
- These specific scrape formats require LLM configuration
- Check LLM is configured (see supacrawl://llm_config)
- For Ollama: ensure ollama is running
- For OpenAI/Anthropic: verify API key is set
- Alternative: Use `supacrawl_extract` or `supacrawl_summary` tools instead
  (they return content for YOU to process - no LLM config needed)

### Rate Limiting
**Symptom**: Too many requests
**Solutions**:
- Wait before retrying
- Reduce request frequency
- Use caching (set SUPACRAWL_CACHE_DIR)

## Retry Strategy
1. Don't immediately retry failed scrapes
2. Wait at least 5-10 seconds between retries
3. Consider if the site is blocking access
4. Try alternative approaches (different URL, search instead)

## Fallback Strategies
1. If scrape fails, try search to find alternative sources
2. If extract fails, try scrape with manual parsing
3. If crawl is too large, use map first to filter
"""


__all__ = [
    "get_scrape_page_prompt",
    "get_extract_data_prompt",
    "get_summary_prompt",
    "get_crawl_website_prompt",
    "get_research_topic_prompt",
    "get_select_tool_prompt",
    "get_search_web_prompt",
    "get_handle_errors_prompt",
]
