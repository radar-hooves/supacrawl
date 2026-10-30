# Configuration

supacrawl runs with no configuration out of the box. When you do want to change a default — pin a browser engine, set your locale, throttle search — there is one typed settings model, one local store, and a clear precedence order. The same model emits a machine-readable schema so a separate control-plane GUI can render a settings form and tune supacrawl without touching the CLI.

## Precedence

Settings resolve in three layers, each overriding the one before:

1. **Built-in defaults** — sensible values baked into the model.
2. **The stored file** — `~/.supacrawl/config.toml` (override the location with `SUPACRAWL_CONFIG_PATH`). Human-readable TOML; only values that differ from the default are written, so the file stays small.
3. **Environment variables** — `SUPACRAWL_<NAME>` (e.g. `SUPACRAWL_TIMEOUT`, `SUPACRAWL_ENGINE`). An environment variable always wins over the stored file.

On top of all three, a **per-command flag** (`supacrawl scrape --engine camoufox`) overrides the standing baseline for that one invocation.

```text
default  <  stored file  <  environment  <  per-command flag
```

## Managing settings from the CLI

```bash
supacrawl config get                 # effective values (environment applied)
supacrawl config get --stored        # only the file + defaults, env ignored
supacrawl config get engine          # a single value
supacrawl config set engine camoufox # persist a default to the store
supacrawl config unset engine        # revert to the default
supacrawl config path                # where the store lives
```

`config set` validates the value against the field's type and range before writing, so a bad value is rejected at the point of entry rather than at scrape time.

## Runtime adoption status

The settings schema, the store, and the `config` CLI are complete. Runtime consumption is being adopted incrementally: the **`strategy_memory`, `metrics`, and `metrics_full_url`** settings are read from the resolved config (store + environment) on every CLI and MCP run today. The remaining knobs (browser, anti-bot, search, locale, cache) are exposed in the schema and persisted in the store for a control-plane GUI; their adoption into each command's option resolution is rolling out. Until then, set those via their environment variables or per-command flags.

## Settings

Every setting is a standing default; a per-request flag or API argument still overrides it. Grouped as the GUI renders them:

| Group | Setting | Env var | Default | Notes |
| --- | --- | --- | --- | --- |
| browser | `timeout` | `SUPACRAWL_TIMEOUT` | `30000` | Page load timeout (ms), 1000–300000. |
| browser | `headless` | `SUPACRAWL_HEADLESS` | `true` | Run without a visible window. |
| browser | `wait_until` | `SUPACRAWL_WAIT_UNTIL` | `domcontentloaded` | `domcontentloaded` / `load` / `networkidle`. |
| browser | `user_agent` | `SUPACRAWL_USER_AGENT` | _(engine default)_ | Override the User-Agent string. |
| browser | _(env only)_ | `SUPACRAWL_DISABLE_DEV_SHM` | _(auto: on in a container)_ | Pass `--disable-dev-shm-usage` to Chromium so it uses disk instead of the container's small `/dev/shm`, avoiding crashes on heavy pages. Auto-detected inside a container; set `1`/`0` to force on/off. Chromium engines only (playwright/patchright); Camoufox is Firefox. |
| browser | _(env only)_ | `SUPACRAWL_BROWSER_CLOSE_TIMEOUT` | `3` | Seconds to allow a graceful browser close before the driver reaps the process instead. Teardown happens after the content is already extracted, and Chromium's own close regularly stalls on a page with live connections until an internal 30s timeout releases it — pure dead time on the caller's clock. Raise it for a politer close; `0` reaps immediately. |
| locale | `locale` | `SUPACRAWL_LOCALE` | `en-US` | Maps to Accept-Language. |
| locale | `timezone` | `SUPACRAWL_TIMEZONE` | `UTC` | e.g. `Australia/Brisbane`. |
| anti_bot | `engine` | `SUPACRAWL_ENGINE` | _(auto)_ | `playwright` / `patchright` / `camoufox`. Leave unset to auto-escalate. |
| anti_bot | `stealth` | `SUPACRAWL_STEALTH` | `false` | Start with Patchright. Usually unnecessary. |
| anti_bot | `escalate` | `SUPACRAWL_ESCALATE` | `true` | Auto-climb the engine/stealth ladder on a poor result. |
| anti_bot | `solve_captcha` | `SUPACRAWL_SOLVE_CAPTCHA` | `false` | Needs a CAPTCHA API key; each solve costs money. |
| anti_bot | _(env only)_ | `SUPACRAWL_CAPTCHA_FAIL_FAST` | `true` | Stop after one attempt on a bare CAPTCHA wall instead of walking the whole engine ladder — a stronger engine does not solve a CAPTCHA, so the three further escalations are wasted (#153). A CAPTCHA inside a CDN managed-challenge interstitial (Cloudflare "just a moment") is a `bot_challenge`, not a `captcha`, and still escalates. Set `0` to restore the full-ladder walk. |
| content | `only_main_content` | `SUPACRAWL_ONLY_MAIN_CONTENT` | `true` | Strip nav/header/footer. |
| search | `search_providers` | `SUPACRAWL_SEARCH_PROVIDERS` | _(none)_ | Ordered fallback chain, e.g. `brave,tavily,serper`. |
| search | `search_provider` | `SUPACRAWL_SEARCH_PROVIDER` | `brave` | Legacy single provider. |
| search | `search_rate_limit` | `SUPACRAWL_SEARCH_RATE_LIMIT` | _(provider default)_ | Requests per second. |
| search | — | `SUPACRAWL_SEARCH_STRICT_PROVIDERS` | `false` | Refuse the built-in DuckDuckGo fallback: a configured provider that cannot be used fails loudly instead of quietly leaving via a provider you did not choose. |
| search | — | `SUPACRAWL_SEARCH_PUBLIC_FALLBACK` | `false` | Opt in to a DuckDuckGo fallback behind a backend that FAILS AT RUNTIME (e.g. SearXNG engines down). Off by default so a self-hosted backend's failure surfaces as a loud error rather than a query silently leaving in-house; `STRICT_PROVIDERS` overrides it. |
| memory | `strategy_memory` | `SUPACRAWL_STRATEGY_MEMORY` | `true` | Per-domain strategy learning. |
| telemetry | `metrics` | `SUPACRAWL_METRICS` | `true` | Record one quality/usage event per scrape/search. |
| telemetry | `metrics_full_url` | `SUPACRAWL_METRICS_FULL_URL` | `false` | Log full URLs, not just the domain. Off for privacy. |
| cache | `cache_dir` | `SUPACRAWL_CACHE_DIR` | _(default location)_ | Where cached content lives. |

## Secrets

Credentials are **environment-only**. They are never written to the store and never appear in the GUI schema — a dashboard can see _whether_ each is set, never its value.

```bash
supacrawl config secrets   # presence only, never the value
```

Honoured: `CAPTCHA_API_KEY`, `BRAVE_API_KEY`, `TAVILY_API_KEY`, `SERPER_API_KEY`, `SERPAPI_API_KEY`, `EXA_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, and `SUPACRAWL_PROXY` (a proxy URL can carry credentials).

`SEARXNG_URL` is a clean instance URL with no credentials embedded (e.g. `https://searxng.example.invalid`); when the instance sits behind an HTTP Basic-auth gate, pair it with `SEARXNG_USERNAME` and `SEARXNG_PASSWORD`, whose presence is reported alongside the keys above. `SEARXNG_URL` itself is ordinary configuration rather than a credential, so it is not listed there. Embedding a credential directly in `SEARXNG_URL` (`https://user:pass@host`) still works as a deprecated fallback, but turns the whole URL into a secret, so prefer the discrete username/password pair.

The MCP server can skip the environment entirely: set `SEARXNG_PORTCULLIS_CREDENTIAL` to the catalogue name of a [Portcullis](https://github.com/radar-hooves/portcullis) credential carrying `username` and `password` fields, and the pair is fetched from the broker in-process at startup — the credential never becomes an environment variable, a rendered config value, or a file on the host. A fetched pair beats `SEARXNG_USERNAME` / `SEARXNG_PASSWORD`, which in turn beat any userinfo left in `SEARXNG_URL`. Unset (the default) means no fetch and no change in behaviour, so the CLI and the REST API are unaffected. When the broker cannot produce a usable credential the server starts degraded and search fails loudly, rather than falling through to a search engine nobody configured.

When a self-hosted SearXNG's own engines go down under load, every query comes back empty. When SearXNG _names_ the failed engines (reports them as unresponsive) supacrawl surfaces a loud error naming them (not a silent `[]`), because for a backend chosen to keep queries in-house, quietly routing them to a public engine is a privacy failure rather than a degradation. `SUPACRAWL_SEARCH_PUBLIC_FALLBACK=1` opts into a DuckDuckGo fallback for exactly that case — degraded-but-answering search, at the cost of that query leaving in-house. Worth knowing before deciding: if your SearXNG already lists DuckDuckGo among its own engines, enabling the fallback introduces **no new third-party recipient** — the same host already sees the query; the fallback only bypasses SearXNG's aggregation for it. `SUPACRAWL_SEARCH_STRICT_PROVIDERS=1` overrides the opt-in back to fail-loudly.

A quieter failure exists: SearXNG's upstream engines get CAPTCHA-walled and return _nothing_ while SearXNG itself answers HTTP 200 with an empty set and names no unresponsive engine — indistinguishable, on a single query, from a genuine no-match. supacrawl handles it without ever turning a real no-match into an error: an empty answer from one provider lets the chain try the next **configured** provider (never an unconfigured public engine unless `PUBLIC_FALLBACK` is on), an unbroken run of empty answers degrades that provider's health (`consecutive_empty` climbs; a query with matches clears it), and — once a full window of caller searches has come back empty in a row — every search response carries `all_recent_empty: true`. That last flag is what lets a caller tell "no matches" from "the backend has stopped answering" off the response itself, without polling `supacrawl_health`. An all-configured-providers-empty result stays `success: true` with `data: []`: a query with nothing to find is a real outcome, not a failure.

## Filtering what the MCP server returns

`SUPACRAWL_OUTPUT_FILTER=package.module:callable` (MCP server only) scrubs every string the server returns to its client through a function you supply, so a PII masker, profanity filter or DLP call sits between scraped pages and the model. Unset (the default) changes nothing. supacrawl knows nothing about what the filter does.

**The callable's contract:** a plain `str -> str`, importable as `module:callable` (a dotted attribute such as `module:obj.method` works) from the same environment as supacrawl. It runs inline on the server's event loop, once per text field, so keep it fast. It must return a string.

**Fail closed.** A value that cannot be imported, is not `module:callable`, or is not callable stops `supacrawl-mcp` at start with an error naming `SUPACRAWL_OUTPUT_FILTER`; the server never serves unfiltered. A filter that raises or returns a non-string fails that tool call with an error naming the setting and carrying no page text.

**Filtered:** every string in every tool result — `scrape` (markdown, html, rawHtml, links, images, metadata, summary, json, structuredData), `crawl`, `batch`, `map`, `search`, `extract`, `summary`, `diagnose`, `health` — dictionary keys, integers whose digits the filter would change (returned as the filtered string; a key collision after filtering withholds the response), and the text of any error a tool raises. A scrape response costs one call per string field, not one per line. Tools' own status fields and URLs are strings too, so they pass through the filter as well. HTML is filtered as markup text: an identifier split by tags is the filter's problem.

**Not filtered:** screenshots and PDFs (base64 binary, not text; skipped only where a `screenshot`, `screenshots` or `pdf` field holds a long, strictly valid base64 run: no whitespace, padded, decodable), other non-string values (booleans, floats), the server's static resources and prompts, and logs and the local cache, which hold unfiltered page content on the host. The REST API (`supacrawl-api`) is a separate surface and is not covered.

**The seam.** A FastMCP middleware on `tools/call`, installed outermost, filters the whole result; a tool added later is covered without any code in it. A result carrying a content block that is neither text, image nor text resource is withheld rather than passed unproven.

## Telemetry over OTLP

supacrawl always writes telemetry to the local `events.jsonl` (the durable record). Each event is **also** emitted as one structured log record on the `supacrawl.telemetry` logger, so a standard log/trace collector can pick it up without supacrawl pushing to any log store's own API or holding a credential for one.

Every entry point (the CLI, `supacrawl serve`, and `supacrawl-mcp`) configures its logging once at startup from the standard OpenTelemetry environment:

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=https://collector.example.invalid   # ship over OTLP
```

Set it and log records (this telemetry included) ship to that collector. Leave it unset and records go to stdout (stderr for `supacrawl-mcp`, whose stdout is the stdio protocol channel) as JSON lines carrying `timestamp`, `level`, `service_name`, `service_version`, `message`, `logger`, and the event's own fields (`domain`, `verdict`, `score`, `latency_ms`, ...) — filter on `message` (`scrape` or `search`) or any field.

Privacy carries over from the local sink: only what `events.jsonl` contains is logged — domain-only unless you opt into `metrics_full_url`.

## The settings schema (for a GUI)

`supacrawl config schema` emits the JSON schema of the settings model. Each field carries `x-ui` render metadata so a control-plane dashboard can build a settings form directly from it:

```json
{
  "properties": {
    "timeout": {
      "type": "integer",
      "default": 30000,
      "minimum": 1000,
      "maximum": 300000,
      "x-ui": {
        "group": "browser",
        "order": 10,
        "widget": "slider",
        "help": "How long to wait for a page to load..."
      }
    }
  }
}
```

The `x-ui` keys are `group`, `order`, `widget`, `help`, and an optional `visible_when` (conditional visibility, e.g. `metrics_full_url` is shown only when `metrics` is on). A GUI reads the schema for layout, reads and writes values through the same store the CLI uses, and reads `config secrets` for credential presence. The CLI emits; a GUI consumes — they share one source of truth.

## Control plane and the UI seam

supacrawl is the control plane; a UI is a separate plane that plugs into it — the engine ships no front-end of its own, the way a coordination server (e.g. Headscale) exposes an API and CLI while its web UIs live in separate projects. The seam has two halves:

- **Settings** — the typed config store, the `x-ui` schema, and the `config` CLI. A UI renders the schema, reads/writes values through the store, and checks credential presence via `config secrets`.
- **Telemetry** — the local `events.jsonl` (read with `MetricsReader` / `metrics summary`) and, over OTLP, whatever collector `OTEL_EXPORTER_OTLP_ENDPOINT` names (see [Telemetry over OTLP](#telemetry-over-otlp)).

When `supacrawl serve` is running, the settings and telemetry state are also exposed read-only over HTTP, so a front-end can plug in without shelling out to the CLI:

| Endpoint                                | Returns                                                                   |
| --------------------------------------- | ------------------------------------------------------------------------- |
| `GET /supacrawl/config/schema`          | the `x-ui` settings schema, to render a form                              |
| `GET /supacrawl/config`                 | effective non-secret values plus a secret **presence** map (never values) |
| `GET /supacrawl/metrics/summary?days=N` | the telemetry rollup, without parsing the raw JSONL                       |

Writes still go through the store, and credentials stay environment-only, so these read endpoints never expose a secret. For live dashboards a UI reads the OTLP collector directly; for a local control panel it reads these endpoints. Either way supacrawl provides the seam and stays UI-agnostic.
