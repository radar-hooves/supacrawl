# Testing Strategies

This document explains testing strategies for supacrawl.

## Testing Overview

Supacrawl uses pytest for testing with patterns for:

- **Unit Tests**: Test individual components in isolation
- **Integration Tests**: Test component interactions
- **E2E Tests**: Test real browser/network operations

## Test Categories

Tests are organised into directories with automatic marker assignment:

- **`tests/unit/`**: Pure logic tests, no I/O or browser (marker: `unit`)
- **`tests/integration/`**: Filesystem operations, mocks (marker: `integration`)
- **`tests/e2e/`**: Real Playwright browser tests, slow (marker: `e2e`)

Run specific categories:

```bash
# Fast tests only (unit + integration, excludes e2e)
pytest -q -m "not e2e"

# End-to-end tests only
pytest -q -m "e2e"

# All tests
pytest -q
```

## Unit Testing Patterns

### Testing Service Classes

Test service initialisation:

```python
def test_scrape_service_initialisation():
    """Test scrape service initialisation."""
    from supacrawl.services import ScrapeService
    service = ScrapeService()
    assert service is not None
```

### Testing Pure Logic

Test URL pattern matching:

```python
def test_matches_patterns():
    """Test URL pattern matching."""
    from supacrawl.services.crawl import CrawlService
    service = CrawlService()
    assert service._matches_patterns("https://example.com/api/v1", ["*/api/*"])
    assert not service._matches_patterns("https://example.com/docs", ["*/api/*"])
```

## Service Testing Patterns

### Mocking Browser Clients

Use dependency injection for testability:

```python
@pytest.fixture
def mock_browser_context():
    """Mock browser context for testing."""
    context = AsyncMock()
    page = AsyncMock()
    page.content = AsyncMock(return_value="<html><body>Test</body></html>")
    page.goto = AsyncMock()
    context.new_page = AsyncMock(return_value=page)
    return context


async def test_scrape_service_success(mock_browser_context):
    """Test scrape service successfully scrapes page."""
    from supacrawl.services import ScrapeService

    service = ScrapeService(browser=mock_browser_context)
    result = await service.scrape("https://example.com")
    assert result.success
    assert result.data.markdown is not None
```

### Testing Service Error Handling

Test error wrapping:

```python
async def test_scrape_service_error_handling():
    """Test scrape service error handling."""
    from supacrawl.services import ScrapeService

    service = ScrapeService()
    result = await service.scrape("https://invalid-domain-12345.com")

    assert not result.success
    assert result.error is not None
```

## CLI Testing Patterns

### Testing CLI Commands

Use subprocess for E2E CLI tests:

```python
def test_scrape_command():
    """Test scrape command returns markdown."""
    result = subprocess.run(
        ["python", "-m", "supacrawl", "scrape", "https://example.com"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0
    assert len(result.stdout) > 100
```

### Testing CLI with Output Files

```python
def test_scrape_with_output(tmp_path):
    """Test scrape saves to file."""
    output_file = tmp_path / "page.md"
    result = subprocess.run(
        ["python", "-m", "supacrawl", "scrape", "https://example.com", "--output", str(output_file)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0
    assert output_file.exists()
```

## Test Fixtures

### Common Fixtures

Create reusable test fixtures:

```python
@pytest.fixture
def sample_scrape_result():
    """Sample scrape result for testing."""
    from supacrawl.models import ScrapeResult, ScrapeData, PageMetadata

    return ScrapeResult(
        success=True,
        data=ScrapeData(
            markdown="# Test Page\n\nContent here.",
            metadata=PageMetadata(
                title="Test Page",
                source_url="https://example.com",
            ),
        ),
    )
```

## Best Practices

1. **Isolation**: Each test should be independent
2. **Fixtures**: Use fixtures for common test data
3. **Mocking**: Mock external dependencies (browser, network)
4. **Coverage**: Test error paths, not just happy paths
5. **Markers**: Use `@pytest.mark.e2e` for slow network tests
6. **Cleanup**: Clean up test artifacts (temp files, directories)

## Running Tests

### Run All Tests

```bash
pytest -q
```

### Run Fast Tests Only

```bash
pytest -q -m "not e2e"
```

### Run Specific Test File

```bash
pytest tests/unit/test_converter.py -q
```

### Run with Coverage

```bash
pytest --cov=supacrawl --cov-report=html
```

## References

- `docs/development/error-handling.md` - Error handling patterns
