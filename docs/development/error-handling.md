# Error Handling Patterns

This document explains supacrawl's error handling patterns and how to use correlation IDs for debugging.

## Exception Hierarchy

Supacrawl uses a custom exception hierarchy with a base exception and specific exception types:

```
SupacrawlError (base)
├── ValidationError (input validation failures)
├── ConfigurationError (config loading/validation failures)
├── FileNotFoundError (missing files)
└── ProviderError (scraper/browser operation failures)
```

### Base Exception

All exceptions inherit from `SupacrawlError`, which includes:

- **Message**: User-friendly error message
- **Correlation ID**: 8-character UUID for request tracking
- **Context**: Dictionary with debugging information

### Specific Exceptions

- **`ValidationError`**: Raised when input validation fails (e.g., empty entrypoints, invalid max_pages)
- **`ConfigurationError`**: Raised when configuration loading or validation fails
- **`FileNotFoundError`**: Raised when required files are missing
- **`ProviderError`**: Raised when scraper/browser operations fail (Playwright errors, timeouts, navigation failures)

## Correlation IDs

### What Are Correlation IDs?

Correlation IDs are 8-character UUID-based identifiers that track requests through the system. They enable:

- **Request Tracking**: Follow a request through multiple components
- **Debugging**: Find all log entries related to a specific error
- **Observability**: Correlate errors across different parts of the system

### Generating Correlation IDs

Correlation IDs are generated using `generate_correlation_id()`:

```python
from supacrawl.exceptions import generate_correlation_id

correlation_id = generate_correlation_id()  # Returns "abc12345"
```

### Using Correlation IDs

**In Exceptions:**
```python
raise ValidationError(
    "At least one entrypoint is required.",
    field="entrypoints",
    value=value,
    correlation_id=correlation_id,
    context={"example": "entrypoints: ['https://example.com']"},
)
```

**In Logging:**
```python
from supacrawl.utils import log_with_correlation

log_with_correlation(
    LOGGER,
    "error",
    f"Provider {self.provider_name} failed: {e}",
    correlation_id,
    {"provider": self.provider_name, "error": str(e)},
)
```

**In CLI:**
```python
except SupacrawlError as e:
    click.echo(
        f"Error: {e.message} [correlation_id={e.correlation_id}]",
        err=True,
    )
```

## Error Context

### Context Dictionary

All exceptions include a `context` dictionary with debugging information:

```python
raise ProviderError(
    f"Failed to crawl site using {self.provider_name}",
    provider=self.provider_name,
    correlation_id=correlation_id,
    context={
        "original_error": str(e),
        "provider": self.provider_name,
        "url": url,
    },
)
```

### Context Best Practices

- Include relevant debugging information (field names, values, provider names)
- Don't include sensitive data (API keys, passwords)
- Include examples when helpful (e.g., valid entrypoint format)
- Keep context concise (don't include entire objects)

## Scraper Error Handling

### Wrapping Browser/Playwright Errors

Browser and Playwright errors are wrapped in `ProviderError`:

```python
from playwright.async_api import Error as PlaywrightError

try:
    content = await browser.fetch_page(url)
except PlaywrightError as e:
    correlation_id = generate_correlation_id()
    LOGGER.error(
        "Browser fetch failed: %s",
        str(e),
        extra={"correlation_id": correlation_id, "url": url},
    )
    raise ProviderError(
        f"Failed to fetch {url}",
        correlation_id=correlation_id,
        context={"url": url, "error": str(e)},
    ) from e
```

### Error Chaining

Use `from e` to preserve original exception:

```python
raise ProviderError(...) from e
```

This preserves the exception chain for debugging while showing user-friendly messages.

## Automatic Error Recovery

### HTTP/2 Protocol Error Fallback

When a Chromium-based engine (Playwright or Patchright) encounters an `ERR_HTTP2_PROTOCOL_ERROR`, supacrawl automatically retries with Camoufox if it is installed. This handles servers that reject Chromium's TLS fingerprint (common with Akamai Bot Manager).

The fallback is a two-stage chain:

1. **Chromium fails** -> retry with Camoufox (different TLS fingerprint via Firefox)
2. **Camoufox with HTTP/2 fails** -> retry with Camoufox + HTTP/1.1 forced (disables HTTP/2 via Firefox preferences)

This fallback is transparent to the user. If Camoufox is not installed, the error is raised with a hint to install it.

### Bot Detection Auto-Retry

When basic Playwright scraping returns what appears to be a bot-detection page (based on status code and content heuristics), supacrawl automatically retries with Patchright stealth mode if available.

The full retry chain for a scrape request:

```
Playwright (Tier 1) -> bot detected? -> Patchright (Tier 2)
                    -> HTTP/2 error?  -> Camoufox (Tier 3)
                                      -> HTTP/2 error? -> Camoufox + HTTP/1.1
```

Each stage only triggers if the required package is installed (`supacrawl[stealth]` for Patchright, `supacrawl[camoufox]` for Camoufox).

## Validation Error Patterns

### Field Validation

Include field name and value in validation errors:

```python
if not value:
    correlation_id = generate_correlation_id()
    raise ValidationError(
        "At least one entrypoint is required.",
        field="entrypoints",
        value=value,
        correlation_id=correlation_id,
        context={"example": "entrypoints: ['https://example.com']"},
    )
```

### Helpful Messages

Provide actionable error messages with examples:

```python
raise ValidationError(
    "max_pages must be greater than 0.",
    field="max_pages",
    value=0,
    correlation_id=correlation_id,
    context={"example": "max_pages: 100"},
)
```

## Configuration Error Patterns

### Validation Failures

Include validation errors in context:

```python
try:
    result = ScrapeResult.model_validate(data)
except ValidationError as e:
    raise ConfigurationError(
        f"Invalid scrape result: {e}",
        correlation_id=correlation_id,
        context={"validation_errors": str(e)},
    ) from e
```

## CLI Error Presentation

### User-Friendly Messages

Show friendly messages to users, not stack traces:

```python
try:
    result = command_function(...)
except SupacrawlError as e:
    click.echo(
        f"Error: {e.message} [correlation_id={e.correlation_id}]",
        err=True,
    )
    raise SystemExit(1)
```

### Error Output

Use `err=True` for error messages (stderr):

```python
click.echo("Error message", err=True)  # Goes to stderr
click.echo("Normal output")  # Goes to stdout
```

## Debugging with Correlation IDs

### Finding Related Logs

1. Note correlation ID from error message
2. Search logs for correlation ID:
   ```bash
   grep "abc12345" logs/*.log
   ```
3. Review all log entries with same correlation ID

### Log Structure

Logs include correlation IDs in structured format:

```json
{
  "level": "error",
  "message": "Scraper failed: ...",
  "correlation_id": "abc12345",
  "context": {
    "url": "https://example.com",
    "error": "..."
  }
}
```

## Best Practices

1. **Always Include Correlation IDs**: Generate and include in all exceptions and logs
2. **User-Friendly Messages**: Show friendly messages to users, log details internally
3. **Error Chaining**: Use `from e` to preserve exception chains
4. **Context Dictionary**: Include relevant debugging information
5. **No Sensitive Data**: Don't include API keys, passwords in errors/logs
6. **Actionable Errors**: Provide examples and suggestions in error messages

## References

- `docs/development/retry-logic.md` - Retry logic patterns
