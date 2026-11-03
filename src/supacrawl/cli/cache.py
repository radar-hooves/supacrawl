"""Cache management commands."""

from pathlib import Path

import click

from supacrawl.cli._common import app


@app.group()
def cache() -> None:
    """Manage the local scrape cache.

    The cache stores scraped content locally for faster repeated requests.
    Use --max-age with scrape to enable caching.

    Examples:
        supacrawl cache stats           # Show cache statistics
        supacrawl cache clear           # Clear all cached entries
        supacrawl cache clear --url URL # Clear cache for specific URL
        supacrawl cache prune           # Remove expired entries
    """
    pass


@cache.command("stats")
@click.option(
    "--cache-dir",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="Cache directory. Defaults to ~/.supacrawl/cache or SUPACRAWL_CACHE_DIR.",
)
def cache_stats(cache_dir: Path | None) -> None:
    """Show cache statistics."""
    from supacrawl.cache import CacheManager

    cache_manager = CacheManager(cache_dir)
    stats = cache_manager.stats()

    click.echo("Cache Statistics:")
    click.echo(f"  Directory: {stats['cache_dir']}")
    click.echo(f"  Total entries: {stats['entries']}")
    click.echo(f"  Valid entries: {stats['valid']}")
    click.echo(f"  Expired entries: {stats['expired']}")
    click.echo(f"  Size: {stats['size_human']}")


@cache.command("clear")
@click.option(
    "--url",
    type=str,
    default=None,
    help="Clear cache for specific URL only. If not specified, clears all cache.",
)
@click.option(
    "--cache-dir",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="Cache directory. Defaults to ~/.supacrawl/cache or SUPACRAWL_CACHE_DIR.",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    default=False,
    help="Skip confirmation prompt.",
)
def cache_clear(url: str | None, cache_dir: Path | None, yes: bool) -> None:
    """Clear cached entries."""
    from supacrawl.cache import CacheManager

    if not yes and not url and not click.confirm("Are you sure you want to clear all cache entries?"):
        click.echo("Aborted.")
        return

    cache_manager = CacheManager(cache_dir)
    cleared = cache_manager.clear(url)

    if url:
        if cleared:
            click.echo(f"Cleared cache for: {url}")
        else:
            click.echo(f"No cache entry found for: {url}")
    else:
        click.echo(f"Cleared {cleared} cache entries")


@cache.command("prune")
@click.option(
    "--cache-dir",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="Cache directory. Defaults to ~/.supacrawl/cache or SUPACRAWL_CACHE_DIR.",
)
def cache_prune(cache_dir: Path | None) -> None:
    """Remove expired cache entries."""
    from supacrawl.cache import CacheManager

    cache_manager = CacheManager(cache_dir)
    pruned = cache_manager.prune_expired()

    click.echo(f"Pruned {pruned} expired cache entries")
