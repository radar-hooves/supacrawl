# Changelog

All notable changes to supacrawl will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project adheres to calendar-based versioning (YYYY.MM.x format).

## [Unreleased]

### Security

- **Outbound SSRF guard on every fetch supacrawl owns** (#152): a crawler fetches whatever URL it is handed, and supacrawl owns the socket, so only supacrawl can refuse a target. New `supacrawl.services.url_guard` resolves each host and refuses the fetch if **any** returned address is in a blocked range — checking the URL string is not enough, since owning a DNS record defeats an IP-literal check — then connects to that validated address literally, carrying the real hostname in `Host` and TLS SNI so nothing can change the answer between check and connect. `guarded_request`/`guarded_stream` follow redirects by hand, one hop at a time, so **every redirect hop is re-validated and re-pinned** by the same code, with no call site able to forget. Wired into the HTTP-first scrape path, PDF fetches, sitemap and robots discovery, map, diagnose, MCP URL validation, and the search-result metadata probe.
- **Every IPv6-embeds-IPv4 spelling of a blocked address is now covered, not just the outer form** (#152, closes a critical bypass found in fresh-context adversarial review): an IPv6 literal is never `in` an IPv4 `ipaddress.Network`, so `::ffff:169.254.169.254` (IPv4-mapped), `::169.254.169.254` (the deprecated IPv4-compatible form), a 6to4-wrapped (`2002::/16`) or NAT64-wrapped (`64:ff9b::/96`) metadata address all routed straight to the cloud-metadata endpoint on a dual-stack host while sailing past a classifier that only ever checked the address's own IPv6 form. The guard now also checks the IPv4 destination any of these forms embeds. Teredo (`2001::/32`) is a documented, accepted residual — its embedded IPv4 is the tunnelling server, not the destination, and no supported OS auto-tunnels it.
- **A parser-divergence backslash/control-character URL is refused before parsing** (#152): `urllib.parse` (RFC 3986) and a WHATWG browser engine disagree on the authority of a URL like `http://169.254.169.254\@allowed-host/` — Python reads host `allowed-host`, Chromium navigates to `169.254.169.254`. Any raw backslash or ASCII control character in a URL is now refused outright (a percent-encoded form still passes), closing the class for both the httpx and browser paths at the shared `assert_safe_url` chokepoint.
- **A non-ASCII host is refused rather than IDNA-normalised** (#152): `urllib.parse` does no IDNA/UTS-46 normalisation, while a browser engine does through its own table, and the two are not guaranteed to agree — the same divergence shape as the backslash bypass, reached through Unicode normalisation instead of authority parsing. A Unicode hostname is refused; the unambiguous punycode (`xn--...`) form is unaffected.
- **`SUPACRAWL_BLOCK_PRIVATE_NETWORKS`** (#152): link-local and cloud-metadata ranges (169.254.0.0/16, fe80::/10 — including the AWS/GCP/Azure IMDS endpoint, in every address form above) are refused **always**; private/RFC1918 and loopback targets stay reachable by default, because crawling an internal documentation site is a first-class use of a self-hosted tool. A consumer accepting untrusted URLs sets this switch to refuse those too. Ranges and semantics mirror ragify's `RAGIFY_BLOCK_PRIVATE_NETWORKS` so the two libraries agree.
- **Browser path: checked every hop, not pinned** (#152): Chromium owns its own resolver and socket, so there is no seam to hand it a validated address — the pinned-address guarantee the httpx path gets is **not** available there. The target is validated before navigation, and a Playwright route handler re-validates every request the page makes (redirect hops and subresources included), aborting any that resolves into a blocked range. The residual time-of-check/time-of-use window on the browser path — the engine re-resolves each hostname itself when it actually connects — is real and is recorded in `url_guard`'s module docstring rather than left silent.
- **A blocked target no longer degrades into an unguarded browser fetch** (#152): the HTTP-first path swallowed every exception and returned `None`, which handed the same refused URL straight to the browser. A guard refusal now propagates instead.
- **The resolver's blocking DNS lookup runs off the event loop** (#152): `resolve_and_pin`'s `getaddrinfo` call is offloaded via `asyncio.to_thread` on every guarded hop and browser pre-flight, so a slow resolver cannot stall other concurrent fetches — matching how httpx threads its own resolution.
- **The IPv6 unspecified address `::` is now blocked in strict mode** (#152, found in adversarial review): a `connect()` to `::` is routed by the OS to loopback, the same shape as `0.0.0.0/8` (already blocked) — without this, `http://[::]/` reached a loopback-bound service even under `SUPACRAWL_BLOCK_PRIVATE_NETWORKS=1`, defeating the one protection strict mode exists to add.
- **The CAPTCHA-solving browser path and the benchmark reference renderer now install the same per-request navigation guard as every other browser path** (#152, found in adversarial review): both previously did the pre-flight `resolve_and_pin` check on the original URL only, with nothing re-validating a subsequent redirect or a page-issued subresource request — the CAPTCHA path is reachable via the ordinary `solve_captcha=True` scrape option, so this was a real gap, not a theoretical one.
- **A malformed redirect authority now fails as `ValidationError`, not a raw `ValueError`** (#152, found in adversarial review): Python 3.14 hardened `urllib.parse` to raise `ValueError` for a malformed bracketed-IPv6 authority rather than returning an odd result; an attacker-controlled `Location` header could trigger this and escape past a caller (e.g. the browser route handler) that only catches `ValidationError`. Both the `assert_safe_url` and redirect-following (`urljoin`) call sites now convert it.

- **A gated SearXNG instance no longer needs its credential inside `SEARXNG_URL`**: the single-string shape was the defect — it made the whole URL a secret, so anything resolving `${SEARXNG_URL}` printed the password, and httpx quotes the request URL verbatim into its own error messages, putting the password in the log on the very 401 an operator is most likely to hit. `SEARXNG_URL` is now a clean URL and the HTTP Basic credential arrives separately as `SEARXNG_USERNAME` / `SEARXNG_PASSWORD`. The `user:pass@host` shape still authenticates so an existing installation keeps working, but the userinfo is split off the URL at construction, so the URL that reaches the wire, the logs, and any error message never carries it; the discrete pair wins when both are configured.
- **The SearXNG credential is applied per request, never on the shared HTTP client**: `create_provider` hands every provider in the chain the same `httpx.AsyncClient`, so a credential set on that client would have been sent to Brave, Tavily, Serper and every other host it talks to — a wider leak than the one being closed. A regression test drives two real providers over one real shared client and asserts the credential appears on the SearXNG request and on nothing else.
- **The MCP server can now take the SearXNG credential without it ever being an environment variable**: splitting the credential off the URL only moved the problem — it still had to reach the process somehow, and an env var is readable by anything that can see the process. With `SEARXNG_PORTCULLIS_CREDENTIAL` set, the server fetches the `username`/`password` pair from the secrets broker in-process at startup, so the credential exists only in memory: not in a rendered config, not in a file, not in the environment. A fetched pair beats `SEARXNG_USERNAME` / `SEARXNG_PASSWORD`, which still beat any userinfo left in `SEARXNG_URL`.
- **A failed credential fetch fails closed**: the server starts in a degraded state (health reports the gap, tools return a typed unavailable error) rather than continuing without the credential and letting the chain answer from a third-party engine nobody configured — the silent-fallback shape this whole line of work exists to close. Half a fetched pair is refused for the same reason: it would look like a working credential right up to the 401, which under a strict provider chain is indistinguishable from the instance being down. The gap surfaces as a typed error rather than a builtin `ConnectionError`, so the MCP retry middleware does not retry-storm a broker outage that will not recover inside its window.

### Added

- **`SEARXNG_PORTCULLIS_CREDENTIAL`**: the catalogue name of a Portcullis credential carrying the SearXNG `username`/`password` pair, fetched by the MCP server at startup. Optional and empty by default — unset, behaviour is exactly what it was, which matters because the REST API container reaches an ungated instance on an internal network with no credential and no broker identity at all. `SearchService`, `build_provider_chain` and `create_provider` gain matching `searxng_username` / `searxng_password` arguments, so any embedder can supply the credential from wherever it keeps secrets rather than through the environment. `supacrawl config secrets` reports when the brokered path is configured (the catalogue name, never a value), so the deliberately-absent `SEARXNG_USERNAME` / `SEARXNG_PASSWORD` no longer read as a misconfiguration to an operator debugging it.
- **`SEARXNG_USERNAME` / `SEARXNG_PASSWORD`**: discrete HTTP Basic credentials for a SearXNG instance behind an auth gate, so the instance URL stays a plain URL. Both optional and independent of availability — an ungated instance still needs only `SEARXNG_URL`, and half a credential is refused with a warning naming the missing variable and what actually goes out instead, rather than being silently dropped. Their presence (never their value) is reported by `supacrawl config secrets`, so "is my credential being picked up?" is answerable from the CLI rather than only from a log line at request time.
- **`quality.verdict: "infrastructure"`** (#160): a tenth verdict, and the only one that does not describe the target site. It means supacrawl's own engine failed and the request never left the building, so a caller can finally tell "this site is a problem, escalate differently" from "the scraper is broken, restart it" — previously identical from the outside. `QualityAssessment.is_scraper_fault` exposes the same split in code.
- **`components.browser.alive` / `.relaunches` on the health surface** (#160): the shared engine's liveness is now read live rather than inferred from failing scrapes, and a dead engine drives the top-level `status` to `degraded` with a warning. A climbing relaunch count is the signal that something keeps killing the engine even though each individual scrape recovered.
- **`SUPACRAWL_SEARCH_STRICT_PROVIDERS`** (#158): opt-in switch that refuses implicit provider fallback. With it set, a configured provider that cannot be used fails the search loudly instead of quietly handing the query to DuckDuckGo. Off by default so a fresh install still answers.
- **`SearchResult.provider` / `SearchResult.provider_fallback`** (#158): every search result now names the provider that actually served it and flags whether that provider was one the operator configured, so a caller no longer has to infer it.
- **`SUPACRAWL_SEARCH_PUBLIC_FALLBACK`** (#161): opt-in switch that adds DuckDuckGo behind a configured backend that FAILS AT RUNTIME (SearXNG's engines going down, a keyed provider's quota exhausted), so search still answers instead of erroring. Off by default and deliberately so — for a self-hosted SearXNG, silently routing a query to a public engine on a hiccup is the privacy failure `SUPACRAWL_SEARCH_STRICT_PROVIDERS` exists to guard against, so the default is a loud typed error naming the dead engines instead. Where a SearXNG instance already lists DuckDuckGo among its own engines, enabling this adds no new third-party recipient; it only bypasses SearXNG's aggregation for that query. `STRICT_PROVIDERS` overrides it.
- **`SearchResult.unresponsive_engines`** (#161): SearXNG reports the engines that failed on a query in its own response (`unresponsive_engines`); this surfaces them on the result, so a caller can tell "no results exist for this query" from "every engine that would have answered was down". Populated on an empty/failed result caused by upstream engine failure.
- **`components.search.recent_search_health` on the health surface** (#161): the last N real caller searches are tracked, and the search component degrades when a full window all came back empty — a continuous-failure state that a per-provider `consecutive_failures` count missed, because a zero-result success was counted as a failure nowhere. The synthetic probe is excluded, so a health check never answers its own question.

### Fixed

- **A crawler tarpit (Nepenthes/iocaine-style) no longer reads as `ok`**: an LLM research agent hit a soft-404 report path on `reports.exodus-privacy.eu.org` that served an HTTP 200 of Markov word-salad prose over an infinite tree of self-linking generated URLs, and the quality gate — judging only word count and link density — called it `ok` (score 62, 148 words), so the agent took the junk for real content. `quality.assess_quality()` gains a new `tarpit` verdict: a link is a maze child when its path is a strict descendant of the page's own URL and its slug looks generated (three or more hyphen-joined words, or a raw non-ASCII codepoint); ≥80% of same-host links being maze children, with ≥50% of those generated-looking, is a page that architecturally cannot be real (a real page links sideways and up, never almost exclusively into paths it just invented). `tarpit` is a hard fail (`success: false`, never cached) but — same reasoning as the #153 CAPTCHA fail-fast — deliberately excluded from the auto-escalation set: a stronger engine just fetches another generated maze page. Verified live against the exact reported URL (now `tarpit`, score 10) and against `en.wikipedia.org/wiki/Web_scraping` (4824 words, hundreds of same-site links, still `ok`, score 85) to confirm heavy internal cross-linking alone does not trip it.

- **A browser scrape no longer waits on a teardown that has nothing left to do**: the markdown is fully extracted before the browser is closed, yet Chromium's own `close()` regularly stalls on a page with live connections and is released only by an internal 30s timeout — pure dead time on the caller's clock, and invisible because the content was already in hand. The graceful close is now bounded (`SUPACRAWL_BROWSER_CLOSE_TIMEOUT`, default 3s) and the driver reaps the process instead. Measured end to end, same three URLs alternating back to back: **29.8s mean before, 15.3s after**, byte-identical markdown; across a 14-URL real-site battery, 6m59s before, 2m00s after. Nothing depended on the graceful path — supacrawl records no video, HAR or trace — and the browser process is reaped either way, with no residue in either arm. Camoufox's teardown is deliberately left unbounded: its context manager closes the browser and only _then_ stops playwright, so abandoning it partway leaks the Firefox and driver processes.

- **CI's test job is collectable again** (red since 2026-08-31): `tests/test_secret_redaction.py` imports the MCP layer, which the public CI job cannot install — `mcp-common` is a private package. Its three sibling MCP-importing test files carry both a `pytest.mark.mcp` marker and a `--ignore` in the workflow; this one carried neither, and a marker would not have saved it anyway, because the ImportError fires at COLLECTION time, before any marker can deselect a module. One `pytest.importorskip("fastmcp")` ahead of the imports now skips the module wherever the extra is absent, in CI and for any contributor who installed without it. Verified by reproducing the CI dependency set locally: the exact `Interrupted: 1 error during collection` before, 1417 passed and 1 skipped after — the same 1417 the job selects.

- **A failed telemetry push no longer spills an HTML error page into stderr**: the failure detail quoted the response body on the stated grounds that "Loki's error body is safe to log" — true of Loki, but a gateway in front of it answers 401/403 with an HTML page, so every CLI command against a misconfigured endpoint printed four lines of markup cut off mid-tag. Loki's own plain-text errors are still quoted (they name the fix); an HTML or empty body now reports the status code alone, on one line.

- **The escalation ladder no longer climbs into an engine whose browser binary was never fetched**: the binary-aware availability gate added for #143/#144 reached the platform short-circuit but not the generic ladder or the HTTP/2 TLS fallback, which still asked an import-only check — "is the package importable", not "can it launch". On a machine where `uv sync --all-extras` had installed camoufox but `camoufox fetch` had never run, two bot-walled pages each spent ~50s escalating into a rung that raised a raw `FileNotFoundError`, then returned an error advising `pip install supacrawl[camoufox]` — already installed. The ladder now stops at the strongest engine that can actually launch, and every hint that quotes `engine_availability` (diagnose included) names the fetch command rather than the pip install the user has already run.

- **`supacrawl_batch`, `supacrawl_diagnose` and `supacrawl_health` are named in the server's `instructions`** (poodle64/mcp-servers#824): `instructions` loads eagerly at session start while per-tool schemas load lazily, so for a tool the agent has not yet selected the instructions are the only standing description it has. Three of the nine registered tools were absent from that list — registered, but with nothing standing to tell an agent they exist (rule 93).

- **Every MCP tool parameter now carries a description in the schema the model actually sees** (poodle64/mcp-servers#824): all 78 parameters across the nine registered tools were bare typed kwargs, so FastMCP derived a JSON Schema with no `description` and every one of them rendered as `<no desc>` in an LLM client — the single largest input to tool-selection accuracy, absent on the whole surface. The prose already existed in each tool's own `Args:` block; it now also lives in `Annotated[T, Field(description=...)]`, which is what FastMCP reads. Driven rather than inferred: constructing the real server and calling `list_tools()` returns 78 described parameters and 0 undescribed, against 78 undescribed before. `supacrawl_scrape.change_tracking_modes` had no `Args:` entry at all and gained one, reusing `supacrawl_crawl`'s wording for the same parameter.

- **`supacrawl_batch` and `supacrawl_extract` raise a typed error on failure instead of returning `{"success": false}`** (poodle64/mcp-servers#824): both tools caught every exception — validation errors included — and returned a diagnostic dict on the SUCCESS path. FastMCP serialises that as a successful tool result, so the calling model saw a success and had no signal the call had failed; four tests pinned the defect as the contract, written on the belief that any raise would be masked into `Error calling tool '<name>'`. It would not: a typed error on the `MCPError` lineage inherits `FastMCPError`, and its real message reaches the model. Both tools now follow their own siblings (`supacrawl_scrape`, `supacrawl_crawl`) — re-raise `SupacrawlValidationError`, `map_exception` the rest. Found by the mcp-servers rule-05 gate the first time it was allowed to look at an embedded server at all.

- **An empty result from one provider is no longer banked as a healthy success that stops the chain** (extends #132/#156/#158/#161): a self-hosted SearXNG whose upstream engines were CAPTCHA-blocked answered HTTP 200 with an empty set for days while the caller saw `success: true, data: [], consecutive_failures: 0` and a green healthcheck — nothing said search was _dead_ rather than that the query had _no matches_. Three changes close it. (1) `ProviderChain.search` now treats an empty answer as "keep looking": it advances to the next **configured** provider instead of returning the first empty, and only ever iterates providers already in the chain, so it never reaches a public engine the operator did not configure or opt into — an all-providers-empty result stays `success: true` with `data: []`, because a query with nothing to find is a real outcome, not an error. (2) `ProviderHealth` separates "answered with results" from "answered with nothing": a new `consecutive_empty` count climbs on empty answers (a result with matches clears it) and degrades the provider once a run passes `EMPTY_DEGRADED_THRESHOLD`, but never trips the UNAVAILABLE circuit breaker — an empty may be a genuine no-match, and dropping the provider for it would be wrong. (3) `SearchResult.all_recent_empty` surfaces the sustained-empty signal on the response itself, so a caller can tell "no matches" from "the backend has stopped answering" without polling `supacrawl_health`. When the `SUPACRAWL_SEARCH_PUBLIC_FALLBACK` opt-in routes an empty-fallthrough on to DuckDuckGo, the response and health surface still report that a query reached the public engine, rather than attributing the empty to the in-house backend and hiding the consultation.
- **A SearXNG-only configuration no longer reads as unconfigured on the health surface**: the static fallback check (taken when no live provider chain is available) enumerated every keyed provider's env var but omitted `searxng`, so a correctly configured self-hosted backend reported `effective_provider: "none"` and `status: "degraded"` regardless of `SEARXNG_URL`.
- **A missing SearXNG backend URL is now asserted against, not discovered in production**: new provider-selection coverage proves an absent `SEARXNG_URL` leaves the chain refusing the search and naming the missing variable, rather than appending a third-party engine nobody configured — the #156 shape, live again now that a secrets broker can refuse to render a credential-bearing URL and drop the variable entirely.
- **A dead browser pool now heals itself instead of failing every scrape until a human restarts the server** (#160): a long-lived server hands one `BrowserManager` to every consumer, so a browser process that died took the whole box down silently — every scrape returned `Browser.new_context: Target page, context or browser has been closed` and nothing in the process could bring it back. `BrowserManager` now checks liveness on every page checkout and relaunches a dead engine in-process, plus relaunches once inline for the race where the browser dies between that check and its use. Deliberately narrow: the relaunch fires only when `is_connected()` confirms the engine is actually gone, because a closed _page_ under a healthy browser produces identical wording, and retrying that would quietly re-run genuine site failures. Consecutive _failed_ relaunches back off exponentially (5s → 5min), so a box that cannot launch a browser at all is refused from the backoff rather than attempting a launch per inbound request; one success resets it. Concurrent requests noticing the same dead engine relaunch it once, not once each, and every liveness judgement is made against the engine instance the failing call was actually running on — under concurrency a peer's relaunch can land first, and judging against the manager's current engine would clear the fresh one and blame the dead one's failure on the site.
- **A failure that reaches the caller always carries a concrete next step** (#160): the tool documents that a non-ok verdict "always carries `quality.suggestion`", and callers act on it — but the browser-crash path returned `verdict: "empty"` with `suggestion: null` and a raw Playwright string, so the agent in the reported session retried the scrape when only a restart would have helped. The promise is now enforced on `QualityAssessment` itself rather than at each construction site, which closed the whole class: `error_status` had no suggestion mapping at all (so every HTTP 4xx/5xx shipped a null one), both PDF failure paths returned no `quality` object whatsoever, and an unmet `expect` assertion put its remediation only in the error prose. A scraper-side failure no longer gets a stealth hint either — it would send the caller after a site that was never contacted. The REST `/scrape` response gains the same signal: `quality` now sits on the envelope, so it survives a failure (where there is no `data` to hang it off) and a REST caller can finally make the same infra-vs-site call an MCP caller can.
- **Silent provider fallback now reads as degraded, not ready** (#158): the health surface reported `status: "ready"` while serving from a provider nobody configured — the exact condition that let a household deployment answer every search from a public engine while its self-hosted backend sat unused (#156). `components.search` gains `provider_fallback_active`, goes `degraded` when the effective provider is outside the configured set, carries a warning naming both, and — critically — that degraded verdict now reaches the top-level `status` instead of being buried one level down. The fallback also logs a warning at the moment it is applied, naming what was configured, what is being used instead, and why.
- **The live search health probe now judges against a floor, not `> 0`** (#161): the probe searched a rare one-word phrase, asked for a single result, and passed on any result at all — so it reported `result_count: 1, status: healthy` while every real multi-word query returned nothing, and three agents concluded the health tool was lying. It now searches a common multi-word phrase and requires a floor of results (a working general backend clears it comfortably); a broken backend that returns nothing or a token single result reads `degraded`, with a warning naming the count, the query, and the floor. The query is per-request not per-result, so a quota-metered provider still pays exactly one query.
- **A SearXNG query emptied by dead engines is now a typed failure, not a silent empty set** (#161): `{"success": true, "data": []}` was indistinguishable from a genuine no-match, so an agent that got `[]` concluded the material did not exist. When a query comes back empty because its engines were down, SearXNG's provider now raises a typed error naming them, the failure is finally counted (a zero-result success was counted nowhere), and — with `SUPACRAWL_SEARCH_PUBLIC_FALLBACK` on — the chain falls back rather than surfacing the empty set. A genuine no-match (empty with no unresponsive engines) is unaffected.
- **The MCP `correlation_id` is minted per request, not once per process** (#161): every MCP tool read the id from a process-wide contextvar before generating one, so in a long-lived server the first call's id was pinned onto every later response — the same value hours apart across unrelated calls, correlating nothing. All tools now generate a fresh id per request.
- **`provider_fallback_active` reflects who actually served, and never mislabels a response under concurrency** (#161): the health surface gains a `fallback_serving` signal (who answered the most recent search, not just who is next in line) so a backend that is up but failing every request — served by the DuckDuckGo fallback behind it — reads `degraded`; the probe reconciles from its own captured provenance. And `SearchResult.provider` / `provider_fallback` are captured the instant the chain answers rather than read back from the shared chain after a scrape await, closing a race where a concurrent request could mislabel a response's provider or mask a real fallback under load.

## [2026.7.0] - 2026-07-14

The MCP tool surface gains its shared-bearer auth floor and a de-vendored mcp_common; the camoufox engine is pinned back onto a compatible playwright.

### Added

- **MCP static-bearer auth floor**: the HTTP tool surface authenticates callers against `SUPACRAWL_MCP_AUTH_TOKEN` (constant-time compare). `--transport http` on a non-loopback `--host` without the token now refuses to start unless `--insecure` is passed explicitly, so a network-reachable scraping surface can no longer ship unauthenticated by accident.

### Changed

- **mcp_common de-vendored** (mcp-servers#641): the MCP layer consumes the live shared `mcp-common` package instead of a vendored copy; the published `[mcp]` extra declares it.
- **camoufox extra pins `playwright<1.61`**: camoufox's bundled Firefox juggler rejects playwright 1.61's `Browser.setDefaultViewport` `isMobile` property, so every camoufox launch failed at `new_page`. The ceiling lifts once a camoufox release accepts the 1.61 protocol.
- **api/mcp decoupling** (#151): the REST `api` layer no longer depends on the `mcp` package. `SupacrawlServices` and the `diagnose`/`summary` core logic moved into a portable `supacrawl.services` layer (sourcing config from `supacrawl.config`), so an `api`-only install imports and constructs the REST app without `fastmcp`/`mcp-common`. `supacrawl.mcp.api_client` is removed (no shim); `mcp.tools.diagnose`/`summary` are now thin FastMCP wrappers delegating to the services layer and translating exceptions via `map_exception`. `tests/test_api/` runs in CI again — the `--ignore=tests/test_api` workaround (a casualty of the old coupling) is removed.

### Fixed

- `validate_url` no longer swallows the "must have a valid host" error. It wrapped both the `urlparse` call and the netloc check in one broad `except Exception`, so the specific hostless-URL message was immediately re-wrapped as the vaguer "is not a valid URL". A hostless URL (`http://`, `https:///path`) now surfaces its own error.

## [2026.6.5] - 2026-06-21

Turns the off-box telemetry path into a clean, point-at-any-Loki client with first-class setup and backfill tooling and a read-only control-plane API for a separate UI. Builds on the 2026.6.4 remote-shipping foundation. The `RemoteSink` seam, fail-open batching, low-cardinality labels, and environment-only credentials are unchanged; no Loki host is hardcoded anywhere.

### Added

- **Point at any Loki (full auth surface)**: the remote sink mirrors the Grafana Alloy / Promtail client — HTTP basic auth (`metrics_remote_username` + `SUPACRAWL_METRICS_PASSWORD`), an `X-Scope-OrgID` tenant header (`metrics_remote_tenant`), a bearer token (`SUPACRAWL_METRICS_TOKEN`), or no auth — so the same configuration reaches a local/LAN Loki, a gated proxy, a self-hosted multi-tenant Loki, or Grafana Cloud. Basic auth takes precedence over a bearer token when both are set; the password is environment-only and never written to the store. A `WARNING` is logged when only one half of a basic-auth pair is configured.
- **`supacrawl metrics test-remote`**: probes the configured endpoint with one diagnostic event and reports the real HTTP status, latency, and a hint on failure (401/403 → auth, 404 → wrong path, 5xx → server/proxy) — so a misconfigured endpoint surfaces immediately instead of being swallowed by the fail-open sink.
- **`supacrawl metrics replay-remote`**: backfills the local `events.jsonl` to the configured Loki in batches, reporting the ingestion result. Loki de-duplicates identical events so re-running is safe; `--since` limits the window and `--dry-run` previews. (Loki may reject events older than its ingestion window, noted in the command help.)
- **Read-only control-plane HTTP endpoints** (`supacrawl serve`) for a separate UI plane — the engine exposes state, the UI is a separate front-end: `GET /supacrawl/config/schema` (the `x-ui` settings schema), `GET /supacrawl/config` (effective non-secret values plus a secret _presence_ map, never values), and `GET /supacrawl/metrics/summary?days=N`. Writes still go through the config store and credentials stay environment-only.
- **Configurable Loki `job` label**: `metrics_job` / `SUPACRAWL_METRICS_JOB` (default `supacrawl`) sets the stream label applied to shipped events (`{job=...}`), so a deployment can fit its own Loki labelling or distinguish multiple instances.

### Changed

- Remote telemetry is host-neutral and discoverable: neutral example placeholders (`https://loki.example.com/...`), a commented telemetry block in `.env.example`, and a README "Field Telemetry" section plus a "Control plane and the UI seam" guide in `docs/configuration.md` (with an auth matrix incl. Grafana Cloud). No Loki host is hardcoded.

### Fixed

- **Telemetry ships promptly and fails loudly.** A long-running MCP server now flushes buffered events on a ~5-second interval (not only in 25-event batches or at process exit), so a dashboard reading Loki updates in near-real-time. A failing remote push — e.g. a missing or stale `SUPACRAWL_METRICS_TOKEN` — now logs a clear `WARNING` pointing at the fix and `supacrawl metrics test-remote`, instead of being silently dropped by the fail-open path.

### Security

- Credentials embedded in `metrics_remote_url` (`https://user:pass@host/...`) are stripped from the `GET /supacrawl/config` response and from every log line and probe result, so a secret in the URL is never echoed.

## [2026.6.4] - 2026-06-20

Field telemetry can now be shipped off-box to a central log store, completing the path from a local scrape to a Grafana dashboard.

### Added

- **Remote telemetry shipping (configurable log sink)**: supacrawl can now ship each field-telemetry event to an external log store in addition to the local `events.jsonl`, so a central dashboard (Grafana reading Loki) can see quality and usage across runs. Opt-in with `supacrawl config set metrics_remote_url <loki-push-url>` plus an optional `SUPACRAWL_METRICS_TOKEN` bearer token. Loki is the first backend (behind a `RemoteSink` interface, leaving room for OTLP); events are grouped into low-cardinality streams (`{job="supacrawl", kind=...}`) with all detail in the JSON line for LogQL `| json`. Pushes are batched, best-effort, and fail-open with a short timeout — a slow or down endpoint never delays or fails a scrape, and the local log stays authoritative. Privacy carries over from the local sink (domain-only unless `metrics_full_url`). See `docs/configuration.md`.

## [2026.6.3] - 2026-06-20

The GUI-backend-foundation release: supacrawl now persists field telemetry, exposes a typed settings schema and store a control-plane dashboard can build against, and learns per-domain across every scrape path — plus a more trustworthy benchmark.

### Changed

- **Benchmark trustworthiness**: the scrape-quality benchmark no longer lets the independent reference renderer's failures masquerade as scrape regressions. When the renderer under-captures a page (it intermittently grabs only a shell on JS-hydrated pages) the reference-based metrics (token-F1, noise) are discarded for that case and it scores on the trustworthy reference-free signals (coverage, anchors, structure, spacing) — recovering a perfectly-scraped static page from a spurious 57.7 to 91.7. The `web-scraping.dev/antibot/easy` case is reclassified as a capability probe (it returns HTTP 403 to the full stealth ladder, camoufox included, while a benign path on the same host scrapes cleanly — a genuine evasion ceiling, not a regression), so an unbeatable wall no longer drags the headline.

### Added

- **Typed settings, a config store, and a GUI schema** (Closes #138): supacrawl now has one typed settings model resolved from built-in defaults, a local TOML store (`~/.supacrawl/config.toml`), and environment variables — in that order of increasing precedence. Manage it with `supacrawl config get | set | unset | schema | secrets | path`. The model emits a JSON schema annotated with `x-ui` render metadata (group/order/widget/help/visible_when) so a separate control-plane dashboard can render a settings form straight from it; credentials live in a separate environment-only model that never enters the schema or the store (`config secrets` reports presence, never values). The `strategy_memory`, `metrics`, and `metrics_full_url` toggles are read from the resolved config at runtime today; the remaining knobs are exposed for the GUI with command-level adoption rolling out. See `docs/configuration.md`.
- **Per-domain memory and telemetry across every scrape path**: per-domain strategy memory (#130) and the field telemetry sink (#137) now also flow through `crawl`, `batch`, and the `search`/`extract`/`agent` commands — previously only the single `scrape` path learned and recorded. A crawl now learns each domain's cheapest working strategy on the first page and seeds the rest, and every multi-page scrape contributes to the quality/usage log. On by default (opt-out `SUPACRAWL_STRATEGY_MEMORY=0` / `SUPACRAWL_METRICS=0`); the offline benchmark stays deliberately stateless.
- **Field telemetry sink** (Closes #137): supacrawl appends one event per scrape and search — quality verdict, score, attempts, escalation, latency, status, and the registrable domain — to a local, append-only log at `~/.supacrawl/metrics/events.jsonl`, so quality and usage can be tracked over time. On by default for the CLI and MCP server (opt-out `SUPACRAWL_METRICS=0`); domain-only by default for privacy, full URLs/queries opt-in via `SUPACRAWL_METRICS_FULL_URL=1`; the event schema is versioned. Inspect with `supacrawl metrics summary | tail | path | prune`. A `MetricsReader` is the clean read API a separate observability dashboard would consume — the CLI emits, a GUI reads.

## [2026.6.2] - 2026-06-20

The self-improving, MCP-first release (Closes #135). supacrawl now tells the calling agent honestly how good each result is, tries harder automatically when a result is poor, and remembers per domain what worked — so defaults quietly become excellent for the sites you actually use.

### Added

- **Runtime quality signal** (Closes #128): every scrape result carries a structured `quality` field — a verdict (`ok` / `thin` / `js_shell` / `paywall` / `bot_challenge` / `captcha` / `error_status` / `garbled_pdf` / `empty`), a 0–100 score, the reasons behind it, and a concrete `suggestion` when the result is poor — so an agent can decide to accept, retry, or escalate without re-deriving quality from the raw bytes. The signal shares one definition of "good" with the offline benchmark (a shared `supacrawl.quality` module both consume). Surfaced through the MCP tool result, the REST response, and the CLI.
- **Adaptive auto-escalation** (Closes #129): on a recoverable poor verdict (block / CAPTCHA / JS-shell / empty), an unmet `--expect`, or an HTTP/2 TLS rejection, supacrawl automatically walks the stealth/engine ladder — Playwright → Patchright → Camoufox → Camoufox+HTTP/1.1 — with a longer hydration wait, within a bounded budget, keeping the best-scoring attempt. Hard sites just work on defaults; no per-request `engine`/`stealth`/`wait_for` needed. A single `escalate` flag caps it. A detected site-builder (Wix/Squarespace/Framer/Foleon) short-circuits to its tuned engine; a user-pinned engine is respected.
- **Per-domain strategy memory** (Closes #130): supacrawl records, per registrable domain, the cheapest strategy that produced a clean result and seeds the next hit there — the first qantas.com scrape learns "camoufox + ~5s wait"; the next starts there. A cost-aware champion bandit (EWMA quality, cheaper-equal demotion, clearly-better upgrade, epsilon-greedy downward exploration, instant champion crash on a hard block, TTL decay) lives in a single local JSON document under `~/.supacrawl/strategies/`. On by default for the CLI and MCP server, opt-out with `SUPACRAWL_STRATEGY_MEMORY=0`; inspect and reset with `supacrawl strategy list | show | forget | clear`. With an empty or disabled store, behaviour is identical to the stateless ladder.
- **Search credit/quota visibility** (Closes #136): `supacrawl_health` surfaces per-provider remaining credits (Brave's `X-RateLimit-Remaining`) and the last error; a low-credit warning is emitted below a threshold; the provider chain fails over to the next configured provider on an out-of-credits/blocked error and surfaces the reason. No local usage counter (it is blind to other consumers of the same key).
- **Experiential improvement loop** (Closes #131): the `improve-supacrawl` workflow makes every "improve supacrawl" session compound — read the lessons registry, measure with the benchmark, target the weakest real signal, fix the root cause, confirm a lift with no regression, sharpen the bench when it is blind, and record a dated lesson.

### Changed

- **Search works out of the box or fails loudly** (Closes #132): with no provider key, a keyless search that returns nothing now fails loudly with an actionable error naming `BRAVE_API_KEY` and the free-tier URL — instead of a silent `{success: true, data: []}`. The DuckDuckGo fallback gets the shared browser-realistic header profile. A genuine no-match from a keyed provider stays a clean success.
- **Benchmark hardening** (Closes #134): the independent reference renderer settles hydration before capturing (polling the main-content length until it stabilises), and large PDF cases run in their own concurrency lane with a one-shot isolated retry so they no longer truncate to 0 words under load.
- **Documentation and MCP tool descriptions** (Closes #133): README / CLI / API docs and the MCP tool descriptions now state that search needs a provider key out of the box, describe the quality field and honest `success`, explain that supacrawl auto-escalates (no manual engine/stealth needed), and document per-domain memory and credit visibility.

### Fixed

- **Honest `success`** (Closes #128): an HTTP ≥ 400 response (including an Amazon `/dp/` soft-404 shell), a recognised bot/CAPTCHA interstitial, garbled PDF text, or an empty page is now reported `success=false` with an actionable reason — it was previously reported `success=true`. Hard-fail results are never cached.
- **Clean errors, never a crash** (Closes #129): a mid-fetch error (network, timeout, TLS rejection, a detached iframe on Reddit) returns a clean `success=false` with a hint rather than a raw traceback; the CLI guards `asyncio.run` so a launch error or interrupt exits cleanly.
- **`only_main_content` over-pruning** (Closes #129): when the main-content selector matches a tiny wrapper, supacrawl recovers the fuller page instead of silently dropping the real content.

## [2026.6.1] - 2026-06-15

### Added

- **Scrape-quality benchmark** (`supacrawl bench`, Closes #125): a curated, mostly-frozen corpus of real-world pages — static, articles, docs, SPA, infinite-scroll, data tables, PDF, CJK/RTL i18n, anti-bot, Australian government tax-law (HTML + PDF), and AU retail — scored 0–100 on completeness, token-F1, gold-anchor presence, boilerplate absence, structure, and inter-word spacing against an independent browser reference. Subcommands `bench run | compare | list | show` persist a per-run JSON document, a flat `metrics.jsonl`, and a run index for trend tracking. Volatile or reference-unfriendly targets are marked as capability probes and excluded from the regression index.
- **`word_spacing` benchmark metric**: detects PDF-extraction defects that fuse adjacent words into one token, guarding the PDF cases against regression. It counts only over-long all-ASCII alphabetic runs, so non-Latin scripts (CJK, Arabic) are never falsely penalised, and short bodies are skipped.
- **Real-world benchmark cases**: a frozen ATO government PDF (RAG/tax-law), two ATO gov-CMS HTML pages, a live AU pet-food retailer (JSON-LD Article), and a JS-rendered GitHub README (microdata).

### Fixed

- **PDF inter-word spacing for RAG quality**: LaTeX/academic PDFs extracted with words run together ("Thedominantsequencetransduction…") because pdfplumber's default gap threshold (3pt) is wider than those PDFs' inter-word spaces. Tightening `x_tolerance` to 2 restores spacing without over-splitting digitally-generated PDFs such as government publications; the arXiv reference PDF goes from ~5,300 fused tokens to ~9,400 correctly-spaced words.
- **JS-shell pages now escalate to a real render** (Closes #126): single-page-app shells whose only payload was inline JSON (e.g. `quotes.toscrape.com/js/`) looked content-rich to the static fast path and never rendered. The JS-requirement estimate now ignores inline JSON/template scripts, so these pages escalate to the browser.
- **PDF detection by content-type and magic bytes** (Closes #127): extensionless PDF URLs (e.g. `arxiv.org/pdf/…` and government content-API URLs) are detected via the `Content-Type` header and the `%PDF-` signature, not only the `.pdf` extension, and the already-fetched bytes are reused for extraction instead of being downloaded twice.

## [2026.6.0] - 2026-06-14

### Added

- **HTTP-first fast path** (Closes #119): `scrape` now tries a cheap HTTP GET before launching a browser, escalating to Playwright only when a render-needed or bot-challenge signal fires (the same heuristics `diagnose` uses). Static pages return several times faster and without browser cost. Enabled by default; disable with `--no-http-first` (CLI), `httpFirst: false` (REST), or `http_first=False` (MCP). Browser-only requests (screenshot, PDF, actions, device emulation, stealth, or a non-default engine) skip the fast path automatically.
- **Optional robots.txt enforcement for crawl** (Closes #119): `crawl` can honour each origin's `robots.txt`, skipping disallowed URLs and respecting `Crawl-delay`. It is opt-in — a crawl fetches the URLs it is given by default — so enable it with `--respect-robots` (CLI), `respect_robots=True` (MCP), or `ignoreRobotsTxt: false` (REST).
- **Per-host courtesy throttle for crawl** (Closes #119): a minimum inter-request gap per host, set with `--delay` (CLI), `delay` (REST), or `request_delay` (MCP). A `robots.txt` `Crawl-delay` automatically raises the gap. Prevents a personal IP being rate-limited or banned during a crawl.
- **`--expect` content gate** (Closes #121): assert that specific content is present before a scrape returns. A bare integer is a minimum word count; any other value is matched first as a CSS selector then as a text substring. When the assertion is unmet, the HTTP-first path escalates to the browser, the browser waits for a selector-shaped expectation to hydrate, and an still-unmet assertion (after a stealth + longer-wait retry) returns `success=False` with a remediation hint instead of a pre-hydration skeleton. Available as `--expect` (CLI), `expect` (REST/MCP).
- **Agent-readable, remediation-shaped errors** (Closes #123): scrape failures and MCP tool errors now carry a concrete, honest recovery hint instead of an opaque stack trace — `[HINT: ...]` for timeouts (raise the timeout/wait_for), DNS/connection/TLS faults, and 4xx/5xx responses, and a "try only_main_content=False" hint on thin-content warnings. Anti-bot failures keep the availability-aware stealth hint; failures with no useful action get no speculative advice (the #107 lesson). Lets an agent retry with a corrected parameter without human intervention.
- **Embedded structured-data extraction (no LLM)** (Closes #120): a new `structuredData` format deterministically harvests the data a site already publishes — schema.org JSON-LD (with `@graph` flattening), Next.js `__NEXT_DATA__`, HTML microdata, and OpenGraph — returned as JSON with no model call. More reliable and far cheaper than the LLM `json` path for facts like prices, ratings, authors, and dates. Available as `-f structuredData` (CLI), `structuredData` in `formats` (REST, surfaced under `structuredData`), and the MCP scrape tool.
- **Search recency, topic, and domain filters** (Closes #122): `search` now accepts `time_range` (day/week/month/year), `start_date`/`end_date`, `topic` (general/news/finance), and `include_domains`/`exclude_domains`, mapped onto each provider's native API (Brave `freshness`, Tavily native fields, Serper/SerpAPI `tbs`, Exa published-date + `category`) or synthesised as `site:` query operators where a provider has no native support. An agent can scope a search at the provider instead of post-filtering. Available across CLI (`--time-range`, `--start-date`, `--end-date`, `--topic`, `--include-domain`, `--exclude-domain`), REST, and the MCP search tool.
- **Agent self-onboarding: `SKILL.md`, `llms.txt`, and `install-skill`** (Closes #124): a concise, shippable `SKILL.md` teaches an agent how to choose between scrape/search/map/crawl/llm-extract/agent and which flags to set, including failure recovery; a root `llms.txt` gives the standard agent-landing overview. `supacrawl install-skill` registers the skill in one command (`./.claude/skills/` by default, `--user` for home, `--dir` for Cursor/Codex/other runtimes).

### Internal

- **Extracted `services/detection.py`**: the pure page-classification heuristics (CDN/WAF, JS framework, bot protection, login wall, render-needed estimate, recommendation generation) moved out of the `diagnose` MCP tool into a shared module so the scrape fast path, crawl, and diagnose share one implementation. A dead `BOT_DETECTION_PATTERNS` constant in `diagnose.py` was removed.
- **Extracted `ScrapeService._assemble_result()`**: the output-format assembly and caching tail is now shared by the browser path and the HTTP-first path, eliminating duplication.
- **Dropped two vestigial result models and tidied the E2E suite**: removed `ContentStats` and `ProcessMetadata`, which were defined but never populated or surfaced; replaced swallowed-exception mock scaffolding with deterministic browser fakes; made the search and the site-dependent crawl/map tests skip gracefully when a live provider or page yields nothing; bounded external-link discovery; and gave the timeout tests short explicit timeouts so they fail fast.

### Documentation

- Brought the CLI and REST references current with the HTTP-first fast path, the `--expect` content gate, the `structuredData` format, the content-extraction dial, search recency/topic/domain filters, and the crawl robots/delay options.

## [2026.5.0] - 2026-05-15

### Added

- **`/healthz` and `/readyz` HTTP probes on the embedded MCP server** (Closes poodle64/mcp-servers#270): The MCP server now exposes the canonical container-orchestration probes alongside the legacy `/health` route. `/healthz` always returns 200 while the process is alive (suitable for liveness); `/readyz` returns 200 once the FastMCP app is serving (suitable for readiness). Containers using the standard `/healthz` Docker healthcheck no longer need a per-service exception. Routes are registered automatically by `BaseMCPServer.__init__`; no caller changes required.
- **`SUPACRAWL_MASK_ERROR_DETAILS` env var** (default `True`): Operators can flip this to `False` in dev/CI to expose raw exception text in MCP tool errors. Production should keep the default. Wired to FastMCP 3.x's `mask_error_details` constructor flag, replacing the silent fallback to FastMCP's own `FASTMCP_MASK_ERROR_DETAILS` env var.
- **`ToolAnnotations` on all 8 MCP tools**: Every scraping/search tool now declares `readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True`; health and diagnose tools declare the same minus `openWorldHint=False`. Lets MCP clients render correct affordances and skip pre-call confirmations on read-only operations.

### Internal

- **Re-vendored `mcp_common` from `poodle64/mcp-servers`**: Replaced the months-stale single-file vendored copy with the current package layout (`server/`, `validators/`, plus new `redaction.py`, `executors/`, `host_shell/`). This is what unlocks `/healthz` and brings the MCP server into line with the rest of the household's MCP fleet. Internal imports rewritten from absolute (`from mcp_common.X`) to relative (`from .X`) to support the nested sub-package layout (`supacrawl.mcp.mcp_common`); `mcp_common.__version__` is now read lazily inside `register_server_info_resource()` to avoid a circular import.
- **`SupacrawlSettings.mask_error_details` field** added to `config.py` under the existing `SUPACRAWL_` env_prefix.
- One pre-existing test (`test_settings_loads_defaults` asserting `search_provider == "duckduckgo"`) remains failing; the assertion drifted out of step with the `brave` default in 2026.3.0. Tracked separately, not introduced by this release.

## [2026.3.2] - 2026-03-21

### Added

- **REST API server via `supacrawl serve`** (Closes #109): Firecrawl v2-compatible REST API with Supacrawl-native extensions. Existing Firecrawl clients (n8n, LangChain, LlamaIndex) work as drop-in backends by pointing their base URL at Supacrawl. Install with `pip install supacrawl[api]`.
  - Synchronous endpoints: POST /scrape, POST /map, POST /search
  - Async job endpoints: POST /crawl, POST /extract, POST /batch/scrape with GET polling and DELETE cancellation
  - Native endpoints: GET /supacrawl/health, POST /supacrawl/diagnose, POST /supacrawl/summary
  - Credential verification stub: GET /team/credit-usage (for n8n compatibility)
  - Optional Bearer token authentication via `SUPACRAWL_API_KEY`
  - In-memory async job store with configurable TTL and concurrency limits
  - camelCase request/response translation matching the v2 protocol
- **Foleon platform detection** with auto-tuned scrape settings

## [2026.3.1] - 2026-03-08

### Fixed

- **Playwright/Patchright lower bound regressed to >=1.58.0** (Closes #104): v2026.3.0 accidentally bumped the Playwright lower bound from `>=1.40.0` to `>=1.58.0`, breaking NixOS users whose package repositories provide 1.52.0 (stable) or 1.57.0 (unstable). Audit confirmed supacrawl uses only core Playwright APIs available since 1.0; restored `>=1.40.0` bound. Added inline comments and CLAUDE.md guardrail to prevent recurrence.

## [2026.3.0] - 2026-03-04

### Features

- **Multi-provider search with automatic fallback** (Closes #101): Refactored monolithic search into a pluggable provider architecture. Supports 6 providers (Brave, Tavily, Serper, SerpAPI, Exa, DuckDuckGo) with automatic fallback on quota exhaustion, rate limiting, or CAPTCHA detection. Configure via `SUPACRAWL_SEARCH_PROVIDERS` env var or `--provider` CLI flag
- **Configurable search rate limiting** (Closes #99): New `SUPACRAWL_SEARCH_RATE_LIMIT` env var. Enhanced health endpoint shows per-provider status and rate limit configuration
- **Brave Search as default provider** (Closes #95): Brave Search replaces DuckDuckGo as the default. DuckDuckGo is deprecated but remains as a last-resort fallback
- **Realistic browser headers for search** (Closes #96): Search requests use full browser-like headers (User-Agent, Sec-CH-UA, Accept-Language) to avoid bot detection. Locale-aware via `SUPACRAWL_LOCALE`
- **Camoufox anti-detection engine** (Closes #80): New `--engine camoufox` option provides Tier 3 anti-bot protection using patched Firefox. Effective against Akamai Bot Manager and advanced TLS fingerprinting. Install: `pip install supacrawl[camoufox]`
- **Change tracking** (Closes #81): New `-f changeTracking` format detects content changes between scrapes by comparing against cached previous versions. Supports `--change-tracking-modes git-diff` for unified diffs
- **PDF URL parsing** (Closes #82): Auto-detects `.pdf` URLs and extracts text directly, bypassing the browser. OCR fallback available via `pip install supacrawl[pdf-ocr]`. Controlled with `--parse-pdf [auto|fast|ocr|off]`
- **Mobile device emulation** (Closes #83): New `--mobile` and `--device TEXT` flags for scraping as mobile devices using Playwright device descriptors. Use `--list-devices` to see available presets
- **Iframe content extraction** (Closes #85): New `--expand-iframes [none|same-origin|all]` option (default: same-origin) expands iframe content inline during scraping
- **JSON comparison mode for change tracking** (Closes #87): `--change-tracking-modes json` compares structured extracted fields between scrapes
- **Change tracking in crawl** (Closes #88): `-f changeTracking` now works in the `crawl` command with `--change-tracking-modes` and `--cache-dir` support
- **Per-request engine in MCP tools** (Closes #90): `engine` parameter on `supacrawl_scrape` and `supacrawl_crawl` MCP tools allows per-request engine selection. Server default configurable via `SUPACRAWL_ENGINE` environment variable

### Fixed

- **DuckDuckGo CAPTCHA detection** (Closes #97): Detect and report CAPTCHA challenges from DuckDuckGo instead of returning empty results
- **ERR_HTTP2_PROTOCOL_ERROR automatic fallback** (Closes #92): Two-stage auto-retry chain (Chromium to Camoufox to Camoufox + HTTP/1.1) handles servers that reject Chromium's TLS fingerprint
- **Camoufox async wrapper** (Closes #91): Use correct `AsyncCamoufox` context manager instead of `AsyncNewBrowser`
- **CLI ScrapeService resource leak**: ScrapeService is now properly closed in the CLI search command's finally block

### Performance

- **Reduced scrape overhead by ~1.7s per page** (Closes #89): Removed unnecessary PDF HEAD request from the scrape hot path

## [2026.2.3] - 2026-02-26

### Fixed

- **Playwright version constraint** (Closes #79): Relaxed from `>=1.49.0` to `>=1.40.0,<2.0.0`. Supacrawl only uses stable core Playwright APIs, so the previous lower bound was unnecessarily restrictive. This allows distributions like NixOS and Guix to pair supacrawl with their system-provided Playwright browser binaries

### Documentation

- Added "System-Managed Playwright Browsers" section to README for users with distro-provided Playwright binaries

### Internal

- CI: use reusable auto-label workflow from master project

## [2026.2.2] - 2026-02-22

### Features

- **CSS background-image extraction**: Extract image URLs from CSS `background-image` and `background` shorthand properties, improving image discovery on sites that use CSS for hero images and backgrounds
- **Improved logo detection**: Better logo identification for site builders (Wix `<wow-image>`, Squarespace `data-section-type`, Framer `data-framer-name`) and nested `<img>` elements inside `role="img"` containers
- **Correlation IDs in MCP responses**: All MCP tool responses now include `correlation_id` for request tracing and debugging
- **WordPress and CSS counter preprocessors**: New site-specific preprocessors for WordPress content and CSS counter-based ordered lists, producing cleaner markdown output
- **MCP map `ignore_cache` parameter**: New parameter to bypass cached URL discovery results
- **MCP map title fallback and timezone detection**: Map results include `<title>` tag fallback for pages without `<meta>` titles, and automatic timezone detection from page content

### Fixed

- **MCP headless browser windows** (Closes #78): Browser windows no longer flash visibly during MCP operations. The `headless` parameter now propagates to all internal `BrowserManager` instances, including CAPTCHA solving and stealth retry paths
- **Screenshot cache key collision**: `screenshot_full_page` setting is now included in the cache key, preventing incorrect cache hits when the same URL is scraped with different screenshot settings
- **CrawlService browser lifecycle**: CrawlService now accepts an injected `BrowserManager`, avoiding duplicate browser instances when used from the MCP server

### Internal

- Remove Docker MCP files (`Dockerfile.mcp`, `docker-compose.mcp.yaml`); MCP server now runs natively via `supacrawl-mcp`
- Add MCP server section to README with installation and configuration instructions

## [2026.2.1] - 2026-02-21

### Features

- **Embedded MCP server**: the MCP server is now bundled as an optional extra (`pip install supacrawl[mcp]`), replacing the standalone server in `mcp-servers`. Includes all tools (scrape, crawl, map, search, extract, summary, diagnose, health), prompts, resources, structured logging, correlation IDs, exception mapping, and input validation. Install and run with `supacrawl-mcp --transport stdio`.
- Docker support for running the MCP server (`Dockerfile.mcp`, `docker-compose.mcp.yaml`)

### Fixed

- Remove duplicate `supacrawl_health` tool registration in MCP server
- MCP exception mapping gap: internal errors now correctly map to JSON-RPC error codes (Closes #69)

## [2026.2.0] - 2026-02-16

### Fixed

- Strip `javascript:` pseudo-protocol links completely during HTML to markdown conversion. These UI controls (print, share, email buttons) are now removed entirely following industry best practice from Readability.js, Newspaper3k, and Trafilatura. Fixes #67.

### Internal

- Add auto-label workflow for GitHub issues with AI-powered classification
- Ignore issue archive directories in git

## [2026.1.0] - 2026-01-12

Initial public release.

### Features

- **scrape** - Extract content from a single URL as markdown, HTML, or JSON
- **crawl** - Crawl websites with URL discovery, resume support, and parallel processing
- **map** - Discover URLs from sitemaps and page links with streaming progress
- **search** - Web search via DuckDuckGo or Brave with optional scraping
- **llm-extract** - LLM-powered structured data extraction
- **agent** - Autonomous web agent for multi-step data gathering
- **cache** - Local caching with statistics and pruning

### Capabilities

- Playwright-based browser automation with anti-bot evasion
- Optional enhanced stealth mode via Patchright (`pip install supacrawl[stealth]`)
- Optional CAPTCHA solving via 2Captcha (`pip install supacrawl[captcha]`)
- Page actions: click, scroll, wait, type, screenshot, JavaScript execution
- Multiple output formats: markdown, HTML, rawHtml, links, images, screenshot, PDF, JSON
- LLM integration: Ollama (local), OpenAI, Anthropic
- Site-specific preprocessors for improved markdown output (MkDocs Material, etc.)
- Proxy support with authentication
- Locale settings: country, language, timezone
- Python 3.12+ support
