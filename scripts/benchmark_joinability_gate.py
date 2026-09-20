#!/usr/bin/env python3
"""Benchmark cheap page-level gates before community extraction.

The production extractor both *finds* communities and generates their fields.
Jev cannot replace that job: it can only decide whether a page is worth sending
to it.  This script measures precisely that cascade decision against cached
pages, with the incumbent extraction's ``records_count > 0`` as a weak label.

Two runners use the exact same deterministic, balanced sample:

    PYTHONPATH=. .venv/bin/python scripts/benchmark_joinability_gate.py local
    TYPESAFE_API_KEY=... PYTHONPATH=. .venv/bin/python \
        scripts/benchmark_joinability_gate.py jev --cache data/jev-gate-cache.json

``local`` is a dependency-free hashed character n-gram Naive Bayes baseline.
It trains on one deterministic split and reports the held-out split.  ``jev``
calls the TypeSafe System One API.  Neither mode mutates the scraper database.

The labels are agreement with the incumbent extractor, not truth.  Before a
gate can drop production work, manually review its false negatives and a
sample of its confident negatives.  The useful number is positive recall at a
threshold that still rejects many negative pages, not overall accuracy.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "scraper.db"
API_URL = "https://api.typesafe.ai/v1/systemone"


@dataclass(frozen=True)
class Page:
    url_hash: str
    url: str
    city: str
    topic: str
    text: str
    positive: bool


def _rank(seed: str, value: str) -> str:
    return hashlib.sha256(f"{seed}|{value}".encode()).hexdigest()


def load_sample(db: Path, per_class: int, seed: str, max_chars: int) -> list[Page]:
    if not db.exists():
        raise SystemExit(f"database not found: {db}")
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            """
            SELECT url_hash, url, city, topic,
                   json_extract(data, '$.raw_text'), records_count
              FROM cache_pages
             WHERE extracted_at IS NOT NULL
               AND records_count >= 0
               AND json_extract(data, '$.raw_text') IS NOT NULL
            """
        ).fetchall()
    classes: dict[bool, list[Page]] = {False: [], True: []}
    for url_hash, url, city, topic, text, count in rows:
        if not text or not url:
            continue
        page = Page(url_hash, url, city or "", topic or "", text[:max_chars], count > 0)
        classes[page.positive].append(page)
    for value in classes.values():
        value.sort(key=lambda p: _rank(seed, p.url_hash))
    available = min(len(classes[False]), len(classes[True]), per_class)
    if available == 0:
        raise SystemExit("need both positive and zero-record extracted pages")
    sample = classes[False][:available] + classes[True][:available]
    sample.sort(key=lambda p: _rank(seed + "-mixed", p.url_hash))
    return sample


def _features(text: str, buckets: int) -> Counter[int]:
    normalized = " ".join(text.casefold().split())
    out: Counter[int] = Counter()
    for n in (3, 4, 5):
        for i in range(max(0, len(normalized) - n + 1)):
            digest = hashlib.blake2s(normalized[i:i + n].encode(), digest_size=4).digest()
            out[int.from_bytes(digest) % buckets] += 1
    return out


def _split(page: Page, seed: str) -> bool:
    """True for train. Split by host so one site's template cannot leak."""
    host = urlparse(page.url).netloc.casefold() or page.url_hash
    return int(_rank(seed + "-split", host)[:8], 16) % 5 != 0


def local_scores(pages: list[Page], seed: str, buckets: int) -> dict[str, float]:
    train = [p for p in pages if _split(p, seed)]
    test = [p for p in pages if not _split(p, seed)]
    # A small sample can hash every host into one side. Fall back to a page split
    # while preserving the normal domain-held-out behavior for real runs.
    if not test or len({p.positive for p in train}) < 2:
        train = [p for p in pages if int(_rank(seed, p.url_hash)[:8], 16) % 5 != 0]
        test = [p for p in pages if p not in train]

    totals = {False: [1] * buckets, True: [1] * buckets}
    token_totals = {False: buckets, True: buckets}
    class_counts = Counter(p.positive for p in train)
    for page in train:
        for idx, count in _features(page.text, buckets).items():
            totals[page.positive][idx] += count
            token_totals[page.positive] += count

    scores: dict[str, float] = {}
    for page in test:
        feats = _features(page.text, buckets)
        logs = {}
        for label in (False, True):
            logs[label] = math.log((class_counts[label] + 1) / (len(train) + 2))
            denom = token_totals[label]
            logs[label] += sum(
                count * math.log(totals[label][idx] / denom)
                for idx, count in feats.items()
            )
        delta = max(-50.0, min(50.0, logs[True] - logs[False]))
        scores[page.url_hash] = 1.0 / (1.0 + math.exp(-delta))
    print(f"local split: train={len(train)}, held_out={len(test)}, "
          f"held_out_positive={sum(p.positive for p in test)}")
    return scores


def _jev_payload(page: Page, model: str) -> dict:
    return {
        "model": model,
        "state": {
            "city": page.city,
            "topic": page.topic,
            "source_url": page.url,
            "page_text": page.text,
        },
        "questions": {
            "has_joinable_community": {
                "type": "noul",
                "instructions": (
                    "Does this page provide evidence of at least one genuine community group "
                    "or club in or near the specified city and relevant to the specified topic, "
                    "where that same group meets or organizes activities regularly, is open to "
                    "new members from the general public, and has a group identity rather than "
                    "being only a venue, commercial course, professional ensemble, one-time "
                    "event, annual event, news article, or directory entry for another city?"
                ),
                "criteria": {
                    "true": "At least one group on the page satisfies every condition.",
                    "false": "No single group on the page satisfies every condition.",
                },
            },
            "contains_recurring_group": {
                "type": "noul",
                "instructions": "Does the page evidence a group that meets or acts regularly?",
            },
            "contains_publicly_open_group": {
                "type": "noul",
                "instructions": "Does the page evidence a group open to new members from the public?",
            },
            "contains_group_identity": {
                "type": "noul",
                "instructions": (
                    "Does the page evidence a group identity, not merely a venue, course, "
                    "business, article, directory, or individual event?"
                ),
            },
        },
    }


async def jev_scores(
    pages: list[Page], model: str, concurrency: int, cache_path: Path | None,
) -> dict[str, float]:
    key = os.environ.get("TYPESAFE_API_KEY", "")
    if not key:
        raise SystemExit("TYPESAFE_API_KEY is required for the jev runner")
    cache: dict = {}
    if cache_path and cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    sem = asyncio.Semaphore(max(1, concurrency))
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=60, headers=headers) as client:
        async def one(page: Page) -> None:
            cache_key = f"{model}:{page.url_hash}"
            if cache_key in cache:
                return
            async with sem:
                response = await client.post(API_URL, json=_jev_payload(page, model))
                response.raise_for_status()
                body = response.json()
                answers = body.get("answers") or {}
                cache[cache_key] = {
                    "label": page.positive,
                    "url": page.url,
                    "city": page.city,
                    "topic": page.topic,
                    "answers": answers,
                    "usage": body.get("usage") or {},
                }
                if cache_path:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_text(
                        json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
                    )

        await asyncio.gather(*(one(page) for page in pages))

    scores = {}
    for page in pages:
        answer = cache[f"{model}:{page.url_hash}"]["answers"]["has_joinable_community"]
        scores[page.url_hash] = float(answer["noul"])
    return scores


def report(pages: list[Page], scores: dict[str, float]) -> None:
    evaluated = [p for p in pages if p.url_hash in scores]
    print(f"evaluated: {len(evaluated)} pages, positives={sum(p.positive for p in evaluated)}")
    print("\nthreshold  skipped  neg_skipped  false_neg  positive_recall")
    for threshold in (0.01, 0.02, 0.05, 0.10, 0.20, 0.30, 0.40, 0.50):
        skipped = [p for p in evaluated if scores[p.url_hash] < threshold]
        false_neg = sum(p.positive for p in skipped)
        positives = sum(p.positive for p in evaluated)
        neg_skipped = sum(not p.positive for p in skipped)
        recall = 1.0 - false_neg / positives if positives else 0.0
        print(f"{threshold:9.2f} {len(skipped):8} {neg_skipped:12} "
              f"{false_neg:10} {recall:15.3%}")

    mistakes = sorted(
        (p for p in evaluated if p.positive), key=lambda p: scores[p.url_hash]
    )[:20]
    print("\nlowest-scored incumbent positives (review these first):")
    for page in mistakes:
        print(f"  {scores[page.url_hash]:.4f}  {page.city}/{page.topic}  {page.url}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("runner", choices=("local", "jev"))
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--per-class", type=int, default=1000)
    parser.add_argument("--max-chars", type=int, default=8000)
    parser.add_argument("--seed", default="joinability-gate-v1")
    parser.add_argument("--buckets", type=int, default=65536)
    parser.add_argument("--model", default="jev-1.13.0")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--cache", type=Path)
    args = parser.parse_args()

    pages = load_sample(args.db, args.per_class, args.seed, args.max_chars)
    print(f"sample: {len(pages)} pages ({len(pages) // 2} per class), seed={args.seed}")
    if args.runner == "local":
        scores = local_scores(pages, args.seed, args.buckets)
    else:
        scores = await jev_scores(pages, args.model, args.concurrency, args.cache)
    report(pages, scores)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
