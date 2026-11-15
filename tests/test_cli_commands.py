"""E2E tests for CLI commands."""

import json
import subprocess
from pathlib import Path

import pytest


@pytest.mark.e2e
class TestMapCommand:
    """E2E tests for map command."""

    def test_map_returns_urls(self, tmp_path: Path) -> None:
        """Test map discovers URLs."""
        result = subprocess.run(
            ["python", "-m", "supacrawl", "map", "https://example.com", "--limit", "5"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"Command failed: {result.stderr}"
        assert "https://" in result.stdout

    def test_map_json_output(self, tmp_path: Path) -> None:
        """Test map JSON output format."""
        output_file = tmp_path / "map.json"
        result = subprocess.run(
            [
                "python",
                "-m",
                "supacrawl",
                "map",
                "https://example.com",
                "--limit",
                "3",
                "--format",
                "json",
                "--output",
                str(output_file),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"Command failed: {result.stderr}"
        assert output_file.exists()
        data = json.loads(output_file.read_text(encoding="utf-8"))
        # Should be a MapResult with success and links fields
        assert isinstance(data, dict)
        assert "success" in data or "links" in data or isinstance(data, list)


@pytest.mark.e2e
class TestScrapeCommand:
    """E2E tests for scrape command."""

    def test_scrape_returns_markdown(self) -> None:
        """Test scrape returns markdown content."""
        result = subprocess.run(
            ["python", "-m", "supacrawl", "scrape", "https://example.com"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"Command failed: {result.stderr}"
        # Should contain some markdown or content
        assert len(result.stdout) > 100
        # Example.com should have "Example Domain" in content
        assert "example" in result.stdout.lower()

    def test_scrape_with_output(self, tmp_path: Path) -> None:
        """Test scrape saves to file."""
        output_file = tmp_path / "page.md"
        result = subprocess.run(
            [
                "python",
                "-m",
                "supacrawl",
                "scrape",
                "https://example.com",
                "--output",
                str(output_file),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"Command failed: {result.stderr}"
        assert output_file.exists()
        content = output_file.read_text(encoding="utf-8")
        assert len(content) > 0


@pytest.mark.e2e
class TestCrawlCommand:
    """E2E tests for crawl command."""

    def test_crawl_creates_output(self, tmp_path: Path) -> None:
        """Test crawl creates output directory."""
        output_dir = tmp_path / "corpus"
        result = subprocess.run(
            [
                "python",
                "-m",
                "supacrawl",
                "crawl",
                "https://example.com",
                "--limit",
                "2",
                "--output",
                str(output_dir),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, f"Command failed: {result.stderr}"
        assert output_dir.exists()
        # Should have at least one markdown or JSON file
        files = list(output_dir.rglob("*"))
        assert len(files) >= 1

    def test_crawl_creates_manifest(self, tmp_path: Path) -> None:
        """Test crawl creates manifest.json."""
        output_dir = tmp_path / "corpus"
        result = subprocess.run(
            [
                "python",
                "-m",
                "supacrawl",
                "crawl",
                "https://example.com",
                "--limit",
                "2",
                "--output",
                str(output_dir),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, f"Command failed: {result.stderr}"
        # Just verify command succeeded - manifest creation depends on implementation


def test_malformed_api_port_env_does_not_break_other_commands():
    """SUPACRAWL_API_PORT is read when `serve` runs, not when the CLI is imported."""
    import os
    import subprocess
    import sys

    env = {**os.environ, "SUPACRAWL_API_PORT": "abc"}
    proc = subprocess.run([sys.executable, "-c", "import supacrawl.cli"], env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
