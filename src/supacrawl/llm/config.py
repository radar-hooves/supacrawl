"""LLM configuration management."""

import os
from dataclasses import dataclass
from typing import Literal

from supacrawl.exceptions import ConfigurationError, generate_correlation_id

type LLMProvider = Literal["ollama", "openai", "anthropic"]

DEFAULT_BASE_URLS: dict[LLMProvider, str] = {
    "ollama": "http://localhost:11434",
    "openai": "https://api.openai.com",
    "anthropic": "https://api.anthropic.com",
}


class LLMNotConfiguredError(ConfigurationError):
    """Raised when LLM features are used without proper configuration."""

    def __init__(self, correlation_id: str | None = None) -> None:
        """
        Initialise with helpful configuration guidance.

        Args:
            correlation_id: Optional correlation ID for tracing.
        """
        message = """LLM not configured. Set these environment variables:
  SUPACRAWL_LLM_PROVIDER=ollama|openai|anthropic
  SUPACRAWL_LLM_MODEL=<model-name>

For Ollama (local):
  SUPACRAWL_LLM_PROVIDER=ollama
  SUPACRAWL_LLM_MODEL=qwen3:8b
  OLLAMA_HOST=http://localhost:11434  (optional, this is the default)

For OpenAI, or any OpenAI-compatible gateway:
  SUPACRAWL_LLM_PROVIDER=openai
  SUPACRAWL_LLM_MODEL=gpt-4o-mini
  OPENAI_API_KEY=sk-...
  SUPACRAWL_LLM_BASE_URL=https://gateway.example.invalid  (optional; the gateway's URL)

For Anthropic:
  SUPACRAWL_LLM_PROVIDER=anthropic
  SUPACRAWL_LLM_MODEL=claude-sonnet-4-20250514
  ANTHROPIC_API_KEY=sk-ant-..."""
        super().__init__(message, correlation_id=correlation_id)


@dataclass(frozen=True)
class LLMConfig:
    """Immutable LLM configuration."""

    provider: LLMProvider
    model: str
    base_url: str
    api_key: str | None = None

    def __post_init__(self) -> None:
        """Validate configuration after initialisation."""
        if self.provider in ("openai", "anthropic") and not self.api_key:
            correlation_id = generate_correlation_id()
            env_var = "OPENAI_API_KEY" if self.provider == "openai" else "ANTHROPIC_API_KEY"
            raise ConfigurationError(
                f"{env_var} is required for {self.provider} provider",
                correlation_id=correlation_id,
                context={"provider": self.provider},
            )


def resolve_base_url(provider: LLMProvider) -> str:
    """The endpoint for ``provider``.

    ``SUPACRAWL_LLM_BASE_URL`` wins for every provider, so ``openai`` can point
    at any OpenAI-compatible gateway (LiteLLM, vLLM, a remote Ollama's
    ``/v1``) with that gateway's bearer key in ``OPENAI_API_KEY``. Below it,
    each provider's own convention: ``OLLAMA_HOST`` and ``OPENAI_BASE_URL``. A
    trailing ``/v1`` is dropped for ``openai``, since the client adds the
    versioned path itself and the OpenAI SDK's convention includes it.
    """
    base_url = os.getenv("SUPACRAWL_LLM_BASE_URL") or ""
    if not base_url and provider == "ollama":
        base_url = os.getenv("OLLAMA_HOST") or ""
    if not base_url and provider == "openai":
        base_url = os.getenv("OPENAI_BASE_URL") or ""
    base_url = (base_url or DEFAULT_BASE_URLS[provider]).rstrip("/")
    if provider == "openai":
        base_url = base_url.removesuffix("/v1")
    return base_url


def load_llm_config() -> LLMConfig:
    """
    Load LLM configuration from environment variables.

    Returns:
        LLMConfig with validated settings.

    Raises:
        LLMNotConfiguredError: If required environment variables are not set.
        ConfigurationError: If configuration is invalid.
    """
    correlation_id = generate_correlation_id()

    provider_str = os.getenv("SUPACRAWL_LLM_PROVIDER")
    model = os.getenv("SUPACRAWL_LLM_MODEL")

    if not provider_str or not model:
        raise LLMNotConfiguredError(correlation_id=correlation_id)

    provider_str = provider_str.lower()
    if provider_str not in ("ollama", "openai", "anthropic"):
        raise ConfigurationError(
            f"Invalid SUPACRAWL_LLM_PROVIDER: {provider_str}. Must be one of: ollama, openai, anthropic",
            correlation_id=correlation_id,
            context={"provider": provider_str},
        )

    provider: LLMProvider = provider_str  # type: ignore[assignment]

    base_url = resolve_base_url(provider)

    # API keys: Use provider-specific environment variables
    api_key: str | None = None
    if provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY")
    elif provider == "anthropic":
        api_key = os.getenv("ANTHROPIC_API_KEY")

    return LLMConfig(
        provider=provider,
        model=model,
        base_url=base_url,
        api_key=api_key,
    )


def is_llm_configured() -> bool:
    """
    Check if LLM is configured without raising an error.

    Returns:
        True if LLM configuration is valid, False otherwise.
    """
    try:
        load_llm_config()
        return True
    except LLMNotConfiguredError, ConfigurationError:
        return False
