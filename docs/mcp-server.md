# MCP server

supacrawl ships a [Model Context Protocol](https://modelcontextprotocol.io) server, `supacrawl-mcp`, that gives an AI assistant the same scrape, crawl, map, search, extract and summary surface as the CLI.

## Installing

The server is built on `mcp-common`, an MCP framework that is not published on PyPI, so `pip install supacrawl[mcp]` installs the server's other dependencies and `supacrawl-mcp` then says, in one sentence, that `mcp-common` is missing. The server is deployed as a container image from a private repository that installs both; the CLI, the Python library and the REST API need neither.

With both installed, the server runs over stdio or HTTP:

```json
{
  "mcpServers": {
    "supacrawl": {
      "command": "supacrawl-mcp",
      "args": ["--transport", "stdio"]
    }
  }
}
```

`supacrawl-mcp --transport http` exposes the tools over the network. It requires `SUPACRAWL_MCP_AUTH_TOKEN` (a shared bearer token every caller presents in an `Authorization: Bearer <token>` header) whenever `--host` is not loopback; the server refuses to start on a non-loopback host with no token unless `--insecure` is passed. Loopback binds work unauthenticated.

## Tools

| Tool | Description |
| --- | --- |
| `supacrawl_scrape` | Scrape a URL to markdown, HTML, screenshot or PDF; `query` keeps the sections that match, `section` one heading's part, `max_chars` bounds the result |
| `supacrawl_crawl` | Crawl multiple pages from a site |
| `supacrawl_map` | Discover URLs on a website without fetching content |
| `supacrawl_search` | Web search with multi-provider fallback |
| `supacrawl_batch` | Scrape a list of URLs in one concurrent pass |
| `supacrawl_extract` | Structured data from pages, conforming to a JSON schema (server LLM) |
| `supacrawl_summary` | A word-bounded summary of a page (server LLM) |
| `supacrawl_diagnose` | Why a URL scrapes badly: bot wall, JavaScript, PDF |
| `supacrawl_health` | Engine, browser, search and LLM liveness in one shape |

The CLI's `agent` command is deliberately absent: over MCP the calling model is the agent. The server also exposes resources (format references, search providers, capabilities) and prompts (workflow guides for scraping, extraction, research and error handling).

## Quality signal

Every scrape result carries `quality`: a `verdict` (`ok`, `thin`, `js_shell`, `paywall`, `bot_challenge`, `captcha`, `error_status`, `garbled_pdf`, `tarpit`, `infrastructure`, `empty`), a 0 to 100 score, a `suggestion` when the result is not clean, and `tried`, each strategy that ran with its verdict. `success` is honest: bot blocks, CAPTCHAs, HTTP errors and empty pages are `success: false`. An `infrastructure` verdict means supacrawl's own engine failed and the request never left the building.

On a poor verdict the server escalates through its engines (Patchright, then Camoufox, then Camoufox over HTTP/1.1) within a bounded number of attempts, and remembers per domain the strategy that produced a clean result. Install the extras to unlock the full ladder:

```bash
pip install 'supacrawl[mcp,stealth,camoufox]'
patchright install chromium
camoufox fetch
```

## Configuration

Pass environment variables through the MCP client's config. All of [the configuration reference](configuration.md) applies; the server also reads `SUPACRAWL_LOG_LEVEL` (default `INFO`). Search needs a provider: `BRAVE_API_KEY` (free tier), `TAVILY_API_KEY`, `SERPER_API_KEY`, `SERPAPI_API_KEY`, `EXA_API_KEY`, or a self-hosted SearXNG through `SEARXNG_URL`. Without one, search falls back to DuckDuckGo, which is bot-walled, and a keyless search that returns nothing fails loudly.

### Serving an untrusted caller

The mode is per process, never per request, so a caller cannot widen it. Run a second `supacrawl-mcp` with `SUPACRAWL_BLOCK_PRIVATE_NETWORKS=1` and `SUPACRAWL_BLOCK_PAGE_SCRIPTS=1` and give that endpoint to the untrusted caller. It refuses loopback, RFC 1918, carrier-grade NAT, link-local, unique-local and metadata addresses in every IPv6 spelling; the browser never resolves or connects itself (every request is fetched at a pinned, validated address and handed to the page, so DNS rebinding has no window); and pages run without JavaScript. Camoufox is unavailable in this mode. Search and LLM backends are the instance's own configuration, never caller-supplied URLs.

### A SearXNG credential from a secrets broker

`SEARXNG_PORTCULLIS_CREDENTIAL` names a [Portcullis](https://github.com/radar-hooves/portcullis) credential carrying `username` and `password` fields; the pair is fetched in-process at startup, so it never exists as an environment variable or a file. Unset, the credential comes from `SEARXNG_USERNAME` and `SEARXNG_PASSWORD`. When the broker cannot produce a usable credential the server starts degraded and search fails loudly rather than handing the query to another engine.

### Filtering what the server returns

`SUPACRAWL_OUTPUT_FILTER=package.module:callable` runs every string in every tool result through a function you supply, a PII masker or DLP call; see [the configuration reference](configuration.md#filtering-what-the-mcp-server-returns).

## Troubleshooting

If a scrape returns little, call `supacrawl_diagnose` first: it names the cause (CDN protection, a JavaScript framework, a PDF). Then `wait_for=3000` for a page that hydrates slowly, `wait_until="load"` or `"networkidle"` where resources must finish, `only_main_content=false` if the wrong region was kept, and `query` to keep only the sections about something.
