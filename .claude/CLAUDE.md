# Supacrawl

Zero-infrastructure CLI web scraper with LLM extraction, for developers working from the terminal — `pip install` and go, no Docker, databases, or services.

## Scope

- **Does not**: no web UI; no database — output goes to stdout/files and a local cache; no auth beyond browser-level; not a hosted service (local execution only).
- **Owns**: faithful-rendering and scrape-quality knowledge belongs here, never in a consumer's config.

## Design constraints

- LLM extraction stays provider-pluggable (Ollama / OpenAI / Anthropic) — never lock to one provider.
- The engine escalation ladder and per-domain strategy memory: `docs/configuration.md`.

## Running it

- Setup needs **all extras**: `uv sync --all-extras` (direnv runs it on `cd`) then `playwright install chromium`. A bare `uv sync` silently omits the `stealth`/`captcha`/`camoufox`/`pdf-ocr` extras and scraping breaks at runtime.
- Extras install PACKAGES, not browser binaries. `patchright install chromium` and `camoufox fetch` are separate and neither is implied by `uv sync --all-extras`, so without them the escalation ladder's upper rungs are unusable.
- Quality gate before done: `pytest -q -m "not e2e and not stealth_smoke"` (drop `not e2e` to include live-network E2E). Run `pytest -m stealth_smoke` once both browser fetches are done. See `docs/development/testing.md`.
- Config and env-var reference: `docs/configuration.md`; LLM selection: `src/supacrawl/llm/config.py`.

## Pitfalls

- **Playwright/Patchright lower bound `>=1.40.0` is intentional** (NixOS compatibility; #79, #104). Do NOT bump it unless new Playwright APIs are actually used.

## CI deviations from the household standard

- **No `auto-label-issues` caller.** supacrawl is public and `radar-hooves/master-project` is private, so the reusable is unresolvable here; labels are applied at creation time by the `/git-issue` skill instead.
- **`publish-wheels.yaml` inlines the `publish-wheels` reusable instead of calling it**, for the same public→private constraint; its header records what to keep aligned with the reusable.
