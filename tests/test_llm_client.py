"""Tests for LLMClient."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from supacrawl.exceptions import ProviderError
from supacrawl.llm import LLMClient, LLMConfig


class TestLLMClient:
    """Tests for LLMClient."""

    @pytest.fixture
    def ollama_config(self) -> LLMConfig:
        """Create Ollama config for testing."""
        return LLMConfig(
            provider="ollama",
            model="qwen3:8b",
            base_url="http://localhost:11434",
        )

    @pytest.fixture
    def openai_config(self) -> LLMConfig:
        """Create OpenAI config for testing."""
        return LLMConfig(
            provider="openai",
            model="gpt-4o-mini",
            base_url="https://api.openai.com",
            api_key="sk-test",
        )

    @pytest.fixture
    def anthropic_config(self) -> LLMConfig:
        """Create Anthropic config for testing."""
        return LLMConfig(
            provider="anthropic",
            model="claude-sonnet-4-20250514",
            base_url="https://api.anthropic.com",
            api_key="sk-ant-test",
        )

    def test_client_init(self, ollama_config: LLMConfig) -> None:
        """Test client initialisation."""
        client = LLMClient(ollama_config)

        assert client._config == ollama_config
        assert client._http_client is None
        assert client._ollama_client is None

    @pytest.mark.asyncio
    async def test_close_closes_http_client(self, openai_config: LLMConfig) -> None:
        """Test that close() properly closes the HTTP client."""
        client = LLMClient(openai_config)

        # Create a mock HTTP client
        mock_http_client = AsyncMock()
        client._http_client = mock_http_client

        await client.close()

        mock_http_client.aclose.assert_called_once()
        assert client._http_client is None

    @pytest.mark.asyncio
    async def test_chat_raises_for_unsupported_provider(self) -> None:
        """Test that chat raises for unsupported provider."""
        # Create a config with invalid provider by bypassing validation
        config = LLMConfig(
            provider="unsupported",  # type: ignore[arg-type]
            model="model",
            base_url="http://localhost",
        )
        client = LLMClient(config)

        with pytest.raises(ProviderError) as exc_info:
            await client.chat([{"role": "user", "content": "test"}])

        assert "unsupported" in exc_info.value.message.lower()

    @pytest.mark.asyncio
    async def test_chat_json_parses_valid_json(self, ollama_config: LLMConfig) -> None:
        """Test that chat_json parses valid JSON responses."""
        client = LLMClient(ollama_config)

        # Mock the chat method
        with patch.object(client, "chat", return_value='{"key": "value"}'):
            result = await client.chat_json([{"role": "user", "content": "test"}])

        assert result == {"key": "value"}

    @pytest.mark.asyncio
    async def test_chat_json_extracts_from_code_block(self, ollama_config: LLMConfig) -> None:
        """Test that chat_json extracts JSON from markdown code blocks."""
        client = LLMClient(ollama_config)

        # Mock the chat method to return JSON in code block
        with patch.object(client, "chat", return_value='```json\n{"key": "value"}\n```'):
            result = await client.chat_json([{"role": "user", "content": "test"}])

        assert result == {"key": "value"}

    @pytest.mark.asyncio
    async def test_chat_json_raises_on_invalid_json(self, ollama_config: LLMConfig) -> None:
        """Test that chat_json raises on unparseable response."""
        client = LLMClient(ollama_config)

        # Mock the chat method to return invalid JSON
        with patch.object(client, "chat", return_value="not valid json at all"):
            with pytest.raises(ProviderError) as exc_info:
                await client.chat_json([{"role": "user", "content": "test"}])

        assert "JSON" in exc_info.value.message

    @pytest.mark.asyncio
    async def test_summarize_puts_the_text_and_bound_in_the_prompt(self, ollama_config: LLMConfig) -> None:
        """summarize sends the text and its word bound, and returns the reply."""
        client = LLMClient(ollama_config)

        with patch.object(client, "chat", return_value="Summary text") as mock_chat:
            result = await client.summarize("Long text to summarize", 100, focus="pricing")

        assert result == "Summary text"
        prompt = mock_chat.call_args[0][0][-1]["content"]
        assert "Long text to summarize" in prompt
        assert "100 words" in prompt
        assert "pricing" in prompt

    @pytest.mark.asyncio
    async def test_summarize_cuts_an_overrun_to_the_bound(self, ollama_config: LLMConfig) -> None:
        """A reply longer than the bound is cut at its last sentence end inside it."""
        client = LLMClient(ollama_config)

        with patch.object(client, "chat", return_value="One two three. Four five six seven eight."):
            result = await client.summarize("Text", 5)

        assert result == "One two three."

    def test_bound_words_cuts_after_a_sentence_ending_word(self) -> None:
        """A decimal is not a sentence end, and a closing quote stays with its sentence."""
        from supacrawl.llm.client import bound_words

        assert bound_words('Version 2.5 shipped. He said "it works." Then more text follows here', 8) == (
            'Version 2.5 shipped. He said "it works."'
        )
        assert bound_words("no sentence ends anywhere in this text", 4) == "no sentence ends anywhere\u2026"

    def test_extract_json_from_plain_json(self, ollama_config: LLMConfig) -> None:
        """Test extracting plain JSON."""
        client = LLMClient(ollama_config)

        result = client._extract_json('{"key": "value"}')

        assert result == {"key": "value"}

    def test_extract_json_from_json_code_block(self, ollama_config: LLMConfig) -> None:
        """Test extracting JSON from ```json block."""
        client = LLMClient(ollama_config)

        content = '```json\n{"key": "value"}\n```'
        result = client._extract_json(content)

        assert result == {"key": "value"}

    def test_extract_json_from_generic_code_block(self, ollama_config: LLMConfig) -> None:
        """Test extracting JSON from generic ``` block."""
        client = LLMClient(ollama_config)

        content = '```\n{"key": "value"}\n```'
        result = client._extract_json(content)

        assert result == {"key": "value"}

    def test_extract_json_returns_none_for_invalid(self, ollama_config: LLMConfig) -> None:
        """Test that _extract_json returns None for invalid content."""
        client = LLMClient(ollama_config)

        result = client._extract_json("not json at all")

        assert result is None


class TestLLMClientOllama:
    """Tests for Ollama-specific LLMClient methods."""

    @pytest.fixture
    def config(self) -> LLMConfig:
        """Create Ollama config."""
        return LLMConfig(
            provider="ollama",
            model="qwen3:8b",
            base_url="http://localhost:11434",
        )

    @pytest.mark.asyncio
    async def test_chat_ollama_success(self, config: LLMConfig) -> None:
        """Test successful Ollama chat call."""
        client = LLMClient(config)

        # Mock the Ollama client
        mock_response = MagicMock()
        mock_response.message.content = "Response text"

        mock_ollama = AsyncMock()
        mock_ollama.chat.return_value = mock_response

        with patch.object(client, "_get_ollama_client", return_value=mock_ollama):
            result = await client.chat([{"role": "user", "content": "Hello"}])

        assert result == "Response text"
        mock_ollama.chat.assert_called_once()

    @pytest.mark.asyncio
    async def test_chat_ollama_json_mode(self, config: LLMConfig) -> None:
        """Test Ollama chat with JSON mode enabled."""
        client = LLMClient(config)

        mock_response = MagicMock()
        mock_response.message.content = '{"result": true}'

        mock_ollama = AsyncMock()
        mock_ollama.chat.return_value = mock_response

        with patch.object(client, "_get_ollama_client", return_value=mock_ollama):
            await client.chat([{"role": "user", "content": "Return JSON"}], json_mode=True)

        # Verify format="json" was passed
        call_kwargs = mock_ollama.chat.call_args[1]
        assert call_kwargs.get("format") == "json"


class TestLLMClientOpenAI:
    """Tests for OpenAI-specific LLMClient methods."""

    @pytest.fixture
    def config(self) -> LLMConfig:
        """Create OpenAI config."""
        return LLMConfig(
            provider="openai",
            model="gpt-4o-mini",
            base_url="https://api.openai.com",
            api_key="sk-test",
        )

    @pytest.mark.asyncio
    async def test_chat_openai_success(self, config: LLMConfig) -> None:
        """Test successful OpenAI chat call."""
        client = LLMClient(config)

        mock_response = MagicMock()
        mock_response.json.return_value = {"choices": [{"message": {"content": "OpenAI response"}}]}
        mock_response.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response

        with patch.object(client, "_get_http_client", return_value=mock_http):
            result = await client.chat([{"role": "user", "content": "Hello"}])

        assert result == "OpenAI response"

    @pytest.mark.asyncio
    async def test_chat_openai_json_mode(self, config: LLMConfig) -> None:
        """Test OpenAI chat with JSON mode enabled."""
        client = LLMClient(config)

        mock_response = MagicMock()
        mock_response.json.return_value = {"choices": [{"message": {"content": '{"result": true}'}}]}
        mock_response.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response

        with patch.object(client, "_get_http_client", return_value=mock_http):
            await client.chat([{"role": "user", "content": "Return JSON"}], json_mode=True)

        # Verify response_format was passed
        call_kwargs = mock_http.post.call_args[1]
        request_body = call_kwargs.get("json", {})
        assert request_body.get("response_format") == {"type": "json_object"}


class TestLLMClientAnthropic:
    """Tests for Anthropic-specific LLMClient methods."""

    @pytest.fixture
    def config(self) -> LLMConfig:
        """Create Anthropic config."""
        return LLMConfig(
            provider="anthropic",
            model="claude-sonnet-4-20250514",
            base_url="https://api.anthropic.com",
            api_key="sk-ant-test",
        )

    @pytest.mark.asyncio
    async def test_chat_anthropic_success(self, config: LLMConfig) -> None:
        """Test successful Anthropic chat call."""
        client = LLMClient(config)

        mock_response = MagicMock()
        mock_response.json.return_value = {"content": [{"text": "Anthropic response"}]}
        mock_response.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response

        with patch.object(client, "_get_http_client", return_value=mock_http):
            result = await client.chat([{"role": "user", "content": "Hello"}])

        assert result == "Anthropic response"

    @pytest.mark.asyncio
    async def test_chat_anthropic_extracts_system_message(self, config: LLMConfig) -> None:
        """Test that Anthropic chat extracts system message."""
        client = LLMClient(config)

        mock_response = MagicMock()
        mock_response.json.return_value = {"content": [{"text": "Response"}]}
        mock_response.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response

        messages = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "Hello"},
        ]

        with patch.object(client, "_get_http_client", return_value=mock_http):
            await client.chat(messages)

        # Verify system was extracted to separate field
        call_kwargs = mock_http.post.call_args[1]
        request_body = call_kwargs.get("json", {})
        assert request_body.get("system") == "You are helpful"
        # User message should still be in messages array
        assert len(request_body.get("messages", [])) == 1
        assert request_body["messages"][0]["role"] == "user"


class TestLLMClientReasoningReplies:
    """A reasoning model's reply must not read as an empty answer.

    `mimir/deep` became a reasoning model on 29/08/2026: the Anthropic
    surface started returning `[thinking, text]` and the OpenAI surface a
    string with a leading `<think>` preamble. Reading `content[0].text`
    yielded "" with no exception raised — six days of silent failure in a
    sibling app before anyone noticed.
    """

    @pytest.fixture
    def openai_config(self) -> LLMConfig:
        """Create OpenAI config."""
        return LLMConfig(
            provider="openai",
            model="mimir/deep",
            base_url="http://gateway.local",
            api_key="sk-test",
        )

    @pytest.fixture
    def anthropic_config(self) -> LLMConfig:
        """Create Anthropic config."""
        return LLMConfig(
            provider="anthropic",
            model="mimir/deep",
            base_url="http://gateway.local",
            api_key="sk-ant-test",
        )

    @staticmethod
    def _http(payload: dict) -> AsyncMock:
        """Mock an HTTP client whose POST returns `payload` as JSON."""
        mock_response = MagicMock()
        mock_response.json.return_value = payload
        mock_response.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response
        return mock_http

    @pytest.mark.asyncio
    async def test_anthropic_thinking_block_before_text(self, anthropic_config: LLMConfig) -> None:
        """The answer sits in content[1]; content[0] carries no text at all."""
        client = LLMClient(anthropic_config)
        mock_http = self._http(
            {
                "content": [
                    {"type": "thinking", "thinking": "working it out", "signature": "s"},
                    {"type": "text", "text": '{"title": "Real answer"}'},
                ]
            }
        )

        with patch.object(client, "_get_http_client", return_value=mock_http):
            result = await client.chat([{"role": "user", "content": "Hello"}])

        assert result == '{"title": "Real answer"}'

    @pytest.mark.asyncio
    async def test_anthropic_thinking_only_reply_raises(self, anthropic_config: LLMConfig) -> None:
        """No text anywhere is a failure to surface, never an empty answer."""
        client = LLMClient(anthropic_config)
        mock_http = self._http({"content": [{"type": "thinking", "thinking": "..."}]})

        with patch.object(client, "_get_http_client", return_value=mock_http):
            with pytest.raises(ProviderError) as exc_info:
                await client.chat([{"role": "user", "content": "Hello"}])

        assert "no text content" in exc_info.value.message.lower()

    @pytest.mark.asyncio
    async def test_openai_think_preamble_is_stripped(self, openai_config: LLMConfig) -> None:
        """json.loads over the raw reply would raise on the preamble."""
        client = LLMClient(openai_config)
        mock_http = self._http(
            {
                "choices": [
                    {
                        "message": {
                            "content": '<think>weighing it up</think>\n{"result": true}',
                            "reasoning_content": "weighing it up",
                        }
                    }
                ]
            }
        )

        with patch.object(client, "_get_http_client", return_value=mock_http):
            result = await client.chat_json([{"role": "user", "content": "Return JSON"}])

        assert result == {"result": True}

    @pytest.mark.asyncio
    async def test_openai_null_content_raises_rather_than_returning_empty(self, openai_config: LLMConfig) -> None:
        """A tool-calls-only completion carries `content: null`.

        Reading it must not hand back "" — the silent-empty failure this whole
        module exists to prevent — and the error should name what went wrong.
        """
        client = LLMClient(openai_config)
        mock_http = self._http({"choices": [{"message": {"content": None, "tool_calls": []}}]})

        with patch.object(client, "_get_http_client", return_value=mock_http):
            with pytest.raises(ProviderError) as exc_info:
                await client.chat([{"role": "user", "content": "Hello"}])

        assert "no text content" in exc_info.value.message.lower()

    @pytest.mark.asyncio
    async def test_openai_block_list_content(self, openai_config: LLMConfig) -> None:
        """Some gateways return the OpenAI message content as a block list."""
        client = LLMClient(openai_config)
        mock_http = self._http(
            {
                "choices": [
                    {
                        "message": {
                            "content": [
                                {"type": "thinking", "thinking": "..."},
                                {"type": "text", "text": "Real answer"},
                            ]
                        }
                    }
                ]
            }
        )

        with patch.object(client, "_get_http_client", return_value=mock_http):
            result = await client.chat([{"role": "user", "content": "Hello"}])

        assert result == "Real answer"

    @pytest.mark.asyncio
    async def test_ollama_think_preamble_is_stripped(self) -> None:
        """Ollama returns the preamble inline in message.content."""
        config = LLMConfig(provider="ollama", model="mimir/deep", base_url="http://localhost:11434")
        client = LLMClient(config)

        mock_message = MagicMock()
        mock_message.content = '<think>reasoning</think>\n{"result": true}'
        mock_response = MagicMock()
        mock_response.message = mock_message

        mock_ollama = AsyncMock()
        mock_ollama.chat.return_value = mock_response

        with patch.object(client, "_get_ollama_client", return_value=mock_ollama):
            result = await client.chat_json([{"role": "user", "content": "Return JSON"}])

        assert result == {"result": True}
