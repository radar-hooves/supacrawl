"""Shared test helpers; imported as ``tests.helpers`` (pytest ``pythonpath`` is the repo root)."""

import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from supacrawl.benchmark.models import CaseMetrics, CaseResult

_LLM_ENV_VARS = ("SUPACRAWL_LLM_PROVIDER", "SUPACRAWL_LLM_MODEL", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OLLAMA_HOST")


@dataclass
class LLMWire:
    """A scripted model endpoint at the httpx wire: ``replies`` answer in order, request bodies are kept."""

    replies: list[str] = field(default_factory=list)
    requests: list[dict[str, Any]] = field(default_factory=list)


def wire_llm(monkeypatch: pytest.MonkeyPatch, provider: str = "openai") -> LLMWire:
    """Configure ``provider`` and answer every HTTP call it makes from an ``LLMWire``."""
    for var in _LLM_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SUPACRAWL_LLM_PROVIDER", provider)
    monkeypatch.setenv("SUPACRAWL_LLM_MODEL", "test-model")
    if provider == "openai":
        monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")
    wire = LLMWire()

    def handler(request: httpx.Request) -> httpx.Response:
        wire.requests.append(json.loads(request.content))
        content = wire.replies.pop(0)
        if request.url.path == "/api/chat":
            return httpx.Response(
                200, json={"model": "test-model", "message": {"role": "assistant", "content": content}, "done": True}
            )
        assert request.url.path == "/v1/chat/completions", request.url
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}]})

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: real_client(*args, transport=transport, **kwargs))
    return wire


def make_case_result(
    case_id: str,
    *,
    quality: float | None = 80.0,
    success: bool = True,
    scored: bool = True,
    category: str = "static",
    latency_ms: float = 123.0,
    markdown_chars: int = 500,
    markdown_words: int = 100,
) -> CaseResult:
    """Build a synthetic CaseResult for benchmark tests.

    The defaults match the original ``_make_case_result`` helper in
    ``test_benchmark_store.py``.  Call sites that previously relied on the
    leaner ``_make_case`` helper in ``test_benchmark_runner.py`` (which used
    ``latency_ms=100.0`` and omitted ``markdown_chars``/``markdown_words``)
    must pass those values explicitly so that effective test inputs are
    unchanged.

    Args:
        case_id: Stable case identifier.
        quality: Composite quality score or None.
        success: Whether the scrape succeeded.
        scored: Whether the case contributes to the aggregate.
        category: Case category string.
        latency_ms: Simulated scrape latency in milliseconds.
        markdown_chars: Character count stored in the metrics.
        markdown_words: Word count stored in the metrics.

    Returns:
        A ``CaseResult`` populated with the requested data.
    """
    return CaseResult(
        case_id=case_id,
        category=category,
        url=f"https://example.com/{case_id}",
        difficulty=2,
        scored=scored,
        metrics=CaseMetrics(
            success=success,
            quality=quality,
            latency_ms=latency_ms,
            markdown_chars=markdown_chars,
            markdown_words=markdown_words,
        ),
    )
