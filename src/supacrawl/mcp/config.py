"""
Configuration for Supacrawl MCP server.

Uses Pydantic Settings for type-safe environment variable loading.

Security notes:
- ``SUPACRAWL_MCP_AUTH_TOKEN``: when set, the HTTP transport requires a bearer
  token on every inbound request. Omitting this env var leaves the HTTP surface
  unauthenticated — acceptable only on a loopback-bound, private network, or
  when the ``--insecure`` flag is explicitly passed to ``supacrawl-mcp``.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from mcp_common.config import BaseMCPSettings
from mcp_common.logging import setup_server_logging
from pydantic import Field, field_validator
from pydantic_settings import SettingsConfigDict

import supacrawl

load_dotenv()


class SupacrawlSettings(BaseMCPSettings):
    """Supacrawl MCP server settings.

    Extends the household base settings, which carry the fleet-wide broker
    fields (``PORTCULLIS_URL``, ``SIGNET_*``) the Portcullis consumer mixin
    reads, the shared ``ALLOWED_ORIGINS`` / ``ALLOWED_HOSTS`` /
    ``mask_error_details`` fields, and the lenient env source that lets a
    comma-separated list env var through.
    """

    model_config = SettingsConfigDict(
        env_prefix="SUPACRAWL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Logging
    log_level: str = Field(default="INFO", description="Logging level")

    # Browser settings
    timeout: int = Field(default=30000, ge=1000, le=300000, description="Page load timeout (ms)")
    headless: bool = Field(default=True, description="Run browser in headless mode")
    user_agent: str | None = Field(default=None, description="Custom user agent string")
    wait_until: str = Field(
        default="domcontentloaded",
        description="Page wait condition: domcontentloaded, load, or networkidle",
    )

    # Anti-bot protection
    engine: Literal["playwright", "patchright", "camoufox"] | None = Field(
        default=None,
        description=(
            "Browser engine to use. Options: playwright (default), patchright (stealth), "
            "camoufox (Akamai/Cloudflare bypass). When not set, falls back to patchright "
            "if stealth=True, else playwright. Per-request engine overrides this."
        ),
    )
    stealth: bool = Field(
        default=False,
        description="Enable enhanced stealth mode via Patchright (requires: pip install supacrawl[stealth])",
    )
    proxy: str | None = Field(
        default=None,
        description="Proxy URL (e.g., http://user:pass@host:port, socks5://host:port)",
    )

    # Locale settings (matches upstream supacrawl)
    locale: str = Field(
        default="en-US",
        description="Browser locale (e.g., en-AU, de-DE). Maps to Accept-Language header.",
    )
    timezone: str = Field(
        default="UTC",
        description="Browser timezone (e.g., Australia/Sydney, Europe/Berlin).",
    )

    # Caching
    cache_dir: str | None = Field(
        default=None,
        description="Cache directory for scraped content. Enables max_age caching when set.",
    )

    # CAPTCHA solving
    solve_captcha: bool = Field(
        default=False,
        description="Enable CAPTCHA solving via 2Captcha (requires: pip install supacrawl[captcha])",
    )
    # Note: Uses CAPTCHA_API_KEY and CAPTCHA_TIMEOUT (no prefix) to match upstream supacrawl
    captcha_api_key: str | None = Field(
        default=None,
        alias="CAPTCHA_API_KEY",
        description="2Captcha API key for CAPTCHA solving. WARNING: Each solve costs ~$0.002-0.003",
    )
    captcha_timeout: int = Field(
        default=120,
        ge=30,
        le=600,
        alias="CAPTCHA_TIMEOUT",
        description="CAPTCHA solving timeout in seconds (default: 120)",
    )

    # Search settings (LLM config is read directly from env by supacrawl)
    search_provider: Literal["duckduckgo", "brave"] = Field(
        default="brave",
        description=(
            "Legacy single search provider. Use SUPACRAWL_SEARCH_PROVIDERS for multi-provider fallback chains instead."
        ),
    )
    search_providers: str | None = Field(
        default=None,
        description=(
            "Comma-separated ordered list of search providers. "
            "Supacrawl tries each in order, falling back on failure. "
            "Supported: brave, tavily, serper, serpapi, exa, duckduckgo. "
            "Example: 'brave,tavily,serper,duckduckgo'"
        ),
    )
    search_rate_limit: float | None = Field(
        default=None,
        ge=0.1,
        le=100.0,
        description=(
            "Search requests per second. Overrides provider default "
            "(Brave: 10/s, DuckDuckGo: 1/s). Set to throttle API usage."
        ),
    )

    # The catalogue name of the SearXNG HTTP Basic credential to vend from the
    # Portcullis broker at server startup. Empty (the default) changes nothing:
    # the provider resolves its credential exactly as it always has, from
    # SEARXNG_USERNAME / SEARXNG_PASSWORD or any userinfo left in SEARXNG_URL.
    # Set it and the pair is fetched in-process instead, so the credential never
    # has to exist as an environment variable, a rendered config value, or a
    # file on the host running the server.
    searxng_portcullis_credential: str = Field(
        default="",
        alias="SEARXNG_PORTCULLIS_CREDENTIAL",
        description=(
            "Portcullis catalogue credential name carrying the SearXNG HTTP Basic "
            "username/password pair. Empty means no vend."
        ),
    )

    # ``package.module:callable``, a ``str -> str`` text filter every tool result
    # passes through. Unset changes nothing; unusable stops the server at start.
    output_filter: str | None = Field(
        default=None,
        description="Import path of a str -> str filter applied to every returned string (SUPACRAWL_OUTPUT_FILTER).",
    )

    # Bearer token for HTTP transport auth. No default — an empty/absent value
    # means the HTTP surface starts unauthenticated. The token is consumed at
    # startup only; it never appears in logs, errors, or tool responses.
    mcp_auth_token: str | None = Field(default=None)

    # MCP Server Configuration (without SUPACRAWL_ prefix). ALLOWED_ORIGINS and
    # ALLOWED_HOSTS are inherited from BaseMCPSettings; SERVICE_NAME stays here
    # because its default IS this server's identity.
    service_name: str = Field(default="supacrawl-mcp", alias="SERVICE_NAME")

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Ensure log level is valid."""
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in valid_levels:
            raise ValueError(f"Invalid log level: {v}. Must be one of {valid_levels}")
        return upper

    @field_validator("wait_until")
    @classmethod
    def validate_wait_until(cls, v: str) -> str:
        """Ensure wait_until is valid."""
        valid_values = {"domcontentloaded", "load", "networkidle"}
        lower = v.lower()
        if lower not in valid_values:
            raise ValueError(f"Invalid wait_until: {v}. Must be one of {valid_values}")
        return lower

    @field_validator("mcp_auth_token", mode="before")
    @classmethod
    def _reject_empty_token(cls, v: str | None) -> str | None:
        """Treat an all-whitespace token as absent so it cannot accidentally open auth."""
        if v is not None and not v.strip():
            return None
        return v

    def get_cache_path(self) -> Path | None:
        """Get cache directory as Path, expanding ~ if present."""
        if self.cache_dir:
            return Path(self.cache_dir).expanduser()
        return None

    def get_locale_config(self):
        """Get LocaleConfig from locale and timezone settings."""
        from supacrawl.models import LocaleConfig

        return LocaleConfig(
            language=self.locale,
            timezone=self.timezone,
        )


@lru_cache
def get_settings() -> SupacrawlSettings:
    """Get cached settings instance."""
    return SupacrawlSettings()


# Create settings instance
settings = get_settings()

# Export module-level constants for explicit imports
ALLOWED_ORIGINS = settings.allowed_origins
ALLOWED_HOSTS = settings.allowed_hosts
SERVICE_NAME = settings.service_name
SUPACRAWL_MASK_ERROR_DETAILS = settings.mask_error_details
SUPACRAWL_MCP_AUTH_TOKEN: str | None = settings.mcp_auth_token

# Setup logging using mcp-common structured JSON logging
# Version tracks the underlying supacrawl library for debugging clarity
SERVICE_VERSION = supacrawl.__version__

logger = setup_server_logging(SERVICE_NAME, SERVICE_VERSION)
