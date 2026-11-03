"""Per-domain strategy memory — supacrawl remembers what worked (#130).

A stateless crawler re-derives the right engine/wait/stealth for a domain on
every hit. supacrawl is local-first, so it can remember: the first qantas.com
scrape that succeeds with ``camoufox + ~5s wait`` seeds the next one there,
short-circuiting the escalation ladder. Over weeks of field use the defaults
quietly become excellent for the exact sites the user visits — the moat a
stateless hosted crawler cannot match.

Design (validated against the satisficing-bandit literature):
- A **cost-aware champion** per registrable domain: the cheapest strategy whose
  exponential moving-average quality stays above the bar. Engine cost order is
  playwright (0) < patchright/stealth (1) < camoufox (2).
- **Epsilon-greedy downward exploration**: occasionally probe a cheaper strategy
  so a site that drops its defences is re-learned at lower cost, not retried
  forever on an expensive one.
- **Hard-block crash**: if the champion strategy is itself blocked, evict it
  immediately (non-stationarity — the site changed) and re-learn.
- **TTL decay**: a champion older than the TTL is forgotten so stale strategies
  expire rather than being trusted indefinitely.

The store is a single small JSON file under ``~/.supacrawl/strategies/`` (the
same zero-infrastructure area as the cache). It is entirely optional: with an
empty or disabled store, behaviour is identical to the stateless ladder.
"""

from __future__ import annotations

import json
import logging
import os
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple
from urllib.parse import urlparse

from pydantic import BaseModel

if TYPE_CHECKING:
    from supacrawl.models import ScrapeResult

LOGGER = logging.getLogger(__name__)

# How responsive the moving average is to the latest observation (0..1).
_EWMA_ALPHA = 0.5
# Score slack to avoid thrashing the champion on noise.
_SCORE_SLACK = 5.0
# A champion older than this is forgotten and re-learned.
_TTL_DAYS = 14
# Default probability of probing a cheaper strategy than the champion.
_DEFAULT_EXPLORE_RATE = 0.10
# A usable result must reach at least this score to be worth remembering.
_MIN_RECORD_SCORE = 40


class StrategyChoice(NamedTuple):
    """A concrete strategy to seed an attempt with."""

    engine: str | None
    stealth: bool
    wait_for: int
    only_main_content: bool


class DomainStrategy(BaseModel):
    """The remembered champion strategy for one domain."""

    engine: str | None
    stealth: bool
    wait_for: int
    only_main_content: bool
    ewma_score: float
    samples: int
    last_verdict: str
    updated_at: str  # ISO 8601 UTC

    def as_choice(self) -> StrategyChoice:
        return StrategyChoice(self.engine, self.stealth, self.wait_for, self.only_main_content)

    def matches(self, engine: str | None, stealth: bool) -> bool:
        """Whether an attempt's engine/stealth identifies this champion (wait and
        only_main_content are within-strategy tuning, not identity)."""
        return self.engine == engine and self.stealth == stealth


def registrable_domain(url: str) -> str | None:
    """Return a stable per-domain key from a URL.

    Uses the hostname with a leading ``www.`` stripped. This is a deliberate,
    dependency-free approximation of the registrable domain (it does not consult
    the Public Suffix List), which is sufficient for keying a local strategy
    cache: the worst case is that ``a.example.com`` and ``b.example.com`` learn
    separately, never a wrong cross-site seed.

    Args:
        url: The URL being scraped.

    Returns:
        The host key (e.g. ``"qantas.com"``), or None when there is no host.
    """
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return None
    return host[4:] if host.startswith("www.") else host


def _engine_cost(engine: str | None, stealth: bool) -> int:
    """Cost rank of a strategy: cheaper is better when quality is comparable."""
    if engine in (None, "playwright"):
        return 1 if stealth else 0
    return 2  # camoufox (and any other heavier engine)


class StrategyStore:
    """A local, per-domain champion-strategy store.

    Thread-safety is not required: the CLI and MCP server drive one event loop.
    All persistence is a single JSON document, loaded and saved whole because the
    set of domains a single user visits is small.
    """

    DEFAULT_STRATEGY_DIR = Path.home() / ".supacrawl" / "strategies"

    def __init__(
        self,
        strategy_dir: Path | None = None,
        *,
        explore_rate: float = _DEFAULT_EXPLORE_RATE,
        rng: random.Random | None = None,
    ) -> None:
        """Initialise the store.

        Args:
            strategy_dir: Directory for the JSON document. Defaults to
                ``~/.supacrawl/strategies`` or ``SUPACRAWL_STRATEGY_DIR``.
            explore_rate: Probability of seeding a cheaper-than-champion strategy
                to detect a site dropping its defences. Set 0 to disable (tests).
            rng: Injectable RNG for deterministic exploration in tests.
        """
        if strategy_dir is not None:
            self.strategy_dir = strategy_dir
        else:
            env_dir = os.environ.get("SUPACRAWL_STRATEGY_DIR")
            self.strategy_dir = Path(env_dir) if env_dir else self.DEFAULT_STRATEGY_DIR
        self.path = self.strategy_dir / "strategies.json"
        self._explore_rate = explore_rate
        self._rng = rng or random.Random()
        self.strategy_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def default(cls) -> "StrategyStore | None":
        """Return a default store, or None when memory is disabled via env.

        ``SUPACRAWL_STRATEGY_MEMORY=0`` (or ``false``/``off``) disables it, as does
        ``strategy_memory = false`` in the config store (env wins over the store).
        Used by the CLI and MCP wiring so per-domain learning is on out of the box
        for the primary entry points while remaining opt-out and local.
        """
        from supacrawl.config import load_config

        if not load_config().strategy_memory:
            return None
        try:
            return cls()
        except OSError as exc:  # unwritable home dir — degrade to stateless
            LOGGER.debug("Strategy memory unavailable (%s); running stateless", exc)
            return None

    def _load(self) -> dict[str, DomainStrategy]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            LOGGER.warning("Failed to read strategy store: %s", exc)
            return {}
        out: dict[str, DomainStrategy] = {}
        for domain, payload in raw.items():
            try:
                out[domain] = DomainStrategy.model_validate(payload)
            except Exception:
                continue
        return out

    def _save(self, data: dict[str, DomainStrategy]) -> None:
        # Write atomically (temp file + os.replace) so a process killed mid-write
        # never leaves a truncated, unparseable store behind.
        payload = json.dumps({d: s.model_dump() for d, s in data.items()}, indent=2)
        tmp = self.path.with_suffix(".json.tmp")
        try:
            tmp.write_text(payload)
            os.replace(tmp, self.path)
        except OSError as exc:
            LOGGER.warning("Failed to write strategy store: %s", exc)
            tmp.unlink(missing_ok=True)

    def _is_expired(self, strategy: DomainStrategy, *, now: datetime) -> bool:
        try:
            updated = datetime.fromisoformat(strategy.updated_at)
        except ValueError:
            return True
        return now - updated > timedelta(days=_TTL_DAYS)

    def get(self, domain: str) -> DomainStrategy | None:
        """Return the stored champion for a domain (no TTL/exploration), or None."""
        return self._load().get(domain)

    def seed(self, domain: str) -> StrategyChoice | None:
        """Return the strategy to start the next attempt with, or None.

        Returns the champion when one is known and fresh. With probability
        ``explore_rate`` (and when the champion is not already the cheapest), it
        instead returns the cheapest strategy so a site that has dropped its
        defences is re-learned at lower cost.
        """
        champion = self._load().get(domain)
        if champion is None:
            return None
        now = datetime.now(UTC)
        if self._is_expired(champion, now=now):
            self.forget(domain)
            return None

        if (
            self._explore_rate > 0
            and _engine_cost(champion.engine, champion.stealth) > 0
            and self._rng.random() < self._explore_rate
        ):
            LOGGER.debug("Strategy memory exploring cheaper path for %s", domain)
            return StrategyChoice(engine=None, stealth=False, wait_for=0, only_main_content=champion.only_main_content)

        return champion.as_choice()

    def record(
        self,
        domain: str,
        *,
        engine: str | None,
        stealth: bool,
        wait_for: int,
        only_main_content: bool,
        result: "ScrapeResult",
    ) -> None:
        """Fold one (strategy, outcome) observation into the domain's champion.

        A hard block on the current champion evicts it (the site changed). A
        usable result either reinforces the champion, replaces it with a cheaper
        equally-good strategy, or upgrades to a better one — whichever keeps the
        cheapest strategy that clears the quality bar.

        Args:
            domain: The registrable-domain key.
            engine: Engine this attempt used (None == playwright).
            stealth: Whether this attempt used stealth (Patchright on Chromium).
            wait_for: The hydration wait (ms) the attempt used.
            only_main_content: The main-content setting the attempt used.
            result: The attempt's result (its quality verdict/score is the reward).
        """
        if result.quality is None:
            return
        verdict = result.quality.verdict.value
        score = float(result.quality.score)
        hard_block = verdict in ("bot_challenge", "captcha")

        data = self._load()
        champion = data.get(domain)

        if hard_block:
            if champion is not None and champion.matches(engine, stealth):
                LOGGER.info("Strategy memory: champion for %s crashed (%s); re-learning", domain, verdict)
                del data[domain]
                self._save(data)
            return

        # Only a genuinely clean page is worth remembering as a champion. A thin,
        # shell, or paywalled result is not (the cheap default path already
        # handles those, and seeding from them would lock in a poor strategy).
        if verdict != "ok" or score < _MIN_RECORD_SCORE:
            return

        now_iso = datetime.now(UTC).isoformat()
        observed = DomainStrategy(
            engine=engine,
            stealth=stealth,
            wait_for=wait_for,
            only_main_content=only_main_content,
            ewma_score=score,
            samples=1,
            last_verdict=verdict,
            updated_at=now_iso,
        )

        if champion is None:
            data[domain] = observed
        elif champion.matches(engine, stealth):
            champion.ewma_score = _EWMA_ALPHA * score + (1 - _EWMA_ALPHA) * champion.ewma_score
            champion.samples += 1
            champion.last_verdict = verdict
            champion.updated_at = now_iso
            data[domain] = champion
        else:
            new_cost = _engine_cost(engine, stealth)
            champ_cost = _engine_cost(champion.engine, champion.stealth)
            cheaper_and_fine = new_cost < champ_cost and score >= champion.ewma_score - _SCORE_SLACK
            clearly_better = score > champion.ewma_score + _SCORE_SLACK
            if cheaper_and_fine or clearly_better:
                LOGGER.info(
                    "Strategy memory: %s champion %s/%s -> %s/%s (score %.0f)",
                    domain,
                    champion.engine,
                    champion.stealth,
                    engine,
                    stealth,
                    score,
                )
                data[domain] = observed

        self._save(data)

    def forget(self, domain: str) -> bool:
        """Forget a domain's learned strategy. Returns True if one was removed."""
        data = self._load()
        if domain in data:
            del data[domain]
            self._save(data)
            return True
        return False

    def clear(self) -> int:
        """Forget every learned strategy. Returns the number removed."""
        data = self._load()
        count = len(data)
        self._save({})
        return count

    def list_domains(self) -> dict[str, DomainStrategy]:
        """Return every learned domain → champion mapping."""
        return self._load()
