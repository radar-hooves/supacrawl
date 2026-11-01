# Supacrawl Documentation

Clean markdown from the web for the terminal and AI assistants. This directory holds the user-facing references and contributor guides.

## User References

| Doc | Purpose |
| --- | --- |
| [CLI Reference](cli-reference.md) | Commands, options, and examples |
| [Configuration](configuration.md) | Settings, environment variables, LLM and search providers, telemetry |
| [MCP Server](mcp-server.md) | Tools, transports, untrusted callers, and what the server is built on |
| [API Reference](api-reference.md) | The optional local REST API (Firecrawl v2 compatible) |
| [Benchmark](benchmark.md) | The measured comparison against the field, and how to re-run it |

## Contributor Guides

| Doc | Purpose |
| --- | --- |
| [Data Flow (LLM)](development/data-flow-llm.md) | The map-to-markdown pipeline phases |
| [Snapshot Contract](development/snapshot-contract.md) | Crawl output layout and manifest format |
| [Retry Logic](development/retry-logic.md) | What is retried, what is not, and backoff |
| [Error Handling](development/error-handling.md) | Exception hierarchy, correlation IDs, recovery chains |
| [Testing](development/testing.md) | Test markers, the quality gate, browsers on NixOS |
| [Product definition](product/) | Intent, competitive reference, decisions |
