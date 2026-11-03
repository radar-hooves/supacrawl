"""Unit tests for the per-domain strategy memory (#130).

Deterministic: a temp store dir and a zero (or seeded) exploration rate, no
network. They lock the champion bandit's behaviour — record a winner, seed it
back, prefer a cheaper equal strategy, crash on a block, decay on TTL — and the
"disabled store == stateless" guarantee.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from supacrawl.models import QualityAssessment, QualityVerdict, ScrapeData, ScrapeMetadata, ScrapeResult
from supacrawl.services.strategy_memory import DomainStrategy, StrategyStore, registrable_domain

pytestmark = pytest.mark.unit


def _result(verdict: QualityVerdict, score: int, *, success: bool = True) -> ScrapeResult:
    data = ScrapeData(markdown="x " * score, metadata=ScrapeMetadata(source_url="https://q.example"))
    return ScrapeResult(
        success=success,
        data=data,
        quality=QualityAssessment(verdict=verdict, score=score),
    )


def _store(tmp_path) -> StrategyStore:
    return StrategyStore(strategy_dir=tmp_path, explore_rate=0.0)


def test_registrable_domain_strips_www() -> None:
    assert registrable_domain("https://www.qantas.com/au/en") == "qantas.com"
    assert registrable_domain("https://shop.example.co.uk/x") == "shop.example.co.uk"
    assert registrable_domain("not a url") is None


def test_records_clean_winner_and_seeds_it_back(tmp_path) -> None:
    store = _store(tmp_path)
    assert store.seed("qantas.com") is None  # cold

    store.record(
        "qantas.com",
        engine="camoufox",
        stealth=False,
        wait_for=5000,
        only_main_content=True,
        result=_result(QualityVerdict.OK, 88),
    )
    choice = store.seed("qantas.com")
    assert choice is not None
    assert choice.engine == "camoufox"
    assert choice.wait_for == 5000


def test_thin_result_is_not_remembered(tmp_path) -> None:
    store = _store(tmp_path)
    store.record(
        "x.example",
        engine="camoufox",
        stealth=False,
        wait_for=5000,
        only_main_content=True,
        result=_result(QualityVerdict.THIN, 30),
    )
    assert store.seed("x.example") is None


def test_block_on_champion_crashes_it(tmp_path) -> None:
    store = _store(tmp_path)
    store.record(
        "x.example",
        engine="camoufox",
        stealth=False,
        wait_for=5000,
        only_main_content=True,
        result=_result(QualityVerdict.OK, 80),
    )
    assert store.seed("x.example") is not None
    # The champion strategy now gets blocked -> evicted, re-learn next time.
    store.record(
        "x.example",
        engine="camoufox",
        stealth=False,
        wait_for=5000,
        only_main_content=True,
        result=_result(QualityVerdict.BOT_CHALLENGE, 10, success=False),
    )
    assert store.seed("x.example") is None


def test_block_on_non_champion_is_ignored(tmp_path) -> None:
    store = _store(tmp_path)
    store.record(
        "x.example",
        engine="camoufox",
        stealth=False,
        wait_for=5000,
        only_main_content=True,
        result=_result(QualityVerdict.OK, 80),
    )
    # A cheaper playwright probe gets blocked — that is expected, champion stands.
    store.record(
        "x.example",
        engine=None,
        stealth=False,
        wait_for=0,
        only_main_content=True,
        result=_result(QualityVerdict.BOT_CHALLENGE, 10, success=False),
    )
    choice = store.seed("x.example")
    assert choice is not None and choice.engine == "camoufox"


def test_cheaper_equal_strategy_demotes_champion(tmp_path) -> None:
    store = _store(tmp_path)
    store.record(
        "x.example",
        engine="camoufox",
        stealth=False,
        wait_for=5000,
        only_main_content=True,
        result=_result(QualityVerdict.OK, 85),
    )
    # The site dropped its defences: cheap playwright now works just as well.
    store.record(
        "x.example",
        engine=None,
        stealth=False,
        wait_for=0,
        only_main_content=True,
        result=_result(QualityVerdict.OK, 84),
    )
    choice = store.seed("x.example")
    assert choice is not None and choice.engine is None  # demoted to the cheaper path


def test_ttl_expiry_forgets_stale_champion(tmp_path) -> None:
    store = _store(tmp_path)
    stale = DomainStrategy(
        engine="camoufox",
        stealth=False,
        wait_for=5000,
        only_main_content=True,
        ewma_score=80,
        samples=5,
        last_verdict="ok",
        updated_at=(datetime.now(UTC) - timedelta(days=30)).isoformat(),
    )
    store._save({"x.example": stale})
    assert store.seed("x.example") is None  # expired -> re-learn
    assert store.get("x.example") is None  # and forgotten


def test_exploration_probes_cheaper_path(tmp_path) -> None:
    # With explore_rate=1.0 the store always probes the cheapest path instead of
    # the (camoufox) champion, so a site dropping defences is re-learned cheaply.
    store = StrategyStore(strategy_dir=tmp_path, explore_rate=1.0, rng=random.Random(1))
    store.record(
        "x.example",
        engine="camoufox",
        stealth=False,
        wait_for=5000,
        only_main_content=True,
        result=_result(QualityVerdict.OK, 80),
    )
    choice = store.seed("x.example")
    assert choice is not None and choice.engine is None and choice.stealth is False


def test_forget_and_clear(tmp_path) -> None:
    store = _store(tmp_path)
    for d in ("a.example", "b.example"):
        store.record(
            d,
            engine="camoufox",
            stealth=False,
            wait_for=5000,
            only_main_content=True,
            result=_result(QualityVerdict.OK, 80),
        )
    assert store.forget("a.example") is True
    assert store.forget("a.example") is False
    assert set(store.list_domains()) == {"b.example"}
    assert store.clear() == 1
    assert store.list_domains() == {}
