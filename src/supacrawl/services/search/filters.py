"""Helpers for mapping provider-agnostic :class:`SearchFilters` onto provider APIs.

Each provider expresses recency and domain filters differently — some have native
parameters, others only honour ``site:`` query operators. These helpers cover the
two cross-provider techniques: rewriting the query with ``site:`` operators, and
converting a relative ``time_range`` to an absolute start date for providers that
only accept date bounds.
"""

from datetime import UTC, datetime, timedelta

# Relative time_range -> number of days back.
TIME_RANGE_DAYS: dict[str, int] = {"day": 1, "week": 7, "month": 30, "year": 365}


def domain_operator_query(
    query: str,
    include_domains: list[str] | None,
    exclude_domains: list[str] | None,
) -> str:
    """Rewrite a query with Google-style ``site:`` operators for domain filtering.

    For providers (Brave, Serper, SerpAPI, DuckDuckGo) that have no native
    domain-filter parameter but honour ``site:``/``-site:`` operators.

    Args:
        query: The original search query.
        include_domains: Domains to restrict results to (OR-combined).
        exclude_domains: Domains to exclude.

    Returns:
        The query with ``site:`` operators appended, unchanged when no domain
        filters are given.
    """
    parts = [query.strip()]
    if include_domains:
        ors = " OR ".join(f"site:{d}" for d in include_domains)
        parts.append(f"({ors})" if len(include_domains) > 1 else ors)
    for domain in exclude_domains or []:
        parts.append(f"-site:{domain}")
    return " ".join(part for part in parts if part)


def time_range_to_start_date(time_range: str | None, *, now: datetime | None = None) -> str | None:
    """Convert a relative ``time_range`` to an absolute ISO start date (UTC).

    For providers (Exa) that accept absolute published-date bounds but not a
    relative range.

    Args:
        time_range: One of ``day``/``week``/``month``/``year``, or None.
        now: Reference time (defaults to the current UTC time); injectable for tests.

    Returns:
        An ISO ``YYYY-MM-DD`` start date, or None when ``time_range`` is unset.
    """
    days = TIME_RANGE_DAYS.get(time_range or "")
    if not days:
        return None
    reference = now or datetime.now(UTC)
    return (reference - timedelta(days=days)).date().isoformat()


def iso_to_us_date(iso_date: str) -> str | None:
    """Convert an ISO ``YYYY-MM-DD`` date to Google's ``M/D/YYYY`` (no zero-pad).

    Used by the Serper/SerpAPI ``tbs=cdr:`` custom-date-range syntax.

    Returns:
        The reformatted date, or None when the input is not a valid ISO date.
    """
    try:
        d = datetime.strptime(iso_date, "%Y-%m-%d")
    except ValueError:
        return None
    return f"{d.month}/{d.day}/{d.year}"


def iso_to_exa_datetime(iso_date: str) -> str | None:
    """Convert an ISO ``YYYY-MM-DD`` date to Exa's required full ISO-8601 with Z.

    Exa rejects bare dates; ``startPublishedDate``/``endPublishedDate`` need a
    full timestamp such as ``2026-01-01T00:00:00.000Z``.

    Returns:
        The full-timestamp form, or None when the input is not a valid ISO date.
    """
    try:
        datetime.strptime(iso_date, "%Y-%m-%d")
    except ValueError:
        return None
    return f"{iso_date}T00:00:00.000Z"
