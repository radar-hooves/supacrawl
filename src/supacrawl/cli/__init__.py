"""Command-line interface for supacrawl.

This package provides the CLI commands for supacrawl. Commands are organized
into modules by functionality:

- scrape: Single URL scraping
- crawl: Website crawling with URL discovery
- map: URL discovery and mapping
- agent: Search, extraction, and agent commands (search, llm-extract, agent)
- cache: Cache management subcommands (cache stats/clear/prune)
- config: Settings store and GUI schema (config get/set/schema/path/secrets)
- strategy: Per-domain strategy memory (strategy list/show/forget/clear)
- metrics: Field telemetry log (metrics summary/tail/path/prune)
- serve: REST API server
- skill: Install the agent skill (install-skill)
"""

# Import all command modules to register them with the app
# The order doesn't matter - Click handles command registration
from supacrawl.cli import (
    agent,
    batch,
    bench,
    cache,
    config,
    crawl,
    map,
    metrics,
    scrape,
    serve,
    skill,
    strategy,
)
from supacrawl.cli._common import app

__all__ = ["app"]
