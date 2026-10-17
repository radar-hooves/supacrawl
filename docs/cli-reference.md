# CLI Usage Guide

This guide covers using the supacrawl command-line interface.

## Command Overview

The `supacrawl` CLI provides commands for web scraping, URL mapping, search, and LLM-based data extraction.

### Available Commands

- `scrape` - Scrape a single URL to markdown
- `crawl` - Crawl a website from a starting URL
- `map` - Map URLs from a website
- `search` - Search the web (requires provider API key; see [search](#search))
- `llm-extract` - Extract structured data using LLM
- `agent` - Run autonomous web agent
- `cache` - Manage the local scrape cache
- `config` - Inspect and edit settings; emit the GUI settings schema (see [configuration](configuration.md))
- `strategy` - Inspect and reset per-domain strategy memory
- `metrics` - Inspect the local field telemetry log
- `serve` - Start the REST API server

## Command Reference

### scrape

Scrape a single URL and output content.

**Usage:**

```bash
supacrawl scrape URL [OPTIONS]
```

**Arguments:**

- `URL` - The URL to scrape

**Options:**

- `-f, --format FORMAT` - Output formats: `markdown`, `html`, `rawHtml`, `links`, `images`, `screenshot`, `pdf`, `json`, `branding`, `structuredData`, `summary` (default: `markdown`, can specify multiple)
- `--schema PATH` - Path to JSON schema file (for json format)
- `-p, --prompt TEXT` - Extraction prompt (for json format)
- `--only-main-content/--no-only-main-content` - Extract main content area only (default: true)
- `--wait-for INT` - Additional wait time in milliseconds after page load (default: 0)
- `--timeout INT` - Page load timeout in milliseconds (default: 30000)
- `-o, --output PATH` - Output file path (default: stdout). Use `.md`, `.json`, `.html`, `.png`, or `.pdf`
- `--full-page/--no-full-page` - Capture full scrollable page for screenshots (default: true)
- `-a, --actions PATH` - Path to JSON file containing page actions
- `--include-tags TEXT` - CSS selectors for elements to include (can be repeated)
- `--exclude-tags TEXT` - CSS selectors for elements to exclude (can be repeated)
- `--country CODE` - ISO country code for locale settings (e.g., AU, US, DE)
- `--language CODE` - Browser language/locale code (e.g., en-AU, de-DE)
- `--timezone TZ` - IANA timezone (e.g., Australia/Sydney)
- `--max-age INT` - Cache freshness in seconds (0=no cache, default: 0)
- `--cache-dir PATH` - Cache directory (default: `~/.supacrawl/cache`)
- `--stealth/--no-stealth` - Enhanced stealth mode via Patchright (requires: `pip install supacrawl[stealth]`)
- `--engine CHOICE` - Browser engine: `playwright` (default), `patchright` (Tier 2 stealth, requires `supacrawl[stealth]`), `camoufox` (Tier 3 for Akamai/Cloudflare, requires `supacrawl[camoufox]`). Overrides `--stealth`. Also reads `SUPACRAWL_ENGINE` env
- `--proxy URL` - Proxy URL (e.g., `http://user:pass@host:port`, `socks5://host:port`)
- `--solve-captcha/--no-solve-captcha` - Enable CAPTCHA solving via 2Captcha (requires: `pip install supacrawl[captcha]` and `CAPTCHA_API_KEY`)
- `--wait-until STRATEGY` - Page load strategy: `commit`, `domcontentloaded`, `load`, `networkidle` (default: domcontentloaded)
- `--change-tracking-modes CHOICE` - Diff modes for change tracking: `git-diff`, `json` (can specify multiple). Requires `-f changeTracking`
- `--expand-iframes CHOICE` - Iframe content expansion: `none` (strip all), `same-origin` (default, expand same-origin inline), `all` (expand all non-blocked)
- `--mobile/--no-mobile` - Scrape as a default mobile device (iPhone 14). Sets mobile viewport, user agent, and touch support
- `--device TEXT` - Emulate a specific device (e.g. "iPhone 15", "Pixel 7"). Overrides `--mobile`. See `--list-devices` for available presets
- `--list-devices` - List all available device presets and exit
- `--parse-pdf CHOICE` - PDF URL parsing mode: `auto` (default, detect .pdf URLs and extract text with OCR fallback), `fast` (text extraction only), `ocr` (force OCR, requires `supacrawl[pdf-ocr]`), `off` (disable, render in browser)
- `--http-first/--no-http-first` - Try a cheap HTTP GET before launching a browser, escalating only when the page actually needs one: JavaScript rendering, a bot challenge, iframes to expand, or content hidden behind collapsed disclosures. Use `--no-http-first` to always render in the browser (default: on)
- `--expect TEXT` - Require asserted content to be present before returning. A bare integer is a minimum word count; any other value is matched first as a CSS selector then as a text substring. When unmet, the scrape waits and retries with escalation rather than returning a pre-hydration page. Examples: `--expect '.price'`, `--expect 'In stock'`, `--expect 200`
- `--content-mode FLOAT` - Content extraction precision/recall dial (0.0–1.0, default: 0.5). Low values keep more content (recall-biased); high values prune aggressively (precision-biased). Requires `supacrawl[readability]` for the full cascade; without it, only the CSS-selector heuristic is active
- `--query TEXT` - Filter extracted sections by relevance to this query (BM25). Flat pages with no headings are never filtered. Requires `rank-bm25` (`supacrawl[readability]`); the result's `query_filter` says whether it was applied and why not
- `-H, --header 'Key: Value'` - Custom HTTP request header. Repeatable. Example: `--header 'Authorization: Bearer token'`

**Example:**

```bash
$ supacrawl scrape https://example.com --format markdown
# Outputs markdown content to stdout

$ supacrawl scrape https://example.com --output result.json
# Writes JSON result to file

$ supacrawl scrape https://example.com --format markdown --format html --output page.md
# Scrapes both formats, writes markdown to file

$ supacrawl scrape https://example.com --format screenshot --output page.png
# Capture full page screenshot

$ supacrawl scrape https://example.com --format pdf --output page.pdf
# Generate PDF of page

$ supacrawl scrape https://example.com --format images --output images.json
# Extract all image URLs

$ supacrawl scrape https://example.com --format branding --output branding.json
# Extract brand identity (colours, fonts, logo)

$ supacrawl scrape https://example.com --format summary
# Generate LLM summary of page content

$ supacrawl scrape https://example.com --format json --prompt "Extract product name and price"
# LLM-based structured extraction

$ supacrawl scrape https://example.com --no-only-main-content --wait-for 2000
# Scrapes full page, waits 2 seconds after load

$ supacrawl scrape https://protected-site.com --stealth
# Use enhanced stealth mode for bot-protected sites

$ supacrawl scrape https://captcha-site.com --stealth --solve-captcha
# Solve CAPTCHAs automatically (costs ~$0.002-0.003 each)

$ supacrawl scrape https://akamai-site.com --engine camoufox
# Use Camoufox engine for Akamai-protected sites

$ supacrawl scrape https://example.com --mobile
# Scrape as default mobile device (iPhone 14)

$ supacrawl scrape https://example.com --device "iPhone 15"
# Emulate a specific device

$ supacrawl scrape https://example.com --mobile -f screenshot -o mobile.png
# Capture mobile screenshot

$ supacrawl scrape --list-devices
# Show all available device presets

$ supacrawl scrape https://example.com/report.pdf
# Auto-detect PDF URL and extract text

$ supacrawl scrape https://example.com/scanned.pdf --parse-pdf ocr
# Force OCR for scanned PDF (requires supacrawl[pdf-ocr])

$ supacrawl scrape https://example.com -f changeTracking --max-age 3600
# Track content changes (compares against cached previous scrape)

$ supacrawl scrape https://example.com -f changeTracking --change-tracking-modes git-diff
# Get unified diff of changes

$ supacrawl scrape https://example.com --expand-iframes all
# Include content from all iframes
```

### crawl

Crawl a website starting from a URL.

**Usage:**

```bash
supacrawl crawl URL [OPTIONS]
```

**Arguments:**

- `URL` - The starting URL for the crawl

**Options:**

- `--limit INT` - Maximum pages to crawl (default: 100)
- `--depth INT` - Maximum crawl depth (default: 3)
- `--include TEXT` - URL patterns to include (glob patterns, can specify multiple)
- `--exclude TEXT` - URL patterns to exclude (glob patterns, can specify multiple)
- `-o, --output DIRECTORY` - Output directory for scraped content (required)
- `--resume` - Resume from previous crawl
- `-f, --format FORMAT` - Output format: `markdown`, `html`, `json`, `changeTracking` (default: `markdown`, can specify multiple)
- `--deduplicate-similar-urls` - Deduplicate URLs that differ only by tracking parameters or fragments
- `--allow-external-links` - Follow and scrape links to external domains
- `--country CODE` - ISO country code for locale settings (e.g., AU, US, DE)
- `--language CODE` - Browser language/locale code (e.g., en-AU, de-DE)
- `--timezone TZ` - IANA timezone (e.g., Australia/Sydney)
- `--stealth/--no-stealth` - Enhanced stealth mode via Patchright
- `--engine CHOICE` - Browser engine: `playwright` (default), `patchright`, `camoufox`. See scrape command for details
- `--proxy URL` - Proxy URL (e.g., `http://user:pass@host:port`)
- `-c, --concurrency INT` - Max concurrent requests (default: 10)
- `--wait-until STRATEGY` - Page load strategy: `commit`, `domcontentloaded`, `load`, `networkidle`
- `--cache-dir PATH` - Cache directory for change tracking (default: `~/.supacrawl/cache`)
- `--change-tracking-modes CHOICE` - Diff modes for change tracking: `git-diff`, `json` (can specify multiple). Requires `-f changeTracking`
- `--expand-iframes CHOICE` - Iframe content expansion: `none`, `same-origin` (default), `all`
- `--respect-robots/--ignore-robots` - Consult each origin's `robots.txt` and skip disallowed URLs (default: ignore)
- `--delay FLOAT` - Minimum seconds between requests to the same host (default: 0)

**Example:**

```bash
$ supacrawl crawl https://docs.example.com -o ./docs --limit 50
# Crawl up to 50 pages to ./docs directory

$ supacrawl crawl https://example.com -o ./output --depth 2
# Crawl with limited depth

$ supacrawl crawl https://example.com -o ./output --include "/docs/*" --exclude "/api/*"
# Crawl with URL pattern filtering

$ supacrawl crawl https://example.com -o ./output --deduplicate-similar-urls
# Remove duplicate URLs with different query params

$ supacrawl crawl https://example.com -o ./output --country AU
# Set browser locale to Australia

$ supacrawl crawl https://example.com -o ./output -f changeTracking --change-tracking-modes git-diff
# Track content changes across all crawled pages

$ supacrawl crawl https://example.com -o ./output --engine camoufox
# Crawl using Camoufox engine for protected sites
```

### map

Map URLs from a website without scraping content.

**Usage:**

```bash
supacrawl map URL [OPTIONS]
```

**Arguments:**

- `URL` - The starting URL to map

**Options:**

- `--limit INT` - Maximum number of URLs to discover (default: 200)
- `--depth INT` - Maximum BFS crawl depth (default: 3)
- `--sitemap CHOICE` - Sitemap handling: `include` (default), `skip`, or `only`
- `--include-subdomains` - Include subdomain URLs (flag)
- `--search TEXT` - Filter URLs containing this text
- `-o, --output PATH` - Output file path (default: stdout)
- `--format FORMAT` - Output format: `json` (full result) or `text` (URLs only) (default: `text`)
- `--ignore-query-params` - Remove query parameters from URLs
- `--stealth/--no-stealth` - Enhanced stealth mode via Patchright
- `--proxy URL` - Proxy URL
- `-c, --concurrency INT` - Max concurrent requests (default: 5)
- `--wait-until STRATEGY` - Page load strategy

**Example:**

```bash
$ supacrawl map https://example.com --limit 100 --format json
# Output full JSON result with link metadata

$ supacrawl map https://example.com --search about --output urls.txt
# Find all URLs containing "about", output as text list

$ supacrawl map https://example.com --sitemap only --format json --output sitemap.json
# Extract only sitemap URLs in JSON format

$ supacrawl map https://docs.example.com --depth 5 --include-subdomains
# Deep crawl including subdomains

$ supacrawl map https://example.com --ignore-query-params
# Deduplicate URLs that differ only by query params
```

### search

Search the web with multi-provider fallback. Supports 6 providers: Brave, Tavily, Serper, SerpAPI, Exa, and DuckDuckGo. Providers are tried in order; if one fails (rate limit, quota, CAPTCHA), the next is tried automatically.

**A search provider API key is required for reliable results.** Brave is recommended (free tier: ~1,000 searches/month, get a key at [brave.com/search/api](https://brave.com/search/api/)). Without any key, supacrawl falls back to DuckDuckGo; if DuckDuckGo returns nothing (it is aggressively bot-walled), the command exits non-zero with an actionable error telling you to set `BRAVE_API_KEY`. See `.env.example`.

**Usage:**

```bash
supacrawl search QUERY [OPTIONS]
```

**Arguments:**

- `QUERY` - Search query string

**Options:**

- `-l, --limit INT` - Maximum results per source type (1-10, default: 5)
- `-s, --source TYPE` - Source types: `web`, `images`, `news`, or `all` (default: `web`, can specify multiple)
- `--scrape/--no-scrape` - Scrape content from result pages (default: no-scrape)
- `--provider PROVIDERS` - Search provider(s), comma-separated for fallback chain. Supported: `brave`, `tavily`, `serper`, `serpapi`, `exa`, `duckduckgo` (default: `brave`). Also configurable via `SUPACRAWL_SEARCH_PROVIDERS` env var
- `--time-range [day|week|month|year]` - Restrict results to the past day, week, month, or year (mapped onto each provider's native API)
- `--start-date YYYY-MM-DD` - Earliest result date
- `--end-date YYYY-MM-DD` - Latest result date
- `--topic [general|news|finance]` - Topic vertical. Honoured natively by Tavily and Exa; other providers ignore it
- `--include-domain TEXT` - Restrict results to this domain. Repeatable
- `--exclude-domain TEXT` - Exclude results from this domain. Repeatable
- `-o, --output PATH` - Output file (JSON). If omitted, prints to stdout

**Example:**

```bash
$ supacrawl search "python web scraping" --limit 5 --source web
# Search web for Python scraping content

$ supacrawl search "AI news 2025" --source news --scrape --output results.json
# Search news and scrape content from result pages

$ supacrawl search "machine learning tutorials" --provider brave --limit 10
# Use Brave search provider (requires BRAVE_API_KEY)

$ supacrawl search "tech updates" --source all
# Search web, images, and news simultaneously

$ supacrawl search "topic" --provider brave,tavily,serper
# Use multiple providers with automatic fallback

$ supacrawl search "AI breakthroughs" --time-range week
# Results from the past week only

$ supacrawl search "earnings report" --topic finance --start-date 2025-01-01 --end-date 2025-06-30
# Finance-vertical results within a date range

$ supacrawl search "open source tools" --include-domain github.com --exclude-domain reddit.com
# Restrict or exclude specific domains
```

### llm-extract

Extract structured data from URLs using an LLM.

**Usage:**

```bash
supacrawl llm-extract URLS... [OPTIONS]
```

**Arguments:**

- `URLS` - One or more URLs to extract data from

**Options:**

- `-p, --prompt TEXT` - Extraction prompt describing what to extract (required)
- `-s, --schema FILE` - Path to JSON schema file for structured output
- `-o, --output PATH` - Output file (JSON). If omitted, prints to stdout

**Environment Variables (required):**

- `SUPACRAWL_LLM_PROVIDER` - LLM provider: `ollama`, `openai`, or `anthropic`
- `SUPACRAWL_LLM_MODEL` - Model name (e.g., `qwen3:8b`, `gpt-4o-mini`)
- `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` - API key for cloud providers

**Example:**

```bash
$ supacrawl llm-extract https://example.com/products --prompt "Extract product names and prices"
# Extract product data from a page

$ supacrawl llm-extract https://example.com/about --prompt "Extract company info" --schema schema.json
# Extract structured data according to a schema

$ supacrawl llm-extract https://a.com https://b.com --prompt "Extract titles" -o results.json
# Extract from multiple URLs
```

### agent

Run an autonomous web agent that searches, navigates, and extracts data.

**Usage:**

```bash
supacrawl agent PROMPT [OPTIONS]
```

**Arguments:**

- `PROMPT` - Natural language description of data to find

**Options:**

- `-u, --url URL` - Starting URLs (can specify multiple). If omitted, agent searches first
- `-s, --schema FILE` - Path to JSON schema for structured output
- `--max-steps INT` - Maximum pages to visit (default: 10)
- `-o, --output PATH` - Output file (JSON). If omitted, prints to stdout
- `-q, --quiet` - Suppress progress output, only show final result

**Environment Variables (required):**

- `SUPACRAWL_LLM_PROVIDER` - LLM provider: `ollama`, `openai`, or `anthropic`
- `SUPACRAWL_LLM_MODEL` - Model name
- `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` - API key for cloud providers

**Example:**

```bash
$ supacrawl agent "Find the pricing plans for Anthropic Claude API"
# Agent will search and navigate to find pricing info

$ supacrawl agent "Extract all team member names and roles" --url https://example.com/about
# Start from a specific URL

$ supacrawl agent "Find Python tutorial websites" --max-steps 5 --quiet
# Limit steps and suppress progress output
```

### cache

Manage the local scrape cache.

**Usage:**

```bash
supacrawl cache COMMAND [OPTIONS]
```

**Subcommands:**

- `stats` - Show cache statistics (entry count, size, directory)
- `clear` - Clear cached entries (all or by URL)
- `prune` - Remove expired cache entries

**Options (all subcommands):**

- `--cache-dir PATH` - Cache directory (default: `~/.supacrawl/cache`)

**Options (clear only):**

- `--url URL` - Clear cache for specific URL only
- `-y, --yes` - Skip confirmation prompt

**Example:**

```bash
$ supacrawl cache stats
# Show cache statistics

$ supacrawl cache clear
# Clear all cached entries (prompts for confirmation)

$ supacrawl cache clear --url https://example.com
# Clear cache for a specific URL

$ supacrawl cache clear -y
# Clear all without confirmation

$ supacrawl cache prune
# Remove expired entries
```

### strategy

Inspect and manage the per-domain strategy memory. Supacrawl remembers which browser engine, wait time, and stealth setting produced a clean result for each domain you visit, and seeds the next request to that domain with it. The memory is local, opt-out only (`SUPACRAWL_STRATEGY_MEMORY=0`), and improves results silently over time.

**Usage:**

```bash
supacrawl strategy COMMAND
```

**Subcommands:**

- `list` — Show every learned domain and its champion strategy
- `show DOMAIN` — Show the detailed strategy for one domain (e.g. `example.com`)
- `forget DOMAIN` — Reset the strategy for one domain
- `clear` — Forget all learned strategies

**Example:**

```bash
$ supacrawl strategy list
# Show all learned domains

$ supacrawl strategy show qantas.com
# Strategy details: engine, wait_for, quality score, sample count

$ supacrawl strategy forget qantas.com
# Reset strategy for one domain

$ supacrawl strategy clear
# Clear all learned strategies
```

### config

Inspect and edit supacrawl's standing settings, and emit the schema a control-plane GUI renders its settings form from. Settings resolve from built-in defaults, then a local TOML store (`~/.supacrawl/config.toml`), then environment variables (`SUPACRAWL_<NAME>`) — each layer overriding the one before. A per-command flag overrides all three for that one run. Credentials are environment-only and never written to the store. See the [configuration guide](configuration.md) for the full field list and the GUI schema contract.

**Usage:**

```bash
supacrawl config COMMAND
```

**Commands:**

- `get [KEY] [--stored] [--json]` — Show the effective config, or one key. `--stored` shows the file + defaults only, ignoring the environment.
- `set KEY VALUE` — Validate and persist a default to the store.
- `unset KEY` — Remove a key from the store, reverting it to the default.
- `schema` — Print the GUI settings schema (JSON, with `x-ui` render metadata).
- `secrets` — Report which credentials are configured (presence only, never values).
- `path` — Print the store location.

**Examples:**

```bash
$ supacrawl config set engine camoufox
# Persist a default browser engine

$ supacrawl config get --stored
# See exactly what the file pins, ignoring environment overrides

$ supacrawl config schema > settings.schema.json
# Emit the schema a dashboard renders its settings form from
```

### serve

Start the REST API server. Requires the `api` optional extra (`pip install supacrawl[api]`).

**Usage:**

```bash
supacrawl serve [OPTIONS]
```

**Options:**

- `--host TEXT` - Host to bind to (default: `0.0.0.0`, or `SUPACRAWL_API_HOST`)
- `--port INTEGER` - Port to listen on (default: `8308`, or `SUPACRAWL_API_PORT`)
- `--reload` - Enable auto-reload on code changes (development only)

**Example:**

```bash
# Start with defaults (0.0.0.0:8308)
supacrawl serve

# Custom port with auto-reload
supacrawl serve --port 9000 --reload

# Bind to localhost only
supacrawl serve --host 127.0.0.1
```

The server exposes a Firecrawl v2-compatible REST API. See [api-reference.md](api-reference.md) for endpoint documentation.

## Common Patterns

### Scraping Documentation Sites

Documentation sites often have structured URLs and sitemaps. Use `map` to discover pages, then `crawl` with filters.

```bash
# Map the site to see available URLs
supacrawl map https://docs.example.com --limit 100

# Filter to specific sections
supacrawl map https://docs.example.com --search "/api/" --limit 50

# Crawl only API documentation
supacrawl crawl https://docs.example.com -o ./api-docs \
  --include "/api/*" \
  --limit 100

# Exclude changelog and release notes
supacrawl crawl https://docs.example.com -o ./docs \
  --exclude "/changelog/*" \
  --exclude "/releases/*" \
  --limit 200
```

### Handling JS-Heavy SPAs

Single-page applications load content dynamically. The default `domcontentloaded` strategy waits for the initial DOM but not for async JavaScript. Use wait strategies to ensure SPA content is rendered:

```bash
# Wait until all network requests complete
supacrawl scrape https://spa.example.com --wait-until networkidle

# Enable SPA stability polling (waits for DOM to stop changing)
supacrawl scrape https://spa.example.com --wait-for 3000

# Both: wait for network idle, then poll for DOM stability
supacrawl scrape https://spa.example.com --wait-until networkidle --wait-for 3000
```

For interactive content, use actions:

```bash
cat > actions.json << 'EOF'
[
  {"type": "wait", "milliseconds": 2000},
  {"type": "scroll", "direction": "down"},
  {"type": "click", "selector": "button.load-more"},
  {"type": "wait", "milliseconds": 2000}
]
EOF

supacrawl scrape https://spa.example.com --actions actions.json
```

### Anti-Bot Protection

Supacrawl uses a three-tier engine system for anti-bot protection:

| Tier | Engine               | Install                           | Use Case                                        |
| ---- | -------------------- | --------------------------------- | ----------------------------------------------- |
| 1    | Playwright (default) | Included                          | Basic stealth scripts, always active            |
| 2    | Patchright           | `pip install supacrawl[stealth]`  | Cloudflare, general anti-bot                    |
| 3    | Camoufox             | `pip install supacrawl[camoufox]` | Akamai Bot Manager, advanced TLS fingerprinting |

Select the engine explicitly or let supacrawl auto-detect:

```bash
# Tier 1: Basic stealth (always active, no flags needed)
supacrawl scrape https://example.com

# Tier 2: Patchright for Cloudflare-protected sites
supacrawl scrape https://protected-site.com --stealth
supacrawl scrape https://protected-site.com --engine patchright

# Tier 3: Camoufox for Akamai-protected sites
supacrawl scrape https://akamai-site.com --engine camoufox

# Set default engine via environment variable
export SUPACRAWL_ENGINE=camoufox
supacrawl scrape https://akamai-site.com

# Debug with visible browser
SUPACRAWL_HEADLESS=false supacrawl scrape https://protected.example.com --stealth

# Capture screenshot to see what browser sees
supacrawl scrape https://protected.example.com -f screenshot -o debug.png
```

If a Chromium-based engine encounters an HTTP/2 protocol error, supacrawl automatically retries with Camoufox (if installed). This two-stage fallback handles servers that reject Chromium's TLS fingerprint.

For CAPTCHA-protected sites:

```bash
pip install supacrawl[captcha]
export CAPTCHA_API_KEY=your-2captcha-api-key
supacrawl scrape https://captcha-site.example.com --stealth --solve-captcha
```

### Change Tracking

Track content changes between scrapes using the cache. Requires a previous cached scrape to compare against.

```bash
# First scrape (populates cache)
supacrawl scrape https://example.com --max-age 3600

# Later: detect changes
supacrawl scrape https://example.com -f changeTracking --max-age 3600

# Get a unified diff of changes
supacrawl scrape https://example.com -f changeTracking --change-tracking-modes git-diff

# JSON comparison mode (requires --prompt or --schema for structured extraction)
supacrawl scrape https://example.com -f changeTracking --change-tracking-modes json \
  --prompt "Extract product prices"

# Track changes across an entire site
supacrawl crawl https://example.com -o ./output -f changeTracking \
  --change-tracking-modes git-diff --cache-dir ~/.supacrawl/cache
```

Change tracking returns a status (`new`, `same`, or `changed`) and optionally a diff. The `git-diff` mode produces unified diffs; the `json` mode compares structured extracted fields.

### PDF Parsing

Supacrawl auto-detects PDF URLs (by `.pdf` extension) and extracts text directly, bypassing the browser entirely.

```bash
# Auto-detect and extract text (default behaviour)
supacrawl scrape https://example.com/report.pdf

# Text extraction only (no OCR fallback)
supacrawl scrape https://example.com/report.pdf --parse-pdf fast

# Force OCR for scanned documents
pip install supacrawl[pdf-ocr]
supacrawl scrape https://example.com/scanned.pdf --parse-pdf ocr

# Extract structured data from PDF
supacrawl scrape https://example.com/report.pdf -f json --prompt "Extract revenue figures"

# Disable PDF parsing (render in browser)
supacrawl scrape https://example.com/report.pdf --parse-pdf off
```

### Mobile Emulation

Scrape pages as a mobile device using Playwright's device descriptors. This sets the viewport, user agent, device scale factor, and touch support.

```bash
# Scrape as default mobile device (iPhone 14)
supacrawl scrape https://example.com --mobile

# Emulate a specific device
supacrawl scrape https://example.com --device "iPhone 15"
supacrawl scrape https://example.com --device "Pixel 7"

# Capture mobile screenshot
supacrawl scrape https://example.com --mobile -f screenshot -o mobile.png

# List all available device presets
supacrawl scrape --list-devices
```

### Extracting Structured Data

Use LLM extraction to get structured data from pages.

```bash
# Extract with a prompt
supacrawl scrape https://shop.example.com/product \
  -f json \
  --prompt "Extract product name, price, and availability"

# Extract from multiple pages
supacrawl llm-extract \
  https://shop.example.com/product/1 \
  https://shop.example.com/product/2 \
  --prompt "Extract product name and price" \
  -o products.json

# Autonomous agent for complex tasks
supacrawl agent "Find the pricing plans for Acme Corp" -o pricing.json
```

### Caching

Use caching to avoid re-fetching unchanged pages.

```bash
# Cache for 1 hour
supacrawl scrape https://example.com --max-age 3600

# Check cache status
supacrawl cache stats

# Clear expired entries
supacrawl cache prune
```

### Locale and Timezone

Set browser locale for region-specific content:

```bash
supacrawl scrape https://example.com --country AU
supacrawl scrape https://example.com --language en-AU --timezone Australia/Sydney
```

## Environment Variables

### LLM Configuration

- `SUPACRAWL_LLM_PROVIDER` - LLM provider: `ollama`, `openai`, or `anthropic`
- `SUPACRAWL_LLM_MODEL` - Model name (e.g., `qwen3:8b`, `gpt-4o-mini`, `claude-sonnet-4-20250514`)
- `OLLAMA_HOST` - Ollama server URL (default: `http://localhost:11434`)
- `OPENAI_API_KEY` - API key for OpenAI
- `ANTHROPIC_API_KEY` - API key for Anthropic

### Search Configuration

- `BRAVE_API_KEY` - Brave Search API key (for `--provider brave`)
- `SEARXNG_URL` - Self-hosted SearXNG instance URL, no credentials embedded (for `--provider searxng`)
- `SEARXNG_USERNAME` - HTTP Basic username for a Basic-auth-gated SearXNG instance (optional, paired with `SEARXNG_PASSWORD`)
- `SEARXNG_PASSWORD` - HTTP Basic password for a Basic-auth-gated SearXNG instance (optional, paired with `SEARXNG_USERNAME`)
- Embedding credentials directly in `SEARXNG_URL` (`https://user:pass@host`) still works as a deprecated fallback, but is discouraged: it turns the whole URL into a secret. Prefer `SEARXNG_USERNAME` / `SEARXNG_PASSWORD`, which win when both are set
- `SEARXNG_PORTCULLIS_CREDENTIAL` - **MCP server only.** Catalogue name of a Portcullis credential carrying the `username`/`password` pair, fetched from the broker at server startup so it never has to be an environment variable. The CLI does not read the broker; when this is set, `supacrawl config secrets` says so rather than reporting the absent env pair as a misconfiguration

### Browser Configuration

- `SUPACRAWL_HEADLESS` - Run headless (default: `true`)
- `SUPACRAWL_TIMEOUT` - Page load timeout in ms (default: `30000`)
- `SUPACRAWL_WAIT_UNTIL` - Default page load strategy (`commit`, `domcontentloaded`, `load`, `networkidle`; default: `domcontentloaded`)
- `SUPACRAWL_ENGINE` - Default browser engine: `playwright`, `patchright`, `camoufox` (overridden by `--engine`)
- `SUPACRAWL_PROXY` - Proxy URL (http/socks5)
- `SUPACRAWL_CACHE_DIR` - Cache directory (default: `~/.supacrawl/cache`)

### CAPTCHA Configuration

- `CAPTCHA_API_KEY` - 2Captcha API key for automatic CAPTCHA solving

See `.env.example` for additional browser configuration options.

## Error Handling

### Common Errors

**URL Not Reachable:**

```text
Error: Failed to fetch URL: https://example.com [correlation_id=abc12345]
```

**Solution:** Check the URL is correct and accessible. The site may be blocking automated requests. Try `--stealth` mode.

**LLM Provider Error:**

```text
Error: LLM extraction failed: Connection refused [correlation_id=abc12345]
```

**Solution:** Ensure Ollama is running (`ollama serve`) or check API keys for cloud providers. Verify `SUPACRAWL_LLM_PROVIDER` and `SUPACRAWL_LLM_MODEL` are set.

**Timeout Error:**

```text
Error: Page load timeout exceeded [correlation_id=abc12345]
```

**Solution:** Increase timeout with `--timeout` option or use `--wait-until networkidle` for JS-heavy sites.

### Debugging with Correlation IDs

All errors include correlation IDs for debugging:

1. Note correlation ID from error message
2. Check logs for entries with same correlation ID
3. Review error context in logs

## Troubleshooting

- Run `playwright install chromium` to verify Playwright browsers are installed
- For 4xx client errors, fix cookies/auth/proxy; retries are skipped by design
- For rate limits, use `--wait-for` to add delays between requests or reduce crawl `--limit`
- Use `--stealth` or `--engine patchright` for bot-protected sites (requires `pip install supacrawl[stealth]`)
- Use `--engine camoufox` for Akamai-protected sites (requires `pip install supacrawl[camoufox]`)
- HTTP/2 protocol errors are automatically retried with Camoufox if installed
- Use `--solve-captcha` for CAPTCHA-protected sites (requires `pip install supacrawl[captcha]` and `CAPTCHA_API_KEY`)
- Set `SUPACRAWL_HEADLESS=false` to see what the browser sees during debugging
- For PDF URLs returning empty content, check `--parse-pdf` mode (default `auto` detects `.pdf` extensions)

## Best Practices

1. **Start with map**: Use `map` to discover URLs before crawling large sites
2. **Start small**: Use `--limit` and `--depth` to control crawl scope
3. **Use caching**: The cache reduces redundant requests; use `cache stats` to monitor
4. **Test with limits**: Start with low `--limit` values when exploring new sites
5. **Choose the right provider**: Use Ollama for local/private data, cloud providers for better quality
