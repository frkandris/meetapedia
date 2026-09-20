"""The worker must be able to tell "nothing to do" from "did something".

On 2026-08-22 it started around 200 runs in a day and the daily report's own
columns show what they produced: 0 pages downloaded, 0 pairs searched, 78 pages
extracted against the previous day's 387. `ai_only` and `search_only`
alternated every three or four minutes for twenty hours.

The collector was measuring itself with `pages_worked`, which subtracts
extraction cache hits and extraction failures. A `search_only` run extracts
nothing, so both subtrahends are zero and the measure degrades to "URLs the
search returned" — non-zero on a pass that downloaded nothing because every URL
was already cached. Every such pass cleared the extraction cooldown.
"""
from scraper.pipeline import (WORKER_COLLECT, WORKER_EXTRACT, WORKER_WAIT,
                              next_worker_action, pages_fetched, pages_worked,
                              worker_after_run)


def test_a_collector_pass_that_downloaded_nothing_reports_nothing():
    """The exact shape of the stuck run: every URL served from the cache."""
    log = [{"urls_found": 10, "fetched_urls": [f"u{i}" for i in range(10)],
            "cache_hits_scrape": 10, "cache_hits_extract": 0, "extract_failed": 0}]
    assert pages_fetched(log) == 0
    # …while the measure it used to consult still calls this a busy pass.
    assert pages_worked(log) == 10


def test_a_collector_pass_counts_only_new_downloads():
    log = [{"urls_found": 10, "fetched_urls": [f"u{i}" for i in range(10)],
            "cache_hits_scrape": 7, "cache_hits_extract": 0, "extract_failed": 0}]
    assert pages_fetched(log) == 3


def test_a_fetch_failure_is_not_a_download():
    """A page that would not load caches nothing, so the next pass repeats it."""
    log = [{"urls_found": 5, "fetched_urls": ["a", "b"],
            "cache_hits_scrape": 2, "fetch_failed": 3}]
    assert pages_fetched(log) == 0


def test_pages_fetched_sums_across_pairs():
    log = [
        {"fetched_urls": ["a", "b", "c"], "cache_hits_scrape": 1},
        {"fetched_urls": ["d"], "cache_hits_scrape": 0},
        {"fetched_urls": [], "cache_hits_scrape": 0},
    ]
    assert pages_fetched(log) == 3


def test_pages_fetched_survives_a_missing_key():
    """Pair logs from an aborted run are partial; a KeyError here stops a run."""
    assert pages_fetched([{}]) == 0
    assert pages_fetched([{"cache_hits_scrape": 4}]) == 0


def test_extraction_still_goes_first_when_there_is_budget():
    """Free quota expires at midnight; collection costs money. Unchanged."""
    assert next_worker_action(is_running=False, paused=False,
                              quota=True, extract_ready=True) == WORKER_EXTRACT
    assert next_worker_action(is_running=False, paused=False,
                              quota=False, extract_ready=True) == WORKER_COLLECT
    assert next_worker_action(is_running=True, paused=False,
                              quota=True, extract_ready=True) == WORKER_WAIT


# ── the decision itself, not just the measure ────────────────────────────────
#
# `pages_fetched` being right is not the same as the worker consulting it.
# These call the function the loop actually uses, so reverting the collector to
# `worked` or dropping the long-idle threshold fails here.

_LIMITS = dict(empty_limit=3, idle_s=60.0, retry_s=900.0)


def _after(mode, **kw):
    base = dict(worked=0, fetched=0, new_records=0, cancelled=False,
                empty_extractions=0, empty_collections=0, **_LIMITS)
    return worker_after_run(mode=mode, **{**base, **kw})


def test_a_collector_pass_that_found_urls_but_downloaded_none_does_not_release_extraction():
    """The exact 2026-08-22 regression, at the decision that caused it."""
    after = _after("search_only", worked=10, fetched=0)
    assert after.extract_cooldown is None       # cooldown left alone
    assert after.empty_collections == 1
    assert after.sleep == 60.0


def test_a_collector_pass_that_downloaded_releases_extraction():
    after = _after("search_only", worked=0, fetched=3, empty_collections=2)
    assert after.extract_cooldown == 0.0
    assert after.empty_collections == 0
    assert after.sleep == 0.0


def test_three_empty_collections_stop_the_polling():
    """Nothing changes a caught-up system but midnight or an operator."""
    assert _after("search_only", empty_collections=1).sleep == 60.0
    assert _after("search_only", empty_collections=2).sleep == 900.0


def test_an_empty_extraction_parks_extraction():
    after = _after("ai_only", worked=0, new_records=0)
    assert after.extract_cooldown == 900.0
    assert after.empty_extractions == 1


def test_a_productive_extraction_resets_the_counter_and_keeps_going():
    after = _after("ai_only", worked=5, new_records=2, empty_extractions=2)
    assert after.empty_extractions == 0
    assert after.extract_cooldown is None
    assert after.sleep == 0.0


def test_a_cancelled_pass_parks_nothing_and_counts_for_nothing():
    """Someone stopped it, or the quota ran out; neither says work is absent.

    The old loop declined to park on a cancelled pass but still counted it
    toward the empty streak, so three interruptions parked extraction for a
    quarter of an hour on no evidence. Writing the rule down as a function is
    what made the contradiction visible.
    """
    after = _after("ai_only", worked=0, cancelled=True, empty_extractions=2)
    assert after.extract_cooldown is None
    assert after.empty_extractions == 2       # unchanged, not 3
    assert after.sleep == 0.0
    assert _after("search_only", fetched=0, cancelled=True).sleep == 0.0
    assert _after("search_only", fetched=0, cancelled=True).empty_collections == 0


def test_three_empty_extractions_park_it_even_if_pages_were_worked():
    """`worked` has been wrong twice; records produced is the backstop."""
    after = _after("ai_only", worked=40, new_records=0, empty_extractions=2)
    assert after.empty_extractions == 3
    assert after.extract_cooldown == 900.0


def test_the_worker_loop_uses_this_function():
    """Guard against the decision drifting back into the closure."""
    from pathlib import Path

    src = Path("scraper/main.py").read_text(encoding="utf-8")
    assert "worker_after_run(" in src
    assert "empty_collections += 1" not in src


def test_enrichment_has_no_competing_boot_task_before_daily_guides():
    """Only the worker may launch enrichment, after its guide publication step."""
    from pathlib import Path

    src = Path("scraper/main.py").read_text(encoding="utf-8")
    assert src.count("asyncio.create_task(_enrich_run())") == 1
    assert src.index("publish_daily_guides(") < src.index(
        "asyncio.create_task(_enrich_run())")


def test_the_outcome_maps_each_measure_to_its_own_key():
    """The mapping is what has been wrong every time.

    `worked` is an extraction measure and `fetched` a collection one; reading
    either through the other produced ~200 empty runs on 2026-08-22 and ~100 on
    08-18. Passing `fetched=worked` in the worker used to leave every test here
    green, because nothing checked the mapping itself.
    """
    from scraper.pipeline import worker_outcome

    logs = [{
        "urls_found": 10,
        "fetched_urls": [f"u{i}" for i in range(10)],
        "cache_hits_scrape": 10,     # everything served from cache
        "cache_hits_extract": 0,
        "extract_failed": 0,
    }]
    out = worker_outcome(logs, total_new=0)
    assert out["worked"] == 10       # the extraction measure says "busy"…
    assert out["fetched"] == 0       # …and the collection measure says "idle"
    assert out["pairs"] == 1
    assert out["new"] == 0


def test_the_worker_builds_its_outcome_with_that_function():
    """Guard the wiring: the callback must not compute the measures itself."""
    from pathlib import Path

    src = Path("scraper/main.py").read_text(encoding="utf-8")
    assert "worker_outcome(pair_logs, total_new)" in src
    assert 'outcome["fetched"] =' not in src
    assert 'outcome["worked"] =' not in src
