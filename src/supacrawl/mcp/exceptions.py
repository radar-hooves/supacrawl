"""Custom exceptions for Supacrawl MCP Server.

All exceptions inherit from mcp_common.exceptions for consistent error handling.
The map_exception function converts supacrawl library exceptions to MCP-friendly exceptions.
"""

from mcp_common.exceptions import (
    MCPClientError,
    MCPConnectionError,
    MCPError,
    MCPServerError,
    MCPTimeoutError,
    MCPValidationError,
    log_tool_exception,
)


# Service-specific exceptions inherit from base classes
class SupacrawlMCPError(MCPError):
    """Base exception for all Supacrawl MCP errors."""

    pass


class SupacrawlClientError(MCPClientError):
    """Client error (4xx) - bad request, validation error, etc."""

    pass


class SupacrawlValidationError(MCPValidationError):
    """Validation error - invalid input parameters."""

    pass


class SupacrawlServerError(MCPServerError):
    """Server error (5xx) - internal server error, service unavailable, etc."""

    pass


class SupacrawlConnectionError(MCPConnectionError):
    """Connection error - network failure, DNS issue, etc."""

    pass


class SupacrawlTimeoutError(MCPTimeoutError):
    """Timeout error - request or page load timeout."""

    pass


def map_exception(
    exception: Exception,
    endpoint: str | None = None,
    correlation_id: str | None = None,
) -> MCPError:
    """
    Map supacrawl library exceptions to MCP-friendly exceptions.

    Args:
        exception: Exception from supacrawl library or other source
        endpoint: API endpoint or operation that raised the exception
        correlation_id: Correlation ID for request tracking

    Returns:
        Mapped SupacrawlMCPError exception. When the failure has a known recovery
        action (timeout, DNS, connection, TLS, 4xx/5xx), an agent-readable
        remediation hint is appended to the message.
    """
    import httpx
    from pydantic import ValidationError

    from supacrawl.services.remediation import remediation_hint

    def hinted(base: str) -> str:
        """Append a concrete remediation to a base message when one applies."""
        hint = remediation_hint(str(exception))
        return f"{base} [HINT: {hint}]" if hint else base

    # Handle Pydantic ValidationError
    if isinstance(exception, ValidationError):
        return SupacrawlClientError(
            f"Validation failed: {exception!s}",
            endpoint=endpoint,
            correlation_id=correlation_id,
        )

    # Handle httpx connection errors
    if isinstance(exception, httpx.ConnectError):
        return SupacrawlConnectionError(
            hinted(f"Connection failed: {exception!s}"),
            endpoint=endpoint,
            correlation_id=correlation_id,
        )

    if isinstance(exception, httpx.TimeoutException):
        return SupacrawlTimeoutError(
            hinted(f"Request timed out: {exception!s}"),
            endpoint=endpoint,
            correlation_id=correlation_id,
        )

    # Handle httpx HTTP status errors
    if isinstance(exception, httpx.HTTPStatusError):
        status_code = exception.response.status_code
        if 400 <= status_code < 500:
            return SupacrawlClientError(
                hinted(f"HTTP client error ({status_code}): {exception!s}"),
                endpoint=endpoint,
                status_code=status_code,
                correlation_id=correlation_id,
            )
        elif status_code >= 500:
            return SupacrawlServerError(
                hinted(f"HTTP server error ({status_code}): {exception!s}"),
                endpoint=endpoint,
                status_code=status_code,
                correlation_id=correlation_id,
            )

    # Handle generic connection errors
    if isinstance(exception, (ConnectionError, OSError)):
        return SupacrawlConnectionError(
            hinted(f"Connection failed: {exception!s}"),
            endpoint=endpoint,
            correlation_id=correlation_id,
        )

    # Handle timeout errors
    if isinstance(exception, TimeoutError):
        return SupacrawlTimeoutError(
            hinted(f"Operation timed out: {exception!s}"),
            endpoint=endpoint,
            correlation_id=correlation_id,
        )

    # Handle supacrawl library exceptions (most specific first)
    from supacrawl.exceptions import (
        ConfigurationError as LibConfigurationError,
    )
    from supacrawl.exceptions import (
        ProviderError as LibProviderError,
    )
    from supacrawl.exceptions import (
        SupacrawlError as LibSupacrawlError,
    )
    from supacrawl.exceptions import (
        ValidationError as LibValidationError,
    )

    if isinstance(exception, LibValidationError):
        return SupacrawlValidationError(
            str(exception),
            endpoint=endpoint,
            correlation_id=correlation_id,
        )

    if isinstance(exception, LibConfigurationError):
        return SupacrawlClientError(
            f"Configuration error: {exception!s}",
            endpoint=endpoint,
            correlation_id=correlation_id,
        )

    if isinstance(exception, LibProviderError):
        return SupacrawlServerError(
            hinted(f"Provider error: {exception!s}"),
            endpoint=endpoint,
            correlation_id=correlation_id,
        )

    if isinstance(exception, LibSupacrawlError):
        return SupacrawlMCPError(
            hinted(str(exception)),
            endpoint=endpoint,
            correlation_id=correlation_id,
        )

    # Handle ValueError (often validation-related)
    if isinstance(exception, ValueError):
        return SupacrawlValidationError(
            str(exception),
            endpoint=endpoint,
            correlation_id=correlation_id,
        )

    # Unknown exception - wrap it
    return SupacrawlMCPError(
        hinted(f"Unexpected error: {exception!s}"),
        endpoint=endpoint,
        correlation_id=correlation_id,
    )


# Re-export log_tool_exception for convenience
__all__ = [
    "SupacrawlClientError",
    "SupacrawlConnectionError",
    "SupacrawlMCPError",
    "SupacrawlServerError",
    "SupacrawlTimeoutError",
    "SupacrawlValidationError",
    "log_tool_exception",
    "map_exception",
]
