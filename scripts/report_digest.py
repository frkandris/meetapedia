"""Multi-day digest of the daily report, without sending anything.

The daily email covers one UTC day and goes to one inbox. This prints the same
numbers for a range of days as one table, so a status check does not need the
mailbox. Read-only: it calls the report's own queries and never touches Resend.

Run inside the production container (the DB and GA4 credentials live there):

    ssh meetapedia-prod "docker exec -i -w /app -e PYTHONPATH=/app <container> \
        python3 - --days 10" < scripts/report_digest.py
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scraper.db import get_daily_counter, get_daily_summary, get_data_guides_for_day
from scraper.providers import load_catalogue
from scraper.report import fetch_ga4_traffic
from scraper.router import QuotaLedger


def _hu_cities(db: Path) -> set:
    from scraper.config import load_config
    return {c.name for c in load_config(db)[0] if c.country == "Hungary"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/app/data/scraper.db")
    ap.add_argument("--days", type=int, default=7, help="days back from yesterday")
    args = ap.parse_args()
    db = Path(args.db)
    hu = _hu_cities(db)
    cat = load_catalogue()
    today = datetime.now(timezone.utc).date()

    print("day        | látog. HU/EN | PV HU/EN  | új köz. HU/EN | AI-oldal | lekeresés | guide | provider-hívások")
    for back in range(args.days, 0, -1):
        day = (today - timedelta(days=back)).isoformat()
        nxt = (today - timedelta(days=back - 1)).isoformat()
        s = get_daily_summary(db, f"{day}T00:00:00", f"{nxt}T00:00:00", hu)
        g = fetch_ga4_traffic(day) or {}
        k, m = g.get("kozossegek", {}), g.get("meetapedia", {})
        guides = get_data_guides_for_day(db, day)
        used = {p["name"]: p["used"] for p in QuotaLedger(db, day=day).snapshot(cat)
                if p["configured"] and p["used"]}
        print(f"{day} | {k.get('visitors', 0):>5}/{m.get('visitors', 0):<5} "
              f"| {k.get('pageviews', 0):>4}/{m.get('pageviews', 0):<4} "
              f"| {s['hu']['new_communities']:>5}/{s['intl']['new_communities']:<6} "
              f"| {s['hu']['pages_extracted'] + s['intl']['pages_extracted']:>8} "
              f"| {s['hu']['searches'] + s['intl']['searches']:>9} "
              f"| {len(guides):>5} | {used}")
        print(f"{'':10}   extract_attempts={get_daily_counter(db, day, 'extract_attempts')} "
              f"enrich={get_daily_counter(db, day, 'enrich_attempts')} "
              f"guide={get_daily_counter(db, day, 'guide_attempts')}")


if __name__ == "__main__":
    main()
