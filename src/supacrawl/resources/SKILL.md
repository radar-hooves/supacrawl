---
name: supacrawl
description: >-
  Scrape, crawl, map, and search the web from the terminal, with clean markdown output and deterministic or LLM extraction. Use when you need web content as markdown/JSON for an agent: fetch a known URL, search the web, discover or crawl a site's pages, or pull structured facts. Zero-infrastructure CLI.
---

# Supacrawl

Supacrawl turns web pages into clean markdown (or JSON) for LLMs and pipelines. It is a single CLI — `supacrawl` — with no server or database. Every command prints to stdout or writes a file, so it composes in shell pipelines.

Install: `pip install supacrawl` then `playwright install chromium` (add `[stealth]` and `[camoufox]` for the anti-bot engines, `[api]` for the REST server). If scraping fails with a Playwright/browser error, run `playwright install chromium`.

## Choosing a command

| You have / want                            | Command                                          |
| ------------------------------------------ | ------------------------------------------------ |
| A known URL → its content as markdown/JSON | `supacrawl scrape <url>`                         |
| A question, not a URL → web results        | `supacrawl search "<query>"`                     |
| A site → the list of its URLs (no content) | `supacrawl map <url>`                            |
| A site → scrape many pages into a folder   | `supacrawl crawl <url> --output dir/`            |
| URLs → specific fields as JSON (LLM)       | `supacrawl llm-extract <url...> --schema s.json` |
| An open-ended gathering task               | `supacrawl agent "<task>"`                       |

Rule of thumb: if you already have the URL, `scrape`; if you don't, `search`; for a whole site, `map` (just links) or `crawl` (links + content).

## scrape — one URL to content

```bash
supacrawl scrape https://example.com                 # markdown to stdout
supacrawl scrape https://example.com -f json --schema schema.json
supacrawl scrape https://shop.example/item -f structuredData
```

Useful flags:

- `-f, --format` (repeatable): `markdown` (default), `html`, `rawHtml`, `links`, `images`, `branding`, `screenshot`, `pdf`, `summary`, `json` (LLM extraction — needs `--schema` or `--prompt`), `structuredData`, `changeTracking`.
- `--structuredData` (via `-f structuredData`): deterministic, no-LLM extraction of the page's own embedded data — schema.org JSON-LD, Next.js `__NEXT_DATA__`, microdata, OpenGraph. Prefer this over `-f json` for facts a site already publishes (price, rating, author, date): it is free, deterministic, and exact.
- `--expect "<assertion>"`: require content before returning. A bare integer is a minimum word count; otherwise it is matched first as a CSS selector then as a text substring. If absent, the scrape waits and escalates rather than returning a half-loaded page. Examples: `--expect ".product-price"`, `--expect "In stock"`, `--expect 200`.
- HTTP-first is automatic: static pages are fetched without a browser (fast); JavaScript-heavy or bot-protected pages transparently escalate to a full render. Pass `--no-http-first` to always render in the browser.
- `--only-main-content/--no-only-main-content` (default on): strip nav/footer chrome. If a page comes back near-empty, retry with `--no-only-main-content`.
- `--wait-for <ms>` for slow/JS pages; `--stealth` or `--engine patchright` / `--engine camoufox` for anti-bot sites; `--header 'Key: Value'` for auth.

## search — web results, optionally scraped

```bash
supacrawl search "rust async runtimes" --limit 10
supacrawl search "ukraine" --source news --time-range week
supacrawl search "earnings" --topic finance --include-domain reuters.com --scrape
```

Filters are pushed to the provider (Brave/Tavily/Serper/Exa/…), so results are pre-scoped rather than filtered after the fact:

- `--time-range day|week|month|year`, or `--start-date`/`--end-date` (YYYY-MM-DD).
- `--topic general|news|finance` (richest on Tavily/Exa).
- `--include-domain` / `--exclude-domain` (repeatable).
- `--source web|images|news|all`, `--scrape` to fetch result pages, `--limit 1-10`.

Configure providers with `--provider brave,tavily` or `SUPACRAWL_SEARCH_PROVIDERS` plus the matching API key env var (e.g. `BRAVE_API_KEY`).

## map / crawl — whole sites

```bash
supacrawl map https://docs.example.com --limit 200
supacrawl crawl https://docs.example.com --output corpus/ --limit 100
```

`map` returns URLs only (cheap discovery). `crawl` scrapes each page into `--output` as markdown/HTML/JSON files. Scope with `--include`/`--exclude` glob patterns, `--max-depth`, and `--limit`. To space out requests to one host, add `--delay <seconds>`; to consult `robots.txt`, add `--respect-robots`.

## llm-extract / agent — structured + autonomous

```bash
supacrawl llm-extract https://a.com https://b.com --schema product.json
supacrawl agent "Find the pricing tiers for the top 3 note-taking apps"
```

`llm-extract` pulls schema-shaped JSON from one or more URLs (needs an LLM provider configured — Ollama by default, or `OPENAI_API_KEY`/`ANTHROPIC_API_KEY`). `agent` plans and gathers across pages for open-ended tasks.

## Reading results and recovering from failures

- Output is markdown by default; `-f json` or `-o out.json` gives the full structured result. Check `success`; on failure read `error`.
- Errors are remediation-shaped. A failure ends with `[HINT: ...]` telling you the fix (raise `--timeout`/`--wait-for`, install `supacrawl[stealth]`, try `--engine camoufox`, check the URL/DNS). Apply the hint and retry — do not retry the same command unchanged.
- A near-empty but `success: true` result carries a `warnings` entry suggesting `--no-only-main-content` or a larger `--wait-for`.
- Bot-protected sites auto-escalate to stealth when `supacrawl[stealth]` is installed; if you still get blocked, the hint will say so honestly.

## REST and MCP

`pip install supacrawl[api]` then `supacrawl serve` exposes a Firecrawl v2-compatible REST API (POST `/scrape`, `/search`, `/map`, `/crawl`). An MCP server (`supacrawl-mcp`) exposes the same tools to MCP-capable agents; it is built on a framework that is not on PyPI and is deployed as a container image (see `docs/mcp-server.md`).

Long pages: `--query "<what you need>"` keeps only the sections about it, and a section whose heading the query names (an option's identifier on a reference page) comes first and whole.
