"""MCP resources for Supacrawl server.

Provides discoverable resources for AI agents to understand available
capabilities, formats, and configuration.
"""

import json
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from supacrawl.services.registry import SupacrawlServices

# Supported output formats for scraping
SUPPORTED_FORMATS = {
    "markdown": {
        "description": "Clean markdown with resolved URLs. Best for text content.",
        "requires_llm": False,
        "default": True,
    },
    "html": {
        "description": "Cleaned HTML with boilerplate removed.",
        "requires_llm": False,
        "default": False,
    },
    "rawHtml": {
        "description": "Full unprocessed HTML source.",
        "requires_llm": False,
        "default": False,
    },
    "links": {
        "description": "All extracted links from the page.",
        "requires_llm": False,
        "default": False,
    },
    "images": {
        "description": "All image URLs from the page.",
        "requires_llm": False,
        "default": False,
    },
    "screenshot": {
        "description": "Base64-encoded PNG screenshot.",
        "requires_llm": False,
        "default": False,
    },
    "pdf": {
        "description": "Base64-encoded PDF document.",
        "requires_llm": False,
        "default": False,
    },
    "json": {
        "description": "LLM-extracted structured data. Requires json_schema or json_prompt.",
        "requires_llm": True,
        "default": False,
    },
    "branding": {
        "description": "Brand identity extraction (colours, fonts, logo).",
        "requires_llm": True,
        "default": False,
    },
    "summary": {
        "description": "LLM-generated 2-3 sentence summary.",
        "requires_llm": True,
        "default": False,
    },
}

# Page action types for scraping
ACTION_TYPES = {
    "wait": {
        "description": "Wait for time or element",
        "parameters": {
            "milliseconds": "Time to wait in ms (use this OR selector)",
            "selector": "CSS selector to wait for (use this OR milliseconds)",
        },
        "example": {"type": "wait", "milliseconds": 2000},
    },
    "click": {
        "description": "Click an element",
        "parameters": {
            "selector": "CSS selector of element to click (required)",
        },
        "example": {"type": "click", "selector": "button.submit"},
    },
    "type": {
        "description": "Type text into an input field",
        "parameters": {
            "selector": "CSS selector of input element (required)",
            "text": "Text to type (required)",
        },
        "example": {"type": "type", "selector": "input[name='search']", "text": "query"},
    },
    "scroll": {
        "description": "Scroll the page",
        "parameters": {
            "direction": "Scroll direction: up, down, left, right (required)",
        },
        "example": {"type": "scroll", "direction": "down"},
    },
    "screenshot": {
        "description": "Capture screenshot mid-workflow",
        "parameters": {
            "full_page": "Capture full page vs viewport (optional, default: true)",
        },
        "example": {"type": "screenshot", "full_page": True},
    },
    "press": {
        "description": "Press a keyboard key",
        "parameters": {
            "key": "Key to press, e.g., Enter, Tab, Escape (required)",
        },
        "example": {"type": "press", "key": "Enter"},
    },
    "executeJavascript": {
        "description": "Execute custom JavaScript",
        "parameters": {
            "script": "JavaScript code to execute (required)",
        },
        "example": {"type": "executeJavascript", "script": "window.scrollTo(0, 1000)"},
    },
}

# Search providers
SEARCH_PROVIDERS = {
    "brave": {
        "description": "Brave Search API (recommended) - requires BRAVE_API_KEY",
        "requires_api_key": True,
        "default": True,
        "sources": ["web", "images", "news"],
    },
    "duckduckgo": {
        "description": "DuckDuckGo search (deprecated) - free but unreliable due to bot detection",
        "requires_api_key": False,
        "default": False,
        "sources": ["web", "images", "news"],
    },
}

# LLM providers
LLM_PROVIDERS = {
    "ollama": {
        "description": "Local Ollama instance - free, private",
        "requires_api_key": False,
        "default": True,
        "env_vars": ["OLLAMA_HOST", "SUPACRAWL_LLM_MODEL"],
    },
    "openai": {
        "description": "OpenAI API - requires OPENAI_API_KEY",
        "requires_api_key": True,
        "default": False,
        "env_vars": ["OPENAI_API_KEY", "SUPACRAWL_LLM_MODEL"],
    },
    "anthropic": {
        "description": "Anthropic API - requires ANTHROPIC_API_KEY",
        "requires_api_key": True,
        "default": False,
        "env_vars": ["ANTHROPIC_API_KEY", "SUPACRAWL_LLM_MODEL"],
    },
}


async def get_formats_resource() -> str:
    """Get all supported output formats with descriptions."""
    return json.dumps(SUPPORTED_FORMATS, indent=2)


async def get_action_types_resource() -> str:
    """Get all supported page action types with examples."""
    return json.dumps(ACTION_TYPES, indent=2)


async def get_search_providers_resource() -> str:
    """Get available search providers and their requirements."""
    return json.dumps(SEARCH_PROVIDERS, indent=2)


async def get_llm_config_resource() -> str:
    """Get current LLM configuration status."""
    provider = os.getenv("SUPACRAWL_LLM_PROVIDER", "ollama")
    model = os.getenv("SUPACRAWL_LLM_MODEL", "llama3.2")

    config = {
        "provider": provider,
        "model": model,
        "configured": False,
        "available_providers": list(LLM_PROVIDERS.keys()),
    }

    # Check if provider is properly configured
    if provider == "ollama":
        config["configured"] = True
        config["host"] = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    elif provider == "openai":
        config["configured"] = bool(os.getenv("OPENAI_API_KEY"))
    elif provider == "anthropic":
        config["configured"] = bool(os.getenv("ANTHROPIC_API_KEY"))

    return json.dumps(config, indent=2)


async def get_capabilities_resource(api_client: "SupacrawlServices | None" = None) -> str:
    """Get overall server capabilities and status."""
    from supacrawl.mcp.config import settings

    capabilities = {
        "tools": {
            "scrape": "Single URL content extraction",
            "map": "URL discovery without content extraction",
            "crawl": "Multi-page website crawling",
            "search": "Web search with optional result scraping",
            "extract": "Scrape URLs and extract structured data with the server LLM, conforming to a JSON schema",
            "summary": "Scrape a URL and return a word-bounded summary from the server LLM",
            "health": "Check server health status",
        },
        "design_note": (
            "Agent tools are intentionally omitted. When using supacrawl via MCP, "
            "the controlling LLM (you) IS the agent - orchestrate the primitives above."
        ),
        "features": {
            "stealth_mode": settings.stealth,
            "proxy_configured": bool(settings.proxy),
            "caching_enabled": bool(settings.cache_dir),
            "captcha_solving": settings.solve_captcha,
            "locale": settings.locale,
            "timezone": settings.timezone,
        },
        "limits": {
            "timeout_ms": {"min": 1000, "max": 300000, "default": 30000},
            "crawl_limit": {"min": 1, "max": 1000, "default": 50},
            "search_limit": {"min": 1, "max": 100, "default": 5},
        },
        "search_provider": settings.search_provider,
    }

    # Add service status if api_client available
    if api_client:
        capabilities["services"] = api_client.get_service_status()

    return json.dumps(capabilities, indent=2)


def check_llm_available() -> tuple[bool, str]:
    """
    Check if LLM is configured and likely available.

    Returns:
        Tuple of (is_available, message)
    """
    provider = os.getenv("SUPACRAWL_LLM_PROVIDER", "ollama")

    if provider == "ollama":
        # Ollama is assumed available if no API key needed
        return True, "Ollama configured (ensure ollama is running)"
    elif provider == "openai":
        if os.getenv("OPENAI_API_KEY"):
            return True, "OpenAI configured"
        return False, "OpenAI requires OPENAI_API_KEY environment variable"
    elif provider == "anthropic":
        if os.getenv("ANTHROPIC_API_KEY"):
            return True, "Anthropic configured"
        return False, "Anthropic requires ANTHROPIC_API_KEY environment variable"
    else:
        return False, f"Unknown LLM provider: {provider}"


__all__ = [
    "SUPPORTED_FORMATS",
    "ACTION_TYPES",
    "SEARCH_PROVIDERS",
    "LLM_PROVIDERS",
    "get_formats_resource",
    "get_action_types_resource",
    "get_search_providers_resource",
    "get_llm_config_resource",
    "get_capabilities_resource",
    "check_llm_available",
]
