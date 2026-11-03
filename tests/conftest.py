"""Pytest configuration and shared fixtures for supacrawl tests."""

import importlib.util
import os
from pathlib import Path

import pytest

# Keep per-domain strategy memory (#130) and field telemetry (#137) off by default
# in tests so a test that drives the CLI/MCP scrape path cannot write to the
# developer's real ~/.supacrawl/. Tests that exercise them pass an explicit
# store / sink with a tmp dir, which bypasses these env defaults.
os.environ.setdefault("SUPACRAWL_STRATEGY_MEMORY", "0")
os.environ.setdefault("SUPACRAWL_METRICS", "0")

# Search-related env vars that a developer's direnv cascade (source_up) may inject
# into the test process.  Each search test class that tests default/keyless behaviour
# must run against a clean slate so assertions about "no key configured" hold
# regardless of what the ambient environment has set.
_SEARCH_ENV_VARS = (
    "BRAVE_API_KEY",
    "SERPER_API_KEY",
    "SERPAPI_API_KEY",
    "TAVILY_API_KEY",
    "EXA_API_KEY",
    "SEARXNG_URL",
    "SEARXNG_USERNAME",
    "SEARXNG_PASSWORD",
    "SUPACRAWL_SEARCH_PROVIDERS",
    "SUPACRAWL_LOCALE",
    "SUPACRAWL_SEARCH_RATE_LIMIT",
)

from tests.helpers import _LLM_ENV_VARS, LLMWire, wire_llm  # noqa: E402


def _chromium_installed() -> bool:
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / ".cache" / "ms-playwright")
    return any(root.glob("chromium*-*"))


@pytest.fixture(autouse=True)
def clean_search_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove search-related env vars that a direnv cascade may inject.

    Tests that assert default/keyless search behaviour must run against a clean
    environment, not the developer's real credentials.  Tests that need a specific
    env var set it themselves via ``monkeypatch.setenv`` or ``patch.dict``, which
    compose correctly with this fixture.
    """
    for var in _SEARCH_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def llm_wire(monkeypatch: pytest.MonkeyPatch) -> LLMWire:
    """The OpenAI provider, answered at the wire."""
    return wire_llm(monkeypatch)


@pytest.fixture
def no_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """No LLM configured, whatever the developer's shell carries."""
    for var in _LLM_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


# The MCP server's framework (mcp-common) is not on PyPI; only the mcp-dev group
# installs it, and CI and a PyPI install do not. Modules that import it at module
# level are not collected without it; tests marked ``mcp`` are skipped.
HAS_MCP_COMMON = importlib.util.find_spec("mcp_common") is not None
if not HAS_MCP_COMMON:
    collect_ignore = [
        "test_health_search_config.py",
        "test_health_search_probe.py",
        "test_provider_fallback_visibility.py",
        "test_secret_redaction.py",
    ]


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers."""
    config.addinivalue_line("markers", "unit: Pure logic tests with no I/O, network, or browser")
    config.addinivalue_line("markers", "integration: Filesystem-heavy tests, may use local HTTP server and Playwright")
    config.addinivalue_line(
        "markers",
        "e2e: End-to-end tests with live network, Playwright, or external services",
    )
    config.addinivalue_line(
        "markers",
        "stealth_smoke: Launches a real stealth engine headless (#145); run in a dedicated CI job "
        "that fetches every engine's browser binary, excluded from the main test job.",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """
    Apply default markers to tests without explicit markers.

    Tests should use explicit markers (@pytest.mark.unit, @pytest.mark.e2e).
    Unmarked tests default to unit. Browser-marked tests skip where Playwright's
    Chromium is not installed.
    """
    skip = None if _chromium_installed() else pytest.mark.skip(reason="Playwright Chromium is not installed")
    mcp_skip = None if HAS_MCP_COMMON else pytest.mark.skip(reason="mcp-common is not installed (the mcp-dev group)")
    for item in items:
        if skip and "browser" in item.keywords:
            item.add_marker(skip)
        if mcp_skip and "mcp" in item.keywords:
            item.add_marker(mcp_skip)
        # Skip if already has a category marker
        markers = list(item.iter_markers())
        marker_names = [m.name for m in markers]
        if any(m in marker_names for m in ("unit", "integration", "e2e", "stealth_smoke")):
            continue

        # Default unmarked tests to unit
        item.add_marker(pytest.mark.unit)


# E2E test fixtures


@pytest.fixture
def test_urls() -> list[str]:
    """Common test URLs for E2E tests."""
    return [
        "https://example.com",
        "https://example.org",
    ]


@pytest.fixture
def urls_file(tmp_path: Path, test_urls: list[str]) -> Path:
    """Create temporary URLs file for E2E tests.

    Args:
        tmp_path: Pytest temporary directory.
        test_urls: List of test URLs.

    Returns:
        Path to created URLs file.
    """
    urls_file = tmp_path / "urls.txt"
    urls_file.write_text("\n".join(test_urls), encoding="utf-8")
    return urls_file
