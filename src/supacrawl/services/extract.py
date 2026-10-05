"""
LLM-powered extraction service for supacrawl.

Extracts structured data from web pages using LLMs.
Provider configuration is via environment variables (SUPACRAWL_LLM_PROVIDER, SUPACRAWL_LLM_MODEL, etc).
"""

import json
import logging
from typing import TYPE_CHECKING, Any

from supacrawl.exceptions import ProviderError, generate_correlation_id
from supacrawl.llm import LLMClient, load_llm_config
from supacrawl.llm.client import clip_content
from supacrawl.llm.schema import schema_validator
from supacrawl.models import ExtractResult, ExtractResultItem
from supacrawl.utils import log_with_correlation

if TYPE_CHECKING:
    from supacrawl.services.scrape import ScrapeService

LOGGER = logging.getLogger(__name__)


class ExtractService:
    """
    LLM-powered structured data extraction from web pages.

    Requires LLM configuration via environment variables:
        SUPACRAWL_LLM_PROVIDER=ollama|openai|anthropic
        SUPACRAWL_LLM_MODEL=<model-name>
        OPENAI_API_KEY or ANTHROPIC_API_KEY  (for cloud providers)

    Example usage:
        >>> from supacrawl.services.scrape import ScrapeService
        >>> scrape_svc = ScrapeService()
        >>> extract_svc = ExtractService(scrape_service=scrape_svc)
        >>> result = await extract_svc.extract(
        ...     urls=["https://example.com/product"],
        ...     prompt="Extract the product name and price",
        ...     schema={"type": "object", "properties": {"name": {"type": "string"}}}
        ... )
    """

    def __init__(self, scrape_service: "ScrapeService") -> None:
        """
        Initialise extraction service.

        Args:
            scrape_service: ScrapeService for fetching page content.
        """
        self._scrape_service = scrape_service
        self._llm_client: LLMClient | None = None

    async def _get_llm_client(self) -> LLMClient:
        """Get or create LLM client."""
        if self._llm_client is None:
            config = load_llm_config()
            self._llm_client = LLMClient(config)
        return self._llm_client

    async def close(self) -> None:
        """Close LLM client."""
        if self._llm_client:
            await self._llm_client.close()
            self._llm_client = None

    async def __aenter__(self) -> "ExtractService":
        """Enter async context manager."""
        return self

    async def __aexit__(self, exc_type: type | None, exc_val: BaseException | None, exc_tb: object) -> bool:
        """Exit async context manager, ensuring cleanup."""
        await self.close()
        return False

    async def extract(
        self,
        urls: list[str],
        prompt: str | None = None,
        schema: dict[str, Any] | None = None,
        allow_external_links: bool = False,
    ) -> ExtractResult:
        """
        Extract structured data from URLs using LLM.

        Args:
            urls: URLs to extract data from.
            prompt: Custom extraction prompt.
            schema: JSON schema each URL's data must conform to.
            allow_external_links: Follow and extract from external links.

        Returns:
            ExtractResult with extracted data for each URL; a URL whose page
            could not be fetched, or whose output breaks ``schema``, is failed.

        Raises:
            ValidationError: ``schema`` is not valid JSON Schema for an object.
            LLMNotConfiguredError: If LLM environment variables are not set.
        """
        correlation_id = generate_correlation_id()
        results: list[ExtractResultItem] = []

        # Refuse a bad schema or a missing LLM before any page is fetched
        if schema is not None:
            schema_validator(schema)
        await self._get_llm_client()

        log_with_correlation(
            LOGGER,
            logging.DEBUG,
            f"Starting extraction for {len(urls)} URLs",
            correlation_id=correlation_id,
        )

        for url in urls:
            try:
                data = await self.extract_one(url, prompt, schema)
                results.append(ExtractResultItem(url=url, success=True, data=data))
            except Exception as e:
                log_with_correlation(
                    LOGGER,
                    logging.ERROR,
                    f"Extraction failed for {url}: {e}",
                    correlation_id=correlation_id,
                    error=str(e),
                )
                results.append(ExtractResultItem(url=url, success=False, error=str(e)))

        all_success = all(r.success for r in results)

        log_with_correlation(
            LOGGER,
            logging.INFO,
            f"Extraction completed: {sum(1 for r in results if r.success)}/{len(results)} successful",
            correlation_id=correlation_id,
        )

        return ExtractResult(success=all_success, data=results)

    async def extract_one(
        self,
        url: str,
        prompt: str | None = None,
        schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Extract from one URL.

        Raises:
            ValidationError: ``schema`` is not valid JSON Schema for an object.
            LLMNotConfiguredError: If LLM environment variables are not set.
            ProviderError: the page could not be fetched or had no content, or
                the model call failed.
            ExtractionSchemaError: the model's output breaks ``schema``.
        """
        # Both checks come before the fetch: neither can be fixed by fetching
        if schema is not None:
            schema_validator(schema)
        client = await self._get_llm_client()

        scrape_result = await self._scrape_service.scrape(
            url=url,
            formats=["markdown"],
            only_main_content=True,
        )
        if not scrape_result.success or not scrape_result.data:
            raise ProviderError(scrape_result.error or "Failed to scrape page", provider="scrape")

        content = scrape_result.data.markdown or ""
        if not content.strip():
            raise ProviderError("No content extracted from page", provider="scrape")

        messages = [
            {"role": "system", "content": self._build_system_prompt(schema)},
            {"role": "user", "content": self._build_user_prompt(content, prompt, schema)},
        ]
        return await client.chat_json(messages, schema=schema)

    def _build_system_prompt(self, schema: dict[str, Any] | None) -> str:
        """Build system prompt for extraction."""
        base = (
            "You are a data extraction assistant. Your task is to extract "
            "structured information from web page content. "
            "Always respond with valid JSON only, no explanations."
        )

        if schema:
            base += f"\n\nOutput must conform to this JSON schema:\n{json.dumps(schema, indent=2)}"

        return base

    def _build_user_prompt(
        self,
        content: str,
        prompt: str | None,
        schema: dict[str, Any] | None,
    ) -> str:
        """Build user prompt for extraction."""
        parts = []

        if prompt:
            parts.append(f"Extraction instructions: {prompt}")

        if schema:
            parts.append("Extract data according to the provided schema.")

        parts.append(f"\n\nWeb page content:\n\n{clip_content(content)}")

        return "\n".join(parts)
