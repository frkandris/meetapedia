"""Regressions for the 2026-07-24 deferred-findings batch (codex full review)."""
import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from scraper.db import get_daily_summary, get_duplicate_candidates, init_db
from scraper.extract import ExtractorUnavailableError, _parse_communities
from scraper.models import CommunityRecord
from scraper.store import save_results


def _db(tmp_path: Path) -> Path:
    p = tmp_path / "scraper.db"
    init_db(p)
    return p


def _rec(name, topic, city="Budapest", **kw):
    return CommunityRecord(
        name=name, topic=topic, city=city, locale="hu",
        source_url="https://a.test", extracted_at="2026-01-01T00:00:00+00:00", **kw,
    )


def test_daily_summary_multi_topic_community_counted_once(tmp_path):
    """A community indexed under two topics shares community_id — the history
    join must not double the new/changed counts (inflated the daily email)."""
    db = _db(tmp_path)
    save_results("Budapest", "running", [_rec("Kör", "running")], db)
    save_results("Budapest", "chess", [_rec("Kör", "chess")], db)

    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    s = get_daily_summary(
        db, (now - timedelta(hours=1)).isoformat(), (now + timedelta(hours=1)).isoformat(),
        hu_cities={"Budapest"})
    assert s["hu"]["new_communities"] == 1


def test_recent_communities_multi_topic_deduped(tmp_path):
    from scraper.db import get_recently_added_communities
    db = _db(tmp_path)
    save_results("Budapest", "running", [_rec("Kör", "running")], db)
    save_results("Budapest", "chess", [_rec("Kör", "chess")], db)
    recents = get_recently_added_communities(db)
    assert len(recents) == 1


def test_duplicate_winner_is_richer_not_lexicographic(tmp_path):
    """winner_key drives the merge — it must be the richer record even when
    its key sorts after the poorer one, and re-scans must stay idempotent."""
    from scraper.duplicates import detect_all
    db = _db(tmp_path)
    # "AAA Klub" sorts first but is poor; "ZZZ AAA Klub"... need fuzzy match:
    # same name, two topics, one much richer.
    save_results("Budapest", "running", [_rec("Futó Kör", "running")], db)
    save_results("Budapest", "fitness", [_rec(
        "Futó Kör", "fitness", description="Gazdag leírás", website="https://futo.hu",
        contact="x@y.hu", meeting_schedule="kedd", fee="ingyenes")], db)

    detect_all(db)
    cands = [c for c in get_duplicate_candidates(db) if c["entity_type"] == "community"]
    assert len(cands) == 1
    from scraper.db import _community_record_key
    rich_key = _community_record_key("Futó Kör", "Budapest", "fitness")
    assert cands[0]["winner_key"] == rich_key, \
        "winner must be the richer (fitness) record regardless of key order"

    detect_all(db)  # idempotent even though key order is no longer canonical
    assert len([c for c in get_duplicate_candidates(db) if c["entity_type"] == "community"]) == 1


def _seed_rich_poor_pair(db):
    from scraper.db import _community_record_key
    save_results("Budapest", "running", [_rec("Futó Kör", "running")], db)
    save_results("Budapest", "fitness", [_rec(
        "Futó Kör", "fitness", description="Gazdag leírás", website="https://futo.hu",
        contact="x@y.hu", meeting_schedule="kedd", fee="ingyenes")], db)
    poor_key = _community_record_key("Futó Kör", "Budapest", "running")
    rich_key = _community_record_key("Futó Kör", "Budapest", "fitness")
    return poor_key, rich_key


def test_stale_pending_candidate_orientation_corrected(tmp_path):
    """Auto rows created before the richer-wins change (poor-first orientation)
    get their winner flipped in place on re-scan."""
    import sqlite3
    from scraper.duplicates import detect_all
    db = _db(tmp_path)
    poor_key, rich_key = _seed_rich_poor_pair(db)  # save_results auto-detects
    # Force the legacy orientation directly, as if created pre-change.
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE duplicate_candidates SET winner_key=?, loser_key=?",
            (poor_key, rich_key))
        conn.commit()

    detect_all(db)
    cands = [c for c in get_duplicate_candidates(db) if c["entity_type"] == "community"]
    assert len(cands) == 1
    assert cands[0]["winner_key"] == rich_key


def test_manual_flag_overrides_auto_row_and_sticks(tmp_path):
    """An admin's manual flag reorients an existing auto candidate AND stamps it
    manual so a later auto re-scan cannot flip it back."""
    from scraper.db import insert_duplicate_candidate
    from scraper.duplicates import detect_all
    db = _db(tmp_path)
    poor_key, rich_key = _seed_rich_poor_pair(db)  # auto row: winner=rich
    # the admin explicitly wants to keep the poorer record
    insert_duplicate_candidate(db, "community", "", "", poor_key, rich_key, 1.0, "manual")
    cands = get_duplicate_candidates(db)
    assert len(cands) == 1
    assert cands[0]["winner_key"] == poor_key and cands[0]["signal"] == "manual"

    detect_all(db)  # richness says otherwise, but the manual choice must hold
    cands = get_duplicate_candidates(db)
    assert len(cands) == 1
    assert cands[0]["winner_key"] == poor_key


def test_leader_cleanup_spares_ai_extracted_persons(tmp_path):
    from scraper.db import (delete_leader_persons_for_community,
                            get_persons_for_community, upsert_persons)
    from scraper.models import PersonRecord
    db = _db(tmp_path)
    ai_person = PersonRecord(
        name="Kiss Anna", role="leader", city="Budapest", topic="running",
        community_name="Futó Kör", source_url="https://a.test",
        extracted_at="2026-01-01T00:00:00+00:00")
    synth = PersonRecord(
        name="Nagy Béla", role="leader", city="Budapest", topic="running",
        community_name="Futó Kör", source_url="https://a.test",
        extracted_at="2026-01-01T00:00:00+00:00")
    upsert_persons(db, [ai_person.model_dump()])
    upsert_persons(db, [{**synth.model_dump(), "origin": "leader_field"}])

    # stale-cleanup mode: only the synthesized row goes
    delete_leader_persons_for_community(db, "Futó Kör", "Budapest", only_synthesized=True)
    names = [p["name"] for p in get_persons_for_community(db, "Futó Kör", "Budapest")]
    assert names == ["Kiss Anna"], "AI-extracted leader must survive stale cleanup"


def test_malformed_llm_json_raises_instead_of_empty(tmp_path):
    with pytest.raises(ExtractorUnavailableError):
        _parse_communities("Sorry, I cannot help with that.", "Budapest", "running",
                           "hu", "https://a.test")
    with pytest.raises(ExtractorUnavailableError):
        _parse_communities('{"communities": "not-a-list"}', "Budapest", "running",
                           "hu", "https://a.test")
    # a well-formed empty result is still a legitimate empty extraction
    assert _parse_communities('{"communities": []}', "Budapest", "running",
                              "hu", "https://a.test") == []


def test_bare_array_response_raises_instead_of_attribute_error():
    """2026-07-30 ai_only crash: the model answered with a top-level JSON array,
    so `.get()` hit a list. Must be a typed, retryable extractor error."""
    from scraper.extract import _parse_persons, _parse_venues
    for raw in ('[{"name": "Futó Kör"}]', '"just a string"', "42", "null"):
        with pytest.raises(ExtractorUnavailableError):
            _parse_communities(raw, "Budapest", "running", "hu", "https://a.test")
        with pytest.raises(ExtractorUnavailableError):
            _parse_venues(raw, "Budapest", "hu", "https://a.test")
        with pytest.raises(ExtractorUnavailableError):
            _parse_persons(raw, "Budapest", "running", "hu", "https://a.test")


def test_renamed_wrapper_key_is_not_a_silent_empty_extraction():
    """{"data": [...]} used to read as 0 communities and get cached as such —
    permanent loss under the current fingerprint. An empty {} stays legitimate."""
    with pytest.raises(ExtractorUnavailableError):
        _parse_communities('{"data": [{"name": "Futó Kör"}]}', "Budapest", "running",
                           "hu", "https://a.test")
    assert _parse_communities("{}", "Budapest", "running", "hu", "https://a.test") == []


def test_enrich_survives_a_bare_array_payload():
    from scraper.extract import _apply_enrich
    rec = _rec("Futó Kör", "running")
    assert _apply_enrich(rec, [{"website": "https://x.test"}]) is rec


def test_scrape_submitted_url_filters_non_joinable(tmp_path):
    from scraper.db import get_communities
    from scraper.pipeline import PipelineConfig, scrape_submitted_url
    db = _db(tmp_path)
    cfg = PipelineConfig(
        search_results_per_query=5, search_max_pages=2, search_rate_limit=1.0,
        fetch_timeout=15, fetch_min_text_length=10, fetch_max_concurrent=3,
        fetch_blocked_domains=[], db_path=db,
    )

    class StubExtractor:
        def __init__(self, primaries): ...
        async def extract(self, **kwargs):
            return [_rec("Nyitott Kör", "running", joinable=True),
                    _rec("Zárt Профи Klub", "running", joinable=False)]

    async def fake_fetch(url, *a, **k):
        return "Elég hosszú oldalszöveg a teszthez."

    with patch("scraper.pipeline.FallbackExtractor", StubExtractor), \
         patch("scraper.pipeline.fetch_and_clean", fake_fetch):
        ok = asyncio.run(scrape_submitted_url(db, cfg, "Budapest", "running",
                                              "https://klub.test/x"))
    assert ok
    names = [c["name"] for c in get_communities(db, "Budapest", "running")]
    assert "Nyitott Kör" in names and len(names) == 1, \
        "non-joinable records must be filtered like in the main pipeline"


def test_merge_entity_into_venue_fills_and_deletes(tmp_path):
    from scraper.db import get_entity_by_record_key, merge_entity_into, upsert_venues
    from scraper.identity import venue_record_key
    db = _db(tmp_path)
    upsert_venues(db, [
        {"name": "Művház", "city": "Budapest", "venue_id": "v1",
         "source_url": "https://a.test", "website": None},
        {"name": "Muvhaz", "city": "Budapest", "venue_id": "v2",
         "source_url": "https://b.test", "website": "https://muvhaz.hu"},
    ])
    wk = venue_record_key("Művház", "Budapest")
    lk = venue_record_key("Muvhaz", "Budapest")
    assert merge_entity_into(db, "venue", wk, lk)
    merged = get_entity_by_record_key(db, "venue", wk)
    assert merged["website"] == "https://muvhaz.hu", "empty winner field filled from loser"
    assert set(merged["source_urls"]) == {"https://a.test", "https://b.test"}
    assert get_entity_by_record_key(db, "venue", lk) is None, "loser row deleted"


def test_apply_venue_edit_closed_and_rename(tmp_path):
    from scraper.db import apply_venue_edit, get_entity_by_record_key, upsert_venues
    from scraper.identity import venue_record_key
    db = _db(tmp_path)
    upsert_venues(db, [{"name": "Régi Név", "city": "Budapest", "venue_id": "v1",
                        "source_url": "https://a.test"}])
    rk = venue_record_key("Régi Név", "Budapest")
    assert apply_venue_edit(db, rk, "name_correction", "Új Név")
    assert get_entity_by_record_key(db, "venue", rk) is None
    nk = venue_record_key("Új Név", "Budapest")
    assert get_entity_by_record_key(db, "venue", nk)["name"] == "Új Név"

    assert apply_venue_edit(db, nk, "closed", None)
    assert get_entity_by_record_key(db, "venue", nk) is None


def test_atomic_cache_update_preserves_concurrent_fields(tmp_path):
    """Two writers updating different field families of the same URL must not
    erase each other's fields (the old two-connection read-modify-write did)."""
    from scraper.cache import CacheManager
    db = _db(tmp_path)
    cache = CacheManager(db)
    url = "https://klub.test/x"
    cache.save_scraped(url, "Hosszú oldalszöveg a teszthez.", "Budapest", "running")
    cache.save_extracted(url, [_rec("Kör", "running")], fingerprint="fp1")
    cache.save_venue_extracted(url, [{"name": "Terem"}], fingerprint="vfp")
    cache.save_person_extracted(url, "Budapest", "running",
                                [{"name": "Kiss Anna"}], fingerprint="pfp")
    entry = cache.get_entry(__import__("scraper.cache", fromlist=["_url_hash"])._url_hash(url))
    assert entry["raw_text"] and entry["records"] and entry["venues_data"]
    assert entry["persons_data"]["Budapest/running"][0]["name"] == "Kiss Anna"
    # dropping the scrape must keep the extraction fields
    assert cache.delete_scraped(entry["url_hash"])
    entry2 = cache.get_entry(entry["url_hash"])
    assert "raw_text" not in entry2 and entry2["records"]


def test_merge_entity_into_unions_list_fields(tmp_path):
    from scraper.db import get_entity_by_record_key, merge_entity_into, upsert_venues
    from scraper.identity import venue_record_key
    db = _db(tmp_path)
    upsert_venues(db, [
        {"name": "Művház", "city": "Budapest", "venue_id": "v1",
         "source_url": "https://a.test", "community_ids": ["c1"],
         "welcomed_topics": ["running"]},
        {"name": "Muvhaz", "city": "Budapest", "venue_id": "v2",
         "source_url": "https://b.test", "community_ids": ["c2", "c1"],
         "welcomed_topics": ["chess"]},
    ])
    wk = venue_record_key("Művház", "Budapest")
    lk = venue_record_key("Muvhaz", "Budapest")
    assert merge_entity_into(db, "venue", wk, lk)
    merged = get_entity_by_record_key(db, "venue", wk)
    assert merged["community_ids"] == ["c1", "c2"], "loser's associations must be unioned"
    assert set(merged["welcomed_topics"]) == {"running", "chess"}


def test_manual_flag_stamps_even_when_orientation_matches(tmp_path):
    """If the admin's choice matches the auto orientation, the row must still be
    stamped manual so a later richness flip can't reorient it."""
    from scraper.db import insert_duplicate_candidate
    db = _db(tmp_path)
    poor_key, rich_key = _seed_rich_poor_pair(db)  # auto row: winner=rich
    insert_duplicate_candidate(db, "community", "", "", rich_key, poor_key, 1.0, "manual")
    c = get_duplicate_candidates(db)[0]
    assert c["winner_key"] == rich_key and c["signal"] == "manual"


def test_venue_edit_approve_falls_back_to_computed_key(tmp_path):
    """The public venue form submits record_key='' — approval must resolve the
    venue from entity_name/entity_city."""
    from scraper.db import apply_venue_edit, get_entity_by_record_key, upsert_venues
    from scraper.identity import venue_record_key
    db = _db(tmp_path)
    upsert_venues(db, [{"name": "Bezárt Ház", "city": "Budapest", "venue_id": "v1",
                        "source_url": "https://a.test"}])
    # simulate the route's fallback: empty record_key → computed from name/city
    vkey = "" or venue_record_key("Bezárt Ház", "Budapest")
    assert apply_venue_edit(db, vkey, "closed", None)
    assert get_entity_by_record_key(db, "venue", vkey) is None


def test_stale_cleanup_spares_manual_candidates(tmp_path):
    """Automatic criteria drift must not dismiss an admin-asserted pair."""
    from scraper.db import _community_record_key, insert_duplicate_candidate
    from scraper.duplicates import cleanup_stale_community_candidates
    db = _db(tmp_path)
    # two dissimilar names that would never pass the 0.85 fuzzy threshold
    save_results("Budapest", "running", [_rec("Futó Kör", "running")], db)
    save_results("Budapest", "chess", [_rec("Sakkbarátok Egyesülete", "chess")], db)
    k1 = _community_record_key("Futó Kör", "Budapest", "running")
    k2 = _community_record_key("Sakkbarátok Egyesülete", "Budapest", "chess")
    insert_duplicate_candidate(db, "community", "", "", k1, k2, 1.0, "manual")

    cleanup_stale_community_candidates(db)
    cands = [c for c in get_duplicate_candidates(db) if c["signal"] == "manual"]
    assert len(cands) == 1 and cands[0]["resolution"] is None


def test_init_db_runs_once_per_path(tmp_path):
    """init_db takes a write lock; a dozen routes call it per request.

    With the pipeline writing concurrently that produced
    "sqlite3.OperationalError: database is locked" and multi-second page loads
    on 2026-08-16. The schema cannot change within a process, so the body runs
    once and later calls are free.
    """
    import time

    from scraper.db import init_db

    db = tmp_path / "s.db"
    t0 = time.monotonic()
    init_db(db)
    first = time.monotonic() - t0

    t0 = time.monotonic()
    for _ in range(50):
        init_db(db)
    repeats = time.monotonic() - t0

    # 50 guarded calls must cost far less than the one real one.
    assert repeats < first, f"50 repeats took {repeats:.4f}s vs first {first:.4f}s"
    # force= still re-runs it, which the migration test relies on.
    init_db(db, force=True)


def test_connection_uses_wal(tmp_path):
    """Readers must not block behind the pipeline's writes."""
    import sqlite3

    from scraper.db import init_db

    db = tmp_path / "s.db"
    init_db(db)
    with sqlite3.connect(db) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_healthz_stays_green_when_the_database_is_slow(tmp_path, monkeypatch):
    """A locked database must not fail the liveness probe.

    /healthz used to COUNT(*) over communities. The Docker healthcheck calls it
    with a 10s timeout, so a pipeline write lock failed the check, marked the
    container unhealthy, and Traefik removed it from rotation — every public
    request 404'd until the lock cleared (2026-08-16, four episodes).
    """
    from fastapi.testclient import TestClient

    from scraper.db import init_db
    from scraper.web import app as web_app
    from scraper.web.state import app_state

    db = tmp_path / "s.db"
    init_db(db)
    old = app_state.db_path
    app_state.db_path = db
    web_app._HEALTH_COUNT_CACHE = (0, 0.0)  # force a fresh lookup
    try:
        # Just past the endpoint's own 3s ceiling, not ten times it. The stub
        # blocks a real worker thread, and the test does not finish until that
        # thread does — so the 30 here was 26 seconds of the suite's runtime
        # buying nothing. What the test asserts is that /healthz answers while
        # the database is still busy, and 4 > 3 establishes that as well as 30.
        def _hangs(*a, **k):
            import time
            time.sleep(web_app._HEALTH_COUNT_TIMEOUT_S + 1.0)

        monkeypatch.setattr(web_app, "get_total_community_count", _hangs)
        resp = TestClient(web_app.app).get("/healthz")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True          # the app is up; that is the question
        assert body["db"] == "busy"        # …and the slowness is still reported
    finally:
        app_state.db_path = old
        web_app._HEALTH_COUNT_CACHE = (0, 0.0)
