"""Tests for page actions service."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from supacrawl.services.actions import (
    Action,
    ActionResult,
    ActionRunner,
    ScrapeActionData,
    parse_actions,
)


class TestAction:
    """Tests for Action dataclass."""

    def test_action_scrape_type(self):
        """Test that scrape is a valid action type."""
        action = Action(type="scrape")
        assert action.type == "scrape"

    def test_action_all_types(self):
        """Test all valid action types."""
        valid_types = [
            "wait",
            "click",
            "type",
            "write",
            "scroll",
            "screenshot",
            "press",
            "executeJavascript",
            "scrape",
        ]
        for action_type in valid_types:
            action = Action(type=action_type)  # type: ignore[arg-type]
            assert action.type == action_type


class TestActionResult:
    """Tests for ActionResult dataclass."""

    def test_action_result_with_scrape(self):
        """Test ActionResult with scrape data."""
        scrape_data = ScrapeActionData(
            url="https://example.com",
            html="<html><body>Test</body></html>",
        )
        result = ActionResult(
            success=True,
            action_type="scrape",
            scrape=scrape_data,
        )
        assert result.success
        assert result.action_type == "scrape"
        assert result.scrape is not None
        assert result.scrape.url == "https://example.com"
        assert result.scrape.html == "<html><body>Test</body></html>"

    def test_action_result_with_screenshot(self):
        """Test ActionResult with screenshot data."""
        result = ActionResult(
            success=True,
            action_type="screenshot",
            screenshot=b"PNG data",
        )
        assert result.success
        assert result.screenshot == b"PNG data"


class TestActionRunner:
    """Tests for ActionRunner."""

    @pytest.fixture
    def mock_page(self):
        """Create a mock Playwright page."""
        page = MagicMock()
        page.url = "https://example.com"
        page.content = AsyncMock(return_value="<html><body>Test content</body></html>")
        page.click = AsyncMock()
        page.fill = AsyncMock()
        page.evaluate = AsyncMock()
        page.screenshot = AsyncMock(return_value=b"PNG screenshot data")
        page.keyboard = MagicMock()
        page.keyboard.press = AsyncMock()
        page.wait_for_selector = AsyncMock()
        return page

    @pytest.mark.asyncio
    async def test_action_scrape(self, mock_page):
        """Test scrape action captures HTML and URL."""
        runner = ActionRunner()
        action = Action(type="scrape")

        results = await runner.run(mock_page, [action])

        assert len(results) == 1
        assert results[0].success
        assert results[0].action_type == "scrape"
        assert results[0].scrape is not None
        assert results[0].scrape.url == "https://example.com"
        assert results[0].scrape.html == "<html><body>Test content</body></html>"

    @pytest.mark.asyncio
    async def test_action_scrape_multiple(self, mock_page):
        """Test multiple scrape actions in sequence."""
        # Simulate content changing between scrapes
        mock_page.content = AsyncMock(
            side_effect=[
                "<html><body>Content 1</body></html>",
                "<html><body>Content 2</body></html>",
            ]
        )

        runner = ActionRunner()
        actions = [
            Action(type="scrape"),
            Action(type="scrape"),
        ]

        results = await runner.run(mock_page, actions)

        assert len(results) == 2
        assert results[0].scrape is not None
        assert results[0].scrape.html == "<html><body>Content 1</body></html>"
        assert results[1].scrape is not None
        assert results[1].scrape.html == "<html><body>Content 2</body></html>"

    @pytest.mark.asyncio
    async def test_action_scrape_with_other_actions(self, mock_page):
        """Test scrape action mixed with other actions."""
        mock_page.content = AsyncMock(
            side_effect=[
                "<html><body>Before scroll</body></html>",
                "<html><body>After scroll</body></html>",
            ]
        )

        runner = ActionRunner()
        actions = [
            Action(type="scrape"),
            Action(type="scroll", direction="down"),
            Action(type="scrape"),
        ]

        results = await runner.run(mock_page, actions)

        assert len(results) == 3
        assert results[0].action_type == "scrape"
        assert results[0].scrape is not None
        assert results[0].scrape.html == "<html><body>Before scroll</body></html>"
        assert results[1].action_type == "scroll"
        assert results[1].success
        assert results[2].action_type == "scrape"
        assert results[2].scrape is not None
        assert results[2].scrape.html == "<html><body>After scroll</body></html>"

    @pytest.mark.asyncio
    async def test_action_scrape_error_handling(self, mock_page):
        """Test scrape action handles errors gracefully."""
        mock_page.content = AsyncMock(side_effect=Exception("Page error"))

        runner = ActionRunner()
        action = Action(type="scrape")

        results = await runner.run(mock_page, [action])

        assert len(results) == 1
        assert not results[0].success
        assert results[0].action_type == "scrape"
        assert results[0].error is not None
        assert "Scrape failed" in results[0].error

    @pytest.mark.asyncio
    async def test_action_screenshot(self, mock_page):
        """Test screenshot action returns bytes."""
        runner = ActionRunner()
        action = Action(type="screenshot")

        results = await runner.run(mock_page, [action])

        assert len(results) == 1
        assert results[0].success
        assert results[0].action_type == "screenshot"
        assert results[0].screenshot == b"PNG screenshot data"

    @pytest.mark.asyncio
    async def test_action_wait_milliseconds(self, mock_page):
        """Test wait action with milliseconds."""
        runner = ActionRunner()
        action = Action(type="wait", milliseconds=100)

        results = await runner.run(mock_page, [action])

        assert len(results) == 1
        assert results[0].success
        assert results[0].action_type == "wait"

    @pytest.mark.asyncio
    async def test_action_click(self, mock_page):
        """Test click action."""
        runner = ActionRunner()
        action = Action(type="click", selector="button#submit")

        results = await runner.run(mock_page, [action])

        assert len(results) == 1
        assert results[0].success
        assert results[0].action_type == "click"
        mock_page.click.assert_called_once()


class TestParseActions:
    """Tests for parse_actions function."""

    def test_parse_scrape_action(self):
        """Test parsing scrape action from JSON."""
        actions_json = [{"type": "scrape"}]
        actions = parse_actions(actions_json)

        assert len(actions) == 1
        assert actions[0].type == "scrape"

    def test_parse_multiple_actions_with_scrape(self):
        """Test parsing multiple actions including scrape."""
        actions_json: list[dict[str, Any]] = [
            {"type": "scroll", "direction": "down"},
            {"type": "wait", "milliseconds": 2000},
            {"type": "scrape"},
            {"type": "scroll", "direction": "down"},
            {"type": "wait", "milliseconds": 2000},
            {"type": "scrape"},
        ]
        actions = parse_actions(actions_json)

        assert len(actions) == 6
        assert actions[2].type == "scrape"
        assert actions[5].type == "scrape"

    def test_parse_empty_actions(self):
        """Test parsing empty action list."""
        actions = parse_actions([])
        assert len(actions) == 0

    def test_parse_actions_with_missing_type(self):
        """Test parsing actions with missing type (should be skipped)."""
        actions_json = [
            {"type": "scrape"},
            {"selector": "button"},  # Missing type
            {"type": "click", "selector": "button#submit"},
        ]
        actions = parse_actions(actions_json)

        assert len(actions) == 2
        assert actions[0].type == "scrape"
        assert actions[1].type == "click"


class TestRawDictActions:
    """REST and the Foleon rung hand ActionRunner raw dicts."""

    async def test_dict_actions_run_like_action_objects(self):
        page = MagicMock()
        page.evaluate = AsyncMock()
        runner = ActionRunner()

        results = await runner.run(page, [{"type": "scroll", "direction": "down", "amount": 3}])  # type: ignore[list-item]

        assert [r.success for r in results] == [True]
        assert page.evaluate.await_count == 3
