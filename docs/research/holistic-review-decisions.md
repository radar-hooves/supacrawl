# Supacrawl holistic review: the three product decisions (10/11/2026)

Condensed from the maintainer's working review (tracked in #192). Security findings and the full evidence tables are kept out of this public repo.

**1. What supacrawl promises a stranger (P01 and the README).**
- **Today:**
  - The README's second line is "One `pip install` … a CLI, a Python library, a Firecrawl-compatible REST API and an MCP server over the same engine" (`README.md:9`). P01's headline metric is "MCP zero-config integration … a single JSON entry and no further setup" (`the product requirements document, line 66`; `features/mcp-server/F01-feature-intent.md:25,54`).
  - From PyPI the MCP server cannot start. It is built on mcp-common, which is private (`docs/mcp-server.md:7`; `mcp_launch.py:22-27` says so in one sentence).
  - The MCP server is also the household's most-used surface: 10,556 tool calls in 30 days, every Claude session's web.
- **Recommendation:** you write the one or two sentences that say what a stranger gets. My suggestion is "a CLI, a Python library and a Firecrawl-shaped REST API from one `pip install`; the MCP server runs from the maintainer's container image". The agent then rewrites the rest of P01, F01, the README's Features table and `llms.txt` to match. That rewrite is master's ruling of 06/11 ("PRD rewritten"); only your sentence waits.
- **Cost:** your sentence; about five documents; no code.
- **Against:** the MCP server is why P01 exists ("when my AI assistant needs the web, it should just work", `the product requirements document, line 13`). Without it, the public product is one more CLI reader. The other way keeps the promise: mcp-servers publishes mcp-common to PyPI. That is mcp-servers' call, and it makes a household framework public.

**2. The `agent` command: keep or cut.**
- **Today:**
  - The command is `services/agent.py` (390 lines) and `cli/agent.py` (438 lines). Both arrived in the initial release (`dc0e59b`).
  - It has no test file. No issue asks for it, and no issue since the initial release names it.
  - It is left out of MCP on purpose: "the controlling LLM is the agent" (`wiring.py:60-62`).
  - the product requirements document, line 44 and P09:77 keep it in scope. CLI runs leave no record, so its use is unknown.
- **Recommendation:** cut it. Every household caller is already the agent, it is untested, and its default model is a 3B local model.
- **Cost:** 828 lines and `features/agent/` deleted, the product requirements document, line 44 and P09:77 struck, and one CHANGELOG line for public users.
- **Against:** P01 lists Agent as a feature module. A stranger among the 373 monthly downloads may use it, and nothing would tell either of us before it breaks for them.

**3. The #138 settings seam (the dashboard's half).**
- **Today:**
  - #138 built a TOML store with `x-ui` schema metadata and `GET /supacrawl/config/schema` (`api/routers/supacrawl.py:57`) for "the planned observability/control-plane GUI (a separate sub-project)". You closed it as shipped on 20/06.
  - Five months on, nothing in the estate calls the schema endpoint (`git grep` across every repo under `repos/`).
  - The store does have four runtime readers (`registry.py:118`, `strategy_memory.py:162`, `telemetry.py:128`, `api/routers/supacrawl.py:86`).
  - Configuration is now three systems: the store, `SupacrawlSettings` and ad hoc `os.getenv`. P09:96 still calls ad hoc reads the accepted trade-off.
- **Recommendation:** say whether the dashboard is still wanted. If not, the schema endpoint and its `x-ui` metadata go when master folds the store into `SupacrawlSettings`. The `supacrawl config` command and the four readers stay.
- **Cost:** your word. The fold is a package either way.
- **Against:** the seam is data only, cheap to keep, and rebuilt in a day if you want the dashboard later.
