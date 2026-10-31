"""Data contracts for the scrape-quality benchmark.

Two families of model live here:

- **Corpus** (``BenchCase``, ``BenchQuery``, ``BenchSuite``) — the input: a
  curated, annotated list of test sites and search queries loaded from a YAML
  manifest.
- **Results** (``CaseMetrics``, ``CaseResult``, ``SearchResult``,
  ``RunAggregate``, ``RunResult``) — the output: per-case scores and a
  run-level aggregate, persisted to disk in a versioned, dashboard-ingestible
  shape.

``SCHEMA_VERSION`` is stamped on every persisted run so that a downstream
dashboard can evolve its reader as the result shape changes.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 2

# Categories a case can belong to. Kept as a closed set so the aggregate can
# report a stable per-category breakdown across runs.
type Category = str

# The token estimate's basis: about four characters per token for English
# markdown under the common BPE tokenisers, stated wherever the figure is shown.
CHARS_PER_TOKEN = 4


class BenchCase(BaseModel):
    """One test site in the benchmark corpus.

    A case bundles the URL with the metadata a reviewer needs to understand
    *why* it is in the corpus and *what* capability it probes, plus the curated
    anchors used for deterministic, reference-free scoring.

    Attributes:
        id: Stable kebab-case identifier; the join key across runs over time.
        url: The page to scrape.
        category: Broad challenge family (e.g. ``static``, ``spa``, ``pdf``).
        title: Human-readable label.
        why: One-line rationale for inclusion.
        js_heavy: True when meaningful content only appears after JavaScript.
        spa: True when the page is a single-page application.
        antibot: True when the target deploys bot defences.
        content_type: ``html`` or ``pdf``; selects the comparison path.
        difficulty: Curator's 1 (trivial) to 5 (brutal) estimate.
        probes: Capability tags exercised (e.g. ``js_render``, ``tables``).
        stable: True when the URL's content is frozen enough for valid
            run-over-run comparison. Unstable targets (live anti-bot, paywalls)
            are capability probes, not regression signal.
        scored: True when the case contributes to the composite regression
            index. Defaults to ``stable`` when not set explicitly.
        expect: Substrings that good output SHOULD contain (gold anchors).
        expect_absent: Boilerplate/nav substrings good output should NOT contain.
    """

    id: str
    url: str
    category: Category
    title: str
    why: str
    js_heavy: bool = False
    spa: bool = False
    antibot: bool = False
    content_type: str = "html"
    difficulty: int = Field(3, ge=1, le=5)
    probes: list[str] = Field(default_factory=list)
    stable: bool = True
    scored: bool | None = None
    expect: list[str] = Field(default_factory=list)
    expect_absent: list[str] = Field(default_factory=list)

    @property
    def is_scored(self) -> bool:
        """Whether this case counts toward the composite index.

        Returns:
            ``scored`` when set explicitly, otherwise ``stable``: a frozen URL
            is regression signal by default, a volatile one is only a probe.
        """
        return self.stable if self.scored is None else self.scored


class BenchQuery(BaseModel):
    """One web-search query: a hit is any result from one of ``expect_domains``."""

    id: str
    query: str
    why: str = ""
    expect_domains: list[str] = Field(default_factory=list)


class BenchSuite(BaseModel):
    """A named, loadable collection of benchmark cases and search queries."""

    name: str
    description: str = ""
    cases: list[BenchCase]
    queries: list[BenchQuery] = Field(default_factory=list)


class CaseMetrics(BaseModel):
    """Computed metrics for a single scraped case.

    All fidelity metrics are ``None`` when their prerequisite is missing (e.g.
    the scrape failed, or the browser reference could not be captured), so a
    populated value always reflects a real measurement rather than a default.
    """

    # Fetch outcome
    success: bool
    status_code: int | None = None
    error: str | None = None
    latency_ms: float | None = None
    used_render: bool | None = None

    # Volume
    markdown_chars: int = 0
    markdown_words: int = 0
    # Output size as a model would pay for it: markdown_chars / CHARS_PER_TOKEN.
    approx_tokens: int = 0
    reference_chars: int | None = None
    reference_words: int | None = None

    # Fidelity vs the browser reference main text
    char_coverage: float | None = None
    token_f1: float | None = None
    rouge_l: float | None = None
    noise: float | None = None
    # True when the reference under-captured (a renderer shell) so the comparison
    # metrics above were discarded; the case scored on reference-free signals.
    reference_degenerate: bool = False

    # Structure retained in the markdown
    link_density: float | None = None
    headings: int = 0
    code_blocks: int = 0
    tables: int = 0
    images: int = 0
    links: int = 0

    # Deterministic structured data
    json_ld_found: bool = False

    # Curated anchors (reference-free): the main content survived, the boilerplate went.
    expect_hit: float | None = None
    expect_absent_ok: float | None = None

    # Text-quality (reference-free): inter-word spacing sanity. Catches fused
    # word runs from a PDF-extraction spacing defect; 1.0 is clean, 0.0 is badly
    # fused, None when there is too little prose to judge.
    word_spacing: float | None = None

    # Optional LLM-as-judge
    judge_score: float | None = None
    judge_rationale: str | None = None

    # Composite, 0-100
    quality: float | None = None


class CaseResult(BaseModel):
    """The full record for one case in one run."""

    case_id: str
    category: Category
    url: str
    difficulty: int
    scored: bool
    metrics: CaseMetrics
    artifacts: dict[str, str] = Field(default_factory=dict)


class SearchResult(BaseModel):
    """One query's outcome through the run's search provider."""

    query_id: str
    query: str
    success: bool
    latency_ms: float | None = None
    results: int = 0
    # Whether a result came from one of the query's expected domains; None when the search failed.
    expect_hit: bool | None = None
    error: str | None = None


class RunAggregate(BaseModel):
    """Run-level rollup, the headline numbers a trend view charts."""

    overall_quality: float | None = None
    by_category: dict[str, float] = Field(default_factory=dict)
    total_cases: int = 0
    # Cases that actually contributed to overall_quality: scored=True AND quality is not None.
    # This is a strict subset of all scored=True cases — failed runs produce scored=True but
    # quality=None, so they appear in total_cases but not here.
    scored_cases: int = 0
    success_rate: float = 0.0
    mean_latency_ms: float | None = None
    median_latency_ms: float | None = None
    # Over successful cases: output size and the two anchor rates.
    mean_approx_tokens: float | None = None
    expect_hit: float | None = None
    expect_absent_ok: float | None = None
    # Search queries, when the run had a search provider.
    search_queries: int = 0
    search_success_rate: float | None = None
    search_expect_hit: float | None = None


class RunResult(BaseModel):
    """Everything produced by one benchmark run.

    Persisted verbatim as the per-run JSON document. The flat ``metrics.jsonl``
    and run-level ``index.jsonl`` used by dashboards are derived from this.
    """

    model_config = ConfigDict(populate_by_name=True)

    schema_version: int = SCHEMA_VERSION
    run_id: str
    started_at: str
    finished_at: str
    supacrawl_version: str
    git_sha: str | None = None
    provider: str = "supacrawl"
    provider_version: str | None = None
    judge_enabled: bool = False
    reference_enabled: bool = True
    # Peak resident memory of this process and its children (the browser's
    # processes included), sampled through the run. None off Linux.
    peak_rss_mb: float | None = None
    # The machine, without its name: enough to say what the timings were measured on.
    platform: str | None = None
    suite_name: str
    cases: list[CaseResult]
    searches: list[SearchResult] = Field(default_factory=list)
    aggregate: RunAggregate
