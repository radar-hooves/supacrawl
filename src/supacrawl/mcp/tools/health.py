"""Health check tool for Supacrawl MCP server."""

import asyncio
import os
from typing import Annotated, Any

from pydantic import Field

from supacrawl.mcp.config import SERVICE_VERSION, settings
from supacrawl.mcp.tools import mask_secrets
from supacrawl.services.content_filter import available_extras
from supacrawl.services.registry import SupacrawlServices

# Query used for the live search probe (#156, #161). It is deliberately a
# common MULTI-WORD phrase: the regression it exists to catch was multi-word
# queries coming back empty while a rare one-word probe still scraped a single
# result and read "healthy". Any working general search returns a full page for
# this, so a result count below the floor means the backend is broken, not that
# the query is obscure. limit is per-query, not per-result, so asking for more
# than one costs a quota-metered provider (Brave) exactly the same one query.
_SEARCH_PROBE_QUERY = "open source software"
_SEARCH_PROBE_LIMIT = 5
# The floor a healthy backend must clear. `result_count > 0` was the old bar and
# it passed on a single scraped result while real queries returned nothing —
# the exact lie this probe now refuses to tell.
_SEARCH_PROBE_MIN_RESULTS = 3
_SEARCH_PROBE_TIMEOUT_S = 15.0


def _get_cache_info() -> dict[str, Any]:
    """Get cache directory statistics if caching is enabled."""
    cache_path = settings.get_cache_path()
    if not cache_path or not cache_path.exists():
        return {"enabled": False}

    try:
        # Count entries and calculate size
        entries = 0
        total_size = 0
        for item in cache_path.rglob("*"):
            if item.is_file():
                entries += 1
                total_size += item.stat().st_size

        return {
            "enabled": True,
            "path": str(cache_path),
            "entries": entries,
            "size_mb": round(total_size / (1024 * 1024), 2),
        }
    except Exception:
        return {"enabled": True, "path": str(cache_path), "error": "could not read stats"}


def _get_llm_config() -> dict[str, Any]:
    """Get LLM configuration status (used for json extraction format)."""
    provider = os.getenv("SUPACRAWL_LLM_PROVIDER")
    model = os.getenv("SUPACRAWL_LLM_MODEL")

    if not provider:
        return {"configured": False}

    return {
        "configured": True,
        "provider": provider,
        "model": model or "default",
    }


def _get_search_config(search_service: Any = None) -> dict[str, Any]:
    """Get search provider configuration and effective runtime state.

    Args:
        search_service: Optional SearchService instance for live provider health.
    """
    from supacrawl.services.search import _PROVIDER_RATE_LIMITS

    has_brave_key = bool(os.getenv("BRAVE_API_KEY"))

    # Determine effective rate limit
    primary_provider = "brave"
    if settings.search_rate_limit is not None:
        rate_limit = settings.search_rate_limit
    else:
        rate_limit = _PROVIDER_RATE_LIMITS.get(primary_provider, 10.0)

    # Provider chain configuration
    configured_providers = settings.search_providers
    if configured_providers:
        provider_list = [p.strip() for p in configured_providers.split(",") if p.strip()]
    else:
        provider_list = [settings.search_provider]

    # Determine overall status
    fallback_active = False
    if search_service and hasattr(search_service, "provider_chain"):
        chain = search_service.provider_chain
        active = chain.active_providers
        provider_health = chain.get_health()

        # The chain knows what was actually asked for; the MCP settings object
        # only knows what this process's env said. Prefer the chain (#158).
        if chain.configured_names:
            provider_list = list(chain.configured_names)

        if active:
            effective_provider = active[0].name
            # A chain answering from a provider outside the configured set is
            # NOT ready. Every field below can read fine while the server serves
            # from somewhere nobody chose — that is exactly what #158 is about.
            # `unconfigured_fallback_active` catches who would serve NEXT;
            # `fallback_serving` catches who served LAST, so a configured backend
            # that is up but failing every request (served by DDG behind it) is
            # not reported ready (#161).
            fallback_active = chain.unconfigured_fallback_active or chain.fallback_serving
            if chain.fallback_serving and chain.last_provider:
                # The fallback is what is actually answering right now.
                effective_provider = chain.last_provider
            status = "degraded" if fallback_active else "ready"
        else:
            status = "degraded"
            effective_provider = provider_list[0] if provider_list else "none"
    else:
        provider_health = {}
        # Static check: verify at least one provider has its API key configured
        _provider_key_envs = {
            "brave": "BRAVE_API_KEY",
            "tavily": "TAVILY_API_KEY",
            "serper": "SERPER_API_KEY",
            "serpapi": "SERPAPI_API_KEY",
            "exa": "EXA_API_KEY",
            "searxng": "SEARXNG_URL",
        }
        effective_provider = "none"
        status = "degraded"
        for p in provider_list:
            if p == "duckduckgo":
                # DDG needs no key but is deprecated/unreliable
                if effective_provider == "none":
                    effective_provider = "duckduckgo"
                continue
            env_var = _provider_key_envs.get(p)
            if env_var and bool(os.getenv(env_var)):
                effective_provider = p
                status = "ready"
                break

    config: dict[str, Any] = {
        "configured_providers": provider_list,
        "effective_provider": effective_provider,
        "provider_fallback_active": fallback_active,
        "status": status,
        "brave_api_key_configured": has_brave_key,
        "rate_limit_rps": rate_limit,
    }

    if fallback_active:
        config["warning"] = (
            f"PROVIDER FALLBACK ACTIVE — configured {provider_list} but serving from "
            f"{effective_provider!r}, which was not configured. Searches are leaving via a provider "
            "the operator did not select; if the configured provider is a self-hosted backend, queries "
            "that were meant to stay in-house are not. Fix the configured provider's credential/URL, "
            "or set SUPACRAWL_SEARCH_STRICT_PROVIDERS=1 to fail instead of falling back."
        )

    if provider_health:
        config["providers"] = provider_health
        # Surface a low-credit warning at the config level so MCP clients can
        # act on it without needing to inspect the per-provider health dict.
        from supacrawl.services.search.providers import LOW_CREDIT_THRESHOLD, renewal_hint

        low_credit = [
            (name, info["remaining_credits"])
            for name, info in provider_health.items()
            if isinstance(info.get("remaining_credits"), int) and info["remaining_credits"] < LOW_CREDIT_THRESHOLD
        ]
        if low_credit:
            listed = ", ".join(f"{name} ({remaining} left)" for name, remaining in low_credit)
            hints = "; ".join(dict.fromkeys(renewal_hint(name) for name, _ in low_credit))
            credit_note = f"Low search credits on: {listed}. {hints} to avoid outages."
            existing = config.get("warning")
            config["warning"] = f"{existing} {credit_note}" if existing else credit_note

    if status == "degraded" and effective_provider == "duckduckgo" and "warning" not in config:
        config["warning"] = (
            "Using DuckDuckGo fallback (deprecated, unreliable). "
            "Set BRAVE_API_KEY for reliable search — see https://brave.com/search/api/"
        )

    # Recent real traffic, not just a synthetic probe: if the last full window of
    # caller searches ALL came back empty, the backend is failing continuously
    # even though a provider is configured — the state that read consecutive_
    # failures: 0 while every caller was getting nothing (#161).
    recent = getattr(search_service, "recent_search_health", None) if search_service is not None else None
    if isinstance(recent, dict):
        config["recent_search_health"] = recent
        if recent.get("all_recent_empty"):
            config["status"] = "degraded"
            note = (
                f"The last {recent['recent_searches']} real searches all returned no results — the search "
                "backend appears to be failing even though a provider is configured."
            )
            existing = config.get("warning")
            config["warning"] = f"{existing} {note}".strip() if existing else note

    return config


async def _run_search_health_probe(search_service: Any) -> dict[str, Any] | None:
    """Run one real, minimal search to verify the effective provider actually returns results.

    ``_get_search_config`` only checks that a provider is *configured* (has a
    URL/API key) — a provider chain can report every provider "available" while
    the underlying search silently returns nothing (#156: SearXNG's multi-word
    query bug went undetected because ``is_available()`` is just ``bool(url)``).
    This runs one real multi-word query through the search path and judges it
    against a result-count floor, so a backend that returns nothing (or a token
    single result) surfaces here instead of reading "healthy" (#161).

    Args:
        search_service: The live SearchService, or None if unavailable.

    Returns:
        None when there is no search service to probe. Otherwise a dict with
        ``probed=True``, ``ok`` (whether the count cleared ``min_results``),
        ``result_count`` / ``min_results`` / ``query`` / any ``error``, and the
        probe's own ``served_by`` / ``provider_fallback`` provenance.
    """
    if search_service is None or not hasattr(search_service, "search"):
        return None

    try:
        result = await asyncio.wait_for(
            # record_recent=False: the probe is a synthetic query, so it must not
            # feed the recent-traffic health signal — health checking its own
            # question would mask what real callers are seeing (#161).
            search_service.search(_SEARCH_PROBE_QUERY, limit=_SEARCH_PROBE_LIMIT, record_recent=False),
            timeout=_SEARCH_PROBE_TIMEOUT_S,
        )
    except Exception as e:
        return {
            "probed": True,
            "ok": False,
            "result_count": 0,
            "min_results": _SEARCH_PROBE_MIN_RESULTS,
            "query": _SEARCH_PROBE_QUERY,
            "error": str(e).strip() or type(e).__name__,
        }

    count = len(result.data)
    return {
        "probed": True,
        "ok": bool(result.success) and count >= _SEARCH_PROBE_MIN_RESULTS,
        "result_count": count,
        "min_results": _SEARCH_PROBE_MIN_RESULTS,
        "query": _SEARCH_PROBE_QUERY,
        "error": result.error,
        # This probe's OWN provenance, captured on its result — race-free, unlike
        # reading the shared chain.last_provider back afterwards (#161).
        "served_by": result.provider,
        "provider_fallback": bool(result.provider_fallback),
    }


def _get_browser_config(browser_manager: Any | None = None) -> dict[str, Any]:
    """Get browser configuration, plus live engine liveness when there is one.

    Configuration alone cannot tell a caller whether the shared engine is still
    running. A long-lived server hands the same browser to every request, so a
    crashed engine is an outage for all of them; ``alive`` makes that visible
    instead of leaving it to be inferred from failing scrapes (#160).

    Args:
        browser_manager: The shared BrowserManager, or None when the server has
            no long-lived engine (nothing to report liveness for).

    Returns:
        Browser settings, plus ``alive``/``relaunches`` when a shared engine exists.
    """
    config: dict[str, Any] = {
        "headless": settings.headless,
        "timeout_ms": settings.timeout,
        "stealth": settings.stealth,
        "proxy_configured": settings.proxy is not None,
        "locale": settings.locale,
        "timezone": settings.timezone,
    }
    if browser_manager is not None:
        config["engine"] = browser_manager.engine
        # Lazy start (#143): a server-side engine does not launch until the first
        # tool needs it, so a never-launched manager is legitimately not alive —
        # not a #160 outage. `launched` separates "not started yet" (fine) from
        # "started then died" (the real outage the verdict must catch). Stubs and
        # older managers without the property default to launched=True, preserving
        # the dead-engine signal.
        config["launched"] = getattr(browser_manager, "has_launched", True)
        config["alive"] = browser_manager.is_alive
        # Relaunches are cumulative for the process: a climbing count is a real
        # signal that something keeps killing the engine.
        config["relaunches"] = browser_manager.relaunches
    return config


def _get_version_info() -> dict[str, str]:
    """Get version information."""
    import supacrawl

    return {
        "supacrawl_lib": supacrawl.__version__,
        "mcp_server": SERVICE_VERSION,  # Now tracks supacrawl lib version
    }


async def supacrawl_health(
    api_client: SupacrawlServices,
    verify_search: Annotated[
        bool,
        Field(
            description='When True (default), run one real, minimal search through the effective provider chain so a search-path regression (provider configured but silently returning nothing — #156) is caught here instead of reading "healthy". Set False for a config-only check with no live network call.'
        ),
    ] = True,
) -> dict:
    """Get Supacrawl server health status, search provider state, and credit levels.

    Use this to verify connectivity, check which search provider is active,
    and detect low-credit conditions before they cause search failures.

    Args:
        api_client: Injected SupacrawlServices instance.
        verify_search: When True (default), run one real, minimal search
            through the effective provider chain so a search-path regression
            (provider configured but silently returning nothing — #156) is
            caught here instead of reading "healthy". Set False for a
            config-only check with no live network call.

    Returns:
        Dictionary containing:
        - status: "healthy" | "degraded"
        - components.search: active provider, configured providers, brave_api_key_configured,
          and — when provider health data is available — per-provider remaining_credits
          and last_error. A "warning" key is added when credits are low, DuckDuckGo
          fallback is in use, or the live search probe found no results. A
          "live_probe" key is present only when the probe actually ran (verify_search=True
          and a search service is available), reporting what it found.
        - components.browser: engine, headless, stealth, timeout settings, plus
          live `launched` / `alive` / `relaunches` for the shared engine. The
          engine starts lazily on the first scrape, so `launched: false` with
          `alive: false` means "not started yet" and is NOT degraded. Once
          launched, `alive: false` means the engine crashed; it self-heals on the
          next scrape and drives the top-level status to "degraded" until it does.
        - components.llm: configured provider and model (for json/summary formats)
        - components.cache: path, entry count, size
        - components.extraction: whether readability_lxml and rank_bm25 are installed;
          `query_filter: false` means a scrape `query` is not applied on this server
        - version: supacrawl library and MCP server versions
    """
    try:
        service_status = api_client.get_service_status()
        all_healthy = all(service_status.values())

        browser_config = _get_browser_config(api_client.browser_manager)
        # A dead shared engine is a real outage for every consumer of this server,
        # so it must reach the top-line verdict rather than sit one level down.
        # But only once it has actually launched: with lazy start (#143) a fresh
        # engine is not-yet-launched, not crashed, and must not read as degraded.
        if browser_config.get("alive") is False and browser_config.get("launched", True):
            all_healthy = False
            browser_config["warning"] = (
                "The shared browser engine is not connected. It is relaunched automatically on the next "
                "scrape; if scrapes keep failing with an 'infrastructure' verdict, restart this server."
            )

        search_config = _get_search_config(api_client.search_service)
        # A degraded search component must reach the top-line verdict. Burying it
        # one level down is what let a server serving from an unconfigured
        # provider read "healthy" for an unknown period (#158).
        if search_config.get("status") == "degraded":
            all_healthy = False
        if verify_search:
            probe = await _run_search_health_probe(api_client.search_service)
            if probe is not None:
                search_config["live_probe"] = probe
                if not probe["ok"]:
                    all_healthy = False
                    search_config["status"] = "degraded"
                    if probe.get("error"):
                        probe_note = f"Live search probe failed: {probe['error']}"
                    else:
                        probe_note = (
                            f"Live search probe returned {probe['result_count']} result(s) for "
                            f"{probe['query']!r}, below the healthy floor of {probe['min_results']} — "
                            "the search backend is configured but not returning usable results."
                        )
                    existing_warning = search_config.get("warning")
                    search_config["warning"] = (
                        f"{existing_warning} {probe_note}".strip() if existing_warning else probe_note
                    )
                # If the probe itself was answered by a fallback — the configured
                # backend is down and DuckDuckGo picked it up — health must read
                # degraded even though the probe returned results, or we have only
                # moved the "healthy while the backend is down" lie one layer
                # along (#161). Read the probe's OWN captured provenance, not the
                # shared chain.last_provider, which a concurrent request may have
                # overwritten since the probe ran.
                if probe.get("provider_fallback") and not search_config.get("provider_fallback_active"):
                    all_healthy = False
                    search_config["provider_fallback_active"] = True
                    search_config["status"] = "degraded"
                    fb_note = (
                        "Search is being served by the DuckDuckGo fallback because the configured backend "
                        "is failing — results are arriving via a provider you did not configure."
                    )
                    existing_warning = search_config.get("warning")
                    search_config["warning"] = f"{existing_warning} {fb_note}".strip() if existing_warning else fb_note

        # Masked unconditionally, with no reveal_secrets thread: every field
        # here is deliberately a boolean, a count or a name
        # (proxy_configured, brave_api_key_configured), so there is nothing an
        # operator would legitimately want revealed -- and the components
        # dicts are assembled from settings, which is exactly where a future
        # field named for a credential would arrive. mcp-servers rule 70
        # §'Allow-list and helper'.
        return mask_secrets(
            {
                "status": "healthy" if all_healthy else "degraded",
                "services": service_status,
                "components": {
                    "browser": browser_config,
                    "search": search_config,
                    "llm": _get_llm_config(),
                    "cache": _get_cache_info(),
                    "extraction": available_extras(),
                },
                "features": {
                    "captcha_solving": settings.solve_captcha,
                    "stealth_mode": settings.stealth,
                },
                "version": _get_version_info(),
            }
        )
    except Exception as e:
        return mask_secrets(
            {
                "status": "unhealthy",
                "error": str(e),
                "services": {},
                "version": _get_version_info(),
            }
        )
