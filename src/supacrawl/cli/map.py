"""URL mapping and discovery commands."""

from pathlib import Path
from typing import Any

import click

from supacrawl.cli._common import app, parse_header_string, parse_headers_env


@app.command("map", help="Discover URLs from a website.")
@click.argument("url")
@click.option(
    "--limit",
    type=int,
    default=200,
    show_default=True,
    help="Maximum number of URLs to discover.",
)
@click.option(
    "--depth",
    type=int,
    default=3,
    show_default=True,
    help="Maximum BFS crawl depth.",
)
@click.option(
    "--sitemap",
    type=click.Choice(["include", "skip", "only"], case_sensitive=False),
    default="include",
    show_default=True,
    help="Sitemap handling: include (default), skip, or only.",
)
@click.option(
    "--include-subdomains",
    is_flag=True,
    default=False,
    help="Include subdomain URLs.",
)
@click.option(
    "--search",
    type=str,
    default=None,
    help="Filter URLs containing this text.",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(file_okay=True, dir_okay=False, path_type=Path),
    default=None,
    help="Output file path. If omitted, prints to stdout.",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["json", "text"], case_sensitive=False),
    default="text",
    show_default=True,
    help="Output format: json (full result) or text (URLs only).",
)
@click.option(
    "--ignore-query-params",
    is_flag=True,
    default=False,
    help="Remove query parameters from URLs.",
)
@click.option(
    "--stealth/--no-stealth",
    default=False,
    help="Enhanced stealth mode via Patchright (requires: pip install supacrawl[stealth]). Note: Basic anti-bot evasion is always active.",
)
@click.option(
    "--proxy",
    type=str,
    default=None,
    help="Proxy URL (e.g., http://user:pass@host:port, socks5://host:port). Also reads SUPACRAWL_PROXY env.",
)
@click.option(
    "--concurrency",
    "-c",
    type=int,
    default=5,
    show_default=True,
    help="Max concurrent requests for URL processing.",
)
@click.option(
    "--wait-until",
    type=click.Choice(["commit", "domcontentloaded", "load", "networkidle"], case_sensitive=False),
    default=None,
    help="Page load strategy. Default: load. Use 'networkidle' for JS-heavy sites. Also reads SUPACRAWL_WAIT_UNTIL env.",
)
@click.option(
    "--engine",
    type=click.Choice(["playwright", "patchright", "camoufox"], case_sensitive=False),
    default=None,
    help="Browser engine. playwright=default, patchright=Tier 2 stealth (requires supacrawl[stealth]), camoufox=Tier 3 for Akamai (requires supacrawl[camoufox]). Overrides --stealth.",
)
@click.option(
    "--ignore-cache",
    is_flag=True,
    default=False,
    help="Bypass cached results and perform fresh URL discovery.",
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
def map_cmd(
    url: str,
    limit: int,
    depth: int,
    sitemap: str,
    include_subdomains: bool,
    search: str | None,
    output: Path | None,
    output_format: str,
    ignore_query_params: bool,
    stealth: bool,
    proxy: str | None,
    concurrency: int,
    wait_until: str | None,
    engine: str | None,
    ignore_cache: bool,
    headers: tuple[str, ...],
) -> None:
    """Map a website to discover all URLs.

    Examples:
        supacrawl map https://example.com
        supacrawl map https://example.com --limit 50 --format json
        supacrawl map https://example.com --search about --output urls.json --format json
    """
    import asyncio
    import json
    import sys

    from supacrawl.services.map import MapService

    # Parse --header flags; overlay with SUPACRAWL_HEADERS env default
    resolved_headers: dict[str, str] | None = parse_headers_env()
    if headers:
        if resolved_headers is None:
            resolved_headers = {}
        for raw in headers:
            name, value = parse_header_string(raw)
            resolved_headers[name] = value

    async def run() -> Any:
        service = MapService(
            stealth=stealth,
            proxy=proxy,
            concurrency=concurrency,
            wait_until=wait_until,  # type: ignore[arg-type]
            engine=engine,
            headers=resolved_headers,
        )
        result = None
        last_progress = ""

        async for event in service.map(
            url=url,
            limit=limit,
            max_depth=depth,
            sitemap=sitemap,  # type: ignore[arg-type]
            include_subdomains=include_subdomains,
            search=search,
            ignore_query_params=ignore_query_params,
            ignore_cache=ignore_cache,
        ):
            if event.type == "complete":
                result = event.result
                # Clear progress line
                if last_progress and sys.stderr.isatty():
                    click.echo("\r" + " " * len(last_progress) + "\r", nl=False, err=True)
            elif event.type == "error":
                return event.result  # Contains error info
            else:
                # Show progress on stderr (only if terminal)
                if sys.stderr.isatty():
                    if event.type == "sitemap":
                        progress = f"Sitemap: {event.message}"
                    elif event.type == "discovery":
                        progress = f"Discovering: {event.discovered}/{event.total} URLs"
                    elif event.type == "metadata":
                        progress = f"Metadata: {event.discovered}/{event.total}"
                    else:
                        progress = event.message or ""
                    # Overwrite previous line
                    click.echo(f"\r{progress:<60}", nl=False, err=True)
                    last_progress = progress

        return result

    result = asyncio.run(run())

    # Handle errors
    if result is None or not result.success:
        click.echo(f"Error: {result.error if result else 'Map failed'}", err=True)
        raise SystemExit(1)

    # Generate output content based on format
    if output_format == "json":
        # exclude_none=True for cleaner output (optional fields omitted when None)
        content = json.dumps(result.model_dump(exclude_none=True), indent=2)
    else:
        # Text format: URLs only, one per line
        content = "\n".join(link.url for link in result.links)

    # Write to file or stdout
    if output:
        with open(output, "w") as f:
            f.write(content)
            if not content.endswith("\n"):
                f.write("\n")
        click.echo(f"Wrote {len(result.links)} URLs to {output}")
    else:
        click.echo(content)
