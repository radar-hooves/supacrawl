"""Shared concurrent batch scrape service.

Provides ``run_batch_scrape`` — a reusable async function that scrapes a list of
URLs concurrently using a single shared browser and a semaphore to cap parallelism.

Used by both the CLI ``batch`` command and the API ``/v1/batch/scrape`` endpoint so
the concurrency and retry logic live in exactly one place.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from supacrawl.models import ScrapeResult
from supacrawl.services.browser import BrowserManager
from supacrawl.services.scrape import ScrapeService

if TYPE_CHECKING:
    from supacrawl.services.strategy_memory import StrategyStore
    from supacrawl.telemetry import MetricsSink

LOGGER = logging.getLogger(__name__)


@dataclass
class BatchURLResult:
    """Result for a single URL in a batch scrape.

    Attributes:
        url: The URL that was scraped.
        success: Whether the scrape succeeded.
        data: The ``ScrapeResult`` when successful; ``None`` on failure.
        error: Human-readable error message when ``success`` is ``False``.
        attempts: Number of attempts made (including retries).
    """

    url: str
    success: bool
    data: ScrapeResult | None = None
    error: str | None = None
    attempts: int = 1


@dataclass
class BatchScrapeResult:
    """Aggregate result from a batch scrape run.

    Attributes:
        results: Per-URL results in the same order as the input URLs.
        succeeded: Count of URLs that scraped successfully.
        failed: Count of URLs that failed after all retries.
        partial: ``True`` when at least one URL failed but
            ``continue_on_error`` was set, so the batch completed anyway.
    """

    results: list[BatchURLResult] = field(default_factory=list)
    succeeded: int = 0
    failed: int = 0
    partial: bool = False


async def run_batch_scrape(
    urls: list[str],
    *,
    browser: BrowserManager | None = None,
    scrape_service: ScrapeService | None = None,
    formats: list[str] | None = None,
    only_main_content: bool = True,
    timeout: int = 30000,
    max_age: int = 0,
    concurrency: int = 5,
    retry: int = 1,
    continue_on_error: bool = True,
    headers: dict[str, str] | None = None,
    proxy: str | None = None,
    engine: str | None = None,
    stealth: bool = False,
    cache_dir: Path | None = None,
    locale_config: Any | None = None,
    strategy_store: "StrategyStore | None" = None,
    telemetry: "MetricsSink | None" = None,
    scrape_kwargs: dict[str, Any] | None = None,
    cancelled: asyncio.Event | None = None,
) -> BatchScrapeResult:
    """Scrape a list of URLs concurrently with a shared browser.

    Accepts either an already-constructed ``scrape_service`` (caller owns the
    browser lifecycle) or creates its own ``BrowserManager`` internally.  The
    semaphore limits how many scrapes are in-flight at the same time.

    Args:
        urls: Ordered list of URLs to scrape.
        browser: Optional shared ``BrowserManager``.  When provided, the
            ``scrape_service`` argument is also expected; neither is closed on
            return.
        scrape_service: Optional ``ScrapeService`` backed by ``browser``.
            Created internally when not provided.
        formats: Output formats (default: ``["markdown"]``).
        only_main_content: Extract main content area only.
        timeout: Per-page load timeout in milliseconds.
        max_age: Cache freshness in seconds (``0`` = no cache).
        concurrency: Maximum number of concurrent in-flight scrapes.
        retry: Maximum per-URL retry attempts on failure (``1`` = one try, no
            retries; ``2`` = one retry, etc.).
        continue_on_error: When ``False``, the first URL failure aborts the
            remaining work and raises ``RuntimeError``.
        headers: Custom HTTP headers forwarded to every scrape call.
        proxy: Proxy URL forwarded to the ``BrowserManager`` when it is
            created internally.
        engine: Browser engine override forwarded to the ``BrowserManager``
            when it is created internally.
        stealth: Enable stealth mode when creating an internal browser.
        cache_dir: Cache directory forwarded to the ``ScrapeService`` when it
            is created internally.
        locale_config: Optional ``LocaleConfig`` forwarded to the service.
        strategy_store: Optional per-domain strategy memory (#130) forwarded to
            an internally-created ``ScrapeService`` so each URL seeds and updates
            its domain's learned strategy. Ignored when ``scrape_service`` is
            supplied (the caller's service carries its own).
        telemetry: Optional field telemetry sink (#137) forwarded to an
            internally-created ``ScrapeService`` so per-URL quality/usage is
            recorded.
        scrape_kwargs: Extra keyword arguments forwarded verbatim to
            ``ScrapeService.scrape`` for every URL (e.g. ``wait_for``,
            ``include_tags``, ``exclude_tags``, ``actions``).  These override
            the per-call defaults built from the explicit arguments, letting the
            API surface forward its full v2 scrape-option set.
        cancelled: Optional ``asyncio.Event``.  When set, in-flight slots
            drain cleanly and no new URLs are dispatched.

    Returns:
        ``BatchScrapeResult`` with per-URL outcomes and aggregate counts.
    """
    if not urls:
        return BatchScrapeResult()

    formats = formats or ["markdown"]
    semaphore = asyncio.Semaphore(concurrency)
    result = BatchScrapeResult()
    # Pre-allocate result slots so we can fill them from concurrent tasks
    # without requiring a lock on the list.
    result.results = [BatchURLResult(url=u, success=False) for u in urls]

    # We own the browser lifecycle only when no service was injected from outside.
    # A caller that supplies ``scrape_service`` already owns the browser; supplying
    # an additional ``browser`` arg is optional and only used for teardown when the
    # caller also wants us to close it.
    owns_browser = scrape_service is None

    # The exact keyword set forwarded to ScrapeService.scrape for each URL.
    # Explicit arguments provide the defaults; scrape_kwargs (used by the API
    # surface) overrides them so the full v2 option set is preserved. A
    # per-request proxy/engine is only meaningful when the browser is shared
    # (an injected service); in the internally-owned path those configure the
    # BrowserManager at construction instead.
    call_kwargs: dict[str, Any] = {
        "formats": formats,
        "only_main_content": only_main_content,
        "timeout": timeout,
        "max_age": max_age,
        "headers": headers,
    }
    if not owns_browser:
        if proxy is not None:
            call_kwargs["proxy"] = proxy
        if engine is not None:
            call_kwargs["engine"] = engine
    if scrape_kwargs:
        call_kwargs.update(scrape_kwargs)

    # The first URL of each domain runs alone so the strategy it learns (the
    # engine that got past a wall) seeds its siblings instead of each one
    # climbing the escalation ladder and tripping the same throttle.
    domain_leaders: dict[str, asyncio.Event] = {}

    async def _do_scrape(idx: int, url: str) -> None:
        from supacrawl.services.strategy_memory import registrable_domain

        domain = registrable_domain(url)
        leader = domain_leaders.get(domain) if domain else None
        if leader is not None:
            await leader.wait()
            await _scrape_one(idx, url)
            return
        if domain is None:
            await _scrape_one(idx, url)
            return
        domain_leaders[domain] = done = asyncio.Event()
        try:
            await _scrape_one(idx, url)
        finally:
            done.set()

    async def _scrape_one(idx: int, url: str) -> None:
        """Scrape one URL, honouring the semaphore and retry budget."""
        if cancelled is not None and cancelled.is_set():
            LOGGER.info("Batch scrape cancelled; skipping %s", url)
            result.results[idx].error = "cancelled"
            return

        async with semaphore:
            if cancelled is not None and cancelled.is_set():
                result.results[idx].error = "cancelled"
                return

            attempts = 0
            last_error: str | None = None
            while attempts < max(1, retry):
                attempts += 1
                try:
                    assert _scrape_service is not None
                    scrape_result = await _scrape_service.scrape(url=url, **call_kwargs)
                    if scrape_result.success:
                        result.results[idx] = BatchURLResult(
                            url=url,
                            success=True,
                            data=scrape_result,
                            attempts=attempts,
                        )
                        return
                    else:
                        last_error = scrape_result.error or "scrape returned success=False"
                        LOGGER.debug(
                            "Attempt %d/%d failed for %s: %s",
                            attempts,
                            retry,
                            url,
                            last_error,
                        )
                except Exception as exc:
                    last_error = str(exc)
                    LOGGER.debug(
                        "Attempt %d/%d raised for %s: %s",
                        attempts,
                        retry,
                        url,
                        last_error,
                    )

            # All attempts exhausted — record failure
            result.results[idx] = BatchURLResult(
                url=url,
                success=False,
                error=last_error,
                attempts=attempts,
            )
            LOGGER.warning("All %d attempt(s) failed for %s: %s", attempts, url, last_error)

    _browser: BrowserManager | None = None
    _scrape_service: ScrapeService

    if owns_browser:
        _browser = BrowserManager(
            timeout_ms=timeout,
            stealth=stealth,
            proxy=proxy,
            engine=engine,
            locale_config=locale_config,
        )
        await _browser.__aenter__()
        _scrape_service = ScrapeService(
            browser=_browser,
            locale_config=locale_config,
            cache_dir=cache_dir,
            stealth=stealth,
            proxy=proxy,
            engine=engine,
            strategy_store=strategy_store,
            telemetry=telemetry,
        )
    else:
        # scrape_service is non-None here by the owns_browser definition above.
        # browser may be None when the caller owns the service but not the browser
        # lifecycle (e.g. tests or the API router that share a long-lived service).
        assert scrape_service is not None  # guarded by owns_browser = scrape_service is None
        _scrape_service = scrape_service
        _browser = browser  # May be None; only used for optional teardown below

    try:
        async with asyncio.TaskGroup() as tg:
            for idx, url in enumerate(urls):
                tg.create_task(_do_scrape(idx, url))
    except* Exception as eg:
        # TaskGroup surfaces exceptions not caught inside _do_scrape.
        # Wrap and re-raise so the caller sees a single RuntimeError rather
        # than a raw ExceptionGroup.
        msgs = "; ".join(str(e) for e in eg.exceptions)
        raise RuntimeError(f"Batch scrape encountered unhandled errors: {msgs}") from eg
    finally:
        if owns_browser and _browser is not None:
            await _browser.__aexit__(None, None, None)

    for url_result in result.results:
        if url_result.success:
            result.succeeded += 1
        else:
            result.failed += 1

    result.partial = result.failed > 0 and continue_on_error

    if not continue_on_error and result.failed > 0:
        failed_urls = [r.url for r in result.results if not r.success]
        raise RuntimeError(
            f"{result.failed} URL(s) failed and --continue-on-error is disabled: " + ", ".join(failed_urls)
        )

    return result
