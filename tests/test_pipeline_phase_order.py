import asyncio
from unittest.mock import patch
from pathlib import Path

from scraper.pipeline import (
    CityConfig,
    PipelineConfig,
    TopicConfig,
    run_pipeline,
)


def _db(tmp_path: Path) -> Path:
    from scraper.db import init_db
    p = tmp_path / "scraper.db"
    init_db(p)
    return p


def _cfg(db: Path) -> PipelineConfig:
    return PipelineConfig(
        search_results_per_query=5,
        search_max_pages=2,
        search_rate_limit=1.0,
        fetch_timeout=15,
        fetch_min_text_length=100,
        fetch_max_concurrent=3,
        fetch_blocked_domains=[],
        db_path=db,
    )


def test_full_mode_runs_reai_before_search(tmp_path):
    """Smart mode must run re-ai phase first, then full search phase."""
    db = _db(tmp_path)
    cities = [CityConfig(name="Budapest", locale="hu", search_variants=[])]
    topics = [TopicConfig(name="running", search_terms={"hu": ["futás"]})]
    cfg = _cfg(db)

    call_order = []

    async def fake_ai_only(*args, **kwargs):
        call_order.append("ai_only")
        return 0, []

    async def fake_full(*args, **kwargs):
        call_order.append("full")
        return 0, []

    with patch("scraper.pipeline._run_ai_only", side_effect=fake_ai_only), \
         patch("scraper.pipeline._run_full", side_effect=fake_full), \
         patch("scraper.pipeline.get_covered_pairs", return_value=set()), \
         patch("scraper.duplicates.detect_all"):
        asyncio.run(run_pipeline(cities, topics, cfg, cache=None, run_mode="full"))

    assert call_order[0] == "ai_only", f"Expected ai_only first, got: {call_order}"
    assert "full" in call_order, f"Expected full to be called, got: {call_order}"


def test_ai_only_mode_does_not_run_full(tmp_path):
    """ai_only mode must not trigger the full search phase."""
    from scraper.db import save_search_cache
    db = _db(tmp_path)
    save_search_cache(db, "Budapest", "running", ["https://x.test/"], ["q"])
    cities = [CityConfig(name="Budapest", locale="hu", search_variants=[])]
    topics = [TopicConfig(name="running", search_terms={"hu": ["futás"]})]
    cfg = _cfg(db)

    call_order = []

    async def fake_ai_only(*args, **kwargs):
        call_order.append("ai_only")
        return 0, []

    async def fake_full(*args, **kwargs):
        call_order.append("full")
        return 0, []

    with patch("scraper.pipeline._run_ai_only", side_effect=fake_ai_only), \
         patch("scraper.pipeline._run_full", side_effect=fake_full), \
         patch("scraper.duplicates.detect_all"):
        asyncio.run(run_pipeline(cities, topics, cfg, cache=None, run_mode="ai_only"))

    assert call_order == ["ai_only"], f"Expected only ai_only, got: {call_order}"


def test_ai_only_skips_pairs_that_were_never_searched(tmp_path):
    """A never-searched pair has no cached page; walking it only logged
    `ai_only_no_cache` (~100 per pass in production, 2026-09-24).
    """
    from scraper.db import save_search_cache
    db = _db(tmp_path)
    save_search_cache(db, "Budapest", "running", ["https://x.test/"], ["q"])
    cities = [CityConfig(name=n, locale="hu", search_variants=[]) for n in ("Budapest", "Szeged")]
    topics = [TopicConfig(name="running", search_terms={"hu": ["futás"]})]
    seen = {}

    async def fake_ai_only(*args, **kwargs):
        seen["pairs"] = kwargs["pairs_filter"]
        return 0, []

    with patch("scraper.pipeline._run_ai_only", side_effect=fake_ai_only), \
         patch("scraper.duplicates.detect_all"):
        asyncio.run(run_pipeline(cities, topics, _cfg(db), cache=None, run_mode="ai_only"))

    assert seen["pairs"] == {("Budapest", "running")}
