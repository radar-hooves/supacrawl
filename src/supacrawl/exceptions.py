"""Custom exceptions for supacrawl with context support."""

import uuid
from typing import Any


def generate_correlation_id() -> str:
    """
    Generate an 8-character UUID-based correlation ID.

    Returns:
        8-character correlation ID string.
    """
    return str(uuid.uuid4())[:8]


class SupacrawlError(Exception):
    """Base exception for supacrawl with context and correlation ID support."""

    def __init__(
        self,
        message: str,
        correlation_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        """
        Initialise exception with message, correlation ID, and context.

        Args:
            message: Error message.
            correlation_id: Optional correlation ID. If None, generates a new one.
            context: Optional context dictionary for debugging.
        """
        self.message = message
        self.correlation_id = correlation_id or generate_correlation_id()
        self.context = context or {}
        super().__init__(f"{message} [correlation_id={self.correlation_id}]")


class ValidationError(SupacrawlError):
    """Raised when input validation fails."""

    def __init__(
        self,
        message: str,
        field: str | None = None,
        value: Any = None,
        correlation_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        """
        Initialise validation error with field and value context.

        Args:
            message: Error message.
            field: Optional field name that failed validation.
            value: Optional value that failed validation.
            correlation_id: Optional correlation ID. If None, generates a new one.
            context: Optional context dictionary for debugging.
        """
        if context is None:
            context = {}
        if field is not None:
            context["field"] = field
        if value is not None:
            context["value"] = str(value)
        super().__init__(message, correlation_id=correlation_id, context=context)


class ConfigurationError(SupacrawlError):
    """Raised when configuration loading or validation fails."""

    def __init__(
        self,
        message: str,
        config_path: str | None = None,
        correlation_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        """
        Initialise configuration error with path context.

        Args:
            message: Error message.
            config_path: Optional path to the configuration file.
            correlation_id: Optional correlation ID. If None, generates a new one.
            context: Optional context dictionary for debugging.
        """
        if context is None:
            context = {}
        if config_path is not None:
            context["config_path"] = str(config_path)
        super().__init__(message, correlation_id=correlation_id, context=context)


class FileNotFoundError(SupacrawlError):
    """Raised when a required file is not found."""

    def __init__(
        self,
        message: str,
        file_path: str | None = None,
        correlation_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        """
        Initialise file not found error with path context.

        Args:
            message: Error message.
            file_path: Optional path to the missing file.
            correlation_id: Optional correlation ID. If None, generates a new one.
            context: Optional context dictionary for debugging.
        """
        if context is None:
            context = {}
        if file_path is not None:
            context["file_path"] = str(file_path)
        super().__init__(message, correlation_id=correlation_id, context=context)


# Alias to avoid shadowing built-in, but keep custom exception
ConfigFileNotFoundError = FileNotFoundError


class ProviderError(SupacrawlError):
    """Raised when scraper operations fail."""

    def __init__(
        self,
        message: str,
        provider: str | None = None,
        correlation_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        """
        Initialise scraper error with provider context.

        Args:
            message: Error message.
            provider: Optional provider name that caused the error (e.g., "playwright").
            correlation_id: Optional correlation ID. If None, generates a new one.
            context: Optional context dictionary for debugging.
        """
        if context is None:
            context = {}
        if provider is not None:
            context["provider"] = provider
        super().__init__(message, correlation_id=correlation_id, context=context)


class ExtractionSchemaError(ProviderError):
    """Raised when the model's output still breaks the caller's JSON schema after its repair turn."""

    def __init__(self, errors: list[str], correlation_id: str | None = None) -> None:
        self.errors = errors
        super().__init__(
            "LLM output does not conform to the schema: " + "; ".join(errors),
            provider="llm",
            correlation_id=correlation_id,
            context={"schema_errors": errors},
        )
