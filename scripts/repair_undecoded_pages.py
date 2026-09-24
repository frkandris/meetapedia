#!/usr/bin/env python3
"""Forget cached pages whose text is undecoded binary, so they are fetched again.

Until 2026-09-24 the fetcher offered Brotli (`Accept-Encoding: br`) without the
`brotli` package installed. Servers that honoured it sent Brotli; httpx passed
the bytes through undecoded; html2text accepted them as text. About 28% of
`cache_pages` hold that noise, and the extractor has been reading it.

For every such page this drops the text and every extraction made from it, and
clears `collected_at` on each search pair that lists the URL. The next
`search_only` pass then re-downloads the page from the search results it already
owns (no search is bought), and `ai_only` extracts the real text.

Communities already saved from these pages are left alone: re-extraction
upserts over them, and deleting records is a moderation decision. The dry run
reports how many there are.

Usage
-----
    python scripts/repair_undecoded_pages.py /app/data/scraper.db          # dry run
    python scripts/repair_undecoded_pages.py /app/data/scraper.db --apply
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper.fetch import looks_undecoded  # noqa: E402

#: Blob keys that were derived from the page text and are now meaningless.
_DERIVED = ("raw_text", "scraped_at", "records", "extracted_at", "extract_fingerprint",
            "extract_model", "extract_quality", "extract_duration_s",
            "venues_data", "venue_extracted_at", "venue_fingerprint", "venue_model",
            "persons_data", "person_extracted_at", "person_fingerprint", "person_model")
_CHUNK = 2000


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=60)
    conn.execute("PRAGMA busy_timeout = 60000")
    return conn


def find_undecoded(conn: sqlite3.Connection) -> dict[str, str]:
    """{url_hash: url} for every page whose stored text is binary noise."""
    found: dict[str, str] = {}
    last = 0
    while True:
        rows = conn.execute(
            "SELECT rowid, url_hash, url, json_extract(data, '$.raw_text') FROM cache_pages"
            " WHERE rowid > ? ORDER BY rowid LIMIT ?", (last, _CHUNK)).fetchall()
        if not rows:
            return found
        for rowid, url_hash, url, text in rows:
            last = rowid
            if text and looks_undecoded(text):
                found[url_hash] = url
        # Commit-free reads, but give the worker's writers a gap.
        conn.commit()


def affected_pairs(conn: sqlite3.Connection, urls: set[str]) -> list[tuple[str, str]]:
    pairs = []
    for city, topic, raw in conn.execute("SELECT city, topic, urls FROM search_cache"):
        try:
            listed = json.loads(raw or "[]")
        except json.JSONDecodeError:
            continue
        if urls.intersection(listed):
            pairs.append((city, topic))
    return pairs


def affected_communities(conn: sqlite3.Connection, urls: set[str]) -> int:
    count = 0
    for (raw,) in conn.execute(
            "SELECT json_extract(data, '$.source_urls') FROM communities"):
        try:
            sources = json.loads(raw or "[]")
        except json.JSONDecodeError:
            continue
        if sources and urls.issuperset(sources):
            count += 1
    return count


def apply(conn: sqlite3.Connection, pages: dict[str, str],
          pairs: list[tuple[str, str]]) -> None:
    paths = ", ".join(f"'$.{key}'" for key in _DERIVED)
    hashes = list(pages)
    for start in range(0, len(hashes), _CHUNK):
        chunk = hashes[start:start + _CHUNK]
        conn.executemany(
            "UPDATE cache_pages SET scraped_at=NULL, extracted_at=NULL,"
            " extract_fingerprint=NULL, venue_fingerprint=NULL, person_fingerprint=NULL,"
            " extract_quality=NULL, extract_model=NULL, records_count=0,"
            f" data=json_remove(data, {paths}) WHERE url_hash=?",
            [(h,) for h in chunk])
        conn.commit()
    for start in range(0, len(pairs), _CHUNK):
        conn.executemany(
            "UPDATE search_cache SET collected_at=NULL WHERE city=? AND topic=?",
            pairs[start:start + _CHUNK])
        conn.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("db", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    conn = _connect(args.db)
    pages = find_undecoded(conn)
    urls = set(pages.values())
    pairs = affected_pairs(conn, urls)
    total = conn.execute("SELECT COUNT(*) FROM cache_pages").fetchone()[0]
    print(f"undecoded pages: {len(pages)} of {total}")
    print(f"search pairs to re-collect: {len(pairs)}")
    print(f"communities sourced only from these pages (left in place): "
          f"{affected_communities(conn, urls)}")
    if not args.apply:
        print("dry run — nothing written; pass --apply to repair")
        return 0
    apply(conn, pages, pairs)
    print("repaired")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
