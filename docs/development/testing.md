# Testing

pytest, with `asyncio_mode = "auto"`. Tests live flat under `tests/` and carry markers:

| Marker | Meaning |
| --- | --- |
| `unit` | Pure logic, no I/O |
| `integration` | Filesystem, a local HTTP server, mocks at the wire |
| `browser` | Drives a real Chromium; skipped where Playwright's Chromium is not installed |
| `e2e` | Live network |
| `stealth_smoke` | Launches each stealth engine headless; the publish workflow's own job |
| `mcp` | Needs `mcp-common`, which only the `mcp-dev` dependency group installs |

## The gate

```bash
ruff check src tests && mypy src
pytest -q                      # the configured default: everything but e2e and stealth_smoke
pytest -q -m e2e               # live network
pytest -m stealth_smoke        # once `patchright install chromium` and `camoufox fetch` have run
```

Run the tests that cover what you changed; CI runs the whole suite on every push. CI installs `uv sync --frozen --all-extras` with the default `dev` group only, so the `mcp` and `bench` tests skip there; run them locally after `uv sync --all-groups --all-extras`.

## Browsers on NixOS

A downloaded Chromium does not run on NixOS, and the Nix-provided Playwright browsers are usually at a different revision from the one the locked Playwright expects. Present the Nix build under the expected revision, as `publish.yaml` does:

```bash
rev=$(uv run python -c "import json,playwright,pathlib;j=json.loads((pathlib.Path(playwright.__file__).parent/'driver/package/browsers.json').read_text());print(next(b['revision'] for b in j['browsers'] if b['name']=='chromium'))")
scratch=/tmp/pw-browsers; nix_browsers=$PLAYWRIGHT_BROWSERS_PATH   # the Nix store path
mkdir -p $scratch/chromium_headless_shell-$rev/chrome-headless-shell-linux64 $scratch/chromium-$rev/chrome-linux64
cp -rs $nix_browsers/chromium_headless_shell-*/*/. $scratch/chromium_headless_shell-$rev/chrome-headless-shell-linux64/
cp -rs $nix_browsers/chromium-*/*/. $scratch/chromium-$rev/chrome-linux64/
export PLAYWRIGHT_BROWSERS_PATH=$scratch
```

Camoufox's Firefox needs the host's GTK libraries; hand them to that engine alone with `SUPACRAWL_CAMOUFOX_LIBS` (a colon-separated library path), never through `LD_LIBRARY_PATH`, which crashes the Chromium engines.

## Patterns

- Mock at the wire (an `httpx` transport, `fetch_static`, a subprocess), never a library's own objects.
- A scrape or browser behaviour change is smoke-tested against a live URL after the unit tests pass (`.claude/rules/scraper-patterns.md`).
- A fixture that stands for a real failure is driven by forcing that failure (a 403, a dead engine), so an empty state cannot pass for an error state.
