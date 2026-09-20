import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_inline_enrichment_report_counts_value(tmp_path):
    module = _load("report_inline_enrichment")
    db = tmp_path / "test.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE cache_pages (data TEXT)")
        conn.execute("INSERT INTO cache_pages VALUES (?)", (json.dumps({
            "enrich_log": [
                {"search_query": "a", "research_urls": [{"fetched": True}],
                 "fields_added": ["website"]},
                {"search_query": "b", "research_urls": [{"fetched": False}],
                 "fields_added": []},
            ]
        }),))
    result = module.measure(db)
    assert result["attempts_and_searches"] == 2
    assert result["approx_llm_calls"] == 1
    assert result["success_rate"] == 0.5
    assert result["fields_added"] == {"website": 1}


def _gate_db(path: Path, pages: int = 30, extracted: bool = True) -> Path:
    """A cache_pages table shaped like production's, including the sentinel.

    `records_count = -1` means scraped but never extracted (see db.py); the
    sampler must skip those, and it is the column it selects on, so a fixture
    without them would not exercise the filter at all.
    """
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE cache_pages (
            url_hash TEXT PRIMARY KEY, url TEXT, city TEXT, topic TEXT, data TEXT,
            records_count INTEGER, extracted_at TEXT, scraped_at TEXT
        )""")
        for i in range(pages):
            positive = i % 2
            text = ("weekly public chess club welcomes members " if positive else
                    "one day commercial sale and news article ") * 20
            conn.execute(
                "INSERT INTO cache_pages VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (f"h{i}", f"https://site{i}.example/page", "City", "chess",
                 json.dumps({"raw_text": text}),
                 positive if extracted else -1,
                 "now" if extracted else None, "now"),
            )
    return path


def test_gate_sample_is_balanced_and_local_runner_holds_pages_out(tmp_path):
    module = _load("benchmark_joinability_gate")
    db = _gate_db(tmp_path / "test.db")
    pages = module.load_sample(db, per_class=10, seed="test", max_chars=8000)
    assert len(pages) == 20
    assert sum(page.positive for page in pages) == 10
    scores = module.local_scores(pages, seed="test", buckets=1024)
    assert 0 < len(scores) < len(pages)
    assert all(0 <= score <= 1 for score in scores.values())


def test_the_sampler_reads_page_text_only_for_the_pages_it_picked(tmp_path):
    """The bug that killed a production run on 2026-09-20.

    The first version selected in Python: one `fetchall()` over every
    extracted page's `raw_text`, then a sort, then 2,000 kept. That is ~4 GB
    resident on the real corpus — 128,072 rows averaging 30 KB of blob — on an
    8 GB host already running the scraper. Selection must happen on keys.
    """
    module = _load("benchmark_joinability_gate")
    db = _gate_db(tmp_path / "test.db", pages=200)

    statements: list[str] = []
    real_connect = sqlite3.connect

    def _tracing_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    module.sqlite3.connect = _tracing_connect
    try:
        pages = module.load_sample(db, per_class=5, seed="test", max_chars=8000)
    finally:
        module.sqlite3.connect = real_connect

    assert len(pages) == 10
    blob_reads = [s for s in statements if "json_extract" in s]
    assert blob_reads, "the text has to be fetched somewhere"
    for statement in blob_reads:
        assert "url_hash IN" in statement, (
            "page text may only be read for the sampled keys, never corpus-wide:\n"
            + statement)
    selection = [s for s in statements if "json_extract" not in s and "FROM cache_pages" in s]
    assert selection, "expected a key-only selection query"
    for statement in selection:
        # The partial index idx_cache_pages_done is only eligible when the
        # query repeats its own WHERE clause.
        assert "scraped_at IS NOT NULL" in statement
        assert "records_count >= 0" in statement


def test_pages_that_were_never_extracted_are_not_sampled(tmp_path):
    module = _load("benchmark_joinability_gate")
    db = _gate_db(tmp_path / "unextracted.db", extracted=False)
    with pytest.raises(SystemExit):
        module.load_sample(db, per_class=5, seed="test", max_chars=8000)
