"""
Page actions service for browser automation.

Provides the ability to perform browser automation actions before scraping,
such as clicking, typing, scrolling, and executing JavaScript.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Literal

from playwright.async_api import Page

LOGGER = logging.getLogger(__name__)


@dataclass
class ScrapeActionData:
    """Data captured by a scrape action."""

    url: str
    html: str


@dataclass
class ActionResult:
    """Result of executing an action."""

    success: bool
    action_type: str
    error: str | None = None
    screenshot: bytes | None = None  # For screenshot action
    scrape: ScrapeActionData | None = None  # For scrape action


@dataclass
class Action:
    """A single page action.

    Supported action types:
    - wait: Wait for time (milliseconds) or selector
    - click: Click on an element by selector
    - type/write: Type text into an input
    - scroll: Scroll the page (up/down)
    - screenshot: Take a screenshot
    - press: Press a keyboard key
    - executeJavascript: Run custom JavaScript
    - scrape: Capture current page content (mid-workflow)
    """

    type: Literal["wait", "click", "type", "write", "scroll", "screenshot", "press", "executeJavascript", "scrape"]
    selector: str | None = None
    milliseconds: int | None = None
    text: str | None = None
    direction: Literal["up", "down"] | None = None
    key: str | None = None
    script: str | None = None
    full_page: bool = True


class ActionRunner:
    """Execute page actions on a Playwright page.

    Usage:
        runner = ActionRunner()
        results = await runner.run(page, [
            Action(type="wait", milliseconds=1000),
            Action(type="click", selector="button#submit"),
            Action(type="screenshot"),
        ])
    """

    def __init__(self, timeout_ms: int = 30000):
        """Initialize action runner.

        Args:
            timeout_ms: Default timeout for actions in milliseconds.
        """
        self.timeout_ms = timeout_ms

    async def run(self, page: Page, actions: list[Action]) -> list[ActionResult]:
        """Execute a sequence of actions on a page.

        Args:
            page: Playwright page instance.
            actions: List of actions to execute.

        Returns:
            List of ActionResult for each action.
        """
        results: list[ActionResult] = []

        for action in actions:
            try:
                result = await self._execute_action(page, action)
                results.append(result)

                if not result.success:
                    LOGGER.warning(f"Action {action.type} failed: {result.error}")
                    # Continue with other actions unless critical

            except Exception as e:
                LOGGER.error(f"Action {action.type} raised exception: {e}")
                results.append(
                    ActionResult(
                        success=False,
                        action_type=action.type,
                        error=str(e),
                    )
                )

        return results

    async def _execute_action(self, page: Page, action: Action) -> ActionResult:
        """Execute a single action.

        Args:
            page: Playwright page instance.
            action: Action to execute.

        Returns:
            ActionResult indicating success/failure.
        """
        action_type = action.type

        if action_type == "wait":
            return await self._action_wait(page, action)
        elif action_type == "click":
            return await self._action_click(page, action)
        elif action_type in ("type", "write"):
            return await self._action_type(page, action)
        elif action_type == "scroll":
            return await self._action_scroll(page, action)
        elif action_type == "screenshot":
            return await self._action_screenshot(page, action)
        elif action_type == "press":
            return await self._action_press(page, action)
        elif action_type == "executeJavascript":
            return await self._action_execute_js(page, action)
        elif action_type == "scrape":
            return await self._action_scrape(page, action)
        else:
            return ActionResult(
                success=False,
                action_type=action_type,
                error=f"Unknown action type: {action_type}",
            )

    async def _action_wait(self, page: Page, action: Action) -> ActionResult:
        """Wait for time or selector."""
        if action.milliseconds:
            await asyncio.sleep(action.milliseconds / 1000)
            return ActionResult(success=True, action_type="wait")
        elif action.selector:
            try:
                await page.wait_for_selector(action.selector, timeout=self.timeout_ms)
                return ActionResult(success=True, action_type="wait")
            except Exception as e:
                return ActionResult(
                    success=False,
                    action_type="wait",
                    error=f"Selector not found: {action.selector} ({e})",
                )
        else:
            return ActionResult(
                success=False,
                action_type="wait",
                error="Wait action requires milliseconds or selector",
            )

    async def _action_click(self, page: Page, action: Action) -> ActionResult:
        """Click on an element."""
        if not action.selector:
            return ActionResult(
                success=False,
                action_type="click",
                error="Click action requires selector",
            )

        try:
            await page.click(action.selector, timeout=self.timeout_ms)
            return ActionResult(success=True, action_type="click")
        except Exception as e:
            return ActionResult(
                success=False,
                action_type="click",
                error=f"Click failed: {e}",
            )

    async def _action_type(self, page: Page, action: Action) -> ActionResult:
        """Type text into an input."""
        if not action.selector:
            return ActionResult(
                success=False,
                action_type="type",
                error="Type action requires selector",
            )
        if not action.text:
            return ActionResult(
                success=False,
                action_type="type",
                error="Type action requires text",
            )

        try:
            await page.fill(action.selector, action.text, timeout=self.timeout_ms)
            return ActionResult(success=True, action_type="type")
        except Exception as e:
            return ActionResult(
                success=False,
                action_type="type",
                error=f"Type failed: {e}",
            )

    async def _action_scroll(self, page: Page, action: Action) -> ActionResult:
        """Scroll the page."""
        direction = action.direction or "down"

        try:
            if direction == "down":
                await page.evaluate("window.scrollBy(0, window.innerHeight)")
            else:
                await page.evaluate("window.scrollBy(0, -window.innerHeight)")
            return ActionResult(success=True, action_type="scroll")
        except Exception as e:
            return ActionResult(
                success=False,
                action_type="scroll",
                error=f"Scroll failed: {e}",
            )

    async def _action_screenshot(self, page: Page, action: Action) -> ActionResult:
        """Take a screenshot."""
        try:
            screenshot_bytes = await page.screenshot(
                full_page=action.full_page,
                type="png",
            )
            return ActionResult(
                success=True,
                action_type="screenshot",
                screenshot=screenshot_bytes,
            )
        except Exception as e:
            return ActionResult(
                success=False,
                action_type="screenshot",
                error=f"Screenshot failed: {e}",
            )

    async def _action_press(self, page: Page, action: Action) -> ActionResult:
        """Press a keyboard key."""
        if not action.key:
            return ActionResult(
                success=False,
                action_type="press",
                error="Press action requires key",
            )

        try:
            await page.keyboard.press(action.key)
            return ActionResult(success=True, action_type="press")
        except Exception as e:
            return ActionResult(
                success=False,
                action_type="press",
                error=f"Press failed: {e}",
            )

    async def _action_execute_js(self, page: Page, action: Action) -> ActionResult:
        """Execute custom JavaScript."""
        if not action.script:
            return ActionResult(
                success=False,
                action_type="executeJavascript",
                error="ExecuteJavascript action requires script",
            )

        try:
            await page.evaluate(action.script)
            return ActionResult(success=True, action_type="executeJavascript")
        except Exception as e:
            return ActionResult(
                success=False,
                action_type="executeJavascript",
                error=f"JavaScript execution failed: {e}",
            )

    async def _action_scrape(self, page: Page, action: Action) -> ActionResult:
        """Capture current page content (mid-workflow scrape).

        Args:
            page: Playwright page instance.
            action: Action configuration (no parameters needed).

        Returns:
            ActionResult with captured HTML and URL.
        """
        try:
            html = await page.content()
            url = page.url

            return ActionResult(
                success=True,
                action_type="scrape",
                scrape=ScrapeActionData(url=url, html=html),
            )
        except Exception as e:
            return ActionResult(
                success=False,
                action_type="scrape",
                error=f"Scrape failed: {e}",
            )


def parse_actions(actions_json: list[dict[str, Any]]) -> list[Action]:
    """Parse JSON action list into Action objects.

    Args:
        actions_json: List of action dictionaries.

    Returns:
        List of Action objects.

    Example input:
        [
            {"type": "wait", "milliseconds": 1000},
            {"type": "click", "selector": "button#submit"},
            {"type": "screenshot", "fullPage": true}
        ]
    """
    actions: list[Action] = []

    for item in actions_json:
        action_type = item.get("type")
        if not action_type:
            continue

        action = Action(
            type=action_type,
            selector=item.get("selector"),
            milliseconds=item.get("milliseconds"),
            text=item.get("text"),
            direction=item.get("direction"),
            key=item.get("key"),
            script=item.get("script"),
            full_page=item.get("fullPage", True),
        )
        actions.append(action)

    return actions
