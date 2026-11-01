<div align="center">

[![Python](https://img.shields.io/badge/Python-3.14+-3776ab?logo=python&logoColor=white)](https://python.org) [![Playwright](https://img.shields.io/badge/Playwright-2ea44f?logo=playwright&logoColor=white)](https://playwright.dev) [![MCP](https://img.shields.io/badge/MCP-Compatible-8A2BE2)](https://modelcontextprotocol.io) [![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

# Supacrawl

_Clean markdown from the web, for the terminal and for AI assistants._

One `pip install`, no Docker, no database, no hosted account: a CLI, a Python library, a Firecrawl-compatible REST API and an MCP server over the same engine.

[Installation](#installation) · [Features](#features) · [Benchmark](docs/benchmark.md) · [Docs](docs/)

</div>

## The Problem

A model that reads the web needs the article, not the navigation, the cookie banner and the footer around it. Pages that need JavaScript, sit behind a bot manager, or are PDFs break a plain HTTP fetch, and the tools that handle them need a server, a queue and a database, or a paid key.

## The Solution

Supacrawl fetches the cheap way first and escalates only when a page needs it: an HTTP GET, then a headless browser, then stealth engines, with a quality verdict on every result so the caller knows what it got. It remembers per domain what worked. Measured on 01/11/2026 against the open-source field over 33 public pages, three pages at a time, on one 20-core host that was busy throughout (load average 29 to 65), so the times compare the readers with each other and are not a speed claim for any of them ([method and full table](docs/benchmark.md)):

| Reader | Pages | Content kept | Boilerplate gone | Median s | Peak MB |
| --- | ---: | ---: | ---: | ---: | ---: |
| supacrawl 2026.11.0 | 88% | 93% | 92% | 9.7 | 16,013 |
| Firecrawl self-hosted | 85% | 95% | 40% | 3.6 | 77 (client only) |
| Crawl4AI 0.9.5 | 82% | 96% | 56% | 5.2 | 2,667 |
| Jina Reader (keyless) | 94% | 95% | 24% | 2.1 | 83 |
| trafilatura 2.3.1 | 76% | 74% | 97% | 0.8 | 174 |

Supacrawl is the one reader that both keeps the content and drops the boilerplate, and the one with web search; it pays in time and memory where a page forces the escalation ladder. One page at a time, on the same host, its median halved and its peak memory fell to a seventh.

## Features

<table>
<tr>
<td width="50%"><strong>HTTP first, browser when needed</strong><br>A static page never starts a browser; JavaScript, bot challenges and PDFs escalate automatically</td>
<td width="50%"><strong>Honest quality signal</strong><br>Every result says <code>ok</code>, <code>thin</code>, <code>js_shell</code>, <code>bot_challenge</code>, <code>captcha</code> or <code>paywall</code>, with the next step and every strategy that ran</td>
</tr>
<tr>
<td width="50%"><strong>Three-tier anti-bot ladder</strong><br>Playwright, Patchright, Camoufox, climbed on a poor verdict and remembered per domain</td>
<td width="50%"><strong>Retrieval for long pages</strong><br><code>query</code> keeps the sections that match, <code>section</code> one heading's part; a section the query names comes first</td>
</tr>
<tr>
<td width="50%"><strong>Structured data without an LLM</strong><br>schema.org JSON-LD, microdata and OpenGraph as JSON, deterministically; LLM extraction and summaries through Ollama, OpenAI, Anthropic or any OpenAI-compatible gateway</td>
<td width="50%"><strong>Search, crawl, map, PDFs</strong><br>Multi-provider web search with recency and domain filters, robots-aware crawling to a folder, URL discovery, PDF text with optional OCR</td>
</tr>
</table>

## Installation

**Prerequisites:** Python 3.14+

```bash
pip install supacrawl
playwright install chromium
supacrawl scrape https://example.com
```

Add `supacrawl[stealth]` and `supacrawl[camoufox]` (then `patchright install chromium` and `camoufox fetch`) for the full anti-bot ladder, `supacrawl[api]` for the REST server, `supacrawl[pdf-ocr]` for scanned PDFs. Set `BRAVE_API_KEY` for search. See [Configuration](docs/configuration.md) for every option.

## Documentation

| Guide | Description |
| --- | --- |
| [CLI reference](docs/cli-reference.md) | Every command and option |
| [Configuration](docs/configuration.md) | Settings, environment variables, LLM providers, search providers, telemetry |
| [MCP server](docs/mcp-server.md) | Tools, transports, serving an untrusted caller, and what the server is built on |
| [REST API](docs/api-reference.md) | The Firecrawl v2-compatible endpoints and the n8n integration |
| [Benchmark](docs/benchmark.md) | How the figures above were measured, and how to re-run them |
| [Agent skill](src/supacrawl/resources/SKILL.md) | Onboarding for an agent that drives the CLI; `supacrawl install-skill` registers it |
| [Product definition](docs/product/) | Intent, competitive reference, decisions |

## Contributing

```bash
git clone https://github.com/radar-hooves/supacrawl.git
cd supacrawl
uv sync --all-groups --all-extras && playwright install chromium
ruff check src tests && mypy src && pytest -q
```

Tests that drive a real browser are marked `browser`; see [docs/development/testing.md](docs/development/testing.md).

<div align="center">
<sub>
Clean markdown from the web.<br>
<a href="LICENSE">MIT Licence</a> · <a href="https://github.com/radar-hooves/supacrawl/issues">Report Bug</a>
</sub>
</div>
