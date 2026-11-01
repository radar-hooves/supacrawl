# Benchmark

`supacrawl bench` measures how well a reader turns web pages into markdown for a model, and what it costs. It drives a fixed corpus through supacrawl and through the open-source field, records the same figures for each, and keeps every run so the next change is measured against the last.

## What is measured

For each of 33 public pages: whether the page came back at all, whether the main content survived (curated anchor phrases present), whether the boilerplate went (navigation, cookie banners and footers absent), wall time per page, output size in tokens (characters divided by four), and the peak resident memory of the reader's whole process tree. Five web-search queries check that the expected domain appears in the top five results.

The corpus (`src/supacrawl/benchmark/corpus/default.yaml`) spans static pages, documentation, a 300,000-character reference page, news, JavaScript applications, package registries, forums, PDFs, government pages behind a bot manager, non-Latin and right-to-left text, and live bot walls. Volatile targets are probes, not regression signal.

## Readers

| Reader | How it ran |
| --- | --- |
| supacrawl | The library, `http_first=True`, main content only, the default escalation ladder (Playwright, Patchright, Camoufox) |
| Crawl4AI | `AsyncWebCrawler` with the pruning content filter (`fit_markdown`), one headless Chromium for the run |
| Jina Reader | `r.jina.ai`, keyless (20 requests a minute), markdown return format; `s.jina.ai` needs a key, so its queries are failures, not skips |
| trafilatura | An HTTP GET, then `trafilatura.extract` to markdown; it reads HTML only |
| Firecrawl | The self-hosted build's `POST /v2/scrape` with `onlyMainContent`; see below for whether it ran |

The figures below were taken on 01/11/2026 with each rival reader driven by its own provider module over the same corpus; those modules (Crawl4AI, Firecrawl, Jina Reader, trafilatura) are removed from the tree and live in git history.

Claude Code's own WebFetch and WebSearch have no programmatic entry point outside a Claude Code session, so they are not in the table.

## Results, 01/11/2026

Measured on a 20-core Linux x86_64 host that was running other work throughout: its one-minute load average read 29 to 33 when sampled during the runs and ranged to 65 across the morning by its own metrics. Three pages at a time, no reference renderer. The times are therefore not a speed claim for any reader; they compare the readers against each other within this table, which ran on the same host within one hour. Every run now records the host's one-minute load at its start and end (the `Load` column of `supacrawl bench table`); a run taken at a load under 10 is the one to quote for absolute speed, and none of today's was.

| Reader | Pages | Content kept | Boilerplate gone | Median s | Mean s | Tokens/page | Peak MB | Search |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| supacrawl 2026.11.0 | 88% of 33 | 93% | 92% | 9.7 | 16.0 | 13,650 | 2,642 | 5 of 5 |
| Firecrawl self-hosted (v2 API) | 85% of 33 | 95% | 40% | 3.6 | 4.1 | 15,103 | 77 (client only) | not run |
| Crawl4AI 0.9.5 | 82% of 33 | 96% | 56% | 5.2 | 5.8 | 11,001 | 2,667 (overstated) | none |
| Jina Reader (keyless) | 94% of 33 | 95% | 24% | 2.1 | 3.1 | 14,535 | 83 | needs a key |
| trafilatura 2.3.1 | 76% of 33 | 74% | 97% | 0.8 | 1.0 | 5,044 | 174 | none |

Peak MB is the process tree's proportional set size (`Pss` from `smaps_rollup`), which divides each shared page among the processes sharing it. supacrawl's figure was re-measured that way in a separate run (load 37 to 49); Crawl4AI's was taken by the earlier resident-set sum, which counts a browser's shared pages once per process and so overstates a multi-process reader, and is left marked rather than re-run on the busy host. The single-process readers are barely affected either way. Firecrawl's figure is the HTTP client's process alone; its five containers were not sampled.

Reading the table: supacrawl is the only reader that both keeps the content and drops the boilerplate, and the only one with search. It pays for that in time and memory: three concurrent escalation ladders, Camoufox's Firefox among them, is what its peak figure measures. Jina keeps nearly everything, navigation included. trafilatura is fastest and cleanest on static HTML and fails on anything that needs a browser or is a PDF. Crawl4AI is close on content and keeps about half the chrome. On the bot-walled pages, supacrawl was the only reader to return the two Australian Taxation Office pages; no reader returned the DataDome, Cloudflare or CAPTCHA probes without a proxy pool.

Run one page at a time, as a CLI user does, on the same loaded host, supacrawl's figures were: 88% of 33 pages, median 4.0 s, mean 8.3 s, and 1,433 MB peak (proportional set size, from a later run at load 47 to 68); the median is less than half the concurrent figure, the memory about half.

### Before and after this release

The same corpus, one page at a time, at the commit before the fast-path changes and after them, run back to back on the same loaded host (so the two rows compare with each other, not with a quiet machine):

| | Pages | Quality | Median s | Mean s |
| --- | ---: | ---: | ---: | ---: |
| 2026.10.2 (2411377) | 76% of 33 | 82.5 | 4.1 | 6.9 |
| 2026.11.0 (7c17fa1) | 88% of 33 | 84.8 | 4.0 | 8.3 |

(These two runs measured memory by the resident-set sum, so their memory column is withdrawn; both read about 2.3 GB by that measure, so the release did not move it.)

What changed and what it did, per page:

- Prose that mentions a CAPTCHA or being blocked no longer sends a page to a browser: Wikipedia 26.2 s to 14.2 s, Renovate's reference page 31.5 s to 12.2 s, the GitHub README 9.7 s to 1.8 s, Japanese Wikipedia 9.2 s to 2.0 s.
- A catalogue page whose main-content prune leaves a few words is recovered on the fast path instead of in a browser: books.toscrape.com 5.3 s to 1.4 s.
- Plain text is served as its own markdown: a 438 KB Project Gutenberg text 6.0 s to 1.9 s.
- The stealth rung starts headless on a host without a display, so the ladder reaches Camoufox: the two Australian Taxation Office pages, npm and the YouTube watch page now come back (14 to 19 s each). Pages behind Cloudflare, DataDome or a CAPTCHA still fail, and the ladder now spends 19 to 38 s proving it.

The mean rose because the ladder now runs where it used to die at its first rung.

### What the field has that supacrawl lacks

Read from the vendors' own pages on 31/10/2026. Firecrawl: a `monitor` endpoint with change diffs and webhooks, a `parse` endpoint for uploaded documents, `product`, `question` and `highlights` formats, PII redaction on output, a location object that selects a proxy and locale, and SDKs in nine languages. Crawl4AI: CSS, XPath and regex extraction without an LLM, a schema generator, BFS, DFS and best-first deep crawling with resume, an adaptive crawler that stops when it has enough, persistent browser profiles, `scan_full_page` for infinite scroll, Firefox and WebKit. Jina Reader: per-request headers for images, links, token budgets and chunking, structured extraction through its own model, OCR, document upload, and search that fetches the results.

Of these, infinite scroll is the one the corpus shows: `quotes.toscrape.com/scroll` returns 34 characters from every reader except Crawl4AI with `scan_full_page`.

### Firecrawl self-hosted

Firecrawl's self-host stack is five services (API, Playwright service, Redis, RabbitMQ, PostgreSQL) from its Docker Compose file, run from the published images. It needed `USE_DB_AUTHENTICATION=false` and a second `docker compose up` once RabbitMQ had passed its health check. The reader then ran over the whole corpus in about three minutes; its failures were the two anti-bot probes, Stack Overflow and the two Australian Taxation Office pages. A hosted Firecrawl account was not used.

## Running it

```bash
uv sync --all-extras
supacrawl bench run                        # supacrawl, with the browser reference renderer
supacrawl bench run --no-reference         # anchors alone, for the reader's own time and memory
supacrawl bench table                      # the newest run of each reader, as markdown
supacrawl bench compare                    # the two newest runs, regressions and improvements
```

Runs land under `.supacrawl/bench/runs/`, one JSON per run with every page's markdown beside it. `--only <id-or-category>` restricts a run; `--concurrency 1` measures a page at a time.
