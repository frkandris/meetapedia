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

#: Jev is also served by Cloudflare Workers AI, which matters because
#: TypeSafe's own console is behind a waitlist while our Cloudflare token is
#: already configured (`CLOUDFLARE_API_TOKEN`, see config/providers.yaml). Same
#: model, same question types; the wire format differs only in the envelope.
#: The account id is not a secret — it is in every dashboard URL — and is
#: inlined for the same reason providers.yaml inlines it.
CF_ACCOUNT_ID = os.environ.get(
    "CLOUDFLARE_ACCOUNT_ID", "809bd7dcd8939fa5e520a96ab4923429")
CF_URL = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/run"
CF_MODEL = "typesafe/jev"


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


#: How many candidates per class to resolve text for, as a multiple of the
#: sample size. A candidate is dropped if its blob turns out to have no
#: `raw_text`, so asking for exactly `per_class` would quietly return a short
#: sample; asking for this many keeps the deterministic order intact and still
#: reads a few thousand rows rather than the corpus.
_CANDIDATE_FACTOR = 3


def load_sample(db: Path, per_class: int, seed: str, max_chars: int) -> list[Page]:
    """A deterministic, class-balanced sample — without materializing the corpus.

    The first version of this selected in Python: it read every extracted
    page's `raw_text` with one `fetchall()`, sorted the lot, and kept 2,000.
    On the production database that is 128,072 rows whose `data` blob averages
    ~30 KB — about 4 GB of page text resident, on an 8 GB host that is also
    running the scraper and a 6 GB SQLite file. Killed on 2026-09-20 with
    `free -m` reporting 237 MB free and climbing. CLAUDE.md forbids exactly
    this shape for `ai_only`; a benchmark script is not an exception to it.

    So selection happens on keys only. `records_count >= 0` is what "has been
    extracted" means (-1 is the scraped-but-not-extracted sentinel, see
    `db.py`), and `scraped_at IS NOT NULL` is repeated verbatim from the
    partial index `idx_cache_pages_done` — a partial index is only eligible
    when the query's WHERE matches its own clause, and with it this read is
    served entirely from the index: the same change measured 11.03 s -> 0.31 s
    in `done-pair-url-hash-not-city-topic`. Only the chosen keys then have
    their text fetched, by primary key.

    The ordering is unchanged, so a given `seed` selects the same pages it
    always did.
    """
    if not db.exists():
        raise SystemExit(f"database not found: {db}")
    with sqlite3.connect(db) as conn:
        keys = conn.execute(
            """
            SELECT url_hash, records_count
              FROM cache_pages
             WHERE scraped_at IS NOT NULL
               AND records_count >= 0
            """
        ).fetchall()

        ranked: dict[bool, list[str]] = {False: [], True: []}
        for url_hash, count in keys:
            ranked[count > 0].append(url_hash)
        for value in ranked.values():
            value.sort(key=lambda h: _rank(seed, h))

        wanted = per_class * _CANDIDATE_FACTOR
        candidates = ranked[False][:wanted] + ranked[True][:wanted]
        if not candidates:
            raise SystemExit("need both positive and zero-record extracted pages")

        by_hash: dict[str, tuple] = {}
        for chunk in range(0, len(candidates), 500):  # SQLite caps parameters
            batch = candidates[chunk:chunk + 500]
            placeholders = ",".join("?" * len(batch))
            # `substr` in SQL, not `[:max_chars]` in Python: the blob holds a
            # whole page (~30 KB on production) and the runners see at most
            # `max_chars` of it. Truncating here is what keeps the resident
            # set flat — it took the measured peak from 331 MB to 79 MB on a
            # 739 MB synthetic corpus, and the difference grows with the blob.
            for row in conn.execute(
                f"""
                SELECT url_hash, url, city, topic,
                       substr(json_extract(data, '$.raw_text'), 1, ?), records_count
                  FROM cache_pages
                 WHERE url_hash IN ({placeholders})
                """, (max_chars, *batch),
            ):
                by_hash[row[0]] = row

    classes: dict[bool, list[Page]] = {False: [], True: []}
    for url_hash in candidates:
        row = by_hash.get(url_hash)
        if row is None:
            continue
        _, url, city, topic, text, count = row
        if not text or not url:
            continue
        page = Page(url_hash, url, city or "", topic or "", text[:max_chars], count > 0)
        classes[page.positive].append(page)

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


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, x))))


def _train_naive_bayes(train: list[Page], buckets: int) -> tuple:
    totals = {False: [1] * buckets, True: [1] * buckets}
    token_totals = {False: buckets, True: buckets}
    class_counts = Counter(p.positive for p in train)
    for page in train:
        for idx, count in _features(page.text, buckets).items():
            totals[page.positive][idx] += count
            token_totals[page.positive] += count
    return totals, token_totals, class_counts, len(train)


def _log_odds_per_token(page: Page, model: tuple, buckets: int) -> float:
    """Naive Bayes log-odds, divided by the number of n-grams that produced it.

    The division is the whole point. Bayes adds one log-probability per feature
    and a page has thousands, so the raw sum scales with page length: a long
    page is automatically extreme, and the ±50 clamp the first version applied
    is where nearly every page ended up. That is what made the 2026-09-20
    measurement's threshold column inert — moving it from 0.01 to 0.50 changed
    recall by half a point, because there was no probability mass in between.
    Per token, the statistic is a rate rather than a total, and comparable
    between a 400-character page and an 8,000-character one.
    """
    totals, token_totals, class_counts, n_train = model
    feats = _features(page.text, buckets)
    n_tokens = sum(feats.values())
    if not n_tokens:
        return 0.0
    logs = {}
    for label in (False, True):
        logs[label] = math.log((class_counts[label] + 1) / (n_train + 2))
        denom = token_totals[label]
        logs[label] += sum(
            count * math.log(totals[label][idx] / denom)
            for idx, count in feats.items()
        )
    return (logs[True] - logs[False]) / n_tokens


def _fit_platt(xs: list[float], ys: list[bool],
               iterations: int = 4000, rate: float = 0.5) -> "tuple[float, float, float, float]":
    """Platt scaling: turn a raw score into a calibrated probability.

    Returns `(mean, stdev, a, b)` — the standardization the fit was done in,
    plus the logistic coefficients, because applying them requires both.

    A gate is only useful if its threshold means something, and a naive Bayes
    score does not: it is monotone in the right direction but has no
    probabilistic reading. Fitting a one-dimensional logistic on held-back data
    gives back a number where "0.02" is a rate of being wrong, which is what a
    recall target is expressed in. It is also what makes the local baseline
    comparable to Jev, whose selling point is exactly this property.
    """
    if not xs:
        return 0.0, 1.0, 1.0, 0.0
    mean = sum(xs) / len(xs)
    variance = sum((x - mean) ** 2 for x in xs) / max(1, len(xs) - 1)
    stdev = math.sqrt(variance) or 1.0
    zs = [(x - mean) / stdev for x in xs]

    a, b = 1.0, 0.0
    n = len(zs)
    for _ in range(iterations):
        grad_a = grad_b = 0.0
        for z, y in zip(zs, ys):
            error = _sigmoid(a * z + b) - (1.0 if y else 0.0)
            grad_a += error * z
            grad_b += error
        a -= rate * grad_a / n
        b -= rate * grad_b / n
    return mean, stdev, a, b


def local_scores(pages: list[Page], seed: str, buckets: int) -> dict[str, float]:
    """Calibrated gate probabilities from a dependency-free local model.

    Three disjoint parts, split by hostname so one site's template cannot leak
    between them: fit (the Bayes counts), calibrate (the Platt coefficients),
    and held-out (what the report scores). Calibrating on the fitting data
    would report a confidence the model has not earned.
    """
    train = [p for p in pages if _split(p, seed)]
    test = [p for p in pages if not _split(p, seed)]
    # A small sample can hash every host into one side. Fall back to a page split
    # while preserving the normal domain-held-out behavior for real runs.
    if not test or len({p.positive for p in train}) < 2:
        train = [p for p in pages if int(_rank(seed, p.url_hash)[:8], 16) % 5 != 0]
        test = [p for p in pages if p not in train]

    calib = [p for p in train if int(_rank(seed + "-calib", p.url_hash)[:8], 16) % 4 == 0]
    fit = [p for p in train if p not in calib]
    if len({p.positive for p in calib}) < 2 or len({p.positive for p in fit}) < 2:
        # Too few pages to hold a calibration set back; fit on everything and
        # say so, rather than reporting a probability nothing supports.
        calib, fit = train, train

    model = _train_naive_bayes(fit, buckets)
    mean, stdev, a, b = _fit_platt(
        [_log_odds_per_token(p, model, buckets) for p in calib],
        [p.positive for p in calib],
    )

    scores = {
        page.url_hash: _sigmoid(
            a * ((_log_odds_per_token(page, model, buckets) - mean) / stdev) + b)
        for page in test
    }
    print(f"local split: fit={len(fit)}, calibrate={len(calib)}, "
          f"held_out={len(test)}, held_out_positive={sum(p.positive for p in test)}")
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
    provider: str = "typesafe",
) -> dict[str, float]:
    """Score every page with Jev, through TypeSafe directly or via Cloudflare.

    The two differ only in envelope. TypeSafe takes `{model, state, questions}`
    and answers `{answers, usage}`; Cloudflare wraps the same payload in
    `{model, input:{…}}` and may wrap the same answer in `{result:{…}}`. Both
    shapes are accepted on the way back, because Workers AI is not consistent
    about the wrapper across models.
    """
    if provider == "cloudflare":
        key = os.environ.get("CLOUDFLARE_API_TOKEN", "")
        if not key:
            raise SystemExit("CLOUDFLARE_API_TOKEN is required for --provider cloudflare")
        url, model = CF_URL, CF_MODEL
    else:
        key = os.environ.get("TYPESAFE_API_KEY", "")
        if not key:
            raise SystemExit("TYPESAFE_API_KEY is required for the jev runner")
        url = API_URL
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
                payload = _jev_payload(page, model)
                if provider == "cloudflare":
                    payload = {"model": model,
                               "input": {k: v for k, v in payload.items()
                                         if k != "model"}}
                response = await client.post(url, json=payload)
                response.raise_for_status()
                body = response.json()
                if isinstance(body.get("result"), dict):
                    body = body["result"]
                answers = body.get("answers") or {}
                if not answers:
                    raise SystemExit(
                        "no answers in the response — the wire format may have "
                        f"changed: {json.dumps(body)[:400]}")
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
    parser.add_argument(
        "--provider", choices=("typesafe", "cloudflare"), default="typesafe",
        help="Where to reach Jev. 'cloudflare' uses CLOUDFLARE_API_TOKEN and "
             "the existing Workers AI account, which is the route that needs "
             "no waitlist; it also spends the same daily neuron allowance the "
             "extraction fleet's gpt-oss-20b runs on.")
    args = parser.parse_args()

    pages = load_sample(args.db, args.per_class, args.seed, args.max_chars)
    print(f"sample: {len(pages)} pages ({len(pages) // 2} per class), seed={args.seed}")
    if args.runner == "local":
        scores = local_scores(pages, args.seed, args.buckets)
    else:
        scores = await jev_scores(pages, args.model, args.concurrency, args.cache,
                                  args.provider)
    report(pages, scores)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
