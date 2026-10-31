"""Tests for LLM configuration loading."""

from dataclasses import FrozenInstanceError

import pytest

from supacrawl.exceptions import ConfigurationError
from supacrawl.llm import LLMConfig, LLMNotConfiguredError, is_llm_configured, load_llm_config


class TestLLMConfig:
    """Tests for LLMConfig dataclass."""

    def test_config_is_frozen(self) -> None:
        """Test that LLMConfig is immutable."""
        config = LLMConfig(
            provider="ollama",
            model="qwen3:8b",
            base_url="http://localhost:11434",
        )

        with pytest.raises(FrozenInstanceError):
            config.model = "other"  # type: ignore[misc]

    def test_config_with_api_key(self) -> None:
        """Test config with API key."""
        config = LLMConfig(
            provider="openai",
            model="gpt-4o-mini",
            base_url="https://api.openai.com",
            api_key="sk-test",
        )

        assert config.provider == "openai"
        assert config.model == "gpt-4o-mini"
        assert config.api_key == "sk-test"


class TestIsLLMConfigured:
    """Tests for is_llm_configured helper."""

    def test_returns_false_when_provider_not_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test returns False when SUPACRAWL_LLM_PROVIDER is not set."""
        monkeypatch.delenv("SUPACRAWL_LLM_PROVIDER", raising=False)
        monkeypatch.delenv("SUPACRAWL_LLM_MODEL", raising=False)

        assert is_llm_configured() is False

    def test_returns_false_when_model_not_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test returns False when SUPACRAWL_LLM_MODEL is not set."""
        monkeypatch.setenv("SUPACRAWL_LLM_PROVIDER", "ollama")
        monkeypatch.delenv("SUPACRAWL_LLM_MODEL", raising=False)

        assert is_llm_configured() is False

    def test_returns_true_when_both_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test returns True when both provider and model are set."""
        monkeypatch.setenv("SUPACRAWL_LLM_PROVIDER", "ollama")
        monkeypatch.setenv("SUPACRAWL_LLM_MODEL", "qwen3:8b")

        assert is_llm_configured() is True


class TestLoadLLMConfig:
    """Tests for load_llm_config function."""

    def test_raises_when_provider_not_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test raises LLMNotConfiguredError when provider not set."""
        monkeypatch.delenv("SUPACRAWL_LLM_PROVIDER", raising=False)
        monkeypatch.delenv("SUPACRAWL_LLM_MODEL", raising=False)

        with pytest.raises(LLMNotConfiguredError) as exc_info:
            load_llm_config()

        error = exc_info.value
        assert "LLM not configured" in error.message
        assert "SUPACRAWL_LLM_PROVIDER" in error.message

    def test_raises_when_model_not_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test raises LLMNotConfiguredError when model not set."""
        monkeypatch.setenv("SUPACRAWL_LLM_PROVIDER", "ollama")
        monkeypatch.delenv("SUPACRAWL_LLM_MODEL", raising=False)

        with pytest.raises(LLMNotConfiguredError) as exc_info:
            load_llm_config()

        error = exc_info.value
        assert "SUPACRAWL_LLM_MODEL" in error.message

    def test_raises_when_openai_api_key_not_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test raises ConfigurationError when OpenAI API key not set."""
        monkeypatch.setenv("SUPACRAWL_LLM_PROVIDER", "openai")
        monkeypatch.setenv("SUPACRAWL_LLM_MODEL", "gpt-4o-mini")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        with pytest.raises(ConfigurationError) as exc_info:
            load_llm_config()

        error = exc_info.value
        assert "OPENAI_API_KEY" in error.message

    def test_raises_when_anthropic_api_key_not_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test raises ConfigurationError when Anthropic API key not set."""
        monkeypatch.setenv("SUPACRAWL_LLM_PROVIDER", "anthropic")
        monkeypatch.setenv("SUPACRAWL_LLM_MODEL", "claude-sonnet-4-20250514")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        with pytest.raises(ConfigurationError) as exc_info:
            load_llm_config()

        error = exc_info.value
        assert "ANTHROPIC_API_KEY" in error.message

    def test_raises_for_invalid_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test raises ConfigurationError for unsupported provider."""
        monkeypatch.setenv("SUPACRAWL_LLM_PROVIDER", "unsupported")
        monkeypatch.setenv("SUPACRAWL_LLM_MODEL", "model")

        with pytest.raises(ConfigurationError) as exc_info:
            load_llm_config()

        error = exc_info.value
        assert "unsupported" in error.message.lower() or "invalid" in error.message.lower()

    def test_loads_ollama_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading Ollama configuration."""
        monkeypatch.setenv("SUPACRAWL_LLM_PROVIDER", "ollama")
        monkeypatch.setenv("SUPACRAWL_LLM_MODEL", "qwen3:8b")
        monkeypatch.delenv("OLLAMA_HOST", raising=False)

        config = load_llm_config()

        assert config.provider == "ollama"
        assert config.model == "qwen3:8b"
        assert config.base_url == "http://localhost:11434"
        assert config.api_key is None

    def test_loads_openai_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading OpenAI configuration."""
        monkeypatch.setenv("SUPACRAWL_LLM_PROVIDER", "openai")
        monkeypatch.setenv("SUPACRAWL_LLM_MODEL", "gpt-4o-mini")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")

        config = load_llm_config()

        assert config.provider == "openai"
        assert config.model == "gpt-4o-mini"
        assert config.base_url == "https://api.openai.com"
        assert config.api_key == "sk-test-key"

    def test_loads_anthropic_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading Anthropic configuration."""
        monkeypatch.setenv("SUPACRAWL_LLM_PROVIDER", "anthropic")
        monkeypatch.setenv("SUPACRAWL_LLM_MODEL", "claude-sonnet-4-20250514")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

        config = load_llm_config()

        assert config.provider == "anthropic"
        assert config.model == "claude-sonnet-4-20250514"
        assert config.base_url == "https://api.anthropic.com"
        assert config.api_key == "sk-ant-test"

    def test_ollama_host_overrides_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that OLLAMA_HOST overrides default Ollama URL."""
        monkeypatch.setenv("SUPACRAWL_LLM_PROVIDER", "ollama")
        monkeypatch.setenv("SUPACRAWL_LLM_MODEL", "qwen3:8b")
        monkeypatch.setenv("OLLAMA_HOST", "http://remote-ollama:11434")

        config = load_llm_config()

        assert config.base_url == "http://remote-ollama:11434"

    def test_openai_points_at_an_openai_compatible_gateway(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SUPACRAWL_LLM_BASE_URL wins for every provider; a trailing /v1 is the SDK's spelling and is dropped."""
        monkeypatch.setenv("SUPACRAWL_LLM_PROVIDER", "openai")
        monkeypatch.setenv("SUPACRAWL_LLM_MODEL", "atlas/fast")
        monkeypatch.setenv("OPENAI_API_KEY", "gateway-key")
        monkeypatch.setenv("SUPACRAWL_LLM_BASE_URL", "https://gateway.example.invalid/v1/")

        assert load_llm_config().base_url == "https://gateway.example.invalid"

        monkeypatch.delenv("SUPACRAWL_LLM_BASE_URL")
        monkeypatch.setenv("OPENAI_BASE_URL", "https://other.example.invalid/v1")
        assert load_llm_config().base_url == "https://other.example.invalid"

    def test_base_url_override_reaches_ollama_and_anthropic_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SUPACRAWL_LLM_MODEL", "m")
        monkeypatch.setenv("SUPACRAWL_LLM_BASE_URL", "http://llm.example.invalid:9000/")
        monkeypatch.setenv("OLLAMA_HOST", "http://ignored:11434")
        monkeypatch.setenv("SUPACRAWL_LLM_PROVIDER", "ollama")
        assert load_llm_config().base_url == "http://llm.example.invalid:9000"
        monkeypatch.setenv("SUPACRAWL_LLM_PROVIDER", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
        assert load_llm_config().base_url == "http://llm.example.invalid:9000"

    def test_error_message_includes_examples(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that error message includes helpful examples."""
        monkeypatch.delenv("SUPACRAWL_LLM_PROVIDER", raising=False)
        monkeypatch.delenv("SUPACRAWL_LLM_MODEL", raising=False)

        with pytest.raises(LLMNotConfiguredError) as exc_info:
            load_llm_config()

        error_message = exc_info.value.message
        # Should include example configurations
        assert "ollama" in error_message.lower()
        assert "openai" in error_message.lower()
        assert "anthropic" in error_message.lower()


class TestLLMNotConfiguredError:
    """Tests for LLMNotConfiguredError exception."""

    def test_has_correlation_id(self) -> None:
        """Test that error has correlation_id."""
        error = LLMNotConfiguredError()

        assert error.correlation_id is not None
        assert len(error.correlation_id) > 0

    def test_message_includes_guidance(self) -> None:
        """Test that message includes configuration guidance."""
        error = LLMNotConfiguredError()

        # Should include helpful configuration examples
        assert "LLM not configured" in error.message
        assert "SUPACRAWL_LLM_PROVIDER" in error.message
        assert "SUPACRAWL_LLM_MODEL" in error.message

    def test_str_representation(self) -> None:
        """Test string representation."""
        error = LLMNotConfiguredError()

        assert "LLM not configured" in str(error)
