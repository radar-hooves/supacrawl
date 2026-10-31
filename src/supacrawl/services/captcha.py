"""CAPTCHA solving service with 2Captcha integration.

WHAT THIS MODULE DOES:
    Detects and solves CAPTCHAs on web pages using third-party solving services.
    This is OPTIONAL functionality that requires:
    1. Installing: pip install supacrawl[captcha]
    2. Setting environment variable: CAPTCHA_API_KEY=your-2captcha-api-key
    3. Using --solve-captcha flag when scraping

SUPPORTED CAPTCHA TYPES:
    - reCAPTCHA v2 (checkbox challenges)
    - reCAPTCHA v3 (invisible/score-based)
    - hCaptcha
    - Cloudflare Turnstile

COST WARNING:
    CAPTCHA solving costs money (~$2-3 per 1000 solves).
    Each solve typically takes 10-60 seconds.

USAGE:
    # CLI
    supacrawl scrape --stealth --solve-captcha https://protected-site.com

    # Python
    from supacrawl.services.captcha import CaptchaSolver
    solver = CaptchaSolver()
    await solver.detect_and_solve(page)

ENVIRONMENT VARIABLES:
    CAPTCHA_API_KEY    - Your 2Captcha API key (required)
    CAPTCHA_TIMEOUT    - Timeout in seconds (default: 120)
"""

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from playwright.async_api import Page

logger = logging.getLogger(__name__)


class CaptchaType(Enum):
    """Types of CAPTCHAs supported."""

    RECAPTCHA_V2 = "recaptcha_v2"
    RECAPTCHA_V3 = "recaptcha_v3"
    HCAPTCHA = "hcaptcha"
    TURNSTILE = "turnstile"
    UNKNOWN = "unknown"


@dataclass
class CaptchaInfo:
    """Information about a detected CAPTCHA."""

    captcha_type: CaptchaType
    sitekey: str
    page_url: str
    action: str | None = None  # For reCAPTCHA v3
    data_s: str | None = None  # For some reCAPTCHA implementations


class CaptchaSolverError(Exception):
    """Base exception for CAPTCHA solving errors."""

    pass


class CaptchaNotConfiguredError(CaptchaSolverError):
    """Raised when CAPTCHA solving is requested but not configured."""

    pass


class CaptchaNotInstalledError(CaptchaSolverError):
    """Raised when 2captcha-python is not installed."""

    pass


class CaptchaSolveTimeoutError(CaptchaSolverError):
    """Raised when CAPTCHA solving times out."""

    pass


def _is_captcha_available() -> bool:
    """Check if 2captcha-python is installed."""
    try:
        import twocaptcha  # noqa: F401

        return True
    except ImportError:
        return False


def _get_api_key() -> str | None:
    """Get CAPTCHA API key from environment."""
    return os.environ.get("CAPTCHA_API_KEY")


def _get_timeout() -> int:
    """Get CAPTCHA solving timeout from environment."""
    try:
        return int(os.environ.get("CAPTCHA_TIMEOUT", "120"))
    except ValueError:
        return 120


class CaptchaSolver:
    """CAPTCHA detection and solving service.

    IMPORTANT FOR LLM/AGENT USERS:
        This service requires external configuration to work.
        It will NOT work out of the box - you must:

        1. Install the captcha extra:
           pip install supacrawl[captcha]

        2. Set your API key:
           export CAPTCHA_API_KEY=your-2captcha-api-key

        3. Use the --solve-captcha flag:
           supacrawl scrape --stealth --solve-captcha URL

    COSTS:
        Each CAPTCHA solve costs approximately $0.002-0.003 (2-3 cents per 1000).
        Solves typically take 10-60 seconds.

    WHEN TO USE:
        - Only when a site requires CAPTCHA to access content
        - When stealth mode alone is not enough
        - For sites with mandatory human verification

    WHEN NOT TO USE:
        - For normal scraping (most sites don't need this)
        - If you can avoid the CAPTCHA by other means
        - If costs are a concern and volume is high
    """

    def __init__(self, api_key: str | None = None, timeout: int | None = None) -> None:
        """Initialise the CAPTCHA solver.

        Args:
            api_key: 2Captcha API key. If not provided, reads from CAPTCHA_API_KEY env var.
            timeout: Timeout in seconds for solving. Defaults to CAPTCHA_TIMEOUT env var or 120.
        """
        self.api_key = api_key or _get_api_key()
        self.timeout = timeout or _get_timeout()
        self._solver: object | None = None

    def _ensure_configured(self) -> None:
        """Ensure CAPTCHA solving is properly configured.

        Raises:
            CaptchaNotInstalledError: If 2captcha-python is not installed.
            CaptchaNotConfiguredError: If API key is not set.
        """
        if not _is_captcha_available():
            raise CaptchaNotInstalledError(
                "CAPTCHA solving requires the captcha extra. [HINT: Install with: pip install supacrawl[captcha]]"
            )

        if not self.api_key:
            raise CaptchaNotConfiguredError(
                "CAPTCHA API key not configured. [HINT: Set environment variable CAPTCHA_API_KEY=your-2captcha-api-key]"
            )

    def _get_solver(self) -> Any:
        """Get or create the 2Captcha solver instance."""
        if self._solver is None:
            self._ensure_configured()
            from twocaptcha import TwoCaptcha

            self._solver = TwoCaptcha(self.api_key)
        return self._solver

    async def detect_captcha(self, page: Page) -> CaptchaInfo | None:
        """Detect if a CAPTCHA is present on the page.

        Args:
            page: Playwright page to check.

        Returns:
            CaptchaInfo if a CAPTCHA is detected, None otherwise.
        """
        page_url = page.url
        html = await page.content()

        # Check for reCAPTCHA v2/v3
        recaptcha_info = await self._detect_recaptcha(page, html, page_url)
        if recaptcha_info:
            return recaptcha_info

        # Check for hCaptcha
        hcaptcha_info = await self._detect_hcaptcha(page, html, page_url)
        if hcaptcha_info:
            return hcaptcha_info

        # Check for Cloudflare Turnstile
        turnstile_info = await self._detect_turnstile(page, html, page_url)
        if turnstile_info:
            return turnstile_info

        return None

    async def _detect_recaptcha(self, page: Page, html: str, page_url: str) -> CaptchaInfo | None:
        """Detect reCAPTCHA v2 or v3."""
        # Look for reCAPTCHA iframe or script
        recaptcha_patterns = [
            r'data-sitekey="([^"]+)"',
            r"data-sitekey='([^']+)'",
            r'class="g-recaptcha"[^>]*data-sitekey="([^"]+)"',
            r"grecaptcha\.render\([^,]+,\s*\{[^}]*sitekey['\"]?\s*:\s*['\"]([^'\"]+)",
            r"grecaptcha\.execute\(['\"]([^'\"]+)['\"]",
        ]

        for pattern in recaptcha_patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                sitekey = match.group(1)

                # Determine if v2 or v3
                is_v3 = (
                    "grecaptcha.execute" in html
                    or 'data-size="invisible"' in html
                    or "recaptcha/api.js?render=" in html
                )

                # Try to extract action for v3
                action = None
                if is_v3:
                    action_match = re.search(r"action['\"]?\s*:\s*['\"]([^'\"]+)['\"]", html)
                    if action_match:
                        action = action_match.group(1)

                return CaptchaInfo(
                    captcha_type=CaptchaType.RECAPTCHA_V3 if is_v3 else CaptchaType.RECAPTCHA_V2,
                    sitekey=sitekey,
                    page_url=page_url,
                    action=action,
                )

        # Check for reCAPTCHA iframe
        recaptcha_iframe = await page.query_selector("iframe[src*='recaptcha']")
        if recaptcha_iframe:
            src = await recaptcha_iframe.get_attribute("src")
            if src:
                sitekey_match = re.search(r"k=([^&]+)", src)
                if sitekey_match:
                    return CaptchaInfo(
                        captcha_type=CaptchaType.RECAPTCHA_V2,
                        sitekey=sitekey_match.group(1),
                        page_url=page_url,
                    )

        return None

    async def _detect_hcaptcha(self, page: Page, html: str, page_url: str) -> CaptchaInfo | None:
        """Detect hCaptcha."""
        hcaptcha_patterns = [
            r'data-sitekey="([^"]+)"[^>]*class="h-captcha"',
            r'class="h-captcha"[^>]*data-sitekey="([^"]+)"',
            r"hcaptcha\.render\([^,]+,\s*\{[^}]*sitekey['\"]?\s*:\s*['\"]([^'\"]+)",
        ]

        for pattern in hcaptcha_patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                return CaptchaInfo(
                    captcha_type=CaptchaType.HCAPTCHA,
                    sitekey=match.group(1),
                    page_url=page_url,
                )

        # Check for hCaptcha iframe
        hcaptcha_iframe = await page.query_selector("iframe[src*='hcaptcha']")
        if hcaptcha_iframe:
            src = await hcaptcha_iframe.get_attribute("src")
            if src:
                sitekey_match = re.search(r"sitekey=([^&]+)", src)
                if sitekey_match:
                    return CaptchaInfo(
                        captcha_type=CaptchaType.HCAPTCHA,
                        sitekey=sitekey_match.group(1),
                        page_url=page_url,
                    )

        # Check for h-captcha div
        hcaptcha_div = await page.query_selector("div.h-captcha[data-sitekey]")
        if hcaptcha_div:
            sitekey = await hcaptcha_div.get_attribute("data-sitekey")
            if sitekey:
                return CaptchaInfo(
                    captcha_type=CaptchaType.HCAPTCHA,
                    sitekey=sitekey,
                    page_url=page_url,
                )

        return None

    async def _detect_turnstile(self, page: Page, html: str, page_url: str) -> CaptchaInfo | None:
        """Detect Cloudflare Turnstile."""
        turnstile_patterns = [
            r'data-sitekey="([^"]+)"[^>]*class="cf-turnstile"',
            r'class="cf-turnstile"[^>]*data-sitekey="([^"]+)"',
            r"turnstile\.render\([^,]+,\s*\{[^}]*sitekey['\"]?\s*:\s*['\"]([^'\"]+)",
        ]

        for pattern in turnstile_patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                return CaptchaInfo(
                    captcha_type=CaptchaType.TURNSTILE,
                    sitekey=match.group(1),
                    page_url=page_url,
                )

        # Check for cf-turnstile div
        turnstile_div = await page.query_selector("div.cf-turnstile[data-sitekey]")
        if turnstile_div:
            sitekey = await turnstile_div.get_attribute("data-sitekey")
            if sitekey:
                return CaptchaInfo(
                    captcha_type=CaptchaType.TURNSTILE,
                    sitekey=sitekey,
                    page_url=page_url,
                )

        return None

    async def solve(self, captcha_info: CaptchaInfo) -> str:
        """Solve a detected CAPTCHA.

        Args:
            captcha_info: Information about the CAPTCHA to solve.

        Returns:
            The solution token.

        Raises:
            CaptchaSolverError: If solving fails.
            CaptchaSolveTimeoutError: If solving times out.
        """
        self._ensure_configured()
        solver = self._get_solver()

        logger.info(f"Solving {captcha_info.captcha_type.value} CAPTCHA (sitekey: {captcha_info.sitekey[:20]}...)")

        try:
            # Run synchronous 2captcha code in executor
            loop = asyncio.get_event_loop()

            if captcha_info.captcha_type == CaptchaType.RECAPTCHA_V2:
                result = await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        lambda: solver.recaptcha(
                            sitekey=captcha_info.sitekey,
                            url=captcha_info.page_url,
                        ),
                    ),
                    timeout=self.timeout,
                )
            elif captcha_info.captcha_type == CaptchaType.RECAPTCHA_V3:
                result = await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        lambda: solver.recaptcha(
                            sitekey=captcha_info.sitekey,
                            url=captcha_info.page_url,
                            version="v3",
                            action=captcha_info.action or "verify",
                            score=0.9,
                        ),
                    ),
                    timeout=self.timeout,
                )
            elif captcha_info.captcha_type == CaptchaType.HCAPTCHA:
                result = await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        lambda: solver.hcaptcha(
                            sitekey=captcha_info.sitekey,
                            url=captcha_info.page_url,
                        ),
                    ),
                    timeout=self.timeout,
                )
            elif captcha_info.captcha_type == CaptchaType.TURNSTILE:
                result = await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        lambda: solver.turnstile(
                            sitekey=captcha_info.sitekey,
                            url=captcha_info.page_url,
                        ),
                    ),
                    timeout=self.timeout,
                )
            else:
                raise CaptchaSolverError(f"Unsupported CAPTCHA type: {captcha_info.captcha_type}")

            # Extract solution code from result
            if isinstance(result, dict):
                solution = result.get("code", str(result))
            else:
                solution = str(result)

            logger.info(f"CAPTCHA solved successfully (token: {solution[:20]}...)")
            return solution

        except asyncio.TimeoutError as err:
            raise CaptchaSolveTimeoutError(
                f"CAPTCHA solving timed out after {self.timeout} seconds. "
                "[HINT: Try increasing CAPTCHA_TIMEOUT environment variable]"
            ) from err
        except Exception as e:
            error_msg = str(e)
            if "ERROR_WRONG_USER_KEY" in error_msg or "ERROR_KEY_DOES_NOT_EXIST" in error_msg:
                raise CaptchaSolverError(
                    "Invalid CAPTCHA API key. [HINT: Check your CAPTCHA_API_KEY environment variable is correct]"
                ) from e
            elif "ERROR_ZERO_BALANCE" in error_msg:
                raise CaptchaSolverError(
                    "CAPTCHA solving account has zero balance. "
                    "[HINT: Top up your 2Captcha account at https://2captcha.com/]"
                ) from e
            elif "ERROR_NO_SLOT_AVAILABLE" in error_msg:
                raise CaptchaSolverError(
                    "No CAPTCHA solving slots available. [HINT: Try again in a few seconds]"
                ) from e
            else:
                raise CaptchaSolverError(f"CAPTCHA solving failed: {error_msg}") from e

    async def inject_solution(self, page: Page, captcha_info: CaptchaInfo, solution: str) -> None:
        """Inject the CAPTCHA solution into the page.

        Args:
            page: Playwright page.
            captcha_info: Information about the CAPTCHA.
            solution: The solution token.
        """
        if captcha_info.captcha_type in (
            CaptchaType.RECAPTCHA_V2,
            CaptchaType.RECAPTCHA_V3,
        ):
            # Inject into g-recaptcha-response textarea
            await page.evaluate(
                """(token) => {
                    // Find and fill the response textarea
                    const textarea = document.querySelector('textarea[name="g-recaptcha-response"]');
                    if (textarea) {
                        textarea.value = token;
                        textarea.style.display = 'block';
                    }

                    // Also try to set in any hidden input
                    const hiddenInputs = document.querySelectorAll('input[name="g-recaptcha-response"]');
                    hiddenInputs.forEach(input => { input.value = token; });

                    // Try to trigger callback if exists
                    if (typeof window.captchaCallback === 'function') {
                        window.captchaCallback(token);
                    }
                    if (typeof window.onRecaptchaSuccess === 'function') {
                        window.onRecaptchaSuccess(token);
                    }
                }""",
                solution,
            )

        elif captcha_info.captcha_type == CaptchaType.HCAPTCHA:
            # Inject into h-captcha-response textarea
            await page.evaluate(
                """(token) => {
                    const textarea = document.querySelector('textarea[name="h-captcha-response"]');
                    if (textarea) {
                        textarea.value = token;
                    }

                    const hiddenInputs = document.querySelectorAll('input[name="h-captcha-response"]');
                    hiddenInputs.forEach(input => { input.value = token; });

                    // Try hcaptcha callback
                    if (typeof hcaptcha !== 'undefined' && typeof hcaptcha.getResponse === 'function') {
                        // The callback is usually bound to the widget
                    }
                }""",
                solution,
            )

        elif captcha_info.captcha_type == CaptchaType.TURNSTILE:
            # Inject into cf-turnstile-response
            await page.evaluate(
                """(token) => {
                    const textarea = document.querySelector('textarea[name="cf-turnstile-response"]');
                    if (textarea) {
                        textarea.value = token;
                    }

                    const input = document.querySelector('input[name="cf-turnstile-response"]');
                    if (input) {
                        input.value = token;
                    }

                    // Turnstile uses a hidden input commonly
                    const hiddenInputs = document.querySelectorAll('[name*="turnstile"]');
                    hiddenInputs.forEach(el => {
                        if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
                            el.value = token;
                        }
                    });
                }""",
                solution,
            )

        logger.info(f"Injected {captcha_info.captcha_type.value} solution into page")

    async def detect_and_solve(self, page: Page) -> bool:
        """Detect and solve any CAPTCHA on the page.

        This is the main entry point for CAPTCHA solving.

        Args:
            page: Playwright page to check and solve.

        Returns:
            True if a CAPTCHA was detected and solved, False if no CAPTCHA found.

        Raises:
            CaptchaSolverError: If CAPTCHA solving fails.
        """
        captcha_info = await self.detect_captcha(page)

        if captcha_info is None:
            logger.debug("No CAPTCHA detected on page")
            return False

        logger.info(f"Detected {captcha_info.captcha_type.value} CAPTCHA")

        solution = await self.solve(captcha_info)
        await self.inject_solution(page, captcha_info, solution)

        # Give the page a moment to process the solution
        await asyncio.sleep(1)

        return True
