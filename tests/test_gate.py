"""The joinability gate: it may save work, it may never lose it.

Every test here is about the same property from a different angle. A gate that
skips a page it should have extracted is a permanent hole in the corpus — the
page is not retried, because the decision is cached — so every uncertainty has
to resolve towards extracting.
"""
import asyncio
from pathlib import Path

import pytest

from scraper.db import (get_daily_counter, get_gate_rejections, get_gate_scores,
                        init_db, release_gate_decision)
from scraper.gate import GATE_QUESTION, JoinabilityGate, gate_fingerprint


def _db(tmp_path: Path) -> Path:
    db = tmp_path / "gate.db"
    init_db(db)
    return db


def _gate(db: Path, monkeypatch, **kw) -> JoinabilityGate:
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "test-token")
    defaults = dict(enabled=True, threshold=0.06, daily_budget_usd=1.0,
                    account_id="acct")
    defaults.update(kw)
    return JoinabilityGate(db, **defaults)


def _answer(gate: JoinabilityGate, score: float, tokens: int = 300):
    async def _ask(url, text, city, topic):
        return score, tokens
    gate._ask = _ask
    return gate


# ── The one rule ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("failure", [
    RuntimeError("provider down"),
    TimeoutError("timed out"),
    ValueError("unreadable answer"),
])
def test_any_failure_lets_the_page_through(tmp_path, monkeypatch, failure):
    """Down, rate-limited, refused, unparseable — all the same answer."""
    gate = _gate(_db(tmp_path), monkeypatch)

    async def _boom(url, text, city, topic):
        raise failure
    gate._ask = _boom

    assert asyncio.run(gate.allows("h1", "https://a.test", "szöveg", "X", "t"))
    assert gate.errors == 1
    assert gate.skipped == 0


def test_a_missing_key_disables_the_gate_rather_than_blocking(tmp_path, monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    gate = JoinabilityGate(_db(tmp_path), enabled=True, daily_budget_usd=1.0)
    assert not gate.enabled
    assert asyncio.run(gate.allows("h1", "https://a.test", "szöveg", "X", "t"))


def test_an_empty_page_is_never_gated(tmp_path, monkeypatch):
    """Nothing to judge; the extractor decides what an empty page means."""
    gate = _answer(_gate(_db(tmp_path), monkeypatch), 0.0)
    assert asyncio.run(gate.allows("h1", "https://a.test", "", "X", "t"))
    assert gate.skipped == 0


def test_a_zero_budget_means_no_gate_calls_at_all(tmp_path, monkeypatch):
    """The 2026-08 paid-provider lesson: a permission without an amount is not
    a control. 0 is the safe default and it must mean off."""
    gate = _answer(_gate(_db(tmp_path), monkeypatch, daily_budget_usd=0.0), 0.01)
    assert asyncio.run(gate.allows("h1", "https://a.test", "szöveg", "X", "t"))
    assert gate.budget_spent and gate.skipped == 0


def test_the_budget_stops_the_gate_but_not_the_run(tmp_path, monkeypatch):
    db = _db(tmp_path)
    # A budget so small that one call's tokens exceed it.
    gate = _answer(_gate(db, monkeypatch, daily_budget_usd=0.00001), 0.01,
                   tokens=1_000_000)
    assert not asyncio.run(gate.allows("h1", "https://a.test", "szöveg", "X", "t"))
    # The first call goes through; the second finds the ceiling reached and
    # lets the page past rather than failing.
    assert asyncio.run(gate.allows("h2", "https://b.test", "szöveg", "X", "t"))
    assert gate.budget_spent


# ── Deciding, caching, releasing ─────────────────────────────────────────────

def test_a_confident_no_skips_and_is_remembered(tmp_path, monkeypatch):
    db = _db(tmp_path)
    gate = _answer(_gate(db, monkeypatch), 0.02)
    assert not asyncio.run(gate.allows("h1", "https://a.test", "szöveg", "X", "t"))
    assert gate.skipped == 1
    assert get_gate_scores(db, gate.fingerprint) == {"h1": 0.02}
    assert get_daily_counter(db, __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc).strftime("%Y-%m-%d"), "gate_calls") == 1


def test_a_remembered_decision_costs_no_second_call(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _answer(_gate(db, monkeypatch), 0.02)
    asyncio.run(_answer(_gate(db, monkeypatch), 0.02).allows(
        "h1", "https://a.test", "szöveg", "X", "t"))

    second = _gate(db, monkeypatch)

    async def _must_not_ask(*a):
        raise AssertionError("a cached decision must not be re-asked")
    second._ask = _must_not_ask
    assert not asyncio.run(second.allows("h1", "https://a.test", "szöveg", "X", "t"))


def test_changing_the_threshold_re_opens_every_held_page(tmp_path, monkeypatch):
    """A page rejected at 0.06 might pass at 0.03, so the old decision does not
    apply — it is not merely stale. The fingerprint carries the threshold."""
    db = _db(tmp_path)
    asyncio.run(_answer(_gate(db, monkeypatch, threshold=0.06), 0.04).allows(
        "h1", "https://a.test", "szöveg", "X", "t"))

    looser = _answer(_gate(db, monkeypatch, threshold=0.03), 0.04)
    assert looser.fingerprint != gate_fingerprint("typesafe/jev", 0.06)
    assert get_gate_scores(db, looser.fingerprint) == {}
    assert asyncio.run(looser.allows("h1", "https://a.test", "szöveg", "X", "t"))


def test_editing_the_question_re_opens_every_held_page():
    """The measured 0.06 was measured for this wording."""
    before = gate_fingerprint("typesafe/jev", 0.06)
    import scraper.gate as gate_module
    original = gate_module.GATE_QUESTION
    gate_module.GATE_QUESTION = original + " And is it in Hungary?"
    try:
        assert gate_fingerprint("typesafe/jev", 0.06) != before
    finally:
        gate_module.GATE_QUESTION = original
    assert GATE_QUESTION == original


def test_a_released_page_is_handed_back_to_extraction(tmp_path, monkeypatch):
    db = _db(tmp_path)
    asyncio.run(_answer(_gate(db, monkeypatch), 0.02).allows(
        "h1", "https://a.test", "szöveg", "X", "t"))
    assert [r["url"] for r in get_gate_rejections(db, gate_fingerprint(
        "typesafe/jev", 0.06), 0.06)] == ["https://a.test"]

    assert release_gate_decision(db, "h1") == 1
    assert get_gate_scores(db, gate_fingerprint("typesafe/jev", 0.06)) == {}


def test_the_gate_never_touches_the_extraction_cache():
    """A classifier's opinion is not an extraction result, and writing one into
    cache_pages would record 'no communities' permanently."""
    source = Path("scraper/gate.py").read_text(encoding="utf-8")
    for forbidden in ("cache_pages", "save_extracted", "update_cache_page",
                      "CacheManager"):
        assert forbidden not in source, f"the gate must not reach {forbidden}"


# ── In the pipeline ──────────────────────────────────────────────────────────

def _pipeline_fixtures(tmp_path: Path):
    from scraper.pipeline import CityConfig, PipelineConfig, TopicConfig
    db = tmp_path / "scraper.db"
    init_db(db)
    cfg = PipelineConfig(
        search_results_per_query=5, search_max_pages=2, search_rate_limit=1.0,
        fetch_timeout=15, fetch_min_text_length=10, fetch_max_concurrent=3,
        fetch_blocked_domains=[], db_path=db, deepseek_api_key="test-key",
        gate_enabled=True, gate_threshold=0.06, gate_daily_budget_usd=1.0,
        gate_account_id="acct",
    )
    cities = [CityConfig(name="Budapest", locale="hu", search_variants=[])]
    topics = [TopicConfig(name="running", search_terms={"hu": ["futás"]})]
    return db, cfg, cities, topics


@pytest.fixture()
def _gate_answer(monkeypatch):
    """Make every gate in the process answer a fixed score, without HTTP."""
    def _set(score: float):
        async def _ask(self, url, text, city, topic):
            return score, 300
        monkeypatch.setattr(JoinabilityGate, "_ask", _ask)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "test-token")
    return _set


def test_a_rejected_page_costs_no_extraction_and_caches_nothing(
        tmp_path, monkeypatch, _gate_answer):
    """The saving, and the rule that makes it safe: no extraction call, and
    nothing written to the extraction cache — only to gate_decisions."""
    from unittest.mock import patch

    from scraper.cache import CacheManager
    from scraper.extract import FallbackExtractor
    from scraper.models import SearchResult
    from scraper.pipeline import run_pipeline

    db, cfg, cities, topics = _pipeline_fixtures(tmp_path)
    cache = CacheManager(db)
    _gate_answer(0.01)

    class _MustNotExtract(FallbackExtractor):
        def __init__(self):
            super().__init__(primaries=[])
            self.calls = 0

        @property
        def exhausted(self):
            return False

        @property
        def providers_down(self):
            return False

        async def extract_traced(self, **kwargs):
            self.calls += 1
            raise AssertionError("a gated-out page must not be extracted")

    class _Search:
        exhausted = False

        def __init__(self, primaries): ...

        async def search_all(self, *a, **k):
            return [SearchResult(url="https://ures.test/a", title="t")]

    async def _fetch(url, *a, **k):
        return "Egy oldal, amin nincs közösség, csak szöveg."

    extractor = _MustNotExtract()
    with patch("scraper.pipeline.FallbackSearchClient", _Search), \
         patch("scraper.pipeline.fetch_and_clean", _fetch), \
         patch("scraper.pipeline.FallbackExtractor", lambda primaries, **kw: extractor):
        logs, _ = asyncio.run(run_pipeline(cities, topics, cfg, cache=cache))

    assert extractor.calls == 0
    # Summed over the run's pair logs: a full run appends more than one, and
    # which of them carries the count is an implementation detail.
    assert sum(x["gate_skipped"] for x in logs) == 1
    assert sum(x["extract_failed"] for x in logs) == 0, \
        "a gate decision is not a failure"
    # The page keeps its text and has no extraction recorded — so releasing the
    # decision, or changing the threshold, hands it straight back.
    assert cache.get_scraped("https://ures.test/a")
    assert cache.get_extracted("https://ures.test/a", fingerprint="") is None
    # The decision lives under the configured fingerprint, and nowhere else.
    from scraper.gate import gate_fingerprint as _fp
    assert list(get_gate_scores(db, _fp("typesafe/jev", 0.06))) == [
        __import__("scraper.cache", fromlist=["CacheManager"]).CacheManager.url_hash(
            "https://ures.test/a")]


def test_a_page_the_gate_allows_is_extracted_as_before(
        tmp_path, monkeypatch, _gate_answer):
    from unittest.mock import patch

    from scraper.cache import CacheManager
    from scraper.extract import FallbackExtractor
    from scraper.models import SearchResult
    from scraper.pipeline import run_pipeline

    db, cfg, cities, topics = _pipeline_fixtures(tmp_path)
    cache = CacheManager(db)
    _gate_answer(0.95)
    seen = []

    class _Extractor(FallbackExtractor):
        def __init__(self):
            super().__init__(primaries=[])

        @property
        def exhausted(self):
            return False

        @property
        def providers_down(self):
            return False

        async def extract_traced(self, **kwargs):
            seen.append(kwargs.get("source_url"))
            return [], "test-model", 50

    class _Search:
        exhausted = False

        def __init__(self, primaries): ...

        async def search_all(self, *a, **k):
            return [SearchResult(url="https://klub.test/a", title="t")]

    async def _fetch(url, *a, **k):
        return "Heti futóklub Budapesten, mindenkit várunk."

    with patch("scraper.pipeline.FallbackSearchClient", _Search), \
         patch("scraper.pipeline.fetch_and_clean", _fetch), \
         patch("scraper.pipeline.FallbackExtractor", lambda primaries, **kw: _Extractor()):
        logs, _ = asyncio.run(run_pipeline(cities, topics, cfg, cache=cache))

    assert "https://klub.test/a" in seen
    assert sum(x["gate_skipped"] for x in logs) == 0


def test_a_disabled_gate_changes_nothing(tmp_path, monkeypatch):
    """The default. Every page reaches the extractor exactly as before."""
    from unittest.mock import patch

    from scraper.cache import CacheManager
    from scraper.extract import FallbackExtractor
    from scraper.models import SearchResult
    from scraper.pipeline import run_pipeline

    db, cfg, cities, topics = _pipeline_fixtures(tmp_path)
    cfg.gate_enabled = False
    cache = CacheManager(db)
    seen = []

    class _Extractor(FallbackExtractor):
        def __init__(self):
            super().__init__(primaries=[])

        @property
        def exhausted(self):
            return False

        @property
        def providers_down(self):
            return False

        async def extract_traced(self, **kwargs):
            seen.append(kwargs.get("source_url"))
            return [], "test-model", 50

    class _Search:
        exhausted = False

        def __init__(self, primaries): ...

        async def search_all(self, *a, **k):
            return [SearchResult(url="https://barmi.test/a", title="t")]

    async def _fetch(url, *a, **k):
        return "Bármilyen oldalszöveg, elég hosszú ahhoz, hogy eljusson idáig."

    def _must_not_ask(*a, **k):
        raise AssertionError("a disabled gate must not call anything")

    monkeypatch.setattr(JoinabilityGate, "_ask", _must_not_ask)
    with patch("scraper.pipeline.FallbackSearchClient", _Search), \
         patch("scraper.pipeline.fetch_and_clean", _fetch), \
         patch("scraper.pipeline.FallbackExtractor", lambda primaries, **kw: _Extractor()):
        logs, _ = asyncio.run(run_pipeline(cities, topics, cfg, cache=cache))

    assert "https://barmi.test/a" in seen
    assert sum(x["gate_skipped"] for x in logs) == 0
