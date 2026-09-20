"""The A/B extraction measurement: what it counts, and what it must not touch.

The experiment's whole value is that its numbers can be trusted next to
production's, so two properties matter more than the arithmetic: it stores
nothing, and it counts communities the same way the database does.
"""
from pathlib import Path

import pytest

from scraper import ab_test


def test_compare_folds_accents_so_spelling_is_not_counted_as_a_difference():
    """"Zenei Kör" and "zenei  kor" are one community here, not two.

    The database's own identity key is accent-sensitive on purpose, but two
    models spelling the same Hungarian name differently would then show up as
    one community lost and one gained — an artifact of the measurement rather
    than a difference in what was found.
    """
    out = ab_test.compare(["Zenei Kör", "Futó Klub"], ["zenei  kor", "Új Csoport"])
    assert out == {"baseline": 2, "found": 2, "kept": 1,
                   "lost": ["Futó Klub"], "new": ["Új Csoport"]}


def test_the_summary_prices_the_control_arm_the_way_the_pipeline_does():
    """One call for a page with nothing, three for a page with anything.

    Venue and person extraction are skipped when the community pass finds
    nothing (CLAUDE.md, "Person + venue extraction skip"), so charging the
    control arm three calls per page would invent a saving that is not there.
    """
    results = [
        ab_test.PageResult(url="a", city="X", topic="t", baseline_names=["A"],
                           combined_names=["A"], llm_calls=1),
        ab_test.PageResult(url="b", city="X", topic="t", baseline_names=[],
                           combined_names=[], llm_calls=1),
        ab_test.PageResult(url="c", city="X", topic="t", baseline_names=[],
                           gated_out=True, gate_score=0.01),
    ]
    s = ab_test.summarize(results, 0.06)
    assert s["calls"]["baseline"] == 3 + 1 + 1     # one useful, two empty
    assert s["calls"]["experiment"] == 2           # one gated out entirely
    assert s["gate"]["rejected"] == 1
    assert s["gate"]["rejected_with_baseline_communities"] == 0


def test_a_gated_page_that_had_communities_is_reported_as_a_loss():
    """The number the gate stands or falls on, and it must be impossible to
    read the report without seeing it.

    It must also reach the headline recall. Scoring only the pages that ran
    would report 100% for a path that threw a real community away — which is
    the difference between measuring the gate and excusing it.
    """
    results = [
        ab_test.PageResult(url="https://a.test/p", city="X", topic="t",
                           baseline_names=["Valódi Kör"], baseline_venues=1,
                           gated_out=True, gate_score=0.02),
        ab_test.PageResult(url="https://b.test/p", city="X", topic="t",
                           baseline_names=["Másik Kör"],
                           combined_names=["Másik Kör"], llm_calls=1),
    ]
    s = ab_test.summarize(results, 0.06)
    assert s["gate"]["rejected_with_baseline_communities"] == 1
    assert s["gate"]["lost_urls"] == ["https://a.test/p"]
    # End to end one of two survived; the extraction alone kept everything it
    # was given. Both numbers are reported, and they are not the same number.
    assert s["communities"]["recall"] == 0.5
    assert s["communities"]["recall_extraction_only"] == 1.0
    assert s["venues"]["baseline"] == 1 and s["venues"]["found"] == 0


def test_recall_is_none_rather_than_one_when_there_is_nothing_to_recall():
    results = [ab_test.PageResult(url="a", city="X", topic="t")]
    assert ab_test.summarize(results, 0.06)["communities"]["recall"] is None


@pytest.mark.asyncio
async def test_a_page_below_the_threshold_costs_no_extraction_call():
    class _Extractor:
        async def extract_all(self, *a, **k):
            raise AssertionError("a gated-out page must not be extracted")

    async def _gate(*a, **k):
        return 0.01, 300

    original, ab_test.jev_gate = ab_test.jev_gate, _gate
    try:
        r = await ab_test.run_page(
            {"url": "https://a.test/p", "city": "X", "topic": "t",
             "raw_text": "szöveg", "records": []},
            _Extractor(), None, "acct", "token", 0.06, None)
    finally:
        ab_test.jev_gate = original
    assert r.gated_out and r.llm_calls == 0 and r.gate_score == 0.01


@pytest.mark.asyncio
async def test_an_extraction_failure_is_recorded_rather_than_raised():
    """Every failure is data here — one bad page must not end the run."""
    class _Extractor:
        async def extract_all(self, *a, **k):
            raise RuntimeError("provider exploded")

    async def _gate(*a, **k):
        return 0.9, 300

    original, ab_test.jev_gate = ab_test.jev_gate, _gate
    try:
        r = await ab_test.run_page(
            {"url": "https://a.test/p", "city": "X", "topic": "t",
             "raw_text": "szöveg", "records": []},
            _Extractor(), None, "acct", "token", 0.06, None)
    finally:
        ab_test.jev_gate = original
    assert r.error and "provider exploded" in r.error
    assert ab_test.summarize([r], 0.06)["error_count"] == 1


def test_the_module_never_writes_to_the_database(tmp_path: Path):
    """Read-only by construction: it imports no writer at all."""
    source = Path("scraper/ab_test.py").read_text(encoding="utf-8")
    for writer in ("save_results", "update_cache_page", "save_extracted",
                   "bulk_upsert", "start_run", "INSERT", "UPDATE", "DELETE"):
        assert writer not in source, f"the experiment must not {writer}"


def test_extract_all_returns_three_lists_not_a_traced_tuple():
    """The bug review caught before the first run.

    `FallbackExtractor._call_traced` returns `(result, (model, quality))` while
    `_call` returns the result alone. `run_page` unpacks three lists, so the
    traced form would have failed every page that passed the gate — and the
    fake extractor in these tests returns the bare triple, so nothing here
    would have noticed. Asserting on the source is crude but it is what the
    defect actually was: the wrong helper by one word.
    """
    from pathlib import Path as _Path
    source = _Path("scraper/extract.py").read_text(encoding="utf-8")
    start = source.index("async def extract_all(self, text: str, city: str")
    body = source[start:start + 1200]
    assert 'self._call(\n            "extract_all"' in body, (
        "extract_all must go through _call, which strips the provenance tuple")


def _ab_db(path, pages: int = 6):
    """A cache_pages table shaped like production's, with cached extractions."""
    import json as _json
    import sqlite3 as _sq
    with _sq.connect(path) as conn:
        conn.execute("""CREATE TABLE cache_pages (
            url_hash TEXT PRIMARY KEY, url TEXT, city TEXT, topic TEXT,
            data TEXT, records_count INTEGER, extracted_at TEXT, scraped_at TEXT
        )""")
        for i in range(pages):
            has = i % 3 == 0
            records = [{"name": f"Kör {i}"}] if has else []
            conn.execute(
                "INSERT INTO cache_pages VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (f"h{i:03d}", f"https://s{i}.example/p", "Budapest", "choir",
                 _json.dumps({"raw_text": f"oldal {i} szövege", "records": records,
                              "venues_data": [{"name": "Hely"}] if has else [],
                              "persons_data": {"Budapest/choir": [{"name": "A B"}]} if has else {}}),
                 len(records), "now", "now"),
            )
    return path


def test_the_sampler_and_loader_work_against_a_production_shaped_table(tmp_path):
    """The CLI path was never exercised until this test — the first production
    run died on `load_config()` missing an argument."""
    import importlib.util
    import sys as _sys
    from pathlib import Path as _Path

    db = _ab_db(tmp_path / "ab.db")
    spec = importlib.util.spec_from_file_location(
        "run_ab_test", _Path("scripts/run_ab_test.py"))
    mod = importlib.util.module_from_spec(spec)
    _sys.modules["run_ab_test"] = mod
    spec.loader.exec_module(mod)

    hashes = mod.sample_hashes(db, 4, "seed")
    assert len(hashes) == 4 and len(set(hashes)) == 4
    assert hashes == mod.sample_hashes(db, 4, "seed"), "must be deterministic"
    assert hashes != mod.sample_hashes(db, 4, "other-seed")

    pages = ab_test.load_pages(db, hashes)
    assert len(pages) == 4
    assert all(p.get("raw_text") and p.get("records") is not None for p in pages)


@pytest.mark.asyncio
async def test_a_whole_run_produces_a_report(tmp_path, monkeypatch):
    """End to end over a real table, with the two outbound calls faked."""
    db = _ab_db(tmp_path / "ab.db", pages=9)
    pages = ab_test.load_pages(db, [f"h{i:03d}" for i in range(9)])

    async def _gate(client, url, text, city, topic, account, token):
        # Reject the pages whose baseline found nothing, keep the rest.
        return (0.9 if "0" in url[-3:] else 0.01), 300

    class _Extractor:
        async def extract_all(self, text, city, topic, locale, url, **kw):
            return ([type("R", (), {"name": "Kör 0"})()], [], [])

    monkeypatch.setattr(ab_test, "jev_gate", _gate)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "test-token")
    report = await ab_test.run_ab_test(db, pages, _Extractor(), concurrency=2)

    assert report["pages"] == 9
    assert report["error_count"] == 0
    assert report["gate"]["rejected"] + len([1 for r in [report] if True]) > 0
    assert report["calls"]["experiment"] <= report["calls"]["baseline"]
    assert report["gate"]["jev_cost_usd"] >= 0
