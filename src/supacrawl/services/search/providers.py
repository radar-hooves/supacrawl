"""
Search provider protocol and provider chain with automatic fallback.

Defines the interface all search providers must implement and the chain
that orchestrates fallback between providers on failure.
"""

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

from supacrawl.models import SearchFilters, SearchResultItem

LOGGER = logging.getLogger(__name__)

# Seconds between repeated auth/billing alert logs for the same provider.
ALERT_DEBOUNCE_SECONDS: float = 300.0


# ---------------------------------------------------------------------------
# Provider status tracking
# ---------------------------------------------------------------------------


class ProviderStatus(str, Enum):
    """Runtime health status of a search provider."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"  # Intermittent errors, still trying
    UNAVAILABLE = "unavailable"  # Quota exhausted or hard failure


# Brave emits a warning when remaining credits drop below this threshold.
# The Brave free tier provides 2 000 queries/month; 100 is a reasonable heads-up.
LOW_CREDIT_THRESHOLD: int = 100

# Per-provider plan/renewal pointer for low-credit warnings. Only providers that
# expose a remaining-credit count can ever warn, so this stays small; the lookup
# keeps the message accurate per provider instead of hard-coding one vendor's URL.
PROVIDER_RENEWAL_URLS: dict[str, str] = {
    "brave": "https://brave.com/search/api/",
}


def renewal_hint(provider_name: str) -> str:
    """Return an actionable renewal pointer for a provider's low-credit warning."""
    url = PROVIDER_RENEWAL_URLS.get(provider_name)
    return f"Top up at {url}" if url else f"Renew the {provider_name} API plan"


@dataclass
class ProviderHealth:
    """In-memory health state for a single provider within a session."""

    # After this many consecutive failures, mark unavailable
    UNAVAILABLE_THRESHOLD: int = 3
    # Re-check an unavailable provider after this many seconds
    COOLDOWN_SECONDS: float = 300.0  # 5 minutes
    # An unbroken run of this many empty-but-successful answers marks the provider
    # degraded. An empty answer is NOT a transport failure, so it never trips the
    # UNAVAILABLE circuit breaker (a provider that legitimately has no match for a
    # run of queries must not be dropped) — but a sustained empty run is a health
    # signal, not a clean record: it is how a CAPTCHA-walled backend answering
    # 200-with-nothing surfaces instead of banking as healthy.
    EMPTY_DEGRADED_THRESHOLD: int = 3

    status: ProviderStatus = ProviderStatus.HEALTHY
    consecutive_failures: int = 0
    # Consecutive successful-but-empty answers. Distinct from consecutive_failures
    # because an empty answer means the provider IS reachable and responding — it
    # just returned nothing. A result with matches resets this to zero.
    consecutive_empty: int = 0
    last_failure_time: float = 0.0
    last_error: str | None = None
    requests_made: int = 0
    last_alert_time: float | None = None  # None = never alerted (monotonic clock makes 0.0 unsafe on fresh hosts)
    # Per-call quota reported by the provider in response headers (Brave only).
    # None means the provider does not expose quota via headers (Serper, Tavily,
    # SerpAPI, Exa) — a missing value is NOT the same as "plenty left".
    remaining_credits: int | None = None

    def record_success(self) -> None:
        """Record a successful request that returned results."""
        self.consecutive_failures = 0
        self.consecutive_empty = 0
        self.status = ProviderStatus.HEALTHY
        self.last_error = None
        self.requests_made += 1

    def record_empty_success(self) -> None:
        """Record a request that succeeded at the transport level but returned no results.

        An empty answer is banked distinctly from a result-bearing one: it clears
        the consecutive-FAILURE count (the provider did answer) but advances a
        consecutive-EMPTY count, so a run of them reads as degraded rather than as
        a clean success. It deliberately does NOT drive the UNAVAILABLE circuit
        breaker — an empty result may be a genuine no-match, and dropping the
        provider for that would be wrong. A single empty is not yet a verdict; a
        run past ``EMPTY_DEGRADED_THRESHOLD`` is. The climbing ``consecutive_empty``
        is visible via ``to_dict()`` throughout.
        """
        self.consecutive_failures = 0
        self.consecutive_empty += 1
        self.last_error = None
        self.requests_made += 1
        if self.consecutive_empty >= self.EMPTY_DEGRADED_THRESHOLD:
            self.status = ProviderStatus.DEGRADED
        elif self.status == ProviderStatus.UNAVAILABLE:
            # It just answered, so the transport has recovered and the failure
            # count is back to zero — it is no longer "unavailable". A bare empty
            # is not a clean bill of health either, so it lands on DEGRADED rather
            # than HEALTHY, keeping the status consistent with consecutive_failures=0.
            self.status = ProviderStatus.DEGRADED

    def record_failure(self, error: str) -> None:
        """Record a failed request and update status."""
        self.consecutive_failures += 1
        self.last_failure_time = time.monotonic()
        self.last_error = error
        self.requests_made += 1

        if self.consecutive_failures >= self.UNAVAILABLE_THRESHOLD:
            self.status = ProviderStatus.UNAVAILABLE
        else:
            self.status = ProviderStatus.DEGRADED

    @property
    def should_skip(self) -> bool:
        """Whether this provider should be skipped (unavailable and not cooled down)."""
        if self.status != ProviderStatus.UNAVAILABLE:
            return False
        elapsed = time.monotonic() - self.last_failure_time
        return elapsed < self.COOLDOWN_SECONDS

    def should_alert(self) -> bool:
        """Whether an auth/billing alert should fire now (debounced per ALERT_DEBOUNCE_SECONDS)."""
        if self.last_alert_time is None:
            return True  # never alerted — fire on the first auth/billing failure
        return time.monotonic() - self.last_alert_time >= ALERT_DEBOUNCE_SECONDS

    def record_alert(self) -> None:
        """Record that an alert was emitted, resetting the debounce window."""
        self.last_alert_time = time.monotonic()

    def record_quota(self, remaining: int) -> None:
        """Cache the remaining-credit count reported by a provider response header."""
        self.remaining_credits = remaining

    def to_dict(self) -> dict:
        """Serialise to dict for health endpoint."""
        result: dict = {
            "status": self.status.value,
            "requests_made": self.requests_made,
            "consecutive_failures": self.consecutive_failures,
            "consecutive_empty": self.consecutive_empty,
            "last_error": self.last_error,
        }
        if self.remaining_credits is not None:
            result["remaining_credits"] = self.remaining_credits
        return result


# ---------------------------------------------------------------------------
# Provider protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class SearchProvider(Protocol):
    """Interface that all search providers must implement."""

    @property
    def name(self) -> str:
        """Provider identifier (e.g. 'brave', 'tavily')."""
        ...

    def is_available(self) -> bool:
        """Whether this provider has required credentials/config to operate."""
        ...

    async def search_web(
        self, query: str, limit: int, correlation_id: str, filters: SearchFilters | None = None
    ) -> list[SearchResultItem]:
        """Search for web pages."""
        ...

    async def search_images(
        self, query: str, limit: int, correlation_id: str, filters: SearchFilters | None = None
    ) -> list[SearchResultItem]:
        """Search for images. May raise NotImplementedError if unsupported."""
        ...

    async def search_news(
        self, query: str, limit: int, correlation_id: str, filters: SearchFilters | None = None
    ) -> list[SearchResultItem]:
        """Search for news articles. May raise NotImplementedError if unsupported."""
        ...

    async def close(self) -> None:
        """Release resources (HTTP clients, etc.)."""
        ...


# ---------------------------------------------------------------------------
# Fallback triggers
# ---------------------------------------------------------------------------

# HTTP status codes that always indicate provider exhaustion (should fallback)
FALLBACK_HTTP_CODES = frozenset(
    {
        401,  # Unauthorised — expired or invalid key
        402,  # Payment Required
        429,  # Too Many Requests / rate limited / quota exhausted
    }
)

# 400 is ambiguous (could be a malformed query or an auth/credit problem).
# Only fallback on 400 if the response body suggests an auth/billing issue.
# 403 is also ambiguous (could be invalid key or an unrelated permissions error).
# Fallback on 403 when the body suggests auth, quota, or billing problems.
_AUTH_BILLING_BODY_PATTERNS = (
    "api key",
    "invalid key",
    "expired",
    "credit",
    "quota",
    "billing",
    "subscription",
    "unauthorized",
    "payment",
    "rate limit",
    "exceeded",
    "too many",
)

# Strings in ProviderError messages that indicate provider exhaustion.
# These are matched against the str() of a ProviderError, so they must be
# phrases that appear in messages raised by provider code — not in HTTP bodies.
# Deliberately excludes "api key" / "invalid key": a ProviderError("… key not
# configured") means the provider was never usable and should NOT fall back
# (there is no point trying it again).
FALLBACK_ERROR_PATTERNS = (
    "quota",
    "rate limit",
    "too many requests",
    "captcha",
    "bot detection",
    "payment required",
    "subscription",
    # SearXNG raising because every upstream engine was down: a genuine outage
    # of the configured backend, so the chain should try the next provider
    # rather than surface a bare empty set (#161).
    "unresponsive",
)


def is_auth_billing_error(error: BaseException) -> bool:
    """Return True when the error almost certainly means an expired or invalid credential.

    Used to decide whether to emit a loud LOGGER.warning prompting the operator
    to renew their API key.  Separate from is_fallback_error so the two
    decisions (should we fall back? should we alert?) can diverge in future.
    """
    import httpx

    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        if status in (401, 402):
            return True
        if status in (400, 403):
            body = error.response.text.lower()
            return any(p in body for p in _AUTH_BILLING_BODY_PATTERNS)

    return False


def is_fallback_error(error: BaseException) -> bool:
    """Determine whether an error should trigger fallback to next provider.

    Returns True for quota/rate-limit/CAPTCHA/auth/billing errors.
    Returns False for malformed queries (plain 400), network-wide outages, etc.
    """
    import httpx

    from supacrawl.exceptions import ProviderError

    # Check httpx HTTP status errors
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        if status in FALLBACK_HTTP_CODES:
            return True
        # 400 and 403 are ambiguous — only fallback when the response body
        # indicates an auth, quota, or billing problem.
        if status in (400, 403):
            body = error.response.text.lower()
            if any(p in body for p in _AUTH_BILLING_BODY_PATTERNS):
                return True

    # Check ProviderError messages (e.g. CAPTCHA detection)
    if isinstance(error, ProviderError):
        msg = str(error).lower()
        return any(pattern in msg for pattern in FALLBACK_ERROR_PATTERNS)

    # Connection timeouts should fallback (provider may be down)
    if isinstance(error, (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.PoolTimeout)):
        return True

    # Generic timeout
    if isinstance(error, TimeoutError):
        return True

    return False


# ---------------------------------------------------------------------------
# Provider chain
# ---------------------------------------------------------------------------


@dataclass
class ProviderChain:
    """Ordered list of search providers with automatic fallback.

    Tries each provider in order. On fallback-eligible errors, moves to
    the next provider. Tracks per-provider health in memory.
    """

    providers: list[SearchProvider] = field(default_factory=list)
    # Provider names the operator actually asked for, before any implicit
    # fallback was appended. Empty means "nobody recorded an intent", in which
    # case no fallback claim can be made either way (#158).
    configured_names: list[str] = field(default_factory=list)
    # Name of the provider that served the most recent successful search, so a
    # caller can tell which provider answered rather than inferring it (#158).
    last_provider: str | None = None
    _health: dict[str, ProviderHealth] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for p in self.providers:
            if p.name not in self._health:
                self._health[p.name] = ProviderHealth()

    def add(self, provider: SearchProvider) -> None:
        """Add a provider to the chain."""
        self.providers.append(provider)
        if provider.name not in self._health:
            self._health[provider.name] = ProviderHealth()

    @property
    def active_providers(self) -> list[SearchProvider]:
        """Providers that are available and not currently skipped."""
        return [p for p in self.providers if p.is_available() and not self._health[p.name].should_skip]

    @property
    def effective_provider(self) -> str | None:
        """Name of the provider that would serve the next search, or None when nothing can."""
        active = self.active_providers
        if active:
            return active[0].name
        # A cooled-down-but-available provider would still be tried as a last
        # resort by search(), so it is the honest answer here too.
        usable = [p for p in self.providers if p.is_available()]
        return usable[0].name if usable else None

    @property
    def unconfigured_fallback_active(self) -> bool:
        """Whether the chain would serve from a provider the operator never configured.

        This is the #158 signal: every individual field can read fine while the
        server answers from a provider nobody asked for. Reporting it as one
        boolean means a reader never has to diff ``configured_names`` against
        ``effective_provider`` to notice.
        """
        if not self.configured_names:
            return False
        effective = self.effective_provider
        return effective is not None and effective not in self.configured_names

    @property
    def fallback_serving(self) -> bool:
        """Whether the MOST RECENT search was served by an unconfigured provider.

        ``unconfigured_fallback_active`` reasons about who would serve NEXT, which
        stays False while a configured provider is still first in line but failing
        every request (the window before it circuit-breaks). This reads who
        actually served LAST, so a health surface can tell an operator that every
        real query is currently being answered by the DuckDuckGo fallback because
        the configured backend is down (#161).
        """
        if not self.configured_names or self.last_provider is None:
            return False
        return self.last_provider not in self.configured_names

    def get_health(self) -> dict[str, dict]:
        """Get health status for all providers (for health endpoint).

        For providers that expose remaining quota in response headers (currently
        Brave only), ``remaining_credits`` is included when a value has been
        observed.  A missing field means the provider does not expose quota via
        headers — not "plenty left".
        """
        result = {}
        for p in self.providers:
            health = self._health[p.name].to_dict()
            health["available"] = p.is_available()
            # Pull per-call quota from the provider if it tracks it (duck-typed).
            # Only BraveProvider sets this attribute; header-less providers do not,
            # so absence correctly signals "unknown" rather than "zero".
            # The provider's live counter is the freshest source of truth; surface
            # it when present (header-less providers never set it, so absence
            # correctly reads as "unknown", not "zero").
            provider_remaining = getattr(p, "remaining_credits", None)
            if provider_remaining is not None:
                health["remaining_credits"] = provider_remaining
            result[p.name] = health
        return result

    async def search(
        self,
        source: str,
        query: str,
        limit: int,
        correlation_id: str,
        filters: SearchFilters | None = None,
    ) -> list[SearchResultItem]:
        """Search using the provider chain with fallback.

        Args:
            source: Source type ('web', 'images', 'news').
            query: Search query.
            limit: Max results.
            correlation_id: Correlation ID for logging.
            filters: Optional recency/topic/domain filters mapped per provider.

        Returns:
            Search results from the first successful provider.

        Raises:
            The last error if all providers fail.
        """
        active = self.active_providers
        if not active:
            # Try all providers including cooled-down ones as last resort
            active = [p for p in self.providers if p.is_available()]

        if not active:
            # Build an actionable message that names the configured providers
            # and their missing credentials.  The registry mapping is imported
            # here to avoid a circular dependency at module level.
            from supacrawl.services.search.registry import _PROVIDER_API_KEY_ENVS

            configured = [p.name for p in self.providers]
            if configured:
                details = "; ".join(
                    f"{name} needs {_PROVIDER_API_KEY_ENVS.get(name, 'configuration')} (not set)" for name in configured
                )
                msg = (
                    f"No usable search provider. Configured: {configured}. "
                    f"{details}. "
                    "Set the required environment variable(s) or change SUPACRAWL_SEARCH_PROVIDERS."
                )
            else:
                msg = (
                    "No search providers configured. "
                    "Set SUPACRAWL_SEARCH_PROVIDERS (e.g. 'brave,tavily') and supply the required API keys."
                )
            raise RuntimeError(msg)

        last_error: BaseException | None = None
        # The first provider to answer with an *empty* result, banked so the chain
        # can keep trying providers that might have matches while still returning a
        # genuine no-match (success, no data) if none do. An empty answer is not a
        # failure — but it is also not a reason to stop looking: stopping on the
        # first empty is exactly what let a CAPTCHA-walled SearXNG read "healthy"
        # while a second configured provider sat inert (extends #132/#156/#158/#161).
        # This only ever iterates providers already IN the chain, so it never
        # reaches a public engine the operator did not configure or opt into — the
        # registry decides who is in the chain, honouring the #156/#158 stance.
        empty_results: list[SearchResultItem] | None = None
        empty_provider: str | None = None
        # The last provider NOT in configured_names that this search actually
        # reached over the wire (it answered, or it raised AFTER making its request
        # — a CAPTCHA/HTTP/timeout error). A NotImplementedError, raised before any
        # request, is deliberately NOT a consultation. Under
        # SUPACRAWL_SEARCH_PUBLIC_FALLBACK the empty-fallthrough can legitimately
        # reach DuckDuckGo; if the whole chain comes back empty the response must
        # still say a query left in-house (provider_fallback), not attribute an
        # all-empty result to the configured backend and hide that the public
        # engine was queried (#158 audit trail).
        consulted_unconfigured: str | None = None
        # The most recent failure from a provider the operator actually configured.
        # Kept separate from ``last_error`` so a terminal failure always reports the
        # configured backend's own diagnostic (e.g. SearXNG naming its dead upstream
        # engines) rather than an unconfigured public fallback's less informative
        # one — a caller must never read "DuckDuckGo CAPTCHA'd" as the reason a
        # configured, self-hosted backend failed (#161 extended).
        last_configured_error: BaseException | None = None

        for provider in active:
            health = self._health[provider.name]
            is_unconfigured = bool(self.configured_names) and provider.name not in self.configured_names
            try:
                LOGGER.debug(f"Trying provider {provider.name} for {source} search [correlation_id={correlation_id}]")

                if source == "web":
                    results = await provider.search_web(query, limit, correlation_id, filters)
                elif source == "images":
                    results = await provider.search_images(query, limit, correlation_id, filters)
                elif source == "news":
                    results = await provider.search_news(query, limit, correlation_id, filters)
                else:
                    LOGGER.warning(f"Unknown source type: {source} [correlation_id={correlation_id}]")
                    return []

            except NotImplementedError:
                # Provider doesn't support this source type — skip silently
                LOGGER.debug(f"Provider {provider.name} does not support {source} search, skipping")
                continue

            except Exception as e:
                # Any non-NotImplementedError exception is raised BY the provider's
                # request (CAPTCHA, HTTP error, timeout) — the wire call was made,
                # so an unconfigured provider reaching here did receive the query.
                # (NotImplementedError, handled above, is raised before any call and
                # so must NOT count as a consultation.)
                if is_unconfigured:
                    consulted_unconfigured = provider.name
                else:
                    last_configured_error = e
                last_error = e
                error_msg = str(e)
                health.record_failure(error_msg)

                if is_auth_billing_error(e) and health.should_alert():
                    health.record_alert()
                    LOGGER.warning(
                        f"ACTION REQUIRED — {provider.name} search provider is returning an auth/billing error "
                        f"({error_msg}). Likely cause: expired API key, exhausted credits, or cancelled "
                        f"subscription. Search will fall back to the next configured provider if one is "
                        f"available, but you should renew the credential for {provider.name} "
                        f"[correlation_id={correlation_id}]"
                    )

                if is_fallback_error(e):
                    LOGGER.warning(
                        f"Provider {provider.name} failed ({error_msg}), "
                        f"falling back to next provider "
                        f"[correlation_id={correlation_id}]"
                    )
                    continue
                # Non-fallback error (e.g. malformed query) — don't try other
                # providers. But if an earlier provider already answered (even
                # with an empty set), that is a valid result in hand: a later
                # provider's hard error must not discard it.
                if empty_results is not None:
                    LOGGER.error(
                        f"Provider {provider.name} failed with non-fallback error: {error_msg}; "
                        f"returning the earlier empty result from {empty_provider!r} "
                        f"[correlation_id={correlation_id}]"
                    )
                    break
                LOGGER.error(
                    f"Provider {provider.name} failed with non-fallback error: {error_msg} "
                    f"[correlation_id={correlation_id}]"
                )
                raise

            else:
                # Provider answered (returned, empty or not) — the wire call was made.
                if is_unconfigured:
                    consulted_unconfigured = provider.name

                # A result WITH matches ends the chain here.
                if results:
                    health.record_success()
                    self.last_provider = provider.name
                    if self.configured_names and provider.name not in self.configured_names:
                        LOGGER.warning(
                            f"Search served by {provider.name!r}, which is NOT a configured provider "
                            f"(configured: {self.configured_names}). Queries are leaving via a provider "
                            f"the operator did not select [correlation_id={correlation_id}]"
                        )

                    # Sync remaining-quota from the provider into health so it is
                    # visible via get_health() / supacrawl_health.  Only providers
                    # that expose quota headers (currently Brave) set this attribute.
                    provider_remaining = getattr(provider, "remaining_credits", None)
                    if provider_remaining is not None:
                        health.record_quota(provider_remaining)
                        if provider_remaining < LOW_CREDIT_THRESHOLD:
                            LOGGER.warning(
                                f"LOW CREDIT WARNING — {provider.name} has {provider_remaining} API credits "
                                f"remaining (threshold: {LOW_CREDIT_THRESHOLD}). "
                                f"{renewal_hint(provider.name)} to avoid search outages "
                                f"[correlation_id={correlation_id}]"
                            )

                    return results

                # An empty answer: mark the empty run on this provider's health,
                # bank the first one for provenance, and try the next provider.
                # Health now separates "answered with results" from "answered with
                # nothing", so a sustained empty run is visible instead of being
                # banked as a clean success.
                health.record_empty_success()
                if empty_results is None:
                    # Bank a fresh empty list, not ``results`` itself: an empty
                    # answer carries no items, and coercing here means a provider
                    # that (against the protocol) returned None can never leak a
                    # None into the returned value or defeat the not-None sentinel.
                    empty_results = []
                    empty_provider = provider.name
                LOGGER.debug(
                    f"Provider {provider.name} returned no results for {source} search, "
                    f"trying next provider [correlation_id={correlation_id}]"
                )

        # Every provider that answered did so with an empty set: a genuine no-match
        # across the whole configured chain. Return it as success-with-no-data
        # (never an error — a query with nothing to find is a real outcome). Attribute
        # it to the unconfigured provider if one was consulted (so the response and
        # the health surface both show a query reached a public engine), otherwise to
        # the first configured provider that answered.
        if empty_results is not None:
            self.last_provider = consulted_unconfigured or empty_provider
            return empty_results

        # All providers failed with fallback-eligible errors. A configured
        # backend's own failure outranks an unconfigured fallback's: the fallback
        # was only ever consulted because the configured backend failed first, so
        # raising ITS error (SearXNG naming its dead engines) rather than the
        # fallback's (DuckDuckGo's opaque CAPTCHA) is what lets the caller tell a
        # configured-backend outage from "the public last resort also failed".
        if last_configured_error is not None:
            raise last_configured_error
        if last_error is None:
            raise RuntimeError("All providers exhausted with no error recorded")
        raise last_error

    async def close(self) -> None:
        """Close all providers."""
        for p in self.providers:
            try:
                await p.close()
            except Exception as e:
                LOGGER.warning(f"Error closing provider {p.name}: {e}")
