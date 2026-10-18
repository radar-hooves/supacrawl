"""
Scrape tool for Supacrawl MCP server.

Wraps supacrawl library's ScrapeService for MCP consumption.
"""

from typing import Annotated, Any, Literal

from api_common.correlation import generate_correlation_id
from pydantic import Field

from supacrawl.excerpt import bound_markdown
from supacrawl.mcp.exceptions import SupacrawlValidationError, log_tool_exception, map_exception
from supacrawl.mcp.validators import validate_timeout, validate_url
from supacrawl.services.browser import ENGINE_CHOICES
from supacrawl.services.registry import SupacrawlServices

# A page this long overflows most agent tool-result budgets (~25k tokens).
LARGE_RESPONSE_CHARS = 100_000
_BOUNDED_FIELDS = ("markdown", "html", "raw_html")


def _bound_response(response: dict, max_chars: int | None, query: str | None) -> None:
    """Cap the text fields of a scrape response in place and say what was cut.

    With ``max_chars`` set, markdown is bounded by ``bound_markdown`` (best
    passages for ``query`` when given, else the head) and html/raw_html are cut
    at the head; ``truncation`` reports every cut field's full length. Without
    it, an oversized markdown field earns a warning.
    """
    data = response.get("data")
    if not data:
        return
    if max_chars is None:
        total = len(data.get("markdown") or "")
        if total > LARGE_RESPONSE_CHARS:
            response["warnings"] = [
                *(response["warnings"] or []),
                f"markdown is {total} chars and may overflow your tool-result budget; "
                "pass max_chars to bound it, with query to keep the best-matching passages.",
            ]
        return
    fields: dict[str, dict[str, int | str]] = {}
    for name in _BOUNDED_FIELDS:
        text = data.get(name)
        if not text or len(text) <= max_chars:
            continue
        if name == "markdown":
            cut, strategy = bound_markdown(text, max_chars, query)
        else:
            cut, strategy = text[:max_chars], "head"
        data[name] = cut
        fields[name] = {"total_chars": len(text), "returned_chars": len(cut), "strategy": strategy}
    response["truncation"] = {"max_chars": max_chars, "truncated": bool(fields), "fields": fields}


async def supacrawl_scrape(
    api_client: SupacrawlServices,
    url: Annotated[str, Field(description="The URL to scrape")],
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
                "structuredData",
                "summary",
                "changeTracking",
            ]
        ]
        | None,
        Field(
            description="Output formats to return. Options: - markdown: Clean markdown with resolved URLs (default) - html: Cleaned HTML with boilerplate removed - rawHtml: Full unprocessed HTML - links: All extracted links (parsed from rendered HTML) - images: All image URLs - screenshot: Base64-encoded PNG - pdf: Base64-encoded PDF document - json: LLM-extracted structured data (requires json_schema or json_prompt) - branding: Brand identity (colours, fonts, logo) - structuredData: deterministic, no-LLM extraction of the page's own embedded data (schema.org JSON-LD, Next.js __NEXT_DATA__, microdata, OpenGraph) — cheaper and more reliable than json for facts the site already publishes (prices, ratings, authors, dates) - summary: LLM-generated 2-3 sentence summary"
        ),
    ] = None,
    only_main_content: Annotated[
        bool, Field(description="Extract only main content, excluding headers/footers/sidebars")
    ] = True,
    wait_for: Annotated[
        int,
        Field(
            description="Additional wait time in ms after page load (for dynamic content). When set to a value > 0, also enables SPA stability polling which waits for the DOM to stop changing before extracting content."
        ),
    ] = 0,
    timeout: Annotated[int, Field(description="Page load timeout in ms (default: 30000)")] = 30000,
    screenshot_full_page: Annotated[bool, Field(description="Capture full scrollable page vs viewport only")] = True,
    actions: Annotated[
        list[dict[str, Any]] | None,
        Field(
            description='Page actions to execute before capturing content. Each action is a dict: - {"type": "wait", "milliseconds": 1000} - Wait for time - {"type": "wait", "selector": "#content"} - Wait for element - {"type": "click", "selector": "button.submit"} - Click element - {"type": "type", "selector": "input", "text": "hello"} - Type text - {"type": "scroll", "direction": "down"} - Scroll page - {"type": "screenshot"} - Capture mid-workflow screenshot - {"type": "press", "key": "Enter"} - Press keyboard key - {"type": "executeJavascript", "script": "..."} - Run custom JS'
        ),
    ] = None,
    json_schema: Annotated[
        dict[str, Any] | None,
        Field(
            description='JSON schema for structured extraction (for json format). Example: {"type": "object", "properties": {"title": {"type": "string"}}}'
        ),
    ] = None,
    json_prompt: Annotated[
        str | None,
        Field(
            description='Custom prompt for LLM extraction (for json format). Example: "Extract the product name, price, and availability"'
        ),
    ] = None,
    include_tags: Annotated[
        list[str] | None,
        Field(
            description='CSS selectors for elements to include. Overrides only_main_content. Example: ["article", "main", ".content"]'
        ),
    ] = None,
    exclude_tags: Annotated[
        list[str] | None,
        Field(description='CSS selectors for elements to exclude. Example: ["nav", "footer", ".sidebar", ".ads"]'),
    ] = None,
    max_age: Annotated[
        int,
        Field(
            description="Cache freshness in seconds. If cached version exists and is fresher, return cached result. Set to 0 to always fetch fresh (default)."
        ),
    ] = 0,
    change_tracking_modes: Annotated[
        list[str] | None,
        Field(
            description=(
                'Optional diff modes when changeTracking format is used. Supports: ["git-diff", "json"]. '
                "git-diff produces unified diffs, json compares extracted structured fields."
            )
        ),
    ] = None,
    expand_iframes: Annotated[
        str,
        Field(
            description='Iframe expansion mode (default: "same-origin"). "none" strips all iframes (legacy), "same-origin" expands same-origin iframes inline, "all" expands all non-blocked iframes.'
        ),
    ] = "same-origin",
    mobile: Annotated[
        bool,
        Field(
            description="Scrape as a default mobile device (iPhone 14). Sets mobile viewport, user agent, device scale factor, and touch support."
        ),
    ] = False,
    device: Annotated[
        str | None,
        Field(
            description='Emulate a specific device by name (e.g. "iPhone 15", "Pixel 7"). Overrides the mobile flag. Uses Playwright\'s built-in device descriptors for accurate viewport, user agent, and DPI emulation.'
        ),
    ] = None,
    parse_pdf: Annotated[
        str,
        Field(
            description='PDF parsing mode (default: "auto"). Options: - "auto": Auto-detect PDF URLs by .pdf extension, extract text with OCR fallback - "fast": Text extraction only (no OCR) - "ocr": Always run OCR (requires supacrawl[pdf-ocr]) - "off": Disable PDF parsing (render PDF in browser as before)'
        ),
    ] = "auto",
    engine: Annotated[
        Literal["playwright", "patchright", "camoufox"] | None,
        Field(
            description='Pin the browser engine for this request. Normally you do NOT need this — supacrawl auto-escalates through engines on a poor verdict. Only set this if you have a specific reason to pin one engine: - "playwright": Standard Chromium with basic stealth scripts - "patchright": Patched Chromium for better anti-detection - "camoufox": Patched Firefox for bypassing Akamai/Cloudflare When not set, uses SUPACRAWL_ENGINE or auto-selects based on verdict.'
        ),
    ] = None,
    headers: Annotated[
        dict[str, str] | None,
        Field(
            description='Custom HTTP headers sent with every request on this page (e.g. {"Authorization": "Bearer token", "Cookie": "session=abc"}). Only header KEYS are logged; values are never persisted or written to logs.'
        ),
    ] = None,
    content_mode: Annotated[
        float,
        Field(
            description="Precision/recall dial for content extraction in [0.0, 1.0]. Low values favour recall (include more content, prune less); high values favour precision (demand denser output, prune more aggressively). Default 0.5 preserves current behaviour. Without supacrawl[readability] only the CSS-selector heuristic is active; readability and BM25 strategies are silently skipped."
        ),
    ] = 0.5,
    query: Annotated[
        str | None,
        Field(
            description="Optional free-text query. When set, extracted sections are filtered by BM25 relevance to retain only query-relevant content. Flat pages (no headings) are never filtered so no content is lost. The result's `query_filter` says whether it was applied (`applied`, and `reason` when not) and how many sections were kept of the total; supacrawl_health reports whether this server can filter."
        ),
    ] = None,
    max_chars: Annotated[
        int | None,
        Field(
            description="Cap markdown/html/rawHtml at this many characters so a large page cannot overflow your tool-result budget. The response then carries `truncation` with the full length of every field it cut. With `query` set, markdown keeps the passages that best match it (strategy: query_ranked), otherwise the head. Default: no cap (a warning is added when markdown exceeds 100,000 characters).",
        ),
    ] = None,
    http_first: Annotated[
        bool,
        Field(
            description="Try a cheap HTTP GET before launching a browser (default True). Static pages return faster; pages needing JavaScript or showing a bot challenge transparently escalate to the full browser render. Set False to always render in the browser."
        ),
    ] = True,
    expect: Annotated[
        str | None,
        Field(
            description="Require asserted content before returning. A bare integer is a minimum word count; any other string is matched first as a CSS selector then as a text substring. When unmet, the scrape escalates (HTTP-first to browser, then a stealth + longer-wait retry) and ultimately returns success=False instead of a pre-hydration skeleton. Use this when an agent must be sure the data it asked for is actually present."
        ),
    ] = None,
) -> dict:
    """
    Scrape a single URL and return content in specified formats.

    This is the primary tool for extracting content from web pages. It supports
    multiple output formats, page interactions, and LLM-powered extraction.
    PDF URLs are auto-detected and text is extracted directly (no browser needed).

    **When to use this tool:**
    - You have a specific URL and need its content
    - You want clean markdown for reading/analysis
    - You need screenshots or PDFs of a page
    - You need to extract structured data (json format) from a single page

    **Quality signal — read this first:**
    Every result includes a `quality` field:
    ```
    {
      "verdict": "ok" | "thin" | "js_shell" | "paywall" | "bot_challenge" |
                 "captcha" | "error_status" | "garbled_pdf" | "empty" |
                 "infrastructure",
      "score": 0-100,
      "reasons": [...],
      "suggestion": "...",   // present when verdict is not "ok"
      "attempts": 1,
      "escalated": false
    }
    ```
    `success` is honest: HTTP >= 400, bot/CAPTCHA interstitials, garbled PDFs,
    and empty pages are reported `success=False`. Check `quality.verdict` and
    `quality.score` to decide whether to accept, retry, or escalate. A non-ok
    verdict always carries `quality.suggestion` with a concrete next step.

    Every verdict except `infrastructure` describes the TARGET SITE.
    `infrastructure` means supacrawl's own engine failed and the request never
    reached the site — the engine relaunches itself, so retry once; if it
    recurs, restart the supacrawl server rather than trying the URL again.

    **Anti-bot and hard sites:**
    Do NOT manually set `engine`, `stealth`, or `wait_for` for hard sites.
    Supacrawl auto-escalates through Patchright → Camoufox → Camoufox+HTTP/1.1
    with extended waits on a poor verdict. Hard sites just work on defaults
    within a bounded budget. Use `escalate=False` only if you want to suppress
    this and keep the first-attempt result.

    **Per-domain memory:**
    Supacrawl remembers the winning strategy per domain and seeds subsequent
    requests with it automatically. No configuration needed.

    **Common patterns:**
    - For clean article text: use only_main_content=True (default)
    - For full page including nav/footer: use only_main_content=False
    - For login-protected content: use actions to click buttons/fill forms first
    - For infinite scroll pages: use actions with scroll + wait sequences
    - To assert content is present: use expect (escalates until assertion met or budget exhausted)

    **Prefer other tools when:**
    - You don't know the URL → use supacrawl_search first
    - You need to find pages on a site → use supacrawl_map
    - You need content from multiple pages → use supacrawl_crawl
    - You want a quick summary → use supacrawl_summary

    Args:
        api_client: Injected SupacrawlServices instance
        url: The URL to scrape
        formats: Output formats to return. Options:
            - markdown: Clean markdown with resolved URLs (default)
            - html: Cleaned HTML with boilerplate removed
            - rawHtml: Full unprocessed HTML
            - links: All extracted links (parsed from rendered HTML)
            - images: All image URLs
            - screenshot: Base64-encoded PNG
            - pdf: Base64-encoded PDF document
            - json: LLM-extracted structured data (requires json_schema or json_prompt)
            - branding: Brand identity (colours, fonts, logo)
            - structuredData: deterministic, no-LLM extraction of the page's own
              embedded data (schema.org JSON-LD, Next.js __NEXT_DATA__, microdata,
              OpenGraph) — cheaper and more reliable than json for facts the site
              already publishes (prices, ratings, authors, dates)
            - summary: LLM-generated 2-3 sentence summary
        only_main_content: Extract only main content, excluding headers/footers/sidebars
        wait_for: Additional wait time in ms after page load (for dynamic content).
            When set to a value > 0, also enables SPA stability polling which
            waits for the DOM to stop changing before extracting content.
        timeout: Page load timeout in ms (default: 30000)
        screenshot_full_page: Capture full scrollable page vs viewport only
        actions: Page actions to execute before capturing content. Each action is a dict:
            - {"type": "wait", "milliseconds": 1000} - Wait for time
            - {"type": "wait", "selector": "#content"} - Wait for element
            - {"type": "click", "selector": "button.submit"} - Click element
            - {"type": "type", "selector": "input", "text": "hello"} - Type text
            - {"type": "scroll", "direction": "down"} - Scroll page
            - {"type": "screenshot"} - Capture mid-workflow screenshot
            - {"type": "press", "key": "Enter"} - Press keyboard key
            - {"type": "executeJavascript", "script": "..."} - Run custom JS
        json_schema: JSON schema for structured extraction (for json format).
            Example: {"type": "object", "properties": {"title": {"type": "string"}}}
        json_prompt: Custom prompt for LLM extraction (for json format).
            Example: "Extract the product name, price, and availability"
        include_tags: CSS selectors for elements to include. Overrides only_main_content.
            Example: ["article", "main", ".content"]
        exclude_tags: CSS selectors for elements to exclude.
            Example: ["nav", "footer", ".sidebar", ".ads"]
        max_age: Cache freshness in seconds. If cached version exists and is fresher,
            return cached result. Set to 0 to always fetch fresh (default).
        change_tracking_modes: Optional diff modes when changeTracking format is used.
            Supports: ["git-diff", "json"]. git-diff produces unified diffs, json
            compares extracted structured fields.
        expand_iframes: Iframe expansion mode (default: "same-origin").
            "none" strips all iframes (legacy), "same-origin" expands same-origin
            iframes inline, "all" expands all non-blocked iframes.
        mobile: Scrape as a default mobile device (iPhone 14). Sets mobile
            viewport, user agent, device scale factor, and touch support.
        device: Emulate a specific device by name (e.g. "iPhone 15", "Pixel 7").
            Overrides the mobile flag. Uses Playwright's built-in device
            descriptors for accurate viewport, user agent, and DPI emulation.
        parse_pdf: PDF parsing mode (default: "auto"). Options:
            - "auto": Auto-detect PDF URLs by .pdf extension, extract text with OCR fallback
            - "fast": Text extraction only (no OCR)
            - "ocr": Always run OCR (requires supacrawl[pdf-ocr])
            - "off": Disable PDF parsing (render PDF in browser as before)
        engine: Pin the browser engine for this request. Normally you do NOT need
            this — supacrawl auto-escalates through engines on a poor verdict.
            Only set this if you have a specific reason to pin one engine:
            - "playwright": Standard Chromium with basic stealth scripts
            - "patchright": Patched Chromium for better anti-detection
            - "camoufox": Patched Firefox for bypassing Akamai/Cloudflare
            When not set, uses SUPACRAWL_ENGINE or auto-selects based on verdict.
        headers: Custom HTTP headers sent with every request on this page (e.g.
            {"Authorization": "Bearer token", "Cookie": "session=abc"}).
            Only header KEYS are logged; values are never persisted or written to logs.
        content_mode: Precision/recall dial for content extraction in [0.0, 1.0].
            Low values favour recall (include more content, prune less);
            high values favour precision (demand denser output, prune more aggressively).
            Default 0.5 preserves current behaviour. Without supacrawl[readability]
            only the CSS-selector heuristic is active; readability and BM25 strategies
            are silently skipped.
        query: Optional free-text query. When set, extracted sections are filtered
            by BM25 relevance to retain only query-relevant content. Flat pages
            (no headings) are never filtered so no content is lost. The result's
            `query_filter` says whether it was applied (`applied`, and `reason`
            when not) and how many sections were kept of the total;
            supacrawl_health reports whether this server can filter.
        max_chars: Cap markdown/html/rawHtml at this many characters so a large
            page cannot overflow your tool-result budget. The response then carries
            `truncation` with the full length of every field it cut. With `query`
            set, markdown keeps the passages that best match it (strategy
            "query_ranked"), otherwise the head. Default: no cap (a warning is added when markdown exceeds 100,000 characters).
        http_first: Try a cheap HTTP GET before launching a browser (default True).
            Static pages return faster; pages needing JavaScript or showing a bot
            challenge transparently escalate to the full browser render. Set False
            to always render in the browser.
        expect: Require asserted content before returning. A bare integer is a
            minimum word count; any other string is matched first as a CSS selector
            then as a text substring. When unmet, the scrape escalates (HTTP-first to
            browser, then a stealth + longer-wait retry) and ultimately returns
            success=False instead of a pre-hydration skeleton. Use this when an agent
            must be sure the data it asked for is actually present.

    Returns:
        Scrape result:
        {
            "success": true,       // False for HTTP>=400, bot blocks, CAPTCHAs, empty pages
            "data": {
                "markdown": "...",
                "html": "...",
                "screenshot": "base64...",
                "llm_extraction": {...},  // For json format
                "summary": "...",         // For summary format
                "branding": {...},        // For branding format
                "metadata": {
                    "title": "...",
                    "description": "...",
                    "og_image": "..."
                },
                "links": [...],
                "images": [...]
            },
            "query_filter": {...},   // Only when query was passed: applied, reason, sections_kept/total
            "truncation": {...},     // Only when max_chars was passed: per-field total_chars/returned_chars
            "quality": {
                "verdict": "ok",   // ok/thin/js_shell/paywall/bot_challenge/captcha/
                                   // error_status/garbled_pdf/empty
                "score": 95,       // 0-100 completeness within the verdict
                "reasons": [...],
                "suggestion": null, // Concrete next step when verdict is not "ok"
                "attempts": 1,      // How many strategies were tried
                "escalated": false  // Whether auto-escalation fired
            }
        }

    Note:
        - Check quality.verdict before using the result. A verdict of "ok" with
          score >= 70 is a clean result. Non-ok verdicts carry quality.suggestion.
        - The json and summary formats require an LLM. Configure with:
          SUPACRAWL_LLM_PROVIDER (ollama/openai/anthropic)
          SUPACRAWL_LLM_MODEL (e.g., qwen3:8b, gpt-4o-mini)
    """
    # Generate correlation ID for request tracking
    correlation_id = generate_correlation_id()

    try:
        # Validate inputs
        validated_url = validate_url(url)
        assert validated_url is not None  # validate_url raises on None
        validated_timeout = validate_timeout(timeout, "timeout") or 30000
        # Some clients send integers as strings, and a schema `ge` bound fails on those before this runs.
        try:
            limit = int(max_chars) if max_chars is not None else None
        except TypeError, ValueError:
            limit = 0
        if limit is not None and limit < 1:
            raise SupacrawlValidationError(f"max_chars must be a positive integer, got {max_chars!r}")

        if engine is not None and engine not in ENGINE_CHOICES:
            raise SupacrawlValidationError(f"Invalid engine '{engine}'. Choose from: {', '.join(ENGINE_CHOICES)}")

        if formats is None:
            formats = ["markdown"]

        # Convert action dicts to Action objects if provided
        action_objects = None
        if actions:
            from supacrawl.services.actions import Action

            action_objects = [
                Action(
                    type=a.get("type", "wait"),
                    selector=a.get("selector"),
                    milliseconds=a.get("milliseconds"),
                    text=a.get("text"),
                    direction=a.get("direction"),
                    key=a.get("key"),
                    script=a.get("script"),
                    full_page=bool(a.get("fullPage", a.get("full_page", True))),
                )
                for a in actions
            ]

        # Resolve mobile/device to a device name
        from supacrawl.models import DEFAULT_MOBILE_DEVICE

        resolved_device: str | None = device
        if mobile and not device:
            resolved_device = DEFAULT_MOBILE_DEVICE

        # Resolve parse_pdf mode
        resolved_parse_pdf = parse_pdf if parse_pdf != "off" else None

        result = await api_client.scrape_service.scrape(
            url=validated_url,
            formats=formats,
            only_main_content=only_main_content,
            wait_for=wait_for,
            timeout=validated_timeout,
            screenshot_full_page=screenshot_full_page,
            actions=action_objects,
            json_schema=json_schema,
            json_prompt=json_prompt,
            include_tags=include_tags,
            exclude_tags=exclude_tags,
            max_age=max_age,
            change_tracking_modes=change_tracking_modes,
            expand_iframes=expand_iframes,  # type: ignore[arg-type]
            device=resolved_device,
            parse_pdf=resolved_parse_pdf,  # type: ignore[arg-type]
            engine=engine,
            headers=headers,
            content_mode=content_mode,
            query=query,
            http_first=http_first,
            expect=expect,
        )

        response = result.model_dump()
        response["correlation_id"] = correlation_id
        _bound_response(response, limit, query)
        return response

    except SupacrawlValidationError:
        raise
    except Exception as e:
        log_tool_exception("supacrawl_scrape", e)
        raise map_exception(e, endpoint="/scrape", correlation_id=correlation_id) from e
