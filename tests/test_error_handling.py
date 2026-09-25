"""Provider-failure handling: typed errors, no empty-result caching, retries."""
import asyncio
import json
import os
from unittest.mock import patch

import pytest

from scraper.extract import (
    ExtractorQuotaError,
    ExtractorRateLimitError,
    ExtractorUnavailableError,
    FallbackExtractor,
)
from scraper.models import SearchResult
from scraper.pipeline import CityConfig, PipelineConfig, TopicConfig, _run_full
from scraper.search import (
    FallbackSearchClient,
    SearchQuotaError,
    SearchUnavailableError,
)


class StubPrimary:
    """Scripted extractor primary raising per-call errors then succeeding."""

    model = "stub-model"
    model_fingerprint = "fp-stub"
    venue_fingerprint = "fp-venue"
    person_fingerprint = "fp-person"
    enrich_fingerprint = "fp-enrich"

    def __init__(self, script):
        self.script = list(script)  # each item: Exception or return value
        self.calls = 0

    async def extract(self, *a, **k):
        self.calls += 1
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def test_quota_error_raises_unavailable_and_exhausts():
    fe = FallbackExtractor(primaries=[StubPrimary([ExtractorQuotaError("402")])])
    with pytest.raises(ExtractorUnavailableError):
        asyncio.run(fe.extract("t", "c", "top", "hu", "http://x"))
    assert fe.exhausted
    # subsequent calls fail fast without touching the provider again
    with pytest.raises(ExtractorUnavailableError):
        asyncio.run(fe.extract("t", "c", "top", "hu", "http://x"))


def test_transient_error_is_retried_once():
    p = StubPrimary([ExtractorUnavailableError("HTTP 500"), ["ok"]])
    fe = FallbackExtractor(primaries=[p])
    assert asyncio.run(fe.extract("t", "c", "top", "hu", "http://x")) == ["ok"]
    assert p.calls == 2
    assert not fe.exhausted


def test_persistent_transient_error_raises():
    p = StubPrimary([ExtractorUnavailableError("boom"), ExtractorUnavailableError("boom")])
    fe = FallbackExtractor(primaries=[p])
    with pytest.raises(ExtractorUnavailableError):
        asyncio.run(fe.extract("t", "c", "top", "hu", "http://x"))
    assert not fe.exhausted  # transient ≠ exhausted; next page will try again


def test_unexpected_error_becomes_unavailable_not_a_run_abort():
    """2026-07-30: a parser AttributeError escaped the chain and killed the whole
    ai_only window (0 pairs). Untyped bugs must surface as a skipped page."""
    p = StubPrimary([AttributeError("'list' object has no attribute 'get'"),
                     AttributeError("'list' object has no attribute 'get'")])
    fe = FallbackExtractor(primaries=[p])
    with pytest.raises(ExtractorUnavailableError):
        asyncio.run(fe.extract("t", "c", "top", "hu", "http://x"))
    assert p.calls == 2  # retried like any transient error
    assert not fe.exhausted


def test_unexpected_errors_still_open_the_circuit_breaker():
    """A systematic bug must not be retried for a whole night either."""
    p = StubPrimary([TypeError("boom")] * 100)
    fe = FallbackExtractor(primaries=[p], failure_threshold=3)
    for _ in range(3):
        with pytest.raises(ExtractorUnavailableError):
            asyncio.run(fe.extract("t", "c", "top", "hu", "http://x"))
    assert fe.providers_down


def test_rate_limit_waits_out_short_window_and_succeeds():
    p = StubPrimary([ExtractorRateLimitError(0.05), ["ok"]])
    fe = FallbackExtractor(primaries=[p])
    assert asyncio.run(fe.extract("t", "c", "top", "hu", "http://x")) == ["ok"]
    assert p.calls == 2


class QuotaSearchProvider:
    async def search(self, query, locale="en", num_results=10):
        raise SearchQuotaError("credits gone")


class MalformedResponseProvider:
    """DataForSEO parsing assumes the documented object shape; a bare array in
    the response raises AttributeError deep inside search()."""

    def __init__(self):
        self.calls = 0

    async def search(self, query, locale="en", num_results=10):
        self.calls += 1
        raise AttributeError("'list' object has no attribute 'get'")


def test_untyped_search_error_does_not_escape_the_chain():
    """Search-side twin of the 2026-07-30 extraction abort: an unexpected error
    must become a typed, uncached failure — not a dead collector window."""
    p = MalformedResponseProvider()
    c = FallbackSearchClient(primaries=[p])
    with pytest.raises(SearchUnavailableError):
        asyncio.run(c.search_all(["q1"]))
    with pytest.raises(SearchUnavailableError):
        asyncio.run(c.search("q1"))
    assert p.calls  # the provider was actually exercised


def test_search_all_raises_when_quota_kills_everything():
    c = FallbackSearchClient(primaries=[QuotaSearchProvider()])
    with pytest.raises(SearchQuotaError):
        asyncio.run(c.search_all(["q1"]))
    assert c.exhausted
    with pytest.raises(SearchQuotaError):  # fail-fast on later pairs
        asyncio.run(c.search_all(["q2"]))


def test_search_all_no_providers_raises():
    c = FallbackSearchClient(primaries=[])
    with pytest.raises(SearchQuotaError):
        asyncio.run(c.search_all(["q1"]))


def test_persistent_search_failure_disables_provider_after_three_attempts():
    class UnavailableSearchProvider:
        def __init__(self):
            self.calls = 0

        async def search(self, query, locale="en", num_results=10):
            self.calls += 1
            raise SearchUnavailableError("queue timed out")

    provider = UnavailableSearchProvider()
    client = FallbackSearchClient(primaries=[provider])
    for query in ("q1", "q2", "q3"):
        with pytest.raises(SearchUnavailableError):
            asyncio.run(client.search_all([query]))
    assert client.exhausted
    with pytest.raises(SearchUnavailableError):
        asyncio.run(client.search_all(["q4"]))
    assert provider.calls == 3


def test_success_resets_search_unavailability_counter():
    class FlakySearchProvider:
        def __init__(self):
            self.script = [
                SearchUnavailableError("temporary"),
                [SearchResult(url="https://ok.test", title="ok")],
                SearchUnavailableError("temporary"),
                SearchUnavailableError("temporary"),
            ]

        async def search(self, query, locale="en", num_results=10):
            result = self.script.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

    client = FallbackSearchClient(primaries=[FlakySearchProvider()])
    with pytest.raises(SearchUnavailableError):
        asyncio.run(client.search_all(["q1"]))
    assert asyncio.run(client.search_all(["q2"]))
    for query in ("q3", "q4"):
        with pytest.raises(SearchUnavailableError):
            asyncio.run(client.search_all([query]))
    assert not client.exhausted


def test_single_search_propagates_transient_failure_instead_of_empty_result():
    class UnavailableSearchProvider:
        async def search(self, query, locale="en", num_results=10):
            raise SearchUnavailableError("temporary")

    client = FallbackSearchClient(primaries=[UnavailableSearchProvider()])
    with pytest.raises(SearchUnavailableError):
        asyncio.run(client.search("q1"))
    assert not client.exhausted


def test_single_search_fails_fast_after_provider_is_disabled():
    class UnavailableSearchProvider:
        def __init__(self):
            self.calls = 0

        async def search(self, query, locale="en", num_results=10):
            self.calls += 1
            raise SearchUnavailableError("temporary")

    provider = UnavailableSearchProvider()
    client = FallbackSearchClient(primaries=[provider])
    for query in ("q1", "q2", "q3", "q4"):
        with pytest.raises(SearchUnavailableError):
            asyncio.run(client.search(query))
    assert client.exhausted
    assert provider.calls == 3


def test_search_all_partial_results_survive_quota():
    class OneThenQuota:
        async def search(self, query, locale="en", num_results=10):
            if query == "q1":
                return [SearchResult(url="https://a.test", title="a")]
            raise SearchQuotaError("gone")
    c = FallbackSearchClient(primaries=[OneThenQuota()])
    results = asyncio.run(c.search_all(["q1", "q2"]))
    assert [r.url for r in results] == ["https://a.test"]  # partial kept, no raise


# ── pipeline-level: failures must not be cached ─────────────────────────────

def _pipeline_fixtures(tmp_path):
    from scraper.db import init_db
    db = tmp_path / "scraper.db"
    init_db(db)
    cfg = PipelineConfig(
        search_results_per_query=5, search_max_pages=2, search_rate_limit=1.0,
        fetch_timeout=15, fetch_min_text_length=10, fetch_max_concurrent=3,
        fetch_blocked_domains=[], db_path=db,
    )
    cities = [CityConfig(name="Budapest", locale="hu", search_variants=[])]
    topics = [TopicConfig(name="running", search_terms={"hu": ["futás"]})]
    return db, cfg, cities, topics


def test_search_quota_pair_not_cached(tmp_path):
    from scraper.db import get_search_cache
    db, cfg, cities, topics = _pipeline_fixtures(tmp_path)

    class QuotaFallback:
        exhausted = False
        def __init__(self, primaries): ...
        async def search_all(self, *a, **k):
            raise SearchQuotaError("credits gone")

    with patch("scraper.pipeline.FallbackSearchClient", QuotaFallback):
        _, logs = asyncio.run(_run_full(
            cities, topics, cfg, FallbackExtractor(primaries=[]), None,
            True, True, {}, None,
        ))

    assert get_search_cache(db, "Budapest", "running", ttl_days=3650) is None, \
        "a quota-failed search must NOT be recorded as searched"
    assert any(p.get("search_failed") for p in logs)


def test_provider_death_aborts_run_instead_of_per_pair_failures(tmp_path):
    """3 real errors must not become thousands of per-pair failures (2026-07-22)."""
    db, cfg, cities, topics = _pipeline_fixtures(tmp_path)
    cities = [CityConfig(name=n, locale="hu", search_variants=[])
              for n in ("Budapest", "Szeged")]
    topics = [TopicConfig(name=t, search_terms={"hu": ["kifejezés"]})
              for t in ("running", "chess")]

    def quota_client(primaries):
        return FallbackSearchClient(primaries=[QuotaSearchProvider()])

    with patch("scraper.pipeline.FallbackSearchClient", quota_client):
        _, logs = asyncio.run(_run_full(
            cities, topics, cfg, FallbackExtractor(primaries=[]), None,
            True, True, {}, None,
        ))

    # pair 1: real quota error; pair 2: abort marker; pairs 3-4: never walked.
    # The prefetch reads ahead lazily — it is triggered by the pair that needs
    # it — so the first pair still sees its own failure rather than inheriting
    # a provider that was already marked dead before the walk began.
    assert len(logs) == 2
    assert all(p["search_failed"] for p in logs)
    assert logs[1]["aborted"] is True
    assert all("credits gone" in (p["search_error"] or "") for p in logs)


def test_missing_credentials_abort_carries_reason(tmp_path):
    db, cfg, cities, topics = _pipeline_fixtures(tmp_path)
    topics = [TopicConfig(name=t, search_terms={"hu": ["kifejezés"]})
              for t in ("running", "chess")]

    _, logs = asyncio.run(_run_full(
        cities, topics, cfg, FallbackExtractor(primaries=[]), None,
        True, True, {}, None,
    ))

    assert len(logs) == 1  # single abort marker, not one failure per pair
    assert logs[0]["search_failed"]
    assert "no search provider configured" in (logs[0]["search_error"] or "")


def test_run_pipeline_skips_catchup_when_provider_dead(tmp_path):
    from scraper.pipeline import run_pipeline
    db, cfg, cities, topics = _pipeline_fixtures(tmp_path)

    def quota_client(primaries):
        return FallbackSearchClient(primaries=[QuotaSearchProvider()])

    with patch("scraper.pipeline.FallbackSearchClient", quota_client):
        logs, _ = asyncio.run(run_pipeline(
            cities, topics, cfg, cache=None, run_mode="search_only",
        ))

    # main pass logs the one real failure; the catch-up pass must not replay it
    assert len(logs) == 1
    assert logs[0]["search_failed"]


def test_extract_failure_not_cached_as_empty(tmp_path):
    from scraper.cache import CacheManager
    db, cfg, cities, topics = _pipeline_fixtures(tmp_path)
    cache = CacheManager(db)

    class OkSearch:
        exhausted = False
        def __init__(self, primaries): ...
        async def search_all(self, *a, **k):
            return [SearchResult(url="https://klub.test/x", title="t")]

    failing = FallbackExtractor(primaries=[StubPrimary([ExtractorUnavailableError("500"),
                                                        ExtractorUnavailableError("500")])])

    async def fake_fetch(url, *a, **k):
        return "Elég hosszú oldalszöveg a teszthez."

    with patch("scraper.pipeline.FallbackSearchClient", OkSearch), \
         patch("scraper.pipeline.fetch_and_clean", fake_fetch):
        _, logs = asyncio.run(_run_full(
            cities, topics, cfg, failing, cache,
            True, True, {}, None, run_venues=False, run_persons=False,
        ))

    # page scraped (raw text cached) but NOT marked extracted — retried next run
    assert cache.get_scraped("https://klub.test/x")
    assert cache.get_extracted("https://klub.test/x", fingerprint="fp-stub") is None, \
        "a failed extraction must not be cached as an empty result"
    assert sum(p.get("extract_failed", 0) for p in logs) == 1


# ── extractor circuit breaker + preflight ───────────────────────────────────

def test_circuit_breaker_opens_after_consecutive_failures():
    """A dead provider must stop the chain instead of failing page after page.

    2026-07-24: a retired model name 400'd 2736 times across a whole off-peak
    window because nothing counted the repetition.
    """
    p = StubPrimary([ExtractorUnavailableError("HTTP 400")] * 20)
    fe = FallbackExtractor(primaries=[p], failure_threshold=3)
    for _ in range(3):
        with pytest.raises(ExtractorUnavailableError):
            asyncio.run(fe.extract("t", "c", "top", "hu", "http://x"))
    assert fe.providers_down
    assert "HTTP 400" in (fe.failure_reason or "")
    # breaker open → no further provider calls (3 attempts × 2 retry rounds)
    calls_when_open = p.calls
    with pytest.raises(ExtractorUnavailableError):
        asyncio.run(fe.extract("t", "c", "top", "hu", "http://x"))
    assert p.calls == calls_when_open


def test_success_resets_circuit_breaker_counter():
    """Scattered transient errors must never trip the breaker."""
    p = StubPrimary([
        ExtractorUnavailableError("500"), ExtractorUnavailableError("500"),
        ["ok"],
        ExtractorUnavailableError("500"), ExtractorUnavailableError("500"),
    ])
    fe = FallbackExtractor(primaries=[p], failure_threshold=2)
    with pytest.raises(ExtractorUnavailableError):
        asyncio.run(fe.extract("t", "c", "top", "hu", "http://x"))
    assert asyncio.run(fe.extract("t", "c", "top", "hu", "http://x")) == ["ok"]
    with pytest.raises(ExtractorUnavailableError):
        asyncio.run(fe.extract("t", "c", "top", "hu", "http://x"))
    assert not fe.providers_down


def test_no_provider_configured_is_not_providers_down():
    """An unset API key is a deliberate no-LLM run, not an outage — it must not
    abort search/fetch work."""
    fe = FallbackExtractor(primaries=[])
    assert fe.exhausted
    assert not fe.providers_down


def test_preflight_noop_without_providers():
    assert asyncio.run(FallbackExtractor(primaries=[]).preflight()) is None


def test_preflight_raises_on_dead_provider():
    fe = FallbackExtractor(primaries=[StubPrimary([ExtractorUnavailableError("HTTP 400")] * 2)])
    with pytest.raises(ExtractorUnavailableError):
        asyncio.run(fe.preflight())


def test_run_pipeline_aborts_before_any_work_when_preflight_fails(tmp_path):
    """A broken model name must fail the run immediately, not one page at a time."""
    from scraper.pipeline import run_pipeline
    db, cfg, cities, topics = _pipeline_fixtures(tmp_path)
    searched = []

    class OkSearch:
        exhausted = False
        def __init__(self, primaries): ...
        async def search_all(self, *a, **k):
            searched.append(a)
            return [SearchResult(url="https://klub.test/x", title="t")]

    dead = FallbackExtractor(primaries=[StubPrimary([ExtractorUnavailableError("HTTP 400")] * 4)])

    with patch("scraper.pipeline.FallbackSearchClient", OkSearch), \
         patch("scraper.pipeline.FallbackExtractor", lambda primaries: dead):
        with pytest.raises(ExtractorUnavailableError) as exc:
            asyncio.run(run_pipeline(cities, topics, cfg, cache=None, run_mode="full"))

    assert "preflight" in str(exc.value)
    assert not searched, "no paid search may run once the extractor is known dead"


def test_ai_only_aborts_when_extractor_dies_midrun(tmp_path):
    """The breaker must end the run, not log one failure per cached page."""
    from scraper.cache import CacheManager
    from scraper.pipeline import _run_ai_only
    db, cfg, cities, topics = _pipeline_fixtures(tmp_path)
    cache = CacheManager(db)
    cities = [CityConfig(name=n, locale="hu", search_variants=[])
              for n in ("Budapest", "Szeged")]
    from scraper.db import save_search_cache
    for city in ("Budapest", "Szeged"):
        for url in (f"https://{city}.test/a", f"https://{city}.test/b"):
            cache.save_scraped(url, "Elég hosszú oldalszöveg a teszthez.", city, "running")
        save_search_cache(db, city, "running",
                          [f"https://{city}.test/a", f"https://{city}.test/b"], ["q"])

    dead = FallbackExtractor(
        primaries=[StubPrimary([ExtractorUnavailableError("HTTP 400")] * 10)],
        failure_threshold=1)

    _, logs = asyncio.run(_run_ai_only(
        cities, topics, cfg, dead, cache, True, {}, None,
        run_venues=False, run_persons=False))

    assert len(logs) == 1, "the run must abort at the first pair, not walk both cities"
    assert logs[0]["extract_error"] and "HTTP 400" in logs[0]["extract_error"]


def test_a_provider_that_recovers_on_retry_is_not_retired():
    """Failing an attempt and succeeding on the retry is a working provider.

    `_call` retries transient errors once, and the failures seen during a call
    are applied at the end — so the provider that eventually answered has to be
    excluded, or twenty self-recovering calls retire a healthy endpoint.
    """
    p = StubPrimary([ExtractorUnavailableError("500"), ["ok"]] * 30)
    fe = FallbackExtractor(primaries=[p], failure_threshold=2)
    for _ in range(30):
        assert asyncio.run(fe.extract("t", "c", "top", "hu", "http://x")) == ["ok"]
    assert not fe.providers_down


def test_completed_extractions_are_cached_even_when_the_pair_stops(tmp_path):
    """Work the fleet was already charged for must reach the cache.

    With extraction concurrent, every page of a pair can finish before the
    consumer loop notices the fleet stopped. Breaking out of the loop there
    threw those results away and made the next pass pay for them again.
    """
    from scraper.cache import CacheManager
    from scraper.pipeline import _run_ai_only
    db, cfg, cities, topics = _pipeline_fixtures(tmp_path)
    cfg.extract_concurrency = 4
    cache = CacheManager(db)
    cities = [CityConfig(name="Budapest", locale="hu", search_variants=[])]
    urls = [f"https://budapest.test/{i}" for i in range(4)]
    for url in urls:
        cache.save_scraped(url, "Elég hosszú oldalszöveg a teszthez.", "Budapest", "running")

    class _Primary:
        provider, model, quality = "p", "m", 50
        model_fingerprint = "fp"

        async def extract(self, text, city, topic, locale, source_url,
                          false_positive_examples=""):
            return []

    chain = FallbackExtractor(primaries=[_Primary()])
    asyncio.run(_run_ai_only(cities, topics, cfg, chain, cache, True, {}, None,
                             run_venues=False, run_persons=False))

    # Every page that was extracted is cached, so the next pass skips it.
    assert all(cache.get_extracted(u, fingerprint=chain.canonical_fingerprint) is not None
               for u in urls)


def test_pages_of_a_pair_overlap(tmp_path):
    """The point of the change: a pair's pages no longer wait for each other."""
    from scraper.cache import CacheManager
    from scraper.pipeline import _run_ai_only
    db, cfg, cities, topics = _pipeline_fixtures(tmp_path)
    cfg.extract_concurrency = 4
    cache = CacheManager(db)
    cities = [CityConfig(name="Budapest", locale="hu", search_variants=[])]
    for i in range(4):
        cache.save_scraped(f"https://budapest.test/{i}", "Elég hosszú oldalszöveg.",
                           "Budapest", "running")

    class _Slow:
        provider, model, quality = "p", "m", 50
        model_fingerprint = "fp"
        in_flight = 0
        peak = 0

        async def extract(self, *a, **kw):
            _Slow.in_flight += 1
            _Slow.peak = max(_Slow.peak, _Slow.in_flight)
            await asyncio.sleep(0.02)
            _Slow.in_flight -= 1
            return []

    asyncio.run(_run_ai_only(cities, topics, cfg, FallbackExtractor(primaries=[_Slow()]),
                             cache, True, {}, None, run_venues=False, run_persons=False))
    assert _Slow.peak > 1, "extraction still serialised"


def test_serial_is_still_available(tmp_path):
    """extract_concurrency=1 must reproduce the old chain exactly — the kill switch."""
    from scraper.cache import CacheManager
    from scraper.pipeline import _run_ai_only
    db, cfg, cities, topics = _pipeline_fixtures(tmp_path)
    cfg.extract_concurrency = 1
    cache = CacheManager(db)
    cities = [CityConfig(name="Budapest", locale="hu", search_variants=[])]
    for i in range(4):
        cache.save_scraped(f"https://budapest.test/{i}", "Elég hosszú oldalszöveg.",
                           "Budapest", "running")

    class _Watch:
        provider, model, quality = "p", "m", 50
        model_fingerprint = "fp"
        in_flight = 0
        peak = 0

        async def extract(self, *a, **kw):
            _Watch.in_flight += 1
            _Watch.peak = max(_Watch.peak, _Watch.in_flight)
            await asyncio.sleep(0.01)
            _Watch.in_flight -= 1
            return []

    asyncio.run(_run_ai_only(cities, topics, cfg, FallbackExtractor(primaries=[_Watch()]),
                             cache, True, {}, None, run_venues=False, run_persons=False))
    assert _Watch.peak == 1


def test_a_citys_searches_are_issued_together(tmp_path):
    """The collector's cost is waiting on queued tasks; pairs are independent."""
    from scraper.pipeline import _run_full
    db, cfg, cities, topics = _pipeline_fixtures(tmp_path)
    cfg.search_concurrency = 4
    cities = [CityConfig(name="Budapest", locale="hu", search_variants=[])]
    topics = [TopicConfig(name=t, search_terms={"hu": ["kifejezés"]})
              for t in ("running", "chess", "music", "dance")]

    class _Slow:
        in_flight = 0
        peak = 0
        exhausted = False

        async def search_all(self, queries, locale="en", num_results=10, stop_after=None, usable=None):
            _Slow.in_flight += 1
            _Slow.peak = max(_Slow.peak, _Slow.in_flight)
            await asyncio.sleep(0.02)
            _Slow.in_flight -= 1
            return []

    asyncio.run(_run_full(cities, topics, cfg, FallbackExtractor(primaries=[]), None,
                          True, True, {}, None, search_client=_Slow()))
    assert _Slow.peak > 1, "searches still issued one pair at a time"


def test_a_prefetched_search_is_saved_even_if_never_consumed(tmp_path):
    """An unsaved paid search is re-paid on every future run."""
    from scraper.db import get_search_cache
    from scraper.pipeline import _prefetch_searches
    from scraper.search import SearchResult
    db, cfg, cities, topics = _pipeline_fixtures(tmp_path)
    city = CityConfig(name="Budapest", locale="hu", search_variants=[])
    topic = TopicConfig(name="running", search_terms={"hu": ["kifejezés"]})

    class _Ok:
        exhausted = False

        async def search_all(self, queries, locale="en", num_results=10, stop_after=None, usable=None):
            return [SearchResult(url="https://a.test", title="t", snippet="s")]

    out = asyncio.run(_prefetch_searches(_Ok(), [(city, topic)], cfg, concurrency=2))

    assert out[("Budapest", "running")]          # returned to the caller
    # …and durable, even though no pair loop ever consumed it.
    assert get_search_cache(db, "Budapest", "running", 7) == ["https://a.test"]


def test_a_too_large_payload_retires_the_model_not_the_fleet(tmp_path):
    """413 means this model's context is too small, not that the fleet is down.

    Production, 2026-08-18: HTTP 413 counted as a plain failure, twenty in a
    row opened the breaker and the night's extraction run was reported as a
    provider outage.
    """
    import httpx

    from scraper.extract import ExtractorModelError
    from scraper.providers import OpenAICompatExtractor

    ex = OpenAICompatExtractor(provider="p", base_url="https://x.test",
                               api_key="k", model="m", quality=50)

    class _Resp:
        status_code = 413
        text = '{"error":"payload too large"}'
        headers: dict = {}

    async def _fake_post(*a, **kw):
        return _Resp()

    with patch.object(httpx.AsyncClient, "post", _fake_post):
        with pytest.raises(ExtractorModelError):
            asyncio.run(ex._post({"messages": []}, "label"))


def test_no_blocking_database_write_is_left_on_the_event_loop():
    """The loop that serves the site must not be held by a SQLite write.

    Fourteen of these ran directly inside coroutines. Serially it was
    tolerable; with eight searches and four extractions in flight /healthz
    reached six seconds, the Docker liveness probe killed the container, and
    Traefik dropped the route — 2026-08-18's 404s, three steps upstream.

    Checked by parsing rather than against a list of names: the first version
    of this test carried a hand-written list and missed ten call sites.
    A wrapped call passes the function by name, so it is not a Call node at
    all — every remaining Call to a write-shaped name is an offender.
    """
    import ast
    from pathlib import Path as _P

    verbs = ("save_", "upsert_", "mark_", "delete_", "update_", "record_",
             "insert_", "replace_")
    # Reads that scan whole tables hold the loop exactly as hard as writes, and
    # the first version of this test only listed verbs that sounded like
    # writing. get_fully_processed_pairs loads every cache_pages row and
    # JSON-parses it; leaving it on the loop is why the 404s continued after
    # the writes were moved.
    heavy_reads = ("get_fully_processed_pairs", "get_covered_pairs",
                   "get_collected_pairs", "corpus_names")
    tree = ast.parse(_P("scraper/pipeline.py").read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name.startswith(verbs) or name in heavy_reads:
            offenders.append(f"line {node.lineno}: {name}(...)")
    assert not offenders, (
        "blocking database calls left on the event loop — wrap them in "
        "_off_loop():\n" + "\n".join(offenders))


def test_prefetched_results_do_not_accumulate(tmp_path):
    """One result list per pair, held for the whole run, is an OOM at scale.

    The prefetch writes to search_cache, so the loop reaches the pair on the
    cache-hit path — which used to leave the entry in the dict forever.
    """
    from scraper.db import save_search_cache
    from scraper.pipeline import _run_full
    db, cfg, cities, topics = _pipeline_fixtures(tmp_path)
    cfg.search_concurrency = 4
    cities = [CityConfig(name=n, locale="hu", search_variants=[])
              for n in ("Budapest", "Szeged", "Pécs", "Győr")]
    topics = [TopicConfig(name="running", search_terms={"hu": ["kifejezés"]})]
    # Every pair already cached: the loop takes the cache-hit path throughout.
    for c in cities:
        save_search_cache(db, c.name, "running", [], ["q"])

    class _Never:
        exhausted = False

        async def search_all(self, *a, **kw):
            raise AssertionError("cached pairs must not be searched again")

    _, logs = asyncio.run(_run_full(cities, topics, cfg,
                                    FallbackExtractor(primaries=[]), None,
                                    True, True, {}, None, search_client=_Never()))
    assert len(logs) == 4
    assert all(p["search_cache_hit"] for p in logs)


# ── The worker's decisions, tested as decisions ───────────────────────────────
# These replace tests that asserted on the *source text* of scraper/main.py.
# Google's review guide asks two questions of a test: will it fail when the code
# is broken, and will it produce false positives when the code changes beneath
# it? A substring assertion answers badly on both — it passes with the string
# present and the logic wrong, and fails on a rename that changes nothing.

def test_pages_worked_ignores_pairs_with_nothing_to_extract():
    """ai_only logs a pair even when it has no cached pages at all.

    Every never-searched pair is in the run's filter, so counting pair logs
    made an empty pass look busy: the worker relaunched extraction forever
    while the paid collector never ran.
    """
    from scraper.pipeline import pages_worked

    assert pages_worked([{"urls_found": 0}] * 92) == 0


def test_pages_worked_does_not_count_a_failing_page():
    """A failure caches nothing, so the next run finds the identical state.

    One permanently failing page drove about a hundred runs on 2026-08-18, one
    every two or three minutes for four and a half hours, each writing a run
    record.
    """
    from scraper.pipeline import pages_worked

    assert pages_worked([{"urls_found": 1, "cache_hits_extract": 0,
                          "extract_failed": 1}]) == 0


def test_pages_worked_does_not_count_a_cache_hit():
    from scraper.pipeline import pages_worked

    assert pages_worked([{"urls_found": 5, "cache_hits_extract": 5}]) == 0


def test_pages_worked_counts_a_page_extracted_for_the_first_time():
    from scraper.pipeline import pages_worked

    assert pages_worked([{"urls_found": 5, "cache_hits_extract": 3,
                          "extract_failed": 1}]) == 1
    # And adds up across a pair log.
    assert pages_worked([{"urls_found": 4, "cache_hits_extract": 1},
                         {"urls_found": 2, "extract_failed": 2}]) == 3


def test_stop_pauses_the_worker_and_run_lets_it_go_again():
    """Cancelling alone stopped nothing: the worker started another run.

    Tested through the endpoints rather than by reading them, so it fails if
    the pause is ever lost and passes only when stopping actually stops.
    """
    from scraper.web import api
    from scraper.web.state import app_state

    before = app_state.worker_paused
    try:
        asyncio.run(api.control_stop("Bearer k"))          # unauthorised: no effect
        assert app_state.worker_paused == before

        os.environ["CONTROL_API_KEY"] = "op-key"
        try:
            asyncio.run(api.control_stop("Bearer op-key"))
            assert app_state.worker_paused is True

            asyncio.run(api.control_resume("Bearer op-key"))
            assert app_state.worker_paused is False
        finally:
            os.environ.pop("CONTROL_API_KEY", None)
    finally:
        app_state.worker_paused = before


def test_the_worker_extracts_while_the_budget_lasts_and_collects_after():
    """Free quota expires at midnight; collection costs money. So: quota first."""
    from scraper.pipeline import (WORKER_COLLECT, WORKER_EXTRACT, WORKER_WAIT,
                                  next_worker_action)

    def choose(**kw):
        base = dict(is_running=False, paused=False, quota=True, extract_ready=True)
        return next_worker_action(**{**base, **kw})

    assert choose() == WORKER_EXTRACT
    assert choose(quota=False) == WORKER_COLLECT
    # Extraction found nothing recently: stand aside rather than repeat it.
    assert choose(extract_ready=False) == WORKER_COLLECT

    # Neither happens while something else owns the run slot, or after a stop —
    # the pause is what made /v1/control/stop mean anything.
    assert choose(is_running=True) == WORKER_WAIT
    assert choose(paused=True) == WORKER_WAIT
    assert choose(paused=True, quota=False) == WORKER_WAIT


def test_the_log_keeps_more_than_the_ring(tmp_path):
    """500 lines is a few minutes under the worker, which is not a log.

    Every "what happened last night?" this week hit a buffer that had already
    forgotten, so history lives in a rotating file and the ring only serves the
    live tail.
    """
    from scraper.web.log_stream import LogBroadcaster

    b = LogBroadcaster()
    b.attach_file(tmp_path / "logs")
    for i in range(3000):
        b.add_line({"event": f"line_{i}", "log_level": "info", "city": "Szentendre"})
    b.add_line({"event": "boom", "log_level": "error"})

    assert len(b.get_all()) == 500                      # the ring is unchanged
    assert len(b.history(limit=5000)) == 3001           # the file is not
    assert b.history(limit=5000)[0]["text"].startswith("line_0")

    # The filters keep the semantics an operator already relies on:
    # case-insensitive, and matching anywhere in the row.
    assert len(b.history(limit=5000, grep="szentendre")) == 3000
    assert [r["text"] for r in b.history(limit=10, level="error")] == ["boom"]


def test_the_log_survives_a_directory_it_cannot_write(tmp_path):
    """Losing history is bad; refusing to serve is worse."""
    from scraper.web.log_stream import LogBroadcaster

    blocker = tmp_path / "logs"
    blocker.write_text("not a directory")            # mkdir will fail on this

    b = LogBroadcaster()
    b.attach_file(blocker)
    b.add_line({"event": "still_works", "log_level": "info"})
    assert b.get_all()[-1]["text"] == "still_works"
    assert b.history(limit=10)[-1]["text"] == "still_works"   # falls back to the ring


def _http_extractor(status: int, body: str):
    import httpx

    from scraper.providers import OpenAICompatExtractor

    ex = OpenAICompatExtractor(provider="groq", base_url="https://x.test",
                               api_key="k", model="gpt-oss", quality=50)

    class _Resp:
        status_code = status
        text = body
        headers: dict = {}

        def json(self):
            import json as _json
            return _json.loads(body)

    async def _fake_post(*a, **kw):
        return _Resp()

    return ex, patch.object(httpx.AsyncClient, "post", _fake_post)


def test_invalid_json_under_response_format_is_a_content_failure():
    """Groq's 400 `json_validate_failed`: the model answered badly. Read as an
    outage it deferred the whole daily guide step (2026-09-22..24).
    """
    from scraper.extract import ExtractorContentError

    ex, patched = _http_extractor(
        400, '{"error":{"code":"json_validate_failed","failed_generation":"ma"}}')
    with patched, pytest.raises(ExtractorContentError, match="groq:gpt-oss"):
        asyncio.run(ex._post({"messages": []}, "label"))


def test_revoked_key_retires_the_model_for_the_run():
    from scraper.extract import ExtractorModelError

    ex, patched = _http_extractor(401, '{"error":"invalid api key"}')
    with patched, pytest.raises(ExtractorModelError):
        asyncio.run(ex._post({"messages": []}, "label"))


def test_http_200_carrying_an_error_is_an_outage_not_an_empty_answer():
    """Parsed as an answer it read as empty text and counted toward quarantine."""
    from scraper.extract import (ExtractorContentError, ExtractorRateLimitError,
                                 ExtractorUnavailableError)

    ex, patched = _http_extractor(200, '{"error":{"code":502,"message":"upstream"}}')
    with patched, pytest.raises(ExtractorUnavailableError) as info:
        asyncio.run(ex._post({"messages": []}, "label"))
    assert not isinstance(info.value, ExtractorContentError)

    ex, patched = _http_extractor(200, '{"error":{"code":429,"message":"slow down"}}')
    with patched, pytest.raises(ExtractorRateLimitError):
        asyncio.run(ex._post({"messages": []}, "label"))


def test_an_empty_answer_recovered_from_reasoning_is_never_a_verdict():
    """"Nothing here" is cached forever. From a reasoning model that ran out of
    budget restating its schema, it is not a verdict about the page.
    """
    from scraper.extract import ExtractorContentError

    ex, patched = _http_extractor(200, json.dumps({"choices": [{
        "finish_reason": "length",
        "message": {"content": "",
                    "reasoning": 'I must output {"communities": []} if none'}}]}))
    with patched, pytest.raises(ExtractorContentError):
        asyncio.run(ex.extract("text", "Pécs", "running", "hu", "https://p.test"))

    # A finished answer saying "nothing here" is still a legitimate empty page.
    ex, patched = _http_extractor(200, json.dumps({"choices": [{
        "finish_reason": "stop", "message": {"content": '{"communities": []}'}}]}))
    with patched:
        assert asyncio.run(ex.extract("text", "Pécs", "running", "hu",
                                      "https://p.test")) == []


def test_an_object_embedded_in_free_text_must_have_the_answers_shape():
    from scraper.extract import ExtractorContentError, _json_items

    with pytest.raises(ExtractorContentError):
        _json_items('prefix {"note": 1} trailing', "communities", "community", "u")
    assert _json_items('{\n{"communities": [{"name": "A"}]}', "communities",
                       "community", "u") == [{"name": "A"}]


def test_an_unusable_description_fails_over_instead_of_counting_as_success():
    from scraper.extract import ExtractorContentError

    ex, patched = _http_extractor(200, json.dumps({"choices": [{
        "finish_reason": "stop", "message": {"content": "Sure! Here it is"}}]}))
    with patched, pytest.raises(ExtractorContentError):
        asyncio.run(ex.write_descriptions("A", "Pécs", "running", "hu", "text"))


def test_round_two_does_not_repeat_a_content_failure():
    """Round 2 exists for transient errors; an answer that did not fit will not
    fit now either, and each retry is charged in full.
    """
    from scraper.extract import (ExtractorContentError, ExtractorUnavailableError,
                                 FallbackExtractor)

    class _P:
        def __init__(self, model, exc):
            self.model, self.exc, self.calls = model, exc, 0

        async def completion(self, messages, **params):
            self.calls += 1
            raise self.exc

    bad = _P("bad-json", ExtractorContentError("invalid JSON"))
    flaky = _P("flaky", ExtractorUnavailableError("HTTP 502"))
    chain = FallbackExtractor(primaries=[bad, flaky])
    with pytest.raises(ExtractorUnavailableError):
        asyncio.run(chain.completion([]))
    assert bad.calls == 1 and flaky.calls == 2


def test_ai_only_makes_no_llm_person_call_but_still_records_leaders(tmp_path):
    """The separate person call spent a quarter of all extraction calls from
    2026-08 to 2026-09-24 and saved no one; leaders come from the community
    record's own `leader` field.
    """
    import sqlite3

    from scraper.cache import CacheManager
    from scraper.db import save_search_cache
    from scraper.models import CommunityRecord
    from scraper.pipeline import _run_ai_only

    db, cfg, cities, topics = _pipeline_fixtures(tmp_path)
    cache = CacheManager(db)
    url = "https://budapest.test/klub"
    cache.save_scraped(url, "Elég hosszú oldalszöveg a teszthez.", "Budapest", "running")
    save_search_cache(db, "Budapest", "running", [url], ["q"])

    record = CommunityRecord(
        name="Budapesti Futókör", city="Budapest", topic="running", locale="hu",
        description="Heti futás a Margitszigeten, bárki csatlakozhat.",
        meeting_schedule="Kedd 18:00", location="Margitsziget", leader="Kovács Anna",
        website="https://futokor.test", source_url=url,
        extracted_at="2026-09-24T00:00:00Z")

    class _Primary(StubPrimary):
        person_calls = 0

        async def extract_persons(self, *a, **k):
            _Primary.person_calls += 1
            return []

    chain = FallbackExtractor(primaries=[_Primary([[record]])])
    asyncio.run(_run_ai_only(cities, topics, cfg, chain, cache, True, {}, None,
                             run_venues=False, run_persons=True))

    assert _Primary.person_calls == 0
    with sqlite3.connect(db) as conn:
        names = [r[0] for r in conn.execute("SELECT json_extract(data, '$.name') FROM persons")]
    assert names == ["Kovács Anna"]


def test_preflight_skips_a_model_that_served_real_work_recently():
    """Probing every model every two-hour pass spent ~80 requests a day per
    model — most of Cloudflare's 100 (review, 2026-09-24).
    """
    from types import SimpleNamespace

    class _Model:
        def __init__(self, model):
            self.provider, self.model, self.probes = "p", model, 0

        async def extract(self, *a, **k):
            self.probes += 1
            return []

    busy, idle = _Model("busy"), _Model("idle")
    router = SimpleNamespace(done_for_today=lambda e: False, note=lambda *a, **k: None,
                             has_capacity=lambda scope=None: True)
    chain = FallbackExtractor(primaries=[busy, idle])
    asyncio.run(chain.extract("t", "c", "top", "hu", "https://x.test"))  # busy serves
    assert busy.probes == 1

    chain.router = router  # preflight probes a fleet only when it is routed
    asyncio.run(chain.preflight())
    assert (busy.probes, idle.probes) == (1, 1)


def _sse_extractor(monkeypatch, body: str, status: int = 200):
    import httpx

    from scraper.providers import OpenAICompatExtractor

    seen = {}

    def handler(request):
        seen["payload"] = json.loads(request.content)
        return httpx.Response(status, text=body,
                              headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("scraper.extract._shared_client", lambda timeout: client)
    ex = OpenAICompatExtractor(provider="localgpu", base_url="https://gpu.test/v1",
                               api_key="k", model="qwen3-4b", quality=73,
                               stream=True, json_schema=True)
    return ex, seen


def test_a_streamed_answer_is_reassembled_into_one_response(monkeypatch):
    """Cloudflare 524s an origin silent for 100 s; our GPU took ~95 s on a
    dozen-community page. Streamed, the first token resets that clock.
    """
    chunks = [
        {"id": "c1", "model": "qwen3-4b", "choices": [{"delta": {"content": '{"communities": [{"name": '}}]},
        {"choices": [{"delta": {"content": '"Pécsi Futók", "confidence": 0.9, "joinable": true}]}'}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 4200, "completion_tokens": 30, "total_tokens": 4230}},
    ]
    body = "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    ex, seen = _sse_extractor(monkeypatch, body)
    async def _run():
        found = await ex.extract("szöveg", "Pécs", "running", "hu", "https://p.test")
        return found, ex.last_tokens  # a ContextVar: read inside the task

    records, tokens = asyncio.run(_run())
    assert [r.name for r in records] == ["Pécsi Futók"]
    assert tokens == 4230
    assert seen["payload"]["stream"] is True
    # And the schema went as a grammar-enforced response format.
    assert seen["payload"]["response_format"]["type"] == "json_schema"


def test_a_streamed_http_error_is_classified_like_any_other(monkeypatch):
    from scraper.extract import ExtractorUnavailableError

    ex, _ = _sse_extractor(monkeypatch, '{"error":{"message":"Context size has been exceeded."}}', 500)
    with pytest.raises(ExtractorUnavailableError, match="HTTP 500"):
        asyncio.run(ex.extract("szöveg", "Pécs", "running", "hu", "https://p.test"))


def test_ai_only_never_extracts_a_cached_page_from_a_blocked_domain(tmp_path):
    """Our own listing pages were cached from search results and re-extracted,
    importing our records back as new sources (1,018 pages, 2026-09-25).
    """
    from scraper.cache import CacheManager
    from scraper.db import save_search_cache
    from scraper.pipeline import _run_ai_only

    db, cfg, cities, topics = _pipeline_fixtures(tmp_path)
    cfg.fetch_blocked_domains = ["kozossegek.com"]
    cache = CacheManager(db)
    own = "https://kozossegek.com/budapest"
    cache.save_scraped(own, "Elég hosszú oldalszöveg a teszthez.", "Budapest", "running")
    save_search_cache(db, "Budapest", "running", [own], ["q"])
    primary = StubPrimary([[]])
    asyncio.run(_run_ai_only(cities, topics, cfg, FallbackExtractor(primaries=[primary]),
                             cache, True, {}, None, run_venues=False, run_persons=False))
    assert primary.calls == 0
