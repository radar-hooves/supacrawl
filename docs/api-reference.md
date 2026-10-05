# REST API Reference

Supacrawl serves a REST API via `supacrawl serve`. The API is compatible with the Firecrawl v2 protocol, so tools that speak Firecrawl (n8n, LangChain, LlamaIndex) work as drop-in backends. Supacrawl-native endpoints extend the surface with capabilities the v2 protocol does not cover.

## Getting Started

Install Supacrawl with the API extra and start the server:

```shell
pip install supacrawl[api]
supacrawl serve
```

The server binds to `0.0.0.0:8308` by default. Scrape a page:

```shell
curl -s http://localhost:8308/scrape \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://example.com"}' | python -m json.tool
```

### CLI Options

```shell
supacrawl serve [--host 0.0.0.0] [--port 8308] [--reload]
```

- `--host` sets the bind address (default `0.0.0.0`).
- `--port` sets the bind port (default `8308`).
- `--reload` enables auto-reload for development.

## Authentication

Authentication uses a Bearer token configured via the `SUPACRAWL_API_KEY` environment variable.

- If `SUPACRAWL_API_KEY` is **set**, every request (except `/supacrawl/health`) must include a matching `Authorization` header.
- If `SUPACRAWL_API_KEY` is **not set**, authentication is disabled and all requests pass through.

```shell
export SUPACRAWL_API_KEY=YOUR_KEY

curl http://localhost:8308/scrape \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer YOUR_KEY' \
  -d '{"url": "https://example.com"}'
```

## Endpoints

### POST /scrape

Scrape a single URL synchronously. Returns scraped content in the requested formats.

**Request body:**

```json
{
  "url": "https://example.com",
  "formats": ["markdown", "html"],
  "onlyMainContent": true,
  "waitFor": 0,
  "timeout": 30000,
  "includeTags": [".article-body"],
  "excludeTags": [".sidebar"],
  "mobile": false,
  "actions": [],
  "location": { "country": "AU", "languages": ["en-AU"] },
  "headers": { "Cookie": "session=abc" },
  "maxAge": 60000,
  "proxy": "basic",
  "storeInCache": true,
  "httpFirst": true,
  "expect": ".product-price",
  "contentMode": 0.5,
  "query": "pricing"
}
```

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `url` | string | required | URL to scrape |
| `formats` | string[] | `null` | Output formats. Supacrawl extras: `"images"`, `"branding"`, `"pdf"`, `"structuredData"` |
| `onlyMainContent` | boolean | `true` | Extract main content area only |
| `waitFor` | integer | `0` | Additional wait time in milliseconds after page load |
| `timeout` | integer | `30000` | Page load timeout in milliseconds |
| `includeTags` | string[] | `null` | CSS selectors for elements to include |
| `excludeTags` | string[] | `null` | CSS selectors for elements to exclude |
| `mobile` | boolean | `null` | Emulate a mobile viewport |
| `actions` | array | `null` | Page actions (click, scroll, wait) |
| `location` | object | `null` | Locale settings: `{ "country": "AU", "languages": ["en-AU"] }` |
| `headers` | object | `null` | Custom HTTP request headers |
| `maxAge` | integer | `null` | Cache freshness in milliseconds (divided by 1000 for service layer) |
| `proxy` | string/boolean | `null` | `"basic"`, `"enhanced"`, `"auto"` map to `true`; a URL string is passed through |
| `storeInCache` | boolean | `null` | `false` bypasses cache entirely (sets `maxAge` to 0) |
| `httpFirst` | boolean | `true` | Try a cheap HTTP GET before launching a browser; escalates automatically when JavaScript or a bot challenge is detected. Set to `false` to always render in the browser |
| `expect` | string | `null` | Require asserted content before returning. A bare integer is a minimum word count; any other value is matched first as a CSS selector then as a text substring |
| `contentMode` | number | `0.5` | Content extraction precision/recall dial (0.0–1.0). Low values keep more content; high values prune aggressively. Requires `supacrawl[readability]` for the full cascade |
| `query` | string | `null` | Filter extracted sections by relevance to this query (BM25). Flat pages with no headings are never filtered. Requires `supacrawl[readability]` |

Fields not listed here (e.g. `skipTlsVerification`, `blockAds`, `removeBase64Images`) are accepted silently and ignored.

**Response body:**

```json
{
  "success": true,
  "data": {
    "markdown": "# Example Domain\n\nThis domain is for use in illustrative examples...",
    "html": "<h1>Example Domain</h1>...",
    "rawHtml": "<!doctype html>...",
    "links": ["https://www.iana.org/domains/example"],
    "screenshot": null,
    "metadata": {
      "title": "Example Domain",
      "description": null,
      "sourceURL": "https://example.com",
      "url": "https://example.com",
      "statusCode": 200,
      "language": "en"
    },
    "actions": null,
    "branding": null,
    "changeTracking": null,
    "structuredData": null
  },
  "quality": {
    "verdict": "ok",
    "score": 95,
    "reasons": [],
    "suggestion": null,
    "attempts": 1,
    "escalated": false
  }
}
```

`success` is honest: HTTP >= 400 responses, bot/CAPTCHA interstitials, garbled PDFs, and empty pages are reported `success=false` even when a response body was returned. The `quality` field is always present:

| Field | Description |
| --- | --- |
| `verdict` | `ok`, `thin`, `js_shell`, `paywall`, `bot_challenge`, `captcha`, `error_status`, `garbled_pdf`, or `empty` |
| `score` | 0-100 completeness within the verdict |
| `reasons` | Why this verdict and score were assigned |
| `suggestion` | Concrete next step when verdict is not `ok` |
| `attempts` | Number of strategies tried (auto-escalation may try several) |
| `escalated` | Whether auto-escalation fired beyond the first attempt |

**Example:**

```shell
curl -s http://localhost:8308/scrape \
  -H 'Content-Type: application/json' \
  -d '{
    "url": "https://example.com",
    "formats": ["markdown", "links"],
    "onlyMainContent": true
  }'
```

---

### POST /crawl

Start an asynchronous crawl job. Returns a job ID immediately; use `GET /crawl/{id}` to poll for results.

**Request body:**

```json
{
  "url": "https://docs.example.com",
  "limit": 100,
  "maxDiscoveryDepth": 3,
  "includePaths": ["/docs/.*"],
  "excludePaths": ["/changelog/.*"],
  "allowExternalLinks": false,
  "allowSubdomains": false,
  "maxConcurrency": 10,
  "ignoreQueryParameters": false,
  "scrapeOptions": {
    "formats": ["markdown"],
    "onlyMainContent": true
  }
}
```

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `url` | string | required | Starting URL for the crawl |
| `limit` | integer | `10000` | Maximum pages to crawl |
| `maxDiscoveryDepth` | integer | `3` | Maximum crawl depth |
| `includePaths` | string[] | `null` | Regex patterns on URL path to include |
| `excludePaths` | string[] | `null` | Regex patterns on URL path to exclude |
| `allowExternalLinks` | boolean | `false` | Follow links to external domains |
| `allowSubdomains` | boolean | `false` | Follow links to subdomains |
| `maxConcurrency` | integer | `10` | Maximum concurrent requests |
| `ignoreQueryParameters` | boolean | `false` | Deduplicate URLs differing only by query params |
| `ignoreRobotsTxt` | boolean | `true` | Skip `robots.txt` checks. Set to `false` to consult each origin's robots.txt and skip disallowed URLs |
| `delay` | number | `0` | Minimum seconds between requests to the same host |
| `scrapeOptions` | object | `null` | Nested scrape options (same fields as POST /scrape minus `url`) |

**Response body:**

```json
{
  "success": true,
  "id": "550e8400-e29b-41d4-a716-446655440000"
}
```

**Example:**

```shell
curl -s http://localhost:8308/crawl \
  -H 'Content-Type: application/json' \
  -d '{
    "url": "https://docs.example.com",
    "limit": 50,
    "maxDiscoveryDepth": 2
  }'
```

---

### GET /crawl/{id}

Poll the status of a crawl job. Results are paginated; follow the `next` URL for additional pages.

**Query parameters:**

| Parameter | Type    | Default | Description                             |
| --------- | ------- | ------- | --------------------------------------- |
| `offset`  | integer | `0`     | Pagination offset into the results list |

**Response body:**

```json
{
  "status": "scraping",
  "total": 50,
  "completed": 12,
  "data": [
    {
      "markdown": "# Page Title\n...",
      "metadata": {
        "title": "Page Title",
        "sourceURL": "https://docs.example.com/page",
        "statusCode": 200
      }
    }
  ],
  "next": "http://localhost:8308/crawl/550e8400-e29b-41d4-a716-446655440000?offset=10"
}
```

**Example:**

```shell
curl -s http://localhost:8308/crawl/550e8400-e29b-41d4-a716-446655440000
```

---

### DELETE /crawl/{id}

Cancel a running crawl job.

**Response body:**

```json
{
  "status": "cancelled",
  "total": 50,
  "completed": 12,
  "data": []
}
```

**Example:**

```shell
curl -s -X DELETE http://localhost:8308/crawl/550e8400-e29b-41d4-a716-446655440000
```

---

### POST /map

Discover URLs on a website synchronously. Returns a list of discovered links with metadata.

**Request body:**

```json
{
  "url": "https://example.com",
  "limit": 100,
  "search": "pricing",
  "sitemap": "include",
  "includeSubdomains": false,
  "ignoreQueryParameters": false,
  "ignoreCache": false,
  "timeout": 30000
}
```

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `url` | string | required | Starting URL to map |
| `limit` | integer | `5000` | Maximum URLs to discover |
| `search` | string | `null` | Filter URLs by relevance to this term |
| `sitemap` | string | `"include"` | Sitemap handling: `"skip"`, `"include"`, or `"only"` |
| `includeSubdomains` | boolean | `false` | Include subdomain URLs |
| `ignoreQueryParameters` | boolean | `false` | Remove query parameters from URLs |
| `ignoreCache` | boolean | `false` | Bypass cached map results |
| `timeout` | integer | `30000` | Timeout in milliseconds |

**Response body:**

```json
{
  "success": true,
  "links": [
    { "url": "https://example.com/about", "title": "About Us", "description": "Learn more..." },
    { "url": "https://example.com/pricing", "title": "Pricing", "description": null }
  ]
}
```

**Example:**

```shell
curl -s http://localhost:8308/map \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://example.com", "limit": 100}'
```

---

### POST /search

Search the web synchronously. Results are bucketed by source type (web, images, news).

**A search provider API key is required.** Set `BRAVE_API_KEY` (free tier at [brave.com/search/api](https://brave.com/search/api/)) before starting the server. Without any key, the server falls back to DuckDuckGo; if DuckDuckGo returns nothing, the endpoint returns `success=false` with an actionable error message rather than an empty result set. See `.env.example`.

**Request body:**

```json
{
  "query": "python web scraping tutorial",
  "limit": 5,
  "sources": [{ "type": "web" }],
  "timeout": 30000,
  "scrapeOptions": null,
  "timeRange": "week",
  "startDate": "2025-01-01",
  "endDate": "2025-06-30",
  "topic": "general",
  "includeDomains": ["docs.python.org"],
  "excludeDomains": ["reddit.com"]
}
```

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `query` | string | required | Search query |
| `limit` | integer | `5` | Maximum results per source type |
| `sources` | array | `[{"type": "web"}]` | Source types. Accepts v2 objects `[{"type": "web"}]` or plain strings `["web"]`. Valid types: `"web"`, `"images"`, `"news"` |
| `timeout` | integer | `30000` | Timeout in milliseconds |
| `scrapeOptions` | object | `null` | Nested scrape options for fetching result page content |
| `timeRange` | string | `null` | Restrict results to the past `"day"`, `"week"`, `"month"`, or `"year"` (mapped per provider) |
| `startDate` | string | `null` | Earliest result date (YYYY-MM-DD) |
| `endDate` | string | `null` | Latest result date (YYYY-MM-DD) |
| `topic` | string | `null` | Topic vertical: `"general"`, `"news"`, or `"finance"`. Honoured natively by Tavily and Exa; other providers ignore it |
| `includeDomains` | string[] | `null` | Restrict results to these domains |
| `excludeDomains` | string[] | `null` | Exclude results from these domains |

**Response body:**

```json
{
  "success": true,
  "data": {
    "web": [
      {
        "title": "Web Scraping with Python",
        "url": "https://...",
        "description": "A guide...",
        "markdown": null
      }
    ],
    "images": [{ "title": "Scraping diagram", "imageUrl": "https://...", "url": "https://..." }],
    "news": [{ "title": "Python 3.13 Released", "url": "https://...", "snippet": "The latest..." }]
  }
}
```

**Example:**

```shell
curl -s http://localhost:8308/search \
  -H 'Content-Type: application/json' \
  -d '{"query": "python web scraping", "limit": 3}'
```

---

### POST /extract

Start an asynchronous LLM extraction job. Returns a job ID immediately; use `GET /extract/{id}` to poll for results.

**Request body:**

```json
{
  "urls": ["https://example.com/products"],
  "prompt": "Extract product names and prices",
  "schema": {
    "type": "object",
    "properties": {
      "name": { "type": "string" },
      "price": { "type": "number" }
    }
  },
  "scrapeOptions": null
}
```

| Field           | Type     | Default  | Description                                  |
| --------------- | -------- | -------- | -------------------------------------------- |
| `urls`          | string[] | required | URLs to extract data from                    |
| `prompt`        | string   | `null`   | Extraction prompt describing what to extract |
| `schema`        | object   | `null`   | JSON Schema for structured output            |
| `scrapeOptions` | object   | `null`   | Nested scrape options                        |

**Response body:**

```json
{
  "success": true,
  "id": "660e8400-e29b-41d4-a716-446655440001"
}
```

**Example:**

```shell
curl -s http://localhost:8308/extract \
  -H 'Content-Type: application/json' \
  -d '{
    "urls": ["https://example.com/about"],
    "prompt": "Extract the company name and founding year"
  }'
```

---

### GET /extract/{id}

Poll the status of an extract job.

**Response body:**

```json
{
  "success": true,
  "status": "completed",
  "data": [{ "name": "Example Corp", "founding_year": 2010 }],
  "error": null
}
```

**Example:**

```shell
curl -s http://localhost:8308/extract/660e8400-e29b-41d4-a716-446655440001
```

---

### POST /batch/scrape

Start an asynchronous batch scrape job. Scrapes multiple URLs with the same options. Returns a job ID; use `GET /batch/scrape/{id}` to poll.

**Request body:**

```json
{
  "urls": ["https://example.com/page1", "https://example.com/page2"],
  "formats": ["markdown"],
  "onlyMainContent": true,
  "waitFor": 0,
  "timeout": 30000
}
```

| Field  | Type     | Default  | Description    |
| ------ | -------- | -------- | -------------- |
| `urls` | string[] | required | URLs to scrape |

All other fields are the same as POST /scrape (formats, onlyMainContent, waitFor, timeout, includeTags, excludeTags, mobile, actions, location, headers, maxAge, proxy, storeInCache), applied to every URL in the batch.

**Response body:**

```json
{
  "success": true,
  "id": "770e8400-e29b-41d4-a716-446655440002"
}
```

**Example:**

```shell
curl -s http://localhost:8308/batch/scrape \
  -H 'Content-Type: application/json' \
  -d '{
    "urls": ["https://example.com", "https://example.org"],
    "formats": ["markdown"]
  }'
```

---

### GET /batch/scrape/{id}

Poll the status of a batch scrape job. Results are paginated; follow the `next` URL for additional pages.

**Query parameters:**

| Parameter | Type    | Default | Description                             |
| --------- | ------- | ------- | --------------------------------------- |
| `offset`  | integer | `0`     | Pagination offset into the results list |

**Response body:**

```json
{
  "status": "completed",
  "total": 2,
  "completed": 2,
  "data": [
    {
      "markdown": "# Example Domain\n...",
      "metadata": {
        "title": "Example Domain",
        "sourceURL": "https://example.com",
        "statusCode": 200
      }
    },
    {
      "markdown": "# Example Org\n...",
      "metadata": { "title": "Example Org", "sourceURL": "https://example.org", "statusCode": 200 }
    }
  ],
  "next": null
}
```

**Example:**

```shell
curl -s http://localhost:8308/batch/scrape/770e8400-e29b-41d4-a716-446655440002
```

---

### GET /team/credit-usage

Credential verification stub. n8n's Firecrawl node tests credentials by hitting this endpoint. Supacrawl is self-hosted, so credits are always zero.

**Response body:**

```json
{
  "success": true,
  "data": { "credits": 0 }
}
```

**Example:**

```shell
curl -s http://localhost:8308/team/credit-usage \
  -H 'Authorization: Bearer YOUR_KEY'
```

---

### GET /supacrawl/health

Health check returning version, uptime, and status. This endpoint never requires authentication.

**Response body:**

```json
{
  "success": true,
  "version": "2026.3.1",
  "status": "healthy",
  "uptime_seconds": 3742
}
```

**Example:**

```shell
curl -s http://localhost:8308/supacrawl/health
```

---

### POST /supacrawl/diagnose

Run pre-scrape diagnostics on a URL. Reports CDN, bot protection, JavaScript requirements, and other characteristics useful for choosing scrape settings.

**Request body:**

```json
{
  "url": "https://example.com"
}
```

**Example:**

```shell
curl -s http://localhost:8308/supacrawl/diagnose \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://protected-site.com"}'
```

---

### POST /supacrawl/summary

Scrape a URL and return a summary of its content from the configured LLM (`SUPACRAWL_LLM_PROVIDER`, `SUPACRAWL_LLM_MODEL`). The page body is never returned; with no LLM configured the request fails before the page is fetched.

**Request body:**

```json
{
  "url": "https://example.com/article",
  "maxLength": 500,
  "focus": "key findings"
}
```

| Field       | Type    | Default  | Description                |
| ----------- | ------- | -------- | -------------------------- |
| `url`       | string  | required | URL to summarise           |
| `maxLength` | integer | `120`    | Most words in the summary, capped at 1000 |
| `focus`     | string  | `null`   | Focus area for the summary |

**Example:**

```shell
curl -s http://localhost:8308/supacrawl/summary \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://example.com/article", "focus": "conclusions"}'
```

**Response:**

```json
{
  "success": true,
  "data": {
    "url": "https://example.com/article",
    "title": "Article title",
    "summary": "..."
  },
  "correlation_id": "1a2b3c4d"
}
```

## Async Jobs

The crawl, extract, and batch scrape endpoints are asynchronous. They follow a consistent lifecycle:

1. **Submit** a POST request. The server returns `{"success": true, "id": "<job-id>"}` immediately.
2. **Poll** with GET using the job ID. The response includes a `status` field.
3. **Status transitions**: `scraping` (in progress), `completed` (finished successfully), `failed` (error occurred), `cancelled` (cancelled via DELETE).

```shell
# 1. Start a crawl
JOB_ID=$(curl -s http://localhost:8308/crawl \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://example.com", "limit": 10}' | python -c "import sys,json; print(json.load(sys.stdin)['id'])")

# 2. Poll until complete
curl -s "http://localhost:8308/crawl/$JOB_ID"

# 3. Cancel if needed
curl -s -X DELETE "http://localhost:8308/crawl/$JOB_ID"
```

### Concurrency Limits

A maximum of 3 concurrent async jobs (crawl, extract, and batch scrape combined) are allowed. Additional submissions return HTTP 429. This is configurable via `SUPACRAWL_API_MAX_JOBS`.

### Pagination

GET endpoints for crawl and batch scrape paginate results (up to 10 MB per response). When more results are available, the response includes a `next` URL to fetch the next page.

### Job Expiry

Jobs are stored in memory and expire after a configurable TTL (default 24 hours). Jobs are lost on server restart.

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `SUPACRAWL_API_KEY` | unset | Bearer token for authentication. When unset, auth is disabled |
| `SUPACRAWL_API_HOST` | `0.0.0.0` | Server bind address |
| `SUPACRAWL_API_PORT` | `8308` | Server bind port |
| `SUPACRAWL_API_JOB_TTL` | `86400` | Async job expiry in seconds (default 24 hours) |
| `SUPACRAWL_API_MAX_JOBS` | `3` | Maximum concurrent async jobs |

## Using with n8n

n8n has a built-in Firecrawl node. Point it at your local Supacrawl server:

1. Install and start Supacrawl:

   ```shell
   pip install supacrawl[api]
   export SUPACRAWL_API_KEY=YOUR_KEY
   supacrawl serve
   ```

2. In n8n, add a **Firecrawl** credential:
   - **API Key**: `YOUR_KEY`
   - **API URL**: `http://localhost:8308`

3. Add a Firecrawl node to your workflow. It will use Supacrawl as its backend.

n8n verifies credentials by calling `GET /team/credit-usage`. Supacrawl returns a valid response, so the credential test passes.

## Error Responses

All errors use a consistent envelope:

```json
{
  "success": false,
  "error": "Human-readable error message"
}
```

### Status Codes

| Code | Meaning |
| --- | --- |
| 200 | Success |
| 400 | Bad request (invalid input). FastAPI's 422 validation errors are remapped to 400 with the standard error envelope |
| 401 | Missing or invalid API key |
| 404 | Job not found |
| 429 | Too many concurrent async jobs |
| 500 | Internal server error |

### Protocol Notes

- The API accepts **camelCase** field names (matching the Firecrawl v2 protocol) and translates to snake_case internally.
- Unsupported v2 fields are accepted silently and ignored. Clients do not need modification.
- CORS is permissive (all origins) by default, suitable for local and homelab deployments.
