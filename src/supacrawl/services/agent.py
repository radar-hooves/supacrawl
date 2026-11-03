"""
Autonomous web agent service for supacrawl.

Navigates the web autonomously to gather data based on a prompt.
Provider configuration is via environment variables (SUPACRAWL_LLM_PROVIDER, SUPACRAWL_LLM_MODEL, etc).
"""

import json
import logging
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from supacrawl.exceptions import generate_correlation_id
from supacrawl.llm import LLMClient, load_llm_config
from supacrawl.models import AgentEvent, AgentResult
from supacrawl.utils import log_with_correlation

if TYPE_CHECKING:
    from supacrawl.services.scrape import ScrapeService
    from supacrawl.services.search import SearchService

LOGGER = logging.getLogger(__name__)


class AgentService:
    """
    Autonomous web agent for complex data gathering.

    Combines search, scrape, and extraction to autonomously
    gather data based on natural language prompts.

    Requires LLM configuration via environment variables:
        SUPACRAWL_LLM_PROVIDER=ollama|openai|anthropic
        SUPACRAWL_LLM_MODEL=<model-name>
        OPENAI_API_KEY or ANTHROPIC_API_KEY  (for cloud providers)

    Example usage:
        >>> from supacrawl.services.scrape import ScrapeService
        >>> from supacrawl.services.search import SearchService
        >>> scrape_svc = ScrapeService()
        >>> search_svc = SearchService(scrape_service=scrape_svc)
        >>> agent = AgentService(scrape_service=scrape_svc, search_service=search_svc)
        >>> async for event in agent.run("Find AI startups founded in 2024"):
        ...     print(event.type, event.message)
    """

    def __init__(
        self,
        scrape_service: "ScrapeService",
        search_service: "SearchService",
    ) -> None:
        """
        Initialise agent service.

        Args:
            scrape_service: ScrapeService for page scraping.
            search_service: SearchService for web search.
        """
        self._scrape_service = scrape_service
        self._search_service = search_service
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

    async def __aenter__(self) -> "AgentService":
        """Enter async context manager."""
        return self

    async def __aexit__(self, exc_type: type | None, exc_val: BaseException | None, exc_tb: object) -> bool:
        """Exit async context manager, ensuring cleanup."""
        await self.close()
        return False

    async def run(
        self,
        prompt: str,
        urls: list[str] | None = None,
        schema: dict[str, Any] | None = None,
        max_steps: int = 10,
    ) -> AsyncGenerator[AgentEvent]:
        """
        Run autonomous agent with given objective.

        Args:
            prompt: Natural language description of data to gather.
            urls: Optional starting URLs to focus on.
            schema: Optional JSON schema for output structure.
            max_steps: Maximum number of steps (pages to visit).

        Yields:
            AgentEvent objects tracking progress.

        Raises:
            LLMNotConfiguredError: If LLM environment variables are not set.
        """
        correlation_id = generate_correlation_id()
        visited_urls: list[str] = []
        gathered_data: list[dict[str, Any]] = []
        step = 0

        # Get client early to fail fast if not configured
        client = await self._get_llm_client()

        log_with_correlation(
            LOGGER,
            logging.INFO,
            f"Agent starting: {prompt[:100]}...",
            correlation_id=correlation_id,
            max_steps=max_steps,
        )

        try:
            yield AgentEvent(type="thinking", message=f"Understanding objective: {prompt[:100]}...")

            # If no URLs provided, start with a search
            if not urls:
                yield AgentEvent(type="action", message="Searching the web for relevant information...")

                search_query = self._create_search_query(prompt)
                log_with_correlation(
                    LOGGER,
                    logging.DEBUG,
                    f"Searching: {search_query}",
                    correlation_id=correlation_id,
                )

                search_result = await self._search_service.search(
                    query=search_query,
                    limit=5,
                )

                if search_result.success and search_result.data:
                    urls = [r.url for r in search_result.data]
                    yield AgentEvent(
                        type="result",
                        message=f"Found {len(urls)} relevant pages",
                        data={"urls": urls},
                    )
                else:
                    yield AgentEvent(type="error", message="No relevant pages found")
                    return

            # Process each URL
            for url in urls:
                if step >= max_steps:
                    yield AgentEvent(
                        type="thinking",
                        message=f"Reached max steps ({max_steps}), stopping...",
                    )
                    break

                if url in visited_urls:
                    continue

                step += 1
                visited_urls.append(url)

                yield AgentEvent(type="action", message=f"Visiting: {url}", url=url)

                log_with_correlation(
                    LOGGER,
                    logging.DEBUG,
                    f"Scraping step {step}/{max_steps}: {url}",
                    correlation_id=correlation_id,
                )

                # Scrape the page
                scrape_result = await self._scrape_service.scrape(
                    url=url,
                    formats=["markdown"],
                    only_main_content=True,
                )

                if not scrape_result.success or not scrape_result.data:
                    yield AgentEvent(type="error", message=f"Failed to scrape: {url}")
                    continue

                content = scrape_result.data.markdown or ""
                if not content.strip():
                    continue

                # Extract relevant information
                yield AgentEvent(type="thinking", message="Analysing page content...")

                extracted = await self._extract_from_content(
                    content=content,
                    prompt=prompt,
                    schema=schema,
                    url=url,
                    correlation_id=correlation_id,
                    client=client,
                )

                if extracted:
                    gathered_data.append({"url": url, **extracted})
                    yield AgentEvent(
                        type="result",
                        message=f"Extracted data from {url}",
                        url=url,
                        data=extracted,
                    )

            # Compile final result
            if schema:
                final_data = await self._compile_results(gathered_data, prompt, schema, correlation_id, client)
            else:
                final_data = {"items": gathered_data}

            log_with_correlation(
                LOGGER,
                logging.INFO,
                f"Agent completed: visited {len(visited_urls)} pages",
                correlation_id=correlation_id,
            )

            yield AgentEvent(
                type="complete",
                message=f"Completed. Visited {len(visited_urls)} pages.",
                data=final_data,
            )

        except Exception as e:
            log_with_correlation(
                LOGGER,
                logging.ERROR,
                f"Agent error: {e}",
                correlation_id=correlation_id,
                error=str(e),
            )
            yield AgentEvent(type="error", message=str(e))

    async def run_sync(
        self,
        prompt: str,
        urls: list[str] | None = None,
        schema: dict[str, Any] | None = None,
        max_steps: int = 10,
    ) -> AgentResult:
        """
        Run agent and return final result.

        Convenience method that consumes all events and returns the final result.

        Args:
            prompt: Natural language description of data to gather.
            urls: Optional starting URLs to focus on.
            schema: Optional JSON schema for output structure.
            max_steps: Maximum number of steps (pages to visit).

        Returns:
            AgentResult with final data.
        """
        visited_urls: list[str] = []
        final_data: dict[str, Any] | None = None
        error: str | None = None

        async for event in self.run(prompt, urls, schema, max_steps):
            if event.url:
                visited_urls.append(event.url)
            if event.type == "complete" and event.data:
                final_data = event.data
            if event.type == "error":
                error = event.message

        if error:
            return AgentResult(
                success=False,
                urls_visited=visited_urls,
                error=error,
            )

        return AgentResult(
            success=True,
            data=final_data,
            urls_visited=visited_urls,
        )

    def _create_search_query(self, prompt: str) -> str:
        """Create search query from prompt."""
        # Simple extraction - remove common instruction words
        query = prompt.lower()
        for word in ["find", "get", "extract", "gather", "collect", "the", "all", "from"]:
            query = query.replace(word, " ")
        return " ".join(query.split())[:100]

    async def _extract_from_content(
        self,
        content: str,
        prompt: str,
        schema: dict[str, Any] | None,
        url: str,
        correlation_id: str,
        client: LLMClient,
    ) -> dict[str, Any] | None:
        """Extract relevant data from page content."""
        system_prompt = (
            "You are a data extraction assistant. Extract information relevant "
            "to the user's objective from the provided web page content. "
            "Return a JSON object with the extracted data. "
            "If no relevant information is found, return an empty object {}."
        )

        if schema:
            system_prompt += f"\n\nOutput must conform to this schema:\n{json.dumps(schema, indent=2)}"

        # Limit content to avoid context overflow
        max_content = 30000
        if len(content) > max_content:
            content = content[:max_content] + "\n\n[Content truncated...]"

        user_prompt = f"""
Objective: {prompt}

Source URL: {url}

Web page content:
{content}

Extract relevant information as JSON:
"""

        try:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
            return await client.chat_json(messages)
        except Exception as e:
            log_with_correlation(
                LOGGER,
                logging.WARNING,
                f"Extraction failed for {url}: {e}",
                correlation_id=correlation_id,
            )
            return None

    async def _compile_results(
        self,
        gathered_data: list[dict[str, Any]],
        prompt: str,
        schema: dict[str, Any],
        correlation_id: str,
        client: LLMClient,
    ) -> dict[str, Any]:
        """Compile gathered data into final structured result."""
        if not gathered_data:
            return {}

        system_prompt = (
            "You are a data compilation assistant. Combine the extracted data "
            "from multiple pages into a single structured result. "
            "Deduplicate and merge information as appropriate."
        )

        if schema:
            system_prompt += f"\n\nOutput must conform to this schema:\n{json.dumps(schema, indent=2)}"

        # Limit data size
        data_str = json.dumps(gathered_data, indent=2)
        if len(data_str) > 50000:
            data_str = data_str[:50000] + "\n...[truncated]"

        user_prompt = f"""
Original objective: {prompt}

Data gathered from {len(gathered_data)} pages:
{data_str}

Compile into a single structured result:
"""

        try:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
            return await client.chat_json(messages)
        except Exception:
            return {"items": gathered_data}
