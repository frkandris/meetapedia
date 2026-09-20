#!/usr/bin/env python3
"""Report whether inline contact enrichment earns its search and LLM calls.

Reads the existing ``enrich_log`` telemetry in cache_pages; it never mutates
the database.  Run this against the production database before changing
``pipeline.enrich_communities``:

    PYTHONPATH=. .venv/bin/python scripts/report_inline_enrichment.py \
        --db data/scraper.db

An attempt means one community triggered the inline enrichment path.  Search
queries equal attempts.  LLM calls are approximated by fetched research URLs;
the extractor returns after its first useful result, while failed URLs can
cause a second call.  ``fields_added`` is the actual value of the work.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def measure(db: Path) -> dict:
    if not db.exists():
        raise SystemExit(f"database not found: {db}")
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT json_extract(data, '$.enrich_log') FROM cache_pages "
            "WHERE json_extract(data, '$.enrich_log') IS NOT NULL"
        ).fetchall()

    attempts = successes = fetched = 0
    fields: Counter[str] = Counter()
    for (raw,) in rows:
        try:
            entries = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("search_query"):
                continue
            attempts += 1
            research = entry.get("research_urls") or []
            fetched += sum(bool(r.get("fetched")) for r in research if isinstance(r, dict))
            added = entry.get("fields_added") or []
            if added:
                successes += 1
                fields.update(str(field) for field in added)
    return {
        "pages_with_telemetry": len(rows),
        "attempts_and_searches": attempts,
        "approx_llm_calls": fetched,
        "successful_records": successes,
        "success_rate": successes / attempts if attempts else 0.0,
        "fields_added": dict(fields.most_common()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "scraper.db")
    args = parser.parse_args()
    result = measure(args.db)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["attempts_and_searches"]:
        print("No telemetry yet; leave enrichment unchanged until production has a sample.")
    elif result["success_rate"] < 0.10:
        print("Below 10% yield: disabling inline enrichment is a strong candidate.")
    else:
        print("Measure field value and search cost before disabling; the path is producing data.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
