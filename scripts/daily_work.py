#!/usr/bin/env python3
"""Where the pipeline worked, day by day — and what one page costs to process.

Answers "which countries and cities are we on?" from what was persisted, not
from logs: `search_cache.cached_at` (pairs searched), `cache_pages.extracted_at`
(pages extracted, with `records_count` communities found). The last block gives
the unit numbers for pricing a model — tokens per call by provider and
communities per extracted page — so "how many communities does $1 buy" can be
computed from measurement. Read-only; runs in the container:

    ssh meetapedia-prod "docker exec -i -w /app -e PYTHONPATH=/app <container> \\
        python3 - --days 10" < scripts/daily_work.py
"""
from __future__ import annotations

import argparse
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/app/data/scraper.db")
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--top", type=int, default=6)
    args = ap.parse_args()

    from scraper.config import load_config
    country_of = {c.name: c.country for c in load_config(Path(args.db))[0]}
    c = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    since = (datetime.now(timezone.utc).date() - timedelta(days=args.days)).isoformat()

    searched: dict[str, Counter] = defaultdict(Counter)
    for day, city in c.execute("SELECT substr(cached_at,1,10), city FROM search_cache "
                               "WHERE cached_at >= ?", (since,)):
        searched[day][city] += 1
    pages: dict[str, Counter] = defaultdict(Counter)
    found: dict[str, Counter] = defaultdict(Counter)
    for day, city, n in c.execute("SELECT substr(extracted_at,1,10), city, records_count "
                                  "FROM cache_pages WHERE extracted_at >= ?", (since,)):
        pages[day][city] += 1
        found[day][city] += n or 0

    def by_country(cnt: Counter) -> Counter:
        out: Counter = Counter()
        for city, n in cnt.items():
            out[country_of.get(city, "?")] += n
        return out

    for day in sorted(set(searched) | set(pages)):
        s, p, f = searched[day], pages[day], found[day]
        print(f"\n{day}  searched pairs={sum(s.values())}  extracted pages={sum(p.values())}  "
              f"communities found={sum(f.values())}")
        print("  search countries:", ", ".join(f"{k} {v}" for k, v in by_country(s).most_common(args.top)))
        print("  search cities:   ", ", ".join(f"{k} {v}" for k, v in s.most_common(args.top)))
        print("  extract countries:", ", ".join(f"{k} {v}" for k, v in by_country(p).most_common(args.top)))
        print("  extract cities:   ", ", ".join(f"{k} {v}" for k, v in p.most_common(args.top)))

    print("\n== unit numbers (since", since + ")")
    for prov, calls, tokens, cost in c.execute(
            "SELECT provider, SUM(calls), SUM(tokens), SUM(cost_usd) FROM provider_usage "
            "WHERE day >= ? GROUP BY provider ORDER BY 2 DESC", (since,)):
        print(f"  {prov:14} calls={calls:7} tokens={tokens:11} "
              f"tok/call={tokens / max(calls, 1):7.0f} cost=${cost:.2f}")
    n, tot, hit = c.execute("SELECT COUNT(*), SUM(COALESCE(records_count,0)), "
                            "SUM(records_count > 0) FROM cache_pages "
                            "WHERE extracted_at >= ? AND records_count IS NOT NULL",
                            (since,)).fetchone()
    print(f"  pages={n} communities={tot} per_page={tot / max(n, 1):.2f} "
          f"pages_with_any={hit / max(n, 1):.0%}")
    for model, k, q in c.execute("SELECT extract_model, COUNT(*), AVG(records_count) FROM cache_pages "
                                 "WHERE extracted_at >= ? GROUP BY 1 ORDER BY 2 DESC LIMIT 8",
                                 (since,)):
        print(f"  served_by {str(model):40} pages={k:6} per_page={q or 0:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
