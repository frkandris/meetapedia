"""Short + long description enrichment: selection, write, durability (LLM mocked)."""
import asyncio
from pathlib import Path

from scraper.cache import CacheManager
from scraper.db import get_communities, get_enrichment_candidates, init_db
from scraper.enrich import enrich_batch, validate
from scraper.models import CommunityRecord
from scraper.store import save_results

HU = {"Budapest"}
LONG = "Bővített, hasznos, valódi tartalommal teli leírás a közösségről a városban. " * 8
SHORT = "Zenei kör Budapesten"


def _rec(description="Rövid.", **kw):
    kw.setdefault("name", "Zenei Kör")
    kw.setdefault("source_url", "https://klub.test/a")
    return CommunityRecord(
        topic="music", city="Budapest", locale="hu",
        extracted_at="2026-01-01T00:00:00+00:00",
        description=description, **kw)


def _setup(base: Path, description="Rövid.", raw_text="Forrásszöveg. " * 60):
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    db = base / "scraper.db"
    init_db(db)
    save_results("Budapest", "music", [_rec(description)], db)
    if raw_text is not None:
        CacheManager(db).save_scraped("https://klub.test/a", raw_text, "Budapest", "music")
    return db


class FakeExtractor:
    exhausted = False

    def __init__(self, short=SHORT, long=LONG):
        self.short, self.long, self.calls = short, long, 0

    async def write_descriptions(self, name, city, topic, locale, page_text):
        self.calls += 1
        return {"short_description": self.short, "long_description": self.long}


def test_selects_unenriched_with_source(tmp_path):
    db = _setup(tmp_path)
    cands = get_enrichment_candidates(db, HU, limit=10)
    assert len(cands) == 1 and cands[0]["name"] == "Zenei Kör"
    assert cands[0]["source_urls"] == ["https://klub.test/a"]
    # other city excluded
    assert get_enrichment_candidates(db, {"Debrecen"}, limit=10) == []


def test_batch_writes_short_and_long(tmp_path):
    db = _setup(tmp_path)
    ex = FakeExtractor()
    stats = asyncio.run(enrich_batch(db, ex, HU, limit=10, fetch_missing=False))
    assert stats["enriched"] == 1 and ex.calls == 1
    rec = get_communities(db, "Budapest", "music")[0]
    assert rec["short_description"] == SHORT
    assert rec["long_description"] == LONG.strip()
    assert rec["description"] == "Rövid."  # base extraction field untouched


def test_deadline_stops_before_any_paid_call(tmp_path):
    from datetime import datetime, timedelta, timezone
    db = _setup(tmp_path)
    ex = FakeExtractor()
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    stats = asyncio.run(enrich_batch(db, ex, HU, limit=10, fetch_missing=False, deadline=past))
    assert stats["stopped_at_deadline"] is True
    assert stats["enriched"] == 0 and ex.calls == 0  # no paid call past the cutoff
    assert not get_communities(db, "Budapest", "music")[0].get("long_description")


def test_future_deadline_does_not_block(tmp_path):
    from datetime import datetime, timedelta, timezone
    db = _setup(tmp_path)
    ex = FakeExtractor()
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    stats = asyncio.run(enrich_batch(db, ex, HU, limit=10, fetch_missing=False, deadline=future))
    assert stats["stopped_at_deadline"] is False
    assert stats["enriched"] == 1 and ex.calls == 1


def test_dry_run_does_not_write(tmp_path):
    db = _setup(tmp_path)
    stats = asyncio.run(enrich_batch(db, FakeExtractor(), HU, limit=10, dry_run=True, fetch_missing=False))
    assert stats["enriched"] == 1
    assert not get_communities(db, "Budapest", "music")[0].get("long_description")


def test_enriched_row_not_reselected(tmp_path):
    db = _setup(tmp_path)
    asyncio.run(enrich_batch(db, FakeExtractor(), HU, limit=10, fetch_missing=False))
    # long_description now set → no longer a candidate
    assert get_enrichment_candidates(db, HU, limit=10) == []
    ex2 = FakeExtractor()
    stats = asyncio.run(enrich_batch(db, ex2, HU, limit=10, fetch_missing=False))
    assert stats["enriched"] == 0 and ex2.calls == 0


def test_enrichment_durable_across_reextraction(tmp_path):
    """A later save_results (re-extraction) with a fresh record lacking the enriched
    fields must NOT drop them — _merge_source_urls carries them forward."""
    db = _setup(tmp_path)
    asyncio.run(enrich_batch(db, FakeExtractor(), HU, limit=10, fetch_missing=False))
    # simulate a re-extraction producing the base record again (no short/long)
    save_results("Budapest", "music", [_rec("Frissen kinyert rövid leírás.")], db)
    rec = get_communities(db, "Budapest", "music")[0]
    assert rec["long_description"] == LONG.strip()   # preserved
    assert rec["short_description"] == SHORT          # preserved
    assert rec["description"] == "Frissen kinyert rövid leírás."  # base updated


def test_skips_refusal_or_thin_long(tmp_path):
    db = _setup(tmp_path)
    ex = FakeExtractor(short="x", long="Sajnos nem tudok segíteni.")
    stats = asyncio.run(enrich_batch(db, ex, HU, limit=10, fetch_missing=False))
    assert stats["enriched"] == 0 and stats["skipped"] == 1
    assert not get_communities(db, "Budapest", "music")[0].get("long_description")


def test_fetch_fallback_when_no_raw_text(tmp_path, monkeypatch):
    db = _setup(tmp_path, raw_text=None)  # no cached raw_text
    assert get_enrichment_candidates(db, HU, limit=10)[0]["raw_text"] is None

    async def fake_fetch(url, blocked, **kw):
        return "Frissen letöltött forrásszöveg a klubról. " * 30

    monkeypatch.setattr("scraper.enrich.fetch_and_clean", fake_fetch)
    stats = asyncio.run(enrich_batch(db, FakeExtractor(), HU, limit=10, fetch_missing=True))
    assert stats["enriched"] == 1
    assert get_communities(db, "Budapest", "music")[0]["long_description"] == LONG.strip()


def test_enriched_only_page_is_indexable_and_in_sitemap(tmp_path):
    """A community with an empty original description but a long_description must be
    indexable (no noindex) and present in the sitemap (codex P1)."""
    from fastapi.testclient import TestClient
    from scraper.pipeline import CityConfig
    from scraper.web import app as web_app
    from scraper.web.state import app_state
    db = _setup(tmp_path, description="")            # empty base description
    asyncio.run(enrich_batch(db, FakeExtractor(), HU, limit=5, fetch_missing=False))
    old = (app_state.db_path, app_state.cities)
    app_state.db_path = db
    app_state.cities = [CityConfig(name="Budapest", country="Hungary", locale="hu", search_variants=[])]
    try:
        c = TestClient(web_app.app)
        page = c.get("/budapest/zenei-kor", headers={"host": "kozossegek.com"}).text
        assert 'name="robots" content="noindex"' not in page
        assert LONG.strip()[:40] in page
        assert "/budapest/zenei-kor" in c.get("/sitemap.xml", headers={"host": "kozossegek.com"}).text
    finally:
        app_state.db_path, app_state.cities = old


def test_failed_attempt_marked_and_not_reselected(tmp_path):
    """A skipped (junk/refusal) candidate is marked so it doesn't block later
    communities every batch, but is retryable once the window passes (codex P1)."""
    db = _setup(tmp_path)
    s1 = asyncio.run(enrich_batch(
        db, FakeExtractor(long="Sajnos nem tudok segíteni."), HU, limit=10, fetch_missing=False))
    assert s1["skipped"] == 1
    # within the retry window → not re-selected
    assert get_enrichment_candidates(db, HU, limit=10) == []
    # window elapsed → retryable again
    assert len(get_enrichment_candidates(db, HU, limit=10, retry_after_days=0)) == 1


def test_validate():
    assert validate(SHORT, LONG) == (SHORT, LONG.strip())
    assert validate("x", "too short") is None                    # long too short
    assert validate("x", "As an AI I cannot " + "w " * 80) is None  # refusal
    # missing short → derived from long's first sentence
    out = validate("", "Első mondat. " + "szó " * 80)
    assert out and out[0].startswith("Első mondat")


def test_validate_accepts_cjk_by_char_count():
    # Japanese: ~few spaces, so word count is tiny but char count is high — must pass.
    jp_long = "東京" * 130  # 260 chars, ~0 spaces
    out = validate("東京の走るクラブ", jp_long)
    assert out is not None and out[1] == jp_long


def test_new_null_fields_do_not_change_fingerprint():
    from scraper.db import _community_content_fingerprint as fp
    base = {"name": "X", "description": "leírás", "extracted_at": "2026-01-01"}
    with_nulls = {**base, "short_description": None, "long_description": None,
                  "enrich_attempted_at": "2026-07-27T00:00:00+00:00",
                  "extracted_at": "2026-07-27"}  # volatile fields differ too
    assert fp(base) == fp(with_nulls)  # adding null/volatile fields ≠ content change


def test_a_rate_limit_pauses_the_batch_but_says_so(tmp_path):
    """A 60-second limit must not end a 9.5-hour window.

    Production, 2026-08-18: every provider hit its per-minute limit at 01:17 and
    the enrichment window ended after 73 records, with 15,000 free calls and
    eight hours left.
    """
    base = Path(tmp_path)
    base.mkdir(parents=True, exist_ok=True)
    db = base / "scraper.db"
    init_db(db)
    save_results("Budapest", "music", [_rec()], db)
    CacheManager(db).save_scraped("https://klub.test/a", "Forrásszöveg. " * 60,
                                  "Budapest", "music")

    class _Limited:
        exhausted = providers_down = quota_exhausted = False
        rate_limited_out = True

        async def write_descriptions(self, *a, **kw):
            raise RuntimeError("write_descriptions unavailable: all providers rate limited")

    stats = asyncio.run(enrich_batch(db, _Limited(), HU, limit=20, fetch_missing=False))

    assert stats["stopped_rate_limited"] is True
    assert stats["stopped_no_provider"] is False   # not an ending — a wait


def test_batch_stops_when_the_fleet_is_down(tmp_path):
    """One failure is enough once the extractor reports it has nothing left.

    Production, 2026-08-17: the circuit breaker opened and the loop logged 368
    identical `enrich_call_failed` lines in seconds, each preceded by a source
    fetch. The records survive (nothing is marked), but the fetches do not.
    """
    base = Path(tmp_path)
    base.mkdir(parents=True, exist_ok=True)
    db = base / "scraper.db"
    init_db(db)
    for i in range(5):
        save_results("Budapest", "music", [_rec(name=f"Klub {i}",
                                                source_url=f"https://e{i}.test")], db)
        CacheManager(db).save_scraped(f"https://e{i}.test", "Forrásszöveg. " * 60,
                                      "Budapest", "music")

    class DeadExtractor:
        exhausted = False
        providers_down = True
        calls = 0

        async def write_descriptions(self, *a, **kw):
            DeadExtractor.calls += 1
            raise RuntimeError(
                "write_descriptions unavailable: no extraction provider configured")

    stats = asyncio.run(enrich_batch(db, DeadExtractor(), HU, limit=20,
                                     fetch_missing=False))

    assert DeadExtractor.calls == 1
    assert stats["failed"] == 1
    assert stats["stopped_no_provider"] is True


# ── the enrichment call counter ──────────────────────────────────────────────
#
# The report subtracts this from the fleet's successful calls to get extraction
# capacity. Every way of getting it wrong shows up as a wrong per-page cost.

def _setup_many(base: Path, count: int) -> Path:
    """A database with `count` un-enriched communities, each with source text."""
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    db = base / "scraper.db"
    init_db(db)
    cache = CacheManager(db)
    # Names have to be genuinely unlike each other: `save_results` dedups
    # fuzzily, and "Zenei Kör 0/1/2" collapses into a single record.
    names = ["Zenei Kör", "Futóklub", "Sakk Egylet", "Kertbarátok",
             "Fotósműhely", "Néptánc Csoport"]
    for i in range(count):
        url = f"https://klub.test/{i}"
        save_results("Budapest", "music",
                     [_rec(name=names[i % len(names)], source_url=url)], db)
        cache.save_scraped(url, "Forrásszöveg. " * 60, "Budapest", "music")
    return db


def _counter(db) -> int:
    from datetime import datetime, timezone

    from scraper.db import get_daily_counter
    return get_daily_counter(db, datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                             "enrich_attempts")


def test_a_refused_description_still_counts_as_a_call(tmp_path):
    """The provider was paid for it; the report must not blame extraction.

    `validate` rejects thin or missing descriptions, and a malformed answer
    arrives as `{}`. Counting accepted records instead of calls left those
    attributed to page extraction — pages they never touched.
    """
    db = _setup(tmp_path)

    class _Refuser:
        exhausted = False

        async def write_descriptions(self, *a, **kw):
            return {"short_description": "", "long_description": ""}

    stats = asyncio.run(enrich_batch(db, _Refuser(), HU, limit=5, fetch_missing=False))
    assert stats["enriched"] == 0
    assert stats["skipped"] == 1
    assert _counter(db) == 1


def test_a_raised_call_still_counts_as_an_attempt(tmp_path):
    """A refused call spends a slot in the daily allowance like any other.

    The report works in attempts, because that is the unit the allowance is
    denominated in. Counting only successes here and dividing by an attempt
    budget overstates capacity by the refusal rate — 47% on 2026-08-23.
    """
    db = _setup(tmp_path)

    class _Broken:
        rate_limited_out = False
        providers_down = False
        quota_exhausted = False
        exhausted = False

        async def write_descriptions(self, *a, **kw):
            raise RuntimeError("upstream 500")

    stats = asyncio.run(enrich_batch(db, _Broken(), HU, limit=5, fetch_missing=False))
    assert stats["failed"] == 1
    assert _counter(db) == 1


def test_completed_calls_survive_a_batch_that_never_finishes(tmp_path):
    """Cancellation is routine — the admin stop route exists for it.

    Writing the total after the loop lost every call the batch had already
    completed, while the records stayed enriched and the ledger stayed charged.
    """
    import pytest

    db = _setup_many(tmp_path, 3)

    class _DiesOnThird:
        exhausted = False

        def __init__(self):
            self.calls = 0

        async def write_descriptions(self, *a, **kw):
            self.calls += 1
            if self.calls == 3:
                raise asyncio.CancelledError()
            return {"short_description": SHORT, "long_description": LONG}

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(enrich_batch(db, _DiesOnThird(), HU, limit=5, fetch_missing=False))
    # Two completed plus the cancelled one — all three were issued.
    assert _counter(db) == 3


def test_a_call_that_walks_the_fallback_chain_counts_every_attempt(tmp_path):
    """The ledger counts provider attempts; so must this, or it under-subtracts.

    One logical description can try several providers. Counting one per
    description leaves the failed attempts attributed to extraction — exactly
    as often as the fleet fails over, which on 2026-08-23 was 858 attempts in
    1,794.
    """
    db = _setup(tmp_path)

    class _FailsOverTwice:
        exhausted = False

        def __init__(self):
            self.calls_made = 0

        async def write_descriptions(self, *a, **kw):
            self.calls_made += 3      # two refusals, then a provider that answers
            return {"short_description": SHORT, "long_description": LONG}

    stats = asyncio.run(enrich_batch(db, _FailsOverTwice(), HU, limit=5,
                                     fetch_missing=False))
    assert stats["enriched"] == 1
    assert _counter(db) == 3


# ── attempt accounting ───────────────────────────────────────────────────────

def test_a_description_that_never_reached_a_provider_counts_zero(tmp_path):
    """A phantom attempt inflates the budget picture exactly when it hurts.

    `_count_attempts` used to floor at `max(1, n)`, so a description that raised
    before any provider call — fleet paced out, breaker open, quota spent, all
    of which `write_descriptions` raises on without calling anyone — still
    booked one attempt. Those cluster on the days a refusing fleet produces them
    in bulk, so the phantom count tracked the refusal rate: the daily report's
    workload counters exceeded the quota ledger by 252 attempts on 2026-09-10
    and 464 on 2026-09-11, having seen calls the ledger never did.
    """
    from scraper.db import init_db
    from scraper.enrich import _count_attempts

    db = tmp_path / "s.db"
    init_db(db)

    _count_attempts(db, 0)
    assert _counter(db) == 0

    _count_attempts(db, 3)          # one description, three providers walked
    assert _counter(db) == 3

    _count_attempts(db, -1)         # never happens, but must not decrement
    assert _counter(db) == 3
