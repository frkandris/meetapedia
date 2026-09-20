#!/usr/bin/env python3
"""Run the A/B extraction measurement against production data.

    PYTHONPATH=. python3 -u scripts/run_ab_test.py --pages 1000

Reads pages that already have an extraction, runs the cheaper path over them
(Jev gate, then one combined call), and reports how the two arms differ in
**counts** — communities, venues and people — and in calls spent.

Stores nothing: no cache write, no communities row, no run record. The control
arm is read from the existing cache, because re-extracting a known answer would
pay twice to learn nothing.
"""
import argparse
import asyncio
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scraper.ab_test import (DEFAULT_GATE_THRESHOLD, load_pages,  # noqa: E402
                             run_ab_test)
from scraper.config import load_config  # noqa: E402
from scraper.pipeline import build_extractor  # noqa: E402


def sample_hashes(db: Path, n: int, seed: str) -> list[str]:
    """`n` extracted pages in the corpus's own proportions, deterministically.

    Not class-balanced on purpose. The question is "how many calls does this
    save on a day's real work", and a day's real work is 81.8% empty pages —
    balancing the sample would answer a question nobody asked.

    Keys only: `url_hash` is already a hash of the URL, so ordering by it is a
    uniform sample, and reading the blobs here would repeat the mistake that
    killed a production run earlier today.
    """
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            """
            SELECT url_hash FROM cache_pages
             WHERE scraped_at IS NOT NULL AND records_count >= 0
            """
        ).fetchall()
    ranked = sorted(
        (h for (h,) in rows),
        key=lambda h: hashlib.sha256(f"{seed}|{h}".encode()).hexdigest())
    return ranked[:n]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "scraper.db")
    parser.add_argument("--pages", type=int, default=1000)
    parser.add_argument("--seed", default="ab-extraction-v1")
    parser.add_argument("--threshold", type=float, default=DEFAULT_GATE_THRESHOLD)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    if not args.db.exists():
        raise SystemExit(f"database not found: {args.db}")

    hashes = sample_hashes(args.db, args.pages, args.seed)
    pages = load_pages(args.db, hashes)
    if not pages:
        raise SystemExit(
            "no page in the sample has a cached extraction — nothing to "
            "compare against. Check --pages, or that this database has been "
            "through an extraction run.")
    empty = sum(1 for p in pages if not (p.get("records") or []))
    print(f"sample: {len(pages)} pages with a cached extraction "
          f"({empty} of them found nothing — {empty / len(pages):.1%})",
          flush=True)

    _cities, topics, config = load_config(args.db)
    extractor = build_extractor(config)
    if extractor.exhausted:
        raise SystemExit("no LLM provider is configured — nothing to measure")
    try:
        await extractor.preflight()
    except Exception as exc:
        raise SystemExit(f"extractor preflight failed: {exc}") from exc

    report = await run_ab_test(
        args.db, pages, extractor, threshold=args.threshold,
        concurrency=args.concurrency,
        valid_topics=[t.name for t in topics],
        progress_path=args.out,
    )
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"\nwritten to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
