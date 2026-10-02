# Retry Logic

This document explains retry logic patterns in supacrawl providers.

## When to Retry

### Retry on Transient Failures

Retry on errors that are likely temporary:

- **5xx Server Errors**: Internal server errors, service unavailable
- **Connection Errors**: Network timeouts, connection refused
- **Rate Limiting**: 429 Too Many Requests (with Retry-After header)
- **Provider-Specific Transient States**: Queued, running, pending (for async providers)

### Don't Retry on Client Errors

Don't retry on errors that indicate permanent problems:

- **4xx Client Errors**: Bad request, unauthorized, forbidden, not found
- **Validation Errors**: Invalid input parameters
- **Configuration Errors**: Missing or invalid configuration

## Exponential Backoff

### Backoff Calculation

Use exponential backoff with jitter:

```python
import time
from random import uniform

_max_attempts = 3
_base_backoff = 0.5  # Base delay in seconds

for attempt in range(_max_attempts):
    try:
        return provider_client.crawl(...)
    except TransientError as e:
        if attempt == _max_attempts - 1:
            raise ProviderError(...) from e
        # Exponential backoff: 0.5s, 1s, 2s
        backoff = _base_backoff * (2 ** attempt)
        # Add jitter: random 0-0.1s
        jitter = uniform(0, 0.1)
        time.sleep(backoff + jitter)
```

### Backoff Intervals

Typical backoff intervals:

- **Attempt 1**: 0.5s + jitter (0.5-0.6s)
- **Attempt 2**: 1.0s + jitter (1.0-1.1s)
- **Attempt 3**: 2.0s + jitter (2.0-2.1s)

### Why Jitter?

Jitter prevents synchronised retries:

- **Without jitter**: All clients retry at same time (thundering herd), synchronised retries
- **With jitter**: Retries spread over time (reduces load spikes), desynchronised

## Retry Implementation Patterns

### Simple Retry Loop

```python
_max_attempts = 3
_base_backoff = 0.5

for attempt in range(_max_attempts):
    try:
        return provider_client.crawl(...)
    except TransientError as e:
        if attempt == _max_attempts - 1:
            raise ProviderError(...) from e
        backoff = _base_backoff * (2 ** attempt) + uniform(0, 0.1)
        time.sleep(backoff)
```

### Retry with Status Checking

For async providers that return status:

```python
_retryable_statuses = {"queued", "running", "pending"}
_max_attempts = 3
_poll_interval = 2

for attempt in range(_max_attempts):
    result = provider_client.crawl(...)
    if result.status not in _retryable_statuses:
        return result
    if attempt == _max_attempts - 1:
        raise ProviderError("Crawl timed out")
    time.sleep(_poll_interval)
```

### Retry with Retry-After Header

Respect `Retry-After` header for rate limiting:

```python
import time

try:
    response = provider_client.crawl(...)
except RateLimitError as e:
    retry_after = int(e.headers.get("Retry-After", 60))
    time.sleep(retry_after)
    # Retry once after waiting
    return provider_client.crawl(...)
```

## Provider-Specific Patterns

### Playwright Browser Automation

The scraper uses Playwright for browser automation with retry logic:

```python
_max_attempts = 3
_base_backoff = 0.5

for attempt in range(_max_attempts):
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.goto(url, timeout=30000)
            content = await page.content()
            return content
    except (TimeoutError, Error) as e:
        if attempt == _max_attempts - 1:
            raise ProviderError(...) from e
        backoff = _base_backoff * (2 ** attempt) + uniform(0, 0.1)
        await asyncio.sleep(backoff)
```

### HTTP Fetcher

For static content, httpx is used with exponential backoff:

```python
_max_attempts = 3
_base_backoff = 0.5

for attempt in range(_max_attempts):
    try:
        response = httpx.get(url, timeout=30.0)
        response.raise_for_status()
        return response.text
    except (httpx.HTTPStatusError, httpx.TimeoutException) as e:
        if attempt == _max_attempts - 1:
            raise ProviderError(...) from e
        if isinstance(e, httpx.HTTPStatusError):
            if e.response.status_code < 500:
                # Don't retry 4xx errors
                raise ProviderError(...) from e
        backoff = _base_backoff * (2 ** attempt) + uniform(0, 0.1)
        time.sleep(backoff)
```

## Logging Retries

### Log Retry Attempts

Log retry attempts with correlation IDs:

```python
correlation_id = generate_correlation_id()

for attempt in range(_max_attempts):
    try:
        return provider_client.crawl(...)
    except TransientError as e:
        log_with_correlation(
            LOGGER,
            "warning",
            f"Retry attempt {attempt + 1}/{_max_attempts}",
            correlation_id,
            {"attempt": attempt + 1, "error": str(e)},
        )
        if attempt == _max_attempts - 1:
            raise ProviderError(...) from e
        backoff = _base_backoff * (2 ** attempt) + uniform(0, 0.1)
        time.sleep(backoff)
```

## Best Practices

1. **Retry Transient Failures**: Retry on 5xx errors, timeouts, connection errors
2. **Don't Retry Client Errors**: Fail immediately on 4xx errors
3. **Exponential Backoff**: Use exponential backoff with jitter
4. **Maximum Attempts**: Limit retry attempts (typically 3)
5. **Log Retries**: Log retry attempts with correlation IDs
6. **Respect Rate Limits**: Use Retry-After header when available
7. **Timeout Protection**: Set maximum retry duration for long-running operations

## References

- `docs/development/error-handling.md` - Error handling patterns
