"""Unified LLM client supporting multiple providers."""

import json
import logging
from typing import Any

import httpx

from supacrawl.exceptions import ExtractionSchemaError, ProviderError, generate_correlation_id
from supacrawl.llm.config import LLMConfig
from supacrawl.llm.response import strip_reasoning_preamble, text_from_blocks
from supacrawl.llm.schema import schema_errors, schema_validator
from supacrawl.utils import log_with_correlation

LOGGER = logging.getLogger(__name__)

MAX_CONTENT_CHARS = 50_000
# The first reply plus one repair turn that is shown its schema errors.
SCHEMA_ATTEMPTS = 2


def clip_content(content: str) -> str:
    """Page content cut to what a prompt carries."""
    if len(content) <= MAX_CONTENT_CHARS:
        return content
    return content[:MAX_CONTENT_CHARS] + "\n\n[Content truncated...]"


def bound_words(text: str, max_words: int) -> str:
    """``text`` cut to ``max_words`` words, at the last sentence end when one falls in the kept half."""
    words = text.split()
    if len(words) <= max_words:
        return text
    clipped = " ".join(words[:max_words])
    end = max(clipped.rfind(mark) for mark in ".!?")
    return clipped[: end + 1] if end >= len(clipped) // 2 else clipped + "…"


class LLMClient:
    """
    Unified async LLM client that abstracts provider differences.

    Usage:
        from supacrawl.llm import LLMClient, load_llm_config

        config = load_llm_config()
        client = LLMClient(config)

        response = await client.chat([
            {"role": "user", "content": "Hello!"}
        ])
        await client.close()
    """

    def __init__(self, config: LLMConfig, timeout: float = 120.0) -> None:
        """
        Initialise LLM client.

        Args:
            config: LLM configuration.
            timeout: Request timeout in seconds.
        """
        self._config = config
        self._timeout = timeout
        self._http_client: httpx.AsyncClient | None = None
        self._ollama_client: Any | None = None

    async def _get_http_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client for cloud providers."""
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=self._timeout)
        return self._http_client

    async def _get_ollama_client(self) -> Any:
        """Get or create Ollama AsyncClient."""
        if self._ollama_client is None:
            from ollama import AsyncClient  # type: ignore[import-untyped]

            self._ollama_client = AsyncClient(
                host=self._config.base_url,
                timeout=self._timeout,
            )
        return self._ollama_client

    async def close(self) -> None:
        """Close HTTP client."""
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

    async def chat(
        self,
        messages: list[dict[str, str]],
        json_mode: bool = False,
        schema: dict[str, Any] | None = None,
        think: bool | None = None,
    ) -> str:
        """
        Send chat messages and return response content.

        Args:
            messages: List of message dicts with 'role' and 'content' keys.
            json_mode: If True, request JSON formatted output.
            schema: JSON schema for the provider's structured-output setting
                (Ollama ``format``, OpenAI ``response_format``); Anthropic has
                it from the prompt only.
            think: Ollama's switch for a reasoning model's thinking; None
                leaves the model's default.

        Returns:
            Assistant's response content, with any reasoning preamble or
            thinking block removed.

        Raises:
            ProviderError: If the request fails, or the reply carries no
                text content at all.
        """
        if self._config.provider == "ollama":
            content = await self._chat_ollama(messages, json_mode, schema, think)
        elif self._config.provider == "openai":
            content = await self._chat_openai(messages, json_mode, schema)
        elif self._config.provider == "anthropic":
            content = await self._chat_anthropic(messages, json_mode)
        else:
            raise ProviderError(
                f"Unsupported provider: {self._config.provider}",
                provider=self._config.provider,
            )

        # A reply we can find no text in is a failure, not an answer. Handing
        # back "" is what let a reasoning model's [thinking, text] turn read as
        # a model with nothing to say, silently, on every call.
        if not content:
            correlation_id = generate_correlation_id()
            log_with_correlation(
                LOGGER,
                logging.ERROR,
                "LLM reply carried no text content",
                correlation_id=correlation_id,
                provider=self._config.provider,
                model=self._config.model,
            )
            raise ProviderError(
                "LLM returned no text content",
                provider=self._config.provider,
                correlation_id=correlation_id,
                context={"model": self._config.model},
            )
        return content

    async def chat_json(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Send chat messages and parse JSON response.

        Args:
            messages: List of message dicts with 'role' and 'content' keys.
            schema: JSON schema the reply must conform to. It goes to the
                provider's structured-output setting and the reply is
                validated against it; a reply that breaks it is sent back once
                with its errors.

        Returns:
            Parsed JSON response as a dict, conforming to ``schema`` when given.

        Raises:
            ValidationError: ``schema`` is not valid JSON Schema for an object.
            ExtractionSchemaError: the repaired reply still breaks ``schema``.
            ProviderError: If request or JSON parsing fails.
        """
        if schema is None:
            return self._parse_json(await self.chat(messages, json_mode=True))

        validator = schema_validator(schema)
        turn = messages
        for _ in range(SCHEMA_ATTEMPTS):
            content = await self.chat(turn, json_mode=True, schema=schema)
            data = self._parse_json(content)
            errors = schema_errors(validator, data)
            if not errors:
                return data
            turn = [
                *messages,
                {"role": "assistant", "content": content},
                {
                    "role": "user",
                    "content": "That JSON does not conform to the schema:\n- "
                    + "\n- ".join(errors)
                    + "\nReply with the corrected JSON only.",
                },
            ]
        raise ExtractionSchemaError(errors)

    def _parse_json(self, content: str) -> dict[str, Any]:
        """Parse a JSON reply, falling back to a fenced code block inside it."""
        correlation_id = generate_correlation_id()
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            parsed = self._extract_json(content)
            if parsed is not None:
                return parsed

            log_with_correlation(
                LOGGER,
                logging.ERROR,
                "Failed to parse JSON response",
                correlation_id=correlation_id,
                content_preview=content[:200],
            )
            raise ProviderError(
                f"Failed to parse JSON from LLM response: {content[:200]}...",
                provider=self._config.provider,
                correlation_id=correlation_id,
                context={"content_preview": content[:200]},
            ) from None

    async def summarize(self, text: str, max_words: int, focus: str | None = None) -> str:
        """
        Summarise web page content in at most ``max_words`` words.

        The model is asked for the bound and its reply is cut to it, so an
        overrun never reaches the caller. A reasoning model is told not to
        think: on a whole page it outran the client timeout (atlas, 05/10/2026:
        120 s with thinking, 6 s without).

        Raises:
            ProviderError: If request fails.
        """
        instruction = f"Summarise the following web page in at most {max_words} words, keeping its key information."
        if focus:
            instruction += f" Focus on: {focus}."
        messages = [
            {"role": "system", "content": "You summarise web pages. Reply with the summary only, as plain text."},
            {"role": "user", "content": f"{instruction}\n\n{clip_content(text)}"},
        ]
        return bound_words(await self.chat(messages, think=False), max_words)

    async def check_health(self) -> bool:
        """
        Check if the LLM provider is accessible.

        Returns:
            True if provider is accessible, False otherwise.
        """
        try:
            if self._config.provider == "ollama":
                client = await self._get_ollama_client()
                response = await client.list()
                return isinstance(response, dict) and "models" in response
            elif self._config.provider == "openai":
                client = await self._get_http_client()
                response = await client.get(
                    f"{self._config.base_url}/v1/models",
                    headers={"Authorization": f"Bearer {self._config.api_key}"},
                )
                return response.status_code == 200
            elif self._config.provider == "anthropic":
                # Anthropic doesn't have a simple health check endpoint
                return bool(self._config.api_key)
            return False
        except Exception:
            return False

    async def _chat_ollama(
        self,
        messages: list[dict[str, str]],
        json_mode: bool,
        schema: dict[str, Any] | None,
        think: bool | None,
    ) -> str:
        """Call Ollama API."""
        correlation_id = generate_correlation_id()
        client = await self._get_ollama_client()

        try:
            log_with_correlation(
                LOGGER,
                logging.DEBUG,
                "Sending chat request to Ollama",
                correlation_id=correlation_id,
                model=self._config.model,
                message_count=len(messages),
                json_mode=json_mode,
            )

            kwargs: dict[str, Any] = {"model": self._config.model, "messages": messages}
            if json_mode:
                kwargs["format"] = schema or "json"
            if think is not None:
                kwargs["think"] = think

            response = await client.chat(**kwargs)
            content = strip_reasoning_preamble(response.message.content or "").strip()

            log_with_correlation(
                LOGGER,
                logging.DEBUG,
                "Ollama chat completed",
                correlation_id=correlation_id,
                model=self._config.model,
                response_length=len(content),
            )
            return content

        except Exception as exc:
            log_with_correlation(
                LOGGER,
                logging.ERROR,
                f"Ollama chat failed: {exc}",
                correlation_id=correlation_id,
                model=self._config.model,
                error=str(exc),
            )
            raise ProviderError(
                f"Ollama chat failed: {str(exc)}",
                provider="ollama",
                correlation_id=correlation_id,
                context={"model": self._config.model, "error": str(exc)},
            ) from exc

    async def _chat_openai(self, messages: list[dict[str, str]], json_mode: bool, schema: dict[str, Any] | None) -> str:
        """Call OpenAI API."""
        correlation_id = generate_correlation_id()
        client = await self._get_http_client()

        try:
            log_with_correlation(
                LOGGER,
                logging.DEBUG,
                "Calling OpenAI API",
                correlation_id=correlation_id,
                model=self._config.model,
            )

            request_body: dict[str, Any] = {
                "model": self._config.model,
                "messages": messages,
            }
            if schema:
                request_body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "extraction", "schema": schema},
                }
            elif json_mode:
                request_body["response_format"] = {"type": "json_object"}

            response = await client.post(
                f"{self._config.base_url}/v1/chat/completions",
                headers={"Authorization": f"Bearer {self._config.api_key}"},
                json=request_body,
            )
            response.raise_for_status()

            data = response.json()
            # The answer is the message content with any reasoning preamble
            # removed; `choices` is a list only because `n` may exceed 1, and
            # supacrawl never asks for more than the single completion.
            message = data["choices"][0]["message"]
            content = text_from_blocks(message.get("content"))
            return strip_reasoning_preamble(content).strip()

        except httpx.HTTPStatusError as exc:
            raise ProviderError(
                f"OpenAI API error: {exc.response.status_code}",
                provider="openai",
                correlation_id=correlation_id,
                context={"status_code": exc.response.status_code},
            ) from exc
        except Exception as exc:
            raise ProviderError(
                f"OpenAI request failed: {str(exc)}",
                provider="openai",
                correlation_id=correlation_id,
            ) from exc

    async def _chat_anthropic(self, messages: list[dict[str, str]], json_mode: bool) -> str:
        """Call Anthropic API."""
        correlation_id = generate_correlation_id()
        client = await self._get_http_client()

        try:
            log_with_correlation(
                LOGGER,
                logging.DEBUG,
                "Calling Anthropic API",
                correlation_id=correlation_id,
                model=self._config.model,
            )

            # Anthropic uses system message differently
            system_content = None
            api_messages = []
            for msg in messages:
                if msg["role"] == "system":
                    system_content = msg["content"]
                else:
                    api_messages.append(msg)

            request_body: dict[str, Any] = {
                "model": self._config.model,
                "max_tokens": 4096,
                "messages": api_messages,
            }
            if system_content:
                request_body["system"] = system_content

            # api_key is required for Anthropic
            assert self._config.api_key is not None
            response = await client.post(
                f"{self._config.base_url}/v1/messages",
                headers={
                    "x-api-key": self._config.api_key,
                    "anthropic-version": "2023-06-01",
                },
                json=request_body,
            )
            response.raise_for_status()

            data = response.json()
            # By shape, never by index: a reasoning model puts a thinking
            # block in front of the text, and that block carries no `text`.
            content = text_from_blocks(data.get("content"))
            return strip_reasoning_preamble(content).strip()

        except httpx.HTTPStatusError as exc:
            raise ProviderError(
                f"Anthropic API error: {exc.response.status_code}",
                provider="anthropic",
                correlation_id=correlation_id,
                context={"status_code": exc.response.status_code},
            ) from exc
        except Exception as exc:
            raise ProviderError(
                f"Anthropic request failed: {str(exc)}",
                provider="anthropic",
                correlation_id=correlation_id,
            ) from exc

    def _extract_json(self, content: str) -> dict[str, Any] | None:
        """
        Try to extract JSON from content that may contain markdown code blocks.

        Args:
            content: Response content that may contain JSON.

        Returns:
            Parsed dict if found, None otherwise.
        """
        content = content.strip()

        # Try direct parse first
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass

        # Try extracting from ```json code block
        if "```json" in content:
            start = content.find("```json") + 7
            end = content.find("```", start)
            if end > start:
                try:
                    return json.loads(content[start:end].strip())
                except json.JSONDecodeError:
                    pass

        # Try extracting from generic ``` code block
        if "```" in content:
            start = content.find("```") + 3
            newline = content.find("\n", start)
            if newline > start:
                start = newline + 1
            end = content.find("```", start)
            if end > start:
                try:
                    return json.loads(content[start:end].strip())
                except json.JSONDecodeError:
                    pass

        return None
