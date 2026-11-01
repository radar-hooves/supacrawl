"""Scraping commands."""

from pathlib import Path

import click

from supacrawl.cli._common import app, parse_header_string, parse_headers_env
from supacrawl.exceptions import SupacrawlError
from supacrawl.models import DEFAULT_MOBILE_DEVICE, QualityVerdict


@app.command("scrape", help="Scrape a single URL to markdown.")
@click.argument("url", required=False, default=None)
@click.option(
    "--format",
    "-f",
    "formats",
    multiple=True,
    type=click.Choice(
        [
            "markdown",
            "html",
            "rawHtml",
            "links",
            "linkDetails",
            "images",
            "screenshot",
            "pdf",
            "json",
            "branding",
            "structuredData",
            "summary",
            "changeTracking",
        ],
        case_sensitive=False,
    ),
    default=["markdown"],
    show_default=True,
    help="Output formats to include",
)
@click.option(
    "--schema",
    type=click.Path(exists=True, file_okay=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to JSON schema file (for json format)",
)
@click.option(
    "--prompt",
    "-p",
    type=str,
    default=None,
    help="Extraction prompt (for json format)",
)
@click.option(
    "--only-main-content/--no-only-main-content",
    default=True,
    show_default=True,
    help="Extract main content area only",
)
@click.option(
    "--wait-for",
    type=int,
    default=0,
    show_default=True,
    help="Additional wait time in ms after page load",
)
@click.option(
    "--timeout",
    type=int,
    default=30000,
    show_default=True,
    help="Page load timeout in ms",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(file_okay=True, dir_okay=False, path_type=Path),
    default=None,
    help="Output file. Use .md, .json, .html, .png (screenshot), or .pdf.",
)
@click.option(
    "--full-page/--no-full-page",
    default=True,
    show_default=True,
    help="Capture full scrollable page for screenshots",
)
@click.option(
    "--actions",
    "-a",
    type=click.Path(exists=True, file_okay=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to JSON file containing page actions.",
)
@click.option(
    "--include-tags",
    multiple=True,
    help="CSS selectors for elements to include (can be repeated). Takes precedence over --only-main-content.",
)
@click.option(
    "--exclude-tags",
    multiple=True,
    help="CSS selectors for elements to exclude (can be repeated). Applied before include-tags.",
)
@click.option(
    "--country",
    type=str,
    default=None,
    help="ISO country code for locale settings (e.g., AU, US, DE). Sets language and timezone defaults.",
)
@click.option(
    "--language",
    type=str,
    default=None,
    help="Browser language/locale code (e.g., en-AU, de-DE). Overrides --country language.",
)
@click.option(
    "--timezone",
    type=str,
    default=None,
    help="IANA timezone (e.g., Australia/Sydney). Overrides --country timezone.",
)
@click.option(
    "--max-age",
    type=int,
    default=0,
    show_default=True,
    help="Cache freshness in seconds (0=no cache). Returns cached content if fresh.",
)
@click.option(
    "--cache-dir",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="Cache directory. Defaults to ~/.supacrawl/cache or SUPACRAWL_CACHE_DIR.",
)
@click.option(
    "--stealth/--no-stealth",
    default=False,
    help="Enhanced stealth mode via Patchright (requires: pip install supacrawl[stealth]). Note: Basic anti-bot evasion is always active.",
)
@click.option(
    "--engine",
    type=click.Choice(["playwright", "patchright", "camoufox"], case_sensitive=False),
    default=None,
    help="Browser engine. playwright=default, patchright=Tier 2 stealth (requires supacrawl[stealth]), camoufox=Tier 3 for Akamai (requires supacrawl[camoufox]). Overrides --stealth.",
)
@click.option(
    "--proxy",
    type=str,
    default=None,
    help="Proxy URL (e.g., http://user:pass@host:port, socks5://host:port). Also reads SUPACRAWL_PROXY env.",
)
@click.option(
    "--solve-captcha/--no-solve-captcha",
    default=False,
    help="Enable CAPTCHA solving via 2Captcha (requires: pip install supacrawl[captcha] and CAPTCHA_API_KEY env var). WARNING: Each solve costs ~$0.002-0.003.",
)
@click.option(
    "--wait-until",
    type=click.Choice(["commit", "domcontentloaded", "load", "networkidle"], case_sensitive=False),
    default=None,
    help="Page load strategy. Default: domcontentloaded. Use 'load' to wait for all resources, 'networkidle' for JS-heavy sites. Also reads SUPACRAWL_WAIT_UNTIL env.",
)
@click.option(
    "--change-tracking-modes",
    multiple=True,
    type=click.Choice(["git-diff", "json"], case_sensitive=False),
    help="Diff modes for change tracking. Requires -f changeTracking. Options: git-diff, json (requires --schema or --prompt).",
)
@click.option(
    "--expand-iframes",
    type=click.Choice(["none", "same-origin", "all"], case_sensitive=False),
    default="same-origin",
    show_default=True,
    help="Iframe expansion mode. none=strip all, same-origin=expand same-origin inline, all=expand all non-blocked.",
)
@click.option(
    "--mobile/--no-mobile",
    default=False,
    help=f"Scrape as a mobile device (default: {DEFAULT_MOBILE_DEVICE}). Sets mobile viewport, user agent, and touch support.",
)
@click.option(
    "--device",
    type=str,
    default=None,
    help='Emulate a specific device (e.g. "iPhone 15", "Pixel 7"). Overrides --mobile. See --list-devices for options.',
)
@click.option(
    "--list-devices",
    is_flag=True,
    default=False,
    help="List available device presets and exit.",
)
@click.option(
    "--header",
    "-H",
    "headers",
    multiple=True,
    metavar="KEY: VALUE",
    help=(
        "Custom HTTP header in 'Key: Value' format. Repeatable. "
        "Also reads SUPACRAWL_HEADERS env (comma-separated 'Key: Value' pairs). "
        "Example: --header 'Authorization: Bearer token'"
    ),
)
@click.option(
    "--parse-pdf",
    type=click.Choice(["fast", "auto", "ocr", "off"], case_sensitive=False),
    default="auto",
    show_default=True,
    help="PDF parsing mode. auto=detect .pdf URLs and extract text (OCR fallback if available), fast=text only, ocr=force OCR, off=disable.",
)
@click.option(
    "--content-mode",
    type=click.FloatRange(0.0, 1.0),
    default=0.5,
    show_default=True,
    help=(
        "Content extraction precision/recall dial [0.0–1.0]. "
        "0.0 = recall-biased (include more, prune less); "
        "1.0 = precision-biased (demand denser output, prune more). "
        "Without supacrawl[readability] only the CSS-selector heuristic is active; "
        "the readability and BM25 strategies are silently skipped."
    ),
)
@click.option(
    "--query",
    type=str,
    default=None,
    help=(
        "Filter extracted sections by relevance to this query (BM25). "
        "Flat pages with no headings are never filtered. "
        "Without supacrawl[readability] this option is accepted but ignored."
    ),
)
@click.option(
    "--http-first/--no-http-first",
    default=True,
    show_default=True,
    help=(
        "Try a cheap HTTP GET before launching a browser, escalating to the "
        "browser only when JavaScript or a bot challenge is detected. "
        "Use --no-http-first to always render in the browser."
    ),
)
@click.option(
    "--expect",
    type=str,
    default=None,
    help=(
        "Require asserted content to be present before returning. A bare integer "
        "is a minimum word count; any other value is matched first as a CSS "
        "selector and then as a text substring. If unmet, the scrape waits and "
        "retries with escalation rather than returning a pre-hydration skeleton. "
        "Example: --expect '.product-price' or --expect 'In stock' or --expect 200"
    ),
)
def scrape_url(
    url: str | None,
    formats: tuple[str, ...],
    schema: Path | None,
    prompt: str | None,
    only_main_content: bool,
    wait_for: int,
    timeout: int,
    output: Path | None,
    full_page: bool,
    actions: Path | None,
    include_tags: tuple[str, ...],
    exclude_tags: tuple[str, ...],
    country: str | None,
    language: str | None,
    timezone: str | None,
    max_age: int,
    cache_dir: Path | None,
    stealth: bool,
    engine: str | None,
    proxy: str | None,
    solve_captcha: bool,
    wait_until: str | None,
    change_tracking_modes: tuple[str, ...],
    expand_iframes: str,
    mobile: bool,
    device: str | None,
    list_devices: bool,
    headers: tuple[str, ...],
    parse_pdf: str,
    content_mode: float,
    query: str | None,
    http_first: bool,
    expect: str | None,
) -> None:
    """Scrape a single URL and extract content.

    ANTI-BOT PROTECTION (automatic, no configuration needed):
        Basic fingerprint evasion, browser headers, and bot detection are always active.
        If blocked, automatically retries with enhanced stealth when patchright is installed.

    FOR HEAVILY PROTECTED SITES:
        Install: pip install supacrawl[stealth]
        Then use: --stealth flag

    FOR SITES WITH CAPTCHA:
        1. Install: pip install supacrawl[captcha]
        2. Configure: export CAPTCHA_API_KEY=your-2captcha-api-key
        3. Use: --solve-captcha flag
        WARNING: Each CAPTCHA solve costs ~$0.002-0.003

    Actions JSON format:
        [
            {"type": "wait", "milliseconds": 2000},
            {"type": "click", "selector": "button#load-more"},
            {"type": "scroll", "direction": "down"},
            {"type": "type", "selector": "input#search", "text": "query"},
            {"type": "press", "key": "Enter"},
            {"type": "executeJavascript", "script": "document.title"}
        ]

    Examples:
        supacrawl scrape https://example.com
        supacrawl scrape https://example.com --output page.md
        supacrawl scrape https://example.com --output page.json
        supacrawl scrape https://example.com --format markdown --format html
        supacrawl scrape https://example.com --format images --output images.json
        supacrawl scrape https://example.com --format markdown --format images
        supacrawl scrape https://example.com --format branding --output branding.json
        supacrawl scrape https://example.com --format summary --output summary.txt
        supacrawl scrape https://example.com --format markdown --format summary
        supacrawl scrape https://example.com --format screenshot --output page.png
        supacrawl scrape https://example.com --format pdf --output page.pdf
        supacrawl scrape https://example.com --format json --prompt "Extract product name and price"
        supacrawl scrape https://example.com --format json --schema schema.json
        supacrawl scrape https://example.com --actions actions.json
        supacrawl scrape https://example.com --include-tags article --include-tags .post-content
        supacrawl scrape https://example.com --exclude-tags nav --exclude-tags .sidebar --exclude-tags footer
        supacrawl scrape https://example.com --country AU
        supacrawl scrape https://example.com --language en-AU --timezone Australia/Sydney
        supacrawl scrape https://example.com --max-age 3600  # Use cache if fresh within 1 hour
        supacrawl scrape https://example.com --max-age 3600 --cache-dir ~/.my-cache
        supacrawl scrape https://protected-site.com --stealth  # Force stealth mode (Patchright)
        supacrawl scrape https://akamai-site.com --engine camoufox  # Tier 3: Akamai bypass
        supacrawl scrape https://captcha-site.com --stealth --solve-captcha  # Solve CAPTCHAs
        supacrawl scrape https://spa-site.com --wait-until networkidle  # Wait for JS to finish
        supacrawl scrape https://example.com --mobile  # Scrape as default mobile device
        supacrawl scrape https://example.com --device "iPhone 15"  # Scrape as iPhone 15
        supacrawl scrape https://example.com --mobile -f screenshot -o mobile.png  # Mobile screenshot
        supacrawl scrape --list-devices  # Show available device presets
        supacrawl scrape https://example.com/report.pdf  # Auto-detect PDF and extract text
        supacrawl scrape https://example.com/report.pdf --parse-pdf fast  # Text extraction only
        supacrawl scrape https://example.com/scanned.pdf --parse-pdf ocr  # Force OCR
        supacrawl scrape https://example.com/report.pdf -f json --prompt "Extract revenue figures"
        supacrawl scrape https://example.com/report.pdf --parse-pdf off  # Disable PDF parsing
    """
    import asyncio
    import base64
    import json

    # Handle --list-devices: print available devices and exit
    if list_devices:

        async def _list():
            from playwright.async_api import async_playwright

            pw = await async_playwright().start()
            try:
                return sorted(pw.devices.keys())
            finally:
                await pw.stop()

        devices = asyncio.run(_list())
        for name in devices:
            click.echo(name)
        return

    # URL is required when not listing devices
    if not url:
        click.echo("Error: Missing argument 'URL'.", err=True)
        raise SystemExit(1)

    # Resolve --mobile / --device to a device name
    resolved_device: str | None = device
    if mobile and not device:
        resolved_device = DEFAULT_MOBILE_DEVICE

    from supacrawl.services.scrape import ScrapeService
    from supacrawl.services.strategy_memory import StrategyStore
    from supacrawl.telemetry import MetricsSink

    # Auto-add format based on output extension if not explicitly provided
    formats_list = list(formats)
    if output:
        suffix = output.suffix.lower()
        if suffix == ".png" and "screenshot" not in formats_list:
            formats_list.append("screenshot")
        elif suffix == ".pdf" and "pdf" not in formats_list:
            formats_list.append("pdf")

    # Parse actions from JSON file if provided
    parsed_actions = None
    if actions:
        from supacrawl.services.actions import parse_actions

        with open(actions) as f:
            actions_json = json.load(f)
        parsed_actions = parse_actions(actions_json)
        click.echo(f"Loaded {len(parsed_actions)} actions from {actions}", err=True)

    # Parse schema from JSON file if provided
    parsed_schema = None
    if schema:
        with open(schema) as f:
            parsed_schema = json.load(f)

    # Validate json format usage
    if "json" in formats_list:
        if not prompt and not parsed_schema:
            click.echo("Error: --prompt or --schema required when using json format", err=True)
            raise SystemExit(1)

    # Build locale config if any location options specified
    locale_config = None
    if country or language or timezone:
        from supacrawl.models import LocaleConfig

        if country:
            locale_config = LocaleConfig.from_country(country)
            # Override with explicit language/timezone if provided
            if language:
                locale_config = locale_config.model_copy(update={"language": language})
            if timezone:
                locale_config = locale_config.model_copy(update={"timezone": timezone})
        else:
            locale_config = LocaleConfig(language=language, timezone=timezone)

    # Resolve cache directory if max_age is set or change tracking is requested
    # Change tracking needs a cache to store/compare previous versions
    needs_cache = max_age > 0 or "changeTracking" in formats_list
    resolved_cache_dir = cache_dir if needs_cache else None
    if needs_cache and not resolved_cache_dir:
        from supacrawl.cache import CacheManager

        resolved_cache_dir = CacheManager.DEFAULT_CACHE_DIR

    # Print cost warning for CAPTCHA solving
    if solve_captcha:
        click.echo(
            "WARNING: CAPTCHA solving is enabled. Each solve costs ~$0.002-0.003.",
            err=True,
        )

    # Parse --header flags into a dict; overlay with SUPACRAWL_HEADERS env default
    resolved_headers: dict[str, str] | None = parse_headers_env()
    if headers:
        if resolved_headers is None:
            resolved_headers = {}
        for raw in headers:
            name, value = parse_header_string(raw)
            resolved_headers[name] = value

    async def run():
        service = ScrapeService(
            locale_config=locale_config,
            cache_dir=resolved_cache_dir,
            stealth=stealth,
            proxy=proxy,
            solve_captcha=solve_captcha,
            engine=engine,
            strategy_store=StrategyStore.default(),
            telemetry=MetricsSink.default(),
        )
        result = await service.scrape(
            url=url,
            formats=formats_list,  # type: ignore[arg-type]
            only_main_content=only_main_content,
            wait_for=wait_for,
            timeout=timeout,
            screenshot_full_page=full_page,
            actions=parsed_actions,
            json_schema=parsed_schema,
            json_prompt=prompt,
            include_tags=list(include_tags) if include_tags else None,
            exclude_tags=list(exclude_tags) if exclude_tags else None,
            max_age=max_age,
            wait_until=wait_until,  # type: ignore[arg-type]
            change_tracking_modes=list(change_tracking_modes) if change_tracking_modes else None,
            expand_iframes=expand_iframes,  # type: ignore[arg-type]
            device=resolved_device,
            parse_pdf=parse_pdf if parse_pdf != "off" else None,  # type: ignore[arg-type]
            headers=resolved_headers,
            content_mode=content_mode,
            query=query,
            http_first=http_first,
            expect=expect,
        )
        return result

    # Top-level guard: the service returns clean failure results rather than
    # raising, but a browser launch error or an interrupt must still exit with a
    # friendly message and a non-zero code, never a raw traceback.
    try:
        result = asyncio.run(run())
    except KeyboardInterrupt:
        click.echo("Aborted.", err=True)
        raise SystemExit(130) from None
    except SupacrawlError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1) from e
    except Exception as e:  # noqa: BLE001 — a CLI must never surface a raw traceback
        click.echo(f"Error: scrape failed: {e}", err=True)
        raise SystemExit(1) from e

    # Surface the runtime quality signal on stderr so a human (and any piped
    # tooling) can see when content came back thin, shell-like, or behind a wall
    # even on a nominal success. Failures carry the same detail in the error.
    if result.quality and result.quality.verdict != QualityVerdict.OK:
        note = f"Quality: {result.quality.verdict.value} (score {result.quality.score})"
        if result.quality.attempts > 1:
            note += f", {result.quality.attempts} attempts"
        if result.quality.suggestion:
            note += f" — {result.quality.suggestion}"
        click.echo(note, err=True)

    # Handle errors
    if not result.success:
        click.echo(f"Error: {result.error}", err=True)
        raise SystemExit(1)

    # Output handling
    if output:
        suffix = output.suffix.lower()
        if suffix == ".json":
            # Full JSON result
            with open(output, "w") as f:
                json.dump(result.model_dump(exclude_none=True), f, indent=2)
        elif suffix == ".html":
            # HTML content
            if result.data and result.data.html:
                with open(output, "w") as f:
                    f.write(result.data.html)
            else:
                click.echo("No HTML content available", err=True)
                raise SystemExit(1)
        elif suffix == ".png":
            # Screenshot (binary)
            if result.data and result.data.screenshot:
                with open(output, "wb") as fb:
                    fb.write(base64.b64decode(result.data.screenshot))
            else:
                click.echo("No screenshot available", err=True)
                raise SystemExit(1)
        elif suffix == ".pdf":
            # PDF (binary)
            if result.data and result.data.pdf:
                with open(output, "wb") as fb:
                    fb.write(base64.b64decode(result.data.pdf))
            else:
                click.echo("No PDF available", err=True)
                raise SystemExit(1)
        elif suffix == ".txt" and "summary" in formats_list:
            # Summary text only
            if result.data and result.data.summary:
                with open(output, "w") as f:
                    f.write(result.data.summary)
            else:
                click.echo("No summary available", err=True)
                raise SystemExit(1)
        else:
            # Default to markdown (.md or any other extension)
            if result.data and result.data.markdown:
                with open(output, "w") as f:
                    # Add YAML frontmatter with metadata
                    frontmatter = result.data.metadata.to_frontmatter(url)
                    f.write(frontmatter)
                    f.write("\n\n")
                    f.write(result.data.markdown)
            else:
                click.echo("No markdown content available", err=True)
                raise SystemExit(1)
        click.echo(f"Wrote {output}")
    else:
        # Print markdown to stdout, or the structured-data JSON when that was the
        # requested output and no markdown was produced.
        if result.data and result.data.markdown:
            click.echo(result.data.markdown)
        elif result.data and "structuredData" in formats_list and result.data.structured_data is not None:
            click.echo(json.dumps(result.data.structured_data.model_dump(exclude_none=True), indent=2))
        else:
            click.echo("No markdown content available", err=True)
            raise SystemExit(1)
