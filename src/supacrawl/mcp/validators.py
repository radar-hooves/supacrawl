"""Validation utilities for Supacrawl MCP server.

Provides input validation with helpful error messages for all tool parameters.
Wraps mcp_common validators with Supacrawl-specific exception types.
"""

import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from mcp_common.exceptions import MCPValidationError
from mcp_common.validators import validate_positive_int as _validate_positive_int

from supacrawl.exceptions import ValidationError as _SupacrawlLibValidationError
from supacrawl.mcp.exceptions import SupacrawlValidationError
from supacrawl.services.url_guard import assert_safe_url

# Words that indicate a time-sensitive search where current year matters
TIME_SENSITIVE_KEYWORDS = {
    # Recency indicators
    "latest",
    "recent",
    "newest",
    "current",
    "new",
    "now",
    "today",
    # Comparison/alternatives
    "best",
    "top",
    "alternative",
    "alternatives",
    "vs",
    "versus",
    "comparison",
    "compared",
    # Software/tech specific
    "download",
    "install",
    "release",
    "version",
    "update",
    "changelog",
    # Rankings/reviews
    "review",
    "reviews",
    "ranking",
    "rankings",
    "guide",
    "tutorial",
}

# Regex to detect years (2020-2099)
YEAR_PATTERN = re.compile(r"\b20[2-9]\d\b")


def validate_url(
    value: Any,
    field_name: str = "url",
    allow_none: bool = False,
) -> str | None:
    """
    Validate that a value is a valid URL.

    Args:
        value: The value to validate
        field_name: Name of the field for error messages
        allow_none: If True, allow None values

    Returns:
        Validated URL as string or None

    Raises:
        SupacrawlValidationError: If value is not a valid URL
    """
    if value is None:
        if allow_none:
            return None
        raise SupacrawlValidationError(
            f"{field_name} is required",
            field=field_name,
            value=value,
        )

    if not isinstance(value, str):
        raise SupacrawlValidationError(
            f"{field_name} must be a string, got {type(value).__name__}",
            field=field_name,
            value=value,
        )

    url = value.strip()
    if not url:
        if allow_none:
            return None
        raise SupacrawlValidationError(
            f"{field_name} cannot be empty",
            field=field_name,
            value=value,
        )

    # Check URL scheme
    if not url.startswith(("http://", "https://")):
        raise SupacrawlValidationError(
            f"{field_name} must start with http:// or https://, got '{url[:20]}...'",
            field=field_name,
            value=value,
        )

    # Parse and validate URL structure
    try:
        parsed = urlparse(url)
        if not parsed.netloc:
            raise SupacrawlValidationError(
                f"{field_name} must have a valid host, got '{url}'",
                field=field_name,
                value=value,
            )
    except Exception as e:
        raise SupacrawlValidationError(
            f"{field_name} is not a valid URL: {e}",
            field=field_name,
            value=value,
        ) from e

    # Cheap, offline SSRF check (#152): scheme already verified above, so this
    # only adds the blocked-IP-literal check. It cannot catch a hostname that
    # *resolves* to a blocked address — the connection-layer guard
    # (supacrawl.services.url_guard.resolve_and_pin / guarded_request) is what
    # closes that window at the point each URL is actually fetched.
    try:
        assert_safe_url(url)
    except _SupacrawlLibValidationError as e:
        raise SupacrawlValidationError(e.message, field=field_name, value=value) from e

    return url


def validate_query(
    value: Any,
    field_name: str = "query",
    min_length: int = 1,
    max_length: int = 1000,
) -> str:
    """
    Validate a search query string.

    Args:
        value: The value to validate
        field_name: Name of the field for error messages
        min_length: Minimum query length (default: 1)
        max_length: Maximum query length (default: 1000)

    Returns:
        Validated query as string

    Raises:
        SupacrawlValidationError: If value is not a valid query string
    """
    if value is None:
        raise SupacrawlValidationError(
            f"{field_name} is required. Provide a search query string.",
            field=field_name,
            value=value,
        )

    if not isinstance(value, str):
        raise SupacrawlValidationError(
            f"{field_name} must be a string, got {type(value).__name__}",
            field=field_name,
            value=value,
        )

    query = value.strip()
    if len(query) < min_length:
        raise SupacrawlValidationError(
            f"{field_name} must be at least {min_length} character(s), got {len(query)}",
            field=field_name,
            value=value,
        )

    if len(query) > max_length:
        raise SupacrawlValidationError(
            f"{field_name} must be at most {max_length} characters, got {len(query)}",
            field=field_name,
            value=value,
        )

    return query


def enhance_query_with_current_year(query: str) -> str:
    """
    Enhance a search query with the current year if it appears time-sensitive.

    LLMs often default to their training data cutoff year when generating searches
    (e.g., using 2024 when it's actually 2026). This function:
    1. Replaces stale (past) years with the current year in time-sensitive queries
    2. Appends the current year to time-sensitive queries missing a year

    Args:
        query: The search query string

    Returns:
        Query with current year (replacing stale year or appended) if time-sensitive,
        otherwise returns the original query unchanged.

    Examples:
        "best python frameworks 2024" -> "best python frameworks 2026" (stale year replaced)
        "best python frameworks" -> "best python frameworks 2026" (year appended)
        "python frameworks 2027" -> "python frameworks 2027" (future year preserved)
        "python syntax" -> "python syntax" (unchanged, not time-sensitive)
    """
    current_year = datetime.now(timezone.utc).year

    # Check if query contains time-sensitive keywords
    query_lower = query.lower()
    query_words = set(re.findall(r"\b\w+\b", query_lower))
    is_time_sensitive = bool(query_words & TIME_SENSITIVE_KEYWORDS)

    if not is_time_sensitive:
        return query

    # Check for existing years in the query
    year_match = YEAR_PATTERN.search(query)

    if year_match:
        found_year = int(year_match.group())
        # Replace stale (past) years with current year
        if found_year < current_year:
            return YEAR_PATTERN.sub(str(current_year), query)
        # Future or current year - leave as-is
        return query

    # No year present - append current year
    return f"{query} {current_year}"


def validate_timeout(
    value: Any,
    field_name: str = "timeout",
    min_ms: int = 1000,
    max_ms: int = 300000,
    allow_none: bool = True,
) -> int | None:
    """
    Validate a timeout value in milliseconds.

    Args:
        value: The value to validate
        field_name: Name of the field for error messages
        min_ms: Minimum timeout in ms (default: 1000 = 1 second)
        max_ms: Maximum timeout in ms (default: 300000 = 5 minutes)
        allow_none: If True, allow None values

    Returns:
        Validated timeout as integer or None

    Raises:
        SupacrawlValidationError: If value is out of range
    """
    if value is None:
        if allow_none:
            return None
        raise SupacrawlValidationError(
            f"{field_name} is required",
            field=field_name,
            value=value,
        )

    try:
        timeout = int(value)
    except (ValueError, TypeError) as e:
        raise SupacrawlValidationError(
            f"{field_name} must be an integer, got {type(value).__name__}",
            field=field_name,
            value=value,
        ) from e

    if timeout < min_ms:
        raise SupacrawlValidationError(
            f"{field_name} must be at least {min_ms}ms, got {timeout}ms",
            field=field_name,
            value=value,
        )

    if timeout > max_ms:
        raise SupacrawlValidationError(
            f"{field_name} must be at most {max_ms}ms, got {timeout}ms",
            field=field_name,
            value=value,
        )

    return timeout


def validate_limit(
    value: Any,
    field_name: str = "limit",
    min_value: int = 1,
    max_value: int = 100,
    allow_none: bool = True,
    default: int | None = None,
) -> int | None:
    """
    Validate a limit/count parameter.

    Args:
        value: The value to validate
        field_name: Name of the field for error messages
        min_value: Minimum allowed value (default: 1)
        max_value: Maximum allowed value (default: 100)
        allow_none: If True, allow None values
        default: Default value to return if None

    Returns:
        Validated limit as integer or default value

    Raises:
        SupacrawlValidationError: If value is out of range
    """
    if value is None:
        if default is not None:
            return default
        if allow_none:
            return None
        raise SupacrawlValidationError(
            f"{field_name} is required",
            field=field_name,
            value=value,
        )

    try:
        return _validate_positive_int(value, field_name, allow_none=False, max_value=max_value)
    except MCPValidationError as e:
        raise SupacrawlValidationError(str(e), field=field_name, value=value) from e


def validate_urls(
    value: Any,
    field_name: str = "urls",
    min_count: int = 1,
    max_count: int = 100,
) -> list[str]:
    """
    Validate a list of URLs.

    Accepts every shape the tool schemas advertise (``list[str] | str``): a
    list, a JSON-encoded array string (some MCP clients send lists that way),
    or one bare URL.

    Args:
        value: The value to validate (list of URL strings, a JSON-encoded
            array string, or a single URL string)
        field_name: Name of the field for error messages
        min_count: Minimum number of URLs required (default: 1)
        max_count: Maximum number of URLs allowed (default: 100)

    Returns:
        Validated list of URL strings

    Raises:
        SupacrawlValidationError: If validation fails
    """
    if value is None:
        raise SupacrawlValidationError(
            f"{field_name} is required. Provide a list of URLs.",
            field=field_name,
            value=value,
        )

    if isinstance(value, str):
        stripped = value.strip()
        parsed: Any = None
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
            except ValueError:
                parsed = None
        value = parsed if isinstance(parsed, list) else [stripped]

    if not isinstance(value, list):
        raise SupacrawlValidationError(
            f"{field_name} must be a list, a single URL string, or a JSON-encoded array of URLs, "
            f"got {type(value).__name__}",
            field=field_name,
            value=value,
        )

    if len(value) < min_count:
        raise SupacrawlValidationError(
            f"{field_name} must contain at least {min_count} URL(s), got {len(value)}",
            field=field_name,
            value=value,
        )

    if len(value) > max_count:
        raise SupacrawlValidationError(
            f"{field_name} must contain at most {max_count} URLs, got {len(value)}",
            field=field_name,
            value=value,
        )

    # Validate each URL
    validated_urls: list[str] = []
    for i, url in enumerate(value):
        validated_url = validate_url(url, field_name=f"{field_name}[{i}]")
        assert validated_url is not None  # validate_url raises on None by default
        validated_urls.append(validated_url)

    return validated_urls


def validate_formats(
    value: Any,
    field_name: str = "formats",
    allowed_formats: list[str] | None = None,
    allow_none: bool = True,
) -> list[str] | None:
    """
    Validate output format options.

    Args:
        value: The value to validate (list of format strings)
        field_name: Name of the field for error messages
        allowed_formats: List of valid format values (default: markdown, html)
        allow_none: If True, allow None values

    Returns:
        Validated list of format strings or None

    Raises:
        SupacrawlValidationError: If validation fails
    """
    if allowed_formats is None:
        allowed_formats = ["markdown", "html", "rawHtml", "screenshot", "links"]

    if value is None:
        if allow_none:
            return None
        raise SupacrawlValidationError(
            f"{field_name} is required",
            field=field_name,
            value=value,
        )

    if not isinstance(value, list):
        raise SupacrawlValidationError(
            f"{field_name} must be a list, got {type(value).__name__}",
            field=field_name,
            value=value,
        )

    validated_formats = []
    for fmt in value:
        if fmt not in allowed_formats:
            raise SupacrawlValidationError(
                f"Invalid format '{fmt}' in {field_name}. Allowed formats: {', '.join(allowed_formats)}",
                field=field_name,
                value=value,
            )
        validated_formats.append(fmt)

    return validated_formats


def validate_sources(
    value: Any,
    field_name: str = "sources",
    allow_none: bool = True,
) -> list[str] | None:
    """
    Validate search source types.

    Args:
        value: The value to validate (list of source type strings)
        field_name: Name of the field for error messages
        allow_none: If True, allow None values

    Returns:
        Validated list of source strings or None

    Raises:
        SupacrawlValidationError: If validation fails
    """
    allowed_sources = ["web", "images", "news"]

    if value is None:
        if allow_none:
            return None
        raise SupacrawlValidationError(
            f"{field_name} is required",
            field=field_name,
            value=value,
        )

    if not isinstance(value, list):
        raise SupacrawlValidationError(
            f"{field_name} must be a list, got {type(value).__name__}",
            field=field_name,
            value=value,
        )

    validated_sources = []
    for src in value:
        if src not in allowed_sources:
            raise SupacrawlValidationError(
                f"Invalid source '{src}' in {field_name}. Allowed sources: {', '.join(allowed_sources)}",
                field=field_name,
                value=value,
            )
        validated_sources.append(src)

    return validated_sources


def validate_prompt(
    value: Any,
    field_name: str = "prompt",
    min_length: int = 10,
    max_length: int = 10000,
    allow_none: bool = False,
) -> str | None:
    """
    Validate a prompt string for LLM operations.

    Args:
        value: The value to validate
        field_name: Name of the field for error messages
        min_length: Minimum prompt length (default: 10)
        max_length: Maximum prompt length (default: 10000)
        allow_none: If True, allow None values

    Returns:
        Validated prompt as string or None

    Raises:
        SupacrawlValidationError: If validation fails
    """
    if value is None:
        if allow_none:
            return None
        raise SupacrawlValidationError(
            f"{field_name} is required. Describe what data you want to extract.",
            field=field_name,
            value=value,
        )

    if not isinstance(value, str):
        raise SupacrawlValidationError(
            f"{field_name} must be a string, got {type(value).__name__}",
            field=field_name,
            value=value,
        )

    prompt = value.strip()
    if len(prompt) < min_length:
        raise SupacrawlValidationError(
            f"{field_name} must be at least {min_length} characters describing what data to gather, got {len(prompt)}",
            field=field_name,
            value=value,
        )

    if len(prompt) > max_length:
        raise SupacrawlValidationError(
            f"{field_name} must be at most {max_length} characters, got {len(prompt)}",
            field=field_name,
            value=value,
        )

    return prompt


def validate_max_steps(
    value: Any,
    field_name: str = "max_steps",
    min_value: int = 1,
    max_value: int = 20,
    default: int = 10,
) -> int:
    """
    Validate max_steps parameter for agent operations.

    Clamps the value to the valid range instead of raising an error.

    Args:
        value: The value to validate
        field_name: Name of the field for error messages
        min_value: Minimum allowed value (default: 1)
        max_value: Maximum allowed value (default: 20)
        default: Default value if None (default: 10)

    Returns:
        Validated max_steps as integer (clamped to range)
    """
    if value is None:
        return default

    try:
        steps = int(value)
    except ValueError, TypeError:
        return default

    # Clamp to valid range
    return max(min_value, min(steps, max_value))


__all__ = [
    "validate_url",
    "validate_query",
    "enhance_query_with_current_year",
    "validate_timeout",
    "validate_limit",
    "validate_urls",
    "validate_formats",
    "validate_sources",
    "validate_prompt",
    "validate_max_steps",
]
