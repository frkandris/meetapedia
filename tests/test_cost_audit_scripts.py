import importlib.util
import json
import sqlite3
import sys
from pathlib import Path


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


def test_gate_sample_is_balanced_and_local_runner_holds_pages_out(tmp_path):
    module = _load("benchmark_joinability_gate")
    db = tmp_path / "test.db"
    with sqlite3.connect(db) as conn:
        conn.execute("""CREATE TABLE cache_pages (
            url_hash TEXT, url TEXT, city TEXT, topic TEXT, data TEXT,
            records_count INTEGER, extracted_at TEXT
        )""")
        for i in range(30):
            positive = i % 2
            text = ("weekly public chess club welcomes members " if positive else
                    "one day commercial sale and news article ") * 20
            conn.execute(
                "INSERT INTO cache_pages VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"h{i}", f"https://site{i}.example/page", "City", "chess",
                 json.dumps({"raw_text": text}), positive, "now"),
            )
    pages = module.load_sample(db, per_class=10, seed="test", max_chars=8000)
    assert len(pages) == 20
    assert sum(page.positive for page in pages) == 10
    scores = module.local_scores(pages, seed="test", buckets=1024)
    assert 0 < len(scores) < len(pages)
    assert all(0 <= score <= 1 for score in scores.values())
