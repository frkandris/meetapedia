"""search_only mode, stop_at boxing, country grouping, transient-search safety.

The twin cron windows this file was named for were deleted on 2026-09-20; what
it covers now is the collector itself, which the worker drives.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from scraper.extract import FallbackExtractor, get_extract_fingerprint
from scraper.main import _saver_city_groups
from scraper.models import SearchResult
from scraper.pipeline import (
    CityConfig,
    PipelineConfig,
    TopicConfig,
    _new_pair_log,
    run_pipeline,
    _run_full,
)
from scraper.search import SearchUnavailableError


def _fixtures(tmp_path):
    from scraper.db import init_db
    db = tmp_path / "scraper.db"
    init_db(db)
    cfg = PipelineConfig(
        search_results_per_query=5, search_max_pages=2, search_rate_limit=1.0,
        fetch_timeout=15, fetch_min_text_length=10, fetch_max_concurrent=3,
        fetch_blocked_domains=[], db_path=db,
        deepseek_api_key="test-key",  # so an extractor primary exists
    )
    cities = [CityConfig(name="Budapest", locale="hu", search_variants=[])]
    topics = [TopicConfig(name="running", search_terms={"hu": ["futás"]})]
    return db, cfg, cities, topics


class CountingExtractor(FallbackExtractor):
    """Fails the test if any LLM method is ever invoked."""
    def __init__(self):
        super().__init__(primaries=[])
        self.llm_calls = 0

    async def _call(self, *a, **k):  # noqa: D401
        self.llm_calls += 1
        raise AssertionError("LLM must not be called in search_only mode")


class OkSearch:
    exhausted = False
    def __init__(self, primaries): ...
    async def search_all(self, *a, **k):
        return [SearchResult(url="https://klub.test/a", title="t")]


def test_search_only_collects_without_llm(tmp_path):
    from scraper.cache import CacheManager
    from scraper.db import get_search_cache
    db, cfg, cities, topics = _fixtures(tmp_path)
    cache = CacheManager(db)

    async def fake_fetch(url, *a, **k):
        return "Elég hosszú oldalszöveg a gyűjtögetéshez."

    counting = CountingExtractor()
    with patch("scraper.pipeline.FallbackSearchClient", OkSearch), \
         patch("scraper.pipeline.fetch_and_clean", fake_fetch), \
         patch("scraper.pipeline.FallbackExtractor", lambda primaries: counting):
        logs, _ = asyncio.run(run_pipeline(
            cities, topics, cfg, cache=cache, run_mode="search_only",
        ))

    assert counting.llm_calls == 0
    assert get_search_cache(db, "Budapest", "running", ttl_days=3650) == ["https://klub.test/a"]
    assert cache.get_scraped("https://klub.test/a")  # page text collected
    assert cache.get_extracted("https://klub.test/a", fingerprint="") is None


def test_search_only_never_reads_extraction_cache_or_writes_entities(tmp_path):
    from scraper.cache import CacheManager
    from scraper.db import get_communities, save_search_cache
    from scraper.models import CommunityRecord

    db, cfg, cities, topics = _fixtures(tmp_path)
    cache = CacheManager(db)
    url = "https://klub.test/cached"
    save_search_cache(db, "Budapest", "running", [url], ["query"])
    cache.save_scraped(url, "Korábban letöltött közösségi oldal.", "Budapest", "running")
    cache.save_extracted(url, [CommunityRecord(
        name="Régi Cache Kör", city="Budapest", topic="running", locale="hu",
        source_url=url, extracted_at="2026-07-13T01:00:00+00:00",
    )], fingerprint=get_extract_fingerprint(cfg.deepseek_model))

    # No entity exists before collection. Reading the extraction cache would have
    # replayed the cached record through save_results in the buggy implementation.
    assert get_communities(db, "Budapest", "running") == []
    with patch.object(cache, "get_extracted", side_effect=AssertionError(
            "search_only must not read extraction caches")), \
         patch("scraper.duplicates.detect_all", side_effect=AssertionError(
             "search_only must not run entity duplicate detection")):
        asyncio.run(run_pipeline(cities, topics, cfg, cache=cache, run_mode="search_only"))

    assert get_communities(db, "Budapest", "running") == []


def test_search_only_retries_pair_after_every_fetch_fails(tmp_path):
    from scraper.cache import CacheManager
    from scraper.db import get_collected_pairs

    db, cfg, cities, topics = _fixtures(tmp_path)
    cache = CacheManager(db)

    async def failed_fetch(url, *a, **k):
        return None

    with patch("scraper.pipeline.FallbackSearchClient", OkSearch), \
         patch("scraper.pipeline.fetch_and_clean", failed_fetch):
        asyncio.run(run_pipeline(cities, topics, cfg, cache=cache, run_mode="search_only"))

    assert get_collected_pairs(db, cfg.search_max_pages) == set()

    class MustNotSearch:
        def __init__(self, primaries): ...
        exhausted = False
        async def search_all(self, *a, **k):
            raise AssertionError("fresh search cache should be reused")

    retried_urls = []

    async def count_failed_fetch(url, *a, **k):
        retried_urls.append(url)
        return None

    with patch("scraper.pipeline.FallbackSearchClient", MustNotSearch), \
         patch("scraper.pipeline.fetch_and_clean", count_failed_fetch):
        logs, _ = asyncio.run(run_pipeline(
            cities, topics, cfg, cache=cache, run_mode="search_only"))
    assert len(logs) == 1
    assert retried_urls == ["https://klub.test/a"]


def test_stop_at_in_past_processes_zero_pairs(tmp_path):
    db, cfg, cities, topics = _fixtures(tmp_path)
    past = datetime.now(timezone.utc) - timedelta(minutes=1)

    class MustNotSearch:
        exhausted = False
        def __init__(self, primaries): ...
        async def search_all(self, *a, **k):
            raise AssertionError("window closed — search must not run")

    with patch("scraper.pipeline.FallbackSearchClient", MustNotSearch):
        total, logs = asyncio.run(_run_full(
            cities, topics, cfg, FallbackExtractor(primaries=[]), None,
            True, True, {}, None, stop_at=past,
        ))
    assert total == 0 and logs == []


def _priority_cities():
    return [
        CityConfig(name="Budapest", country="Hungary", locale="hu", search_variants=[]),
        CityConfig(name="Berlin", country="Germany", locale="de", search_variants=[]),
        CityConfig(name="Stockholm", country="Sweden", locale="sv", search_variants=[]),
        CityConfig(name="Bandung", country="Indonesia", locale="id", search_variants=[]),
        CityConfig(name="Oslo", country="Norway", locale="no", search_variants=[]),
    ]


def test_saver_city_groups_follow_default_country_priority():
    # Hungary leads since the 1000+ import (2026-08-16) left it with the largest
    # unprocessed backlog on the primary market; unnamed countries come last.
    groups = _saver_city_groups(_priority_cities())
    assert [[city.country for city in group] for group in groups] == [
        ["Hungary"], ["Germany"], ["Indonesia"], ["Sweden"], ["Norway"],
    ]


def test_saver_city_groups_honour_settings_override():
    groups = _saver_city_groups(_priority_cities(), ["Indonesia", "Hungary"])
    assert [[city.country for city in group] for group in groups] == [
        ["Indonesia"], ["Hungary"], ["Germany", "Sweden", "Norway"],
    ]


def test_saver_city_groups_tolerate_unknown_country_in_priority():
    # A country configured but not present yields an empty group the caller skips.
    groups = _saver_city_groups(_priority_cities(), ["Atlantis", "Hungary"])
    assert groups[0] == []
    assert [city.country for city in groups[1]] == ["Hungary"]


def test_transient_search_failure_not_cached(tmp_path):
    from scraper.db import get_search_cache
    db, cfg, cities, topics = _fixtures(tmp_path)

    class TransientFail:
        exhausted = False
        def __init__(self, primaries): ...
        async def search_all(self, *a, **k):
            raise SearchUnavailableError("DataForSEO HTTP 500")

    with patch("scraper.pipeline.FallbackSearchClient", TransientFail):
        _, logs = asyncio.run(_run_full(
            cities, topics, cfg, FallbackExtractor(primaries=[]), None,
            True, True, {}, None,
        ))

    assert get_search_cache(db, "Budapest", "running", ttl_days=3650) is None, \
        "transient search failure must not be recorded as searched"
    assert any(p.get("search_failed") for p in logs)


def test_failure_pair_logs_carry_all_template_keys():
    """run_detail.html sums/iterates these keys with strict Undefined."""
    log = _new_pair_log("Budapest", "running", ["q"])
    for key in ("records_extracted", "urls_found", "fetched_urls",
                "cache_hits_scrape", "cache_hits_extract", "fetch_failed",
                "search_cache_hit", "search_failed", "extract_failed"):
        assert key in log, f"missing pair-log key: {key}"
