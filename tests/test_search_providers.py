"""Unit tests for Tavily, Serper, SerpAPI, and Exa response parsing.

Uses httpx MockTransport to intercept outbound HTTP calls so no real network
requests are made.  Mirrors the HTTP-mocking style already used in
test_search_service.py / test_provider_chain.py.
"""

import json
import os
from typing import ClassVar
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from supacrawl.exceptions import ProviderError
from supacrawl.models import SearchSourceType
from supacrawl.services.search.exa import ExaProvider
from supacrawl.services.search.serpapi import SerpAPIProvider
from supacrawl.services.search.serper import SerperProvider
from supacrawl.services.search.tavily import TavilyProvider

# Helpers

CORRELATION_ID = "test-1234"


def _mock_response(status: int, body: dict) -> httpx.Response:
    """Build a minimal httpx.Response with a JSON body."""
    content = json.dumps(body).encode()
    return httpx.Response(
        status,
        content=content,
        headers={"content-type": "application/json"},
        request=httpx.Request("POST", "https://example.invalid/"),
    )


def _mock_get_response(status: int, body: dict, url: str = "https://example.invalid/") -> httpx.Response:
    """Build a minimal httpx.Response for GET requests."""
    content = json.dumps(body).encode()
    return httpx.Response(
        status,
        content=content,
        headers={"content-type": "application/json"},
        request=httpx.Request("GET", url),
    )


# TavilyProvider


class TestTavilyProvider:
    """Tests for TavilyProvider response parsing and error handling."""

    SEARCH_RESPONSE: ClassVar = {
        "results": [
            {
                "url": "https://example.com/page",
                "title": "Example Page",
                "content": "A detailed description of the page.",
                "published_date": None,
            },
            {
                "url": "https://example.com/other",
                "title": "Other Page",
                "content": "Another description.",
                "published_date": None,
            },
        ]
    }

    NEWS_RESPONSE: ClassVar = {
        "results": [
            {
                "url": "https://news.example.com/story",
                "title": "Breaking Story",
                "content": "Story snippet here.",
                "published_date": "2026-06-13",
            }
        ]
    }

    @pytest.mark.asyncio
    async def test_search_web_field_mapping(self):
        """search_web maps url/title/content correctly to SearchResultItem."""
        provider = TavilyProvider(api_key="test-key")
        try:
            mock_client = AsyncMock()
            mock_client.post.return_value = _mock_response(200, self.SEARCH_RESPONSE)

            with patch.object(provider, "_get_client", return_value=mock_client):
                results = await provider.search_web("python", 5, CORRELATION_ID)

            assert len(results) == 2
            assert results[0].url == "https://example.com/page"
            assert results[0].title == "Example Page"
            assert results[0].description == "A detailed description of the page."
            assert results[0].source_type == SearchSourceType.WEB
            assert results[0].published_at is None
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_search_web_respects_limit(self):
        """search_web returns at most limit results even when the API returns more."""
        provider = TavilyProvider(api_key="test-key")
        try:
            mock_client = AsyncMock()
            mock_client.post.return_value = _mock_response(200, self.SEARCH_RESPONSE)

            with patch.object(provider, "_get_client", return_value=mock_client):
                results = await provider.search_web("python", 1, CORRELATION_ID)

            assert len(results) == 1
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_search_web_missing_optional_fields(self):
        """search_web handles items with missing optional fields gracefully."""
        body = {"results": [{"url": "https://example.com", "title": ""}]}
        provider = TavilyProvider(api_key="test-key")
        try:
            mock_client = AsyncMock()
            mock_client.post.return_value = _mock_response(200, body)

            with patch.object(provider, "_get_client", return_value=mock_client):
                results = await provider.search_web("python", 5, CORRELATION_ID)

            assert len(results) == 1
            assert results[0].description == ""
            assert results[0].published_at is None
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_search_news_sets_source_type_and_published_at(self):
        """search_news sets source_type=NEWS and maps published_date."""
        provider = TavilyProvider(api_key="test-key")
        try:
            mock_client = AsyncMock()
            mock_client.post.return_value = _mock_response(200, self.NEWS_RESPONSE)

            with patch.object(provider, "_get_client", return_value=mock_client):
                results = await provider.search_news("news query", 5, CORRELATION_ID)

            assert len(results) == 1
            assert results[0].source_type == SearchSourceType.NEWS
            assert results[0].published_at == "2026-06-13"
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_search_images_raises_not_implemented(self):
        """Tavily does not support image search."""
        provider = TavilyProvider(api_key="test-key")
        try:
            with pytest.raises(NotImplementedError):
                await provider.search_images("cats", 5, CORRELATION_ID)
        finally:
            await provider.close()

    def test_require_api_key_raises_when_absent(self):
        """_require_api_key raises ProviderError when no key is configured."""
        provider = TavilyProvider(api_key=None)
        with pytest.raises(ProviderError, match="Tavily API key not configured"):
            provider._require_api_key()

    def test_is_available_false_without_key(self):
        provider = TavilyProvider(api_key=None)
        assert not provider.is_available()

    def test_is_available_true_with_key(self):
        provider = TavilyProvider(api_key="some-key")
        assert provider.is_available()

    def test_reads_key_from_env(self):
        with patch.dict(os.environ, {"TAVILY_API_KEY": "env-key"}):
            provider = TavilyProvider()
        assert provider.is_available()


# SerperProvider


class TestSerperProvider:
    """Tests for SerperProvider response parsing and error handling."""

    WEB_RESPONSE: ClassVar = {
        "organic": [
            {"link": "https://example.com/1", "title": "Result One", "snippet": "First snippet."},
            {"link": "https://example.com/2", "title": "Result Two", "snippet": "Second snippet."},
        ]
    }

    IMAGE_RESPONSE: ClassVar = {
        "images": [
            {
                "imageUrl": "https://example.com/photo.jpg",
                "link": "https://example.com/page",
                "title": "A Photo",
                "source": "example.com",
                "thumbnailUrl": "https://example.com/thumb.jpg",
                "imageWidth": 1920,
                "imageHeight": 1080,
            }
        ]
    }

    NEWS_RESPONSE: ClassVar = {
        "news": [
            {
                "link": "https://news.example.com/story",
                "title": "Breaking News",
                "snippet": "The story so far.",
                "date": "2026-06-13T12:00:00Z",
                "source": "Example News",
            }
        ]
    }

    @pytest.mark.asyncio
    async def test_search_web_field_mapping(self):
        """search_web maps link/title/snippet to SearchResultItem."""
        provider = SerperProvider(api_key="test-key")
        try:
            mock_client = AsyncMock()
            mock_client.post.return_value = _mock_response(200, self.WEB_RESPONSE)

            with patch.object(provider, "_get_client", return_value=mock_client):
                results = await provider.search_web("query", 5, CORRELATION_ID)

            assert len(results) == 2
            assert results[0].url == "https://example.com/1"
            assert results[0].title == "Result One"
            assert results[0].description == "First snippet."
            assert results[0].source_type == SearchSourceType.WEB
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_search_images_field_mapping(self):
        """search_images maps imageUrl/thumbnail/dimensions correctly."""
        provider = SerperProvider(api_key="test-key")
        try:
            mock_client = AsyncMock()
            mock_client.post.return_value = _mock_response(200, self.IMAGE_RESPONSE)

            with patch.object(provider, "_get_client", return_value=mock_client):
                results = await provider.search_images("photo", 5, CORRELATION_ID)

            assert len(results) == 1
            item = results[0]
            assert item.url == "https://example.com/page"
            assert item.image_url == "https://example.com/photo.jpg"
            assert item.title == "A Photo"
            assert item.source_type == SearchSourceType.IMAGES
            assert item.thumbnail == "https://example.com/thumb.jpg"
            assert item.image_width == 1920
            assert item.image_height == 1080
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_search_news_field_mapping(self):
        """search_news maps link/snippet/date/source to SearchResultItem."""
        provider = SerperProvider(api_key="test-key")
        try:
            mock_client = AsyncMock()
            mock_client.post.return_value = _mock_response(200, self.NEWS_RESPONSE)

            with patch.object(provider, "_get_client", return_value=mock_client):
                results = await provider.search_news("news", 5, CORRELATION_ID)

            assert len(results) == 1
            item = results[0]
            assert item.url == "https://news.example.com/story"
            assert item.title == "Breaking News"
            assert item.description == "The story so far."
            assert item.source_type == SearchSourceType.NEWS
            assert item.published_at == "2026-06-13T12:00:00Z"
            assert item.source_name == "Example News"
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_search_web_missing_optional_fields(self):
        """search_web handles items missing optional fields."""
        body = {"organic": [{"link": "https://x.com", "title": "X"}]}
        provider = SerperProvider(api_key="test-key")
        try:
            mock_client = AsyncMock()
            mock_client.post.return_value = _mock_response(200, body)

            with patch.object(provider, "_get_client", return_value=mock_client):
                results = await provider.search_web("q", 5, CORRELATION_ID)

            assert results[0].description == ""
        finally:
            await provider.close()

    def test_require_api_key_raises_when_absent(self):
        provider = SerperProvider(api_key=None)
        with pytest.raises(ProviderError, match="Serper API key not configured"):
            provider._require_api_key()

    def test_is_available_false_without_key(self):
        provider = SerperProvider(api_key=None)
        assert not provider.is_available()

    def test_reads_key_from_env(self):
        with patch.dict(os.environ, {"SERPER_API_KEY": "env-key"}):
            provider = SerperProvider()
        assert provider.is_available()


# SerpAPIProvider


class TestSerpAPIProvider:
    """Tests for SerpAPIProvider response parsing and error handling."""

    WEB_RESPONSE: ClassVar = {
        "organic_results": [
            {"link": "https://example.com/1", "title": "SerpAPI Result", "snippet": "A snippet."},
        ]
    }

    IMAGE_RESPONSE: ClassVar = {
        "images_results": [
            {
                "original": "https://example.com/img.jpg",
                "link": "https://example.com/img-page",
                "title": "An Image",
                "source": "example.com",
                "thumbnail": "https://example.com/thumb.jpg",
                "original_width": 800,
                "original_height": 600,
            }
        ]
    }

    NEWS_RESPONSE: ClassVar = {
        "news_results": [
            {
                "link": "https://news.example.com/article",
                "title": "News Article",
                "snippet": "News snippet.",
                "date": "2026-06-13",
                "source": {"name": "News Source"},
            }
        ]
    }

    @pytest.mark.asyncio
    async def test_search_web_field_mapping(self):
        """search_web maps organic_results fields to SearchResultItem."""
        provider = SerpAPIProvider(api_key="test-key")
        try:
            mock_client = AsyncMock()
            mock_client.get.return_value = _mock_get_response(200, self.WEB_RESPONSE)

            with patch.object(provider, "_get_client", return_value=mock_client):
                results = await provider.search_web("query", 5, CORRELATION_ID)

            assert len(results) == 1
            assert results[0].url == "https://example.com/1"
            assert results[0].title == "SerpAPI Result"
            assert results[0].description == "A snippet."
            assert results[0].source_type == SearchSourceType.WEB
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_search_images_field_mapping(self):
        """search_images maps images_results including dimensions."""
        provider = SerpAPIProvider(api_key="test-key")
        try:
            mock_client = AsyncMock()
            mock_client.get.return_value = _mock_get_response(200, self.IMAGE_RESPONSE)

            with patch.object(provider, "_get_client", return_value=mock_client):
                results = await provider.search_images("img", 5, CORRELATION_ID)

            assert len(results) == 1
            item = results[0]
            assert item.url == "https://example.com/img-page"
            assert item.image_url == "https://example.com/img.jpg"
            assert item.source_type == SearchSourceType.IMAGES
            assert item.thumbnail == "https://example.com/thumb.jpg"
            assert item.image_width == 800
            assert item.image_height == 600
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_search_news_field_mapping(self):
        """search_news maps news_results including nested source name."""
        provider = SerpAPIProvider(api_key="test-key")
        try:
            mock_client = AsyncMock()
            mock_client.get.return_value = _mock_get_response(200, self.NEWS_RESPONSE)

            with patch.object(provider, "_get_client", return_value=mock_client):
                results = await provider.search_news("news", 5, CORRELATION_ID)

            assert len(results) == 1
            item = results[0]
            assert item.url == "https://news.example.com/article"
            assert item.source_type == SearchSourceType.NEWS
            assert item.published_at == "2026-06-13"
            assert item.source_name == "News Source"
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_search_news_missing_source_name(self):
        """search_news gracefully handles missing nested source name."""
        body = {"news_results": [{"link": "https://x.com", "title": "X", "snippet": "s", "source": {}}]}
        provider = SerpAPIProvider(api_key="test-key")
        try:
            mock_client = AsyncMock()
            mock_client.get.return_value = _mock_get_response(200, body)

            with patch.object(provider, "_get_client", return_value=mock_client):
                results = await provider.search_news("q", 5, CORRELATION_ID)

            assert results[0].source_name == ""
        finally:
            await provider.close()

    def test_require_api_key_raises_when_absent(self):
        provider = SerpAPIProvider(api_key=None)
        with pytest.raises(ProviderError, match="SerpAPI API key not configured"):
            provider._require_api_key()

    def test_is_available_false_without_key(self):
        provider = SerpAPIProvider(api_key=None)
        assert not provider.is_available()

    def test_reads_key_from_env(self):
        with patch.dict(os.environ, {"SERPAPI_API_KEY": "env-key"}):
            provider = SerpAPIProvider()
        assert provider.is_available()


# ExaProvider


class TestExaProvider:
    """Tests for ExaProvider response parsing and error handling."""

    WEB_RESPONSE: ClassVar = {
        "results": [
            {
                "url": "https://example.com/page",
                "title": "Exa Result",
                "text": "Body text extracted by Exa.",
                "publishedDate": "2026-05-01",
            },
            {
                "url": "https://example.com/other",
                "title": "Other",
                "snippet": "Fallback snippet.",
                "publishedDate": None,
            },
        ]
    }

    NEWS_RESPONSE: ClassVar = {
        "results": [
            {
                "url": "https://news.example.com/story",
                "title": "News Story",
                "text": "Story body.",
                "publishedDate": "2026-06-13",
            }
        ]
    }

    @pytest.mark.asyncio
    async def test_search_web_field_mapping(self):
        """search_web maps url/title/text to SearchResultItem."""
        provider = ExaProvider(api_key="test-key")
        try:
            mock_client = AsyncMock()
            mock_client.post.return_value = _mock_response(200, self.WEB_RESPONSE)

            with patch.object(provider, "_get_client", return_value=mock_client):
                results = await provider.search_web("query", 5, CORRELATION_ID)

            assert len(results) == 2
            assert results[0].url == "https://example.com/page"
            assert results[0].title == "Exa Result"
            assert results[0].description == "Body text extracted by Exa."
            assert results[0].source_type == SearchSourceType.WEB
            assert results[0].published_at == "2026-05-01"
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_search_web_falls_back_to_snippet_when_text_absent(self):
        """search_web uses snippet as description when text is absent."""
        provider = ExaProvider(api_key="test-key")
        try:
            mock_client = AsyncMock()
            mock_client.post.return_value = _mock_response(200, self.WEB_RESPONSE)

            with patch.object(provider, "_get_client", return_value=mock_client):
                results = await provider.search_web("query", 5, CORRELATION_ID)

            # Second result has no 'text', falls back to 'snippet'
            assert results[1].description == "Fallback snippet."
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_search_news_sets_source_type_news(self):
        """search_news sets source_type=NEWS and maps publishedDate."""
        provider = ExaProvider(api_key="test-key")
        try:
            mock_client = AsyncMock()
            mock_client.post.return_value = _mock_response(200, self.NEWS_RESPONSE)

            with patch.object(provider, "_get_client", return_value=mock_client):
                results = await provider.search_news("news", 5, CORRELATION_ID)

            assert len(results) == 1
            assert results[0].source_type == SearchSourceType.NEWS
            assert results[0].published_at == "2026-06-13"
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_search_images_raises_not_implemented(self):
        """Exa does not support image search."""
        provider = ExaProvider(api_key="test-key")
        try:
            with pytest.raises(NotImplementedError):
                await provider.search_images("dogs", 5, CORRELATION_ID)
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_search_web_missing_published_date(self):
        """search_web handles None publishedDate gracefully."""
        body = {"results": [{"url": "https://x.com", "title": "X", "publishedDate": None}]}
        provider = ExaProvider(api_key="test-key")
        try:
            mock_client = AsyncMock()
            mock_client.post.return_value = _mock_response(200, body)

            with patch.object(provider, "_get_client", return_value=mock_client):
                results = await provider.search_web("q", 5, CORRELATION_ID)

            assert results[0].published_at is None
        finally:
            await provider.close()

    def test_require_api_key_raises_when_absent(self):
        provider = ExaProvider(api_key=None)
        with pytest.raises(ProviderError, match="Exa API key not configured"):
            provider._require_api_key()

    def test_is_available_false_without_key(self):
        provider = ExaProvider(api_key=None)
        assert not provider.is_available()

    def test_reads_key_from_env(self):
        with patch.dict(os.environ, {"EXA_API_KEY": "env-key"}):
            provider = ExaProvider()
        assert provider.is_available()
