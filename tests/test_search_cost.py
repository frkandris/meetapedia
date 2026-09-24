"""Cost-control behaviors: query short-circuit, failover semantics, topic tiering."""
import pytest

from scraper.models import SearchResult
from scraper.pipeline import CityConfig, _tier_allows
from scraper.search import FallbackSearchClient, SearchQuotaError


class FakeProvider:
    """Scripted provider: returns per-query results, or raises on marked queries."""

    def __init__(self, per_query: dict[str, list[str] | Exception]):
        self.per_query = per_query
        self.calls: list[str] = []

    async def search(self, query, locale="en", num_results=10):
        self.calls.append(query)
        val = self.per_query.get(query, [])
        if isinstance(val, Exception):
            raise val
        return [SearchResult(url=u, title=u) for u in val]


@pytest.mark.asyncio
async def test_stop_after_skips_remaining_queries():
    p = FakeProvider({
        "q1": [f"https://a{i}.test" for i in range(10)],
        "q2": ["https://never.test"],
        "q3": ["https://never2.test"],
    })
    client = FallbackSearchClient(primaries=[p])
    results = await client.search_all(["q1", "q2", "q3"], stop_after=10)
    assert len(results) == 10
    assert p.calls == ["q1"]  # q2/q3 never issued — money saved


@pytest.mark.asyncio
async def test_all_queries_run_when_below_stop_after():
    p = FakeProvider({"q1": ["https://a.test"], "q2": ["https://b.test"]})
    client = FallbackSearchClient(primaries=[p])
    results = await client.search_all(["q1", "q2"], stop_after=10)
    assert p.calls == ["q1", "q2"]
    assert {r.url for r in results} == {"https://a.test", "https://b.test"}


@pytest.mark.asyncio
async def test_quota_midway_keeps_partials_and_moves_remaining_to_next_provider():
    p1 = FakeProvider({"q1": ["https://a.test"], "q2": SearchQuotaError("credits")})
    p2 = FakeProvider({"q2": ["https://b.test"]})
    client = FallbackSearchClient(primaries=[p1, p2])
    results = await client.search_all(["q1", "q2"])
    assert {r.url for r in results} == {"https://a.test", "https://b.test"}
    assert p2.calls == ["q2"]  # q1 was already answered by p1 — not re-paid


@pytest.mark.asyncio
async def test_total_empty_retries_full_set_on_next_provider():
    p1 = FakeProvider({"q1": [], "q2": []})
    p2 = FakeProvider({"q1": ["https://x.test"], "q2": []})
    client = FallbackSearchClient(primaries=[p1, p2])
    results = await client.search_all(["q1", "q2"])
    assert [r.url for r in results] == ["https://x.test"]
    assert p1.calls == ["q1", "q2"]
    assert p2.calls == ["q1", "q2"]


def test_tier_allows():
    core_city = CityConfig(name="Åsele", locale="sv", search_variants=[], topic_tier="core")
    full_city = CityConfig(name="Stockholm", locale="sv", search_variants=[])
    core = ["running", "music"]
    assert _tier_allows(core_city, "running", core)
    assert not _tier_allows(core_city, "chess", core)
    assert _tier_allows(full_city, "chess", core)          # full tier: everything
    assert _tier_allows(core_city, "chess", [])            # empty core list disables tiering


def test_empty_search_result_is_cached(tmp_path):
    """A search that finds nothing must still be recorded in search_cache —
    otherwise the pair is re-paid on every run (and twice via catch-up)."""
    import asyncio
    from unittest.mock import patch

    from scraper.db import init_db, get_search_cache
    from scraper.extract import FallbackExtractor
    from scraper.pipeline import PipelineConfig, TopicConfig, _run_full

    db = tmp_path / "scraper.db"
    init_db(db)
    cfg = PipelineConfig(
        search_results_per_query=5, search_max_pages=2, search_rate_limit=1.0,
        fetch_timeout=15, fetch_min_text_length=100, fetch_max_concurrent=3,
        fetch_blocked_domains=[], db_path=db,
    )
    cities = [CityConfig(name="Budapest", locale="hu", search_variants=[])]
    topics = [TopicConfig(name="running", search_terms={"hu": ["futás"]})]

    class FakeFallback:
        exhausted = False
        def __init__(self, primaries): ...
        async def search_all(self, queries, locale="en", num_results=10, stop_after=None, usable=None):
            return []

    with patch("scraper.pipeline.FallbackSearchClient", FakeFallback):
        asyncio.run(_run_full(
            cities, topics, cfg, FallbackExtractor(primaries=[]), None,
            True, True, {}, None,
        ))

    cached = get_search_cache(db, "Budapest", "running", ttl_days=3650)
    assert cached == [], f"empty search should be cached as [], got {cached!r}"
