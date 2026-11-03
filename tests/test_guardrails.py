"""Guardrail tests to enforce test categorisation rules."""

import ast
from pathlib import Path

import pytest

# Tests root directory
TESTS_ROOT = Path(__file__).parent

# E2E test files that are allowed to import Playwright
# These files have @pytest.mark.e2e markers
E2E_TEST_FILES: set[str] = {
    "test_output_format.py",
    "test_error_handling.py",
    "test_pipeline.py",
    "test_cli_commands.py",
    "test_browser.py",  # Uses Playwright for browser testing
    "test_scrape_service.py",  # E2E service tests
    "test_crawl_service.py",  # E2E service tests
    "test_map_service.py",  # E2E service tests
    "test_benchmark_reference.py",  # One @pytest.mark.e2e test drives a real Playwright render
}

# Forbidden imports for unit tests
FORBIDDEN_IMPORTS = {
    "playwright",
}


def _get_imports(filepath: Path) -> set[str]:
    """Extract all imported module names from a Python file."""
    imports: set[str] = set()
    try:
        tree = ast.parse(filepath.read_text(encoding="utf-8"))
    except SyntaxError:
        return imports

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])

    return imports


@pytest.mark.unit
def test_unit_tests_do_not_import_playwright_directly() -> None:
    """
    Verify that unit test files do not import playwright.

    Unit tests should not depend on Playwright directly.
    E2E tests (listed in E2E_TEST_FILES) are allowed to use Playwright.
    """
    violations: list[str] = []

    for test_file in TESTS_ROOT.glob("test_*.py"):
        # Skip E2E test files
        if test_file.name in E2E_TEST_FILES:
            continue
        # Skip this guardrail test
        if test_file.name == "test_guardrails.py":
            continue

        imports = _get_imports(test_file)
        forbidden_found = imports & FORBIDDEN_IMPORTS

        if forbidden_found:
            violations.append(f"{test_file.name} imports {forbidden_found} - add to E2E_TEST_FILES if intentional")

    if violations:
        msg = (
            "Unit tests should not import playwright directly.\n"
            "Either:\n"
            "  1. Add test to E2E_TEST_FILES in test_guardrails.py\n"
            "  2. Use mocks instead of real imports\n\n"
            "Violations:\n" + "\n".join(f"  - {v}" for v in violations)
        )
        pytest.fail(msg)


@pytest.mark.unit
def test_e2e_test_files_exist() -> None:
    """Verify that all files in E2E_TEST_FILES actually exist."""
    for filename in E2E_TEST_FILES:
        full_path = TESTS_ROOT / filename
        if not full_path.exists():
            pytest.fail(
                f"File '{filename}' in E2E_TEST_FILES does not exist. Remove it from the set or create the file."
            )
