#!/usr/bin/env python3
"""Score a model served by a local llama-server on production's golden set.

`POST /v1/score` and `scripts/score_candidates.py` run in the container and can
only reach models production can route to. A model being *considered* for the
local GPU runs on this Mac, behind no tunnel, so the golden set comes here
instead: export it once (README), then score any GGUF against it with the same
`scoring.score_model` production uses — the same pages, expected names,
generic-token list and place names, so the numbers compare with the
catalogue's `quality:`.

    python tools/localgpu-governor/score_local.py --golden .preview/bench/golden40.json \\
        --url http://127.0.0.1:8090/v1 --label qwen3.5-2b-q6k --concurrency 2
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


async def main() -> int:
    from scraper.providers import OpenAICompatExtractor
    from scraper.scoring import score_page

    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", required=True)
    ap.add_argument("--url", default="http://127.0.0.1:8090/v1")
    ap.add_argument("--key-file", default=str(Path.home() / ".meetapedia/localgpu.key"))
    ap.add_argument("--label", required=True)
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--max-tokens", type=int, default=2000)
    args = ap.parse_args()

    g = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    generic, places = frozenset(g["generic"]), frozenset(g["places"])
    ex = OpenAICompatExtractor(
        provider="localgpu", base_url=args.url,
        api_key=Path(args.key_file).expanduser().read_text().strip(),
        model="qwen3.5-4b-q6k", quality=0, timeout_seconds=600,
        max_output_tokens=args.max_tokens, stream=True, json_schema=True,
        max_concurrency=args.concurrency, fingerprint_model="deepseek-chat")
    sem = asyncio.Semaphore(args.concurrency)
    scores: list[float] = []
    failed = 0

    async def one(page: dict) -> None:
        nonlocal failed
        async with sem:
            try:
                recs = await ex.extract(text=page["text"], city=page["city"],
                                        topic=page["topic"], locale="hu", source_url=page["url"])
            except Exception as exc:  # noqa: BLE001 - a failure is counted, not fatal
                failed += 1
                print(f"  fail {page['url'][:60]}: {type(exc).__name__}: {str(exc)[:80]}")
                return
        scores.append(score_page(page["expected"], [r.name for r in recs if r.name],
                                 generic, places))

    t0 = time.monotonic()
    await asyncio.gather(*[one(p) for p in g["pages"]])
    wall = time.monotonic() - t0
    n = len(g["pages"])
    print(f"{args.label:22} score={round(sum(scores) / len(scores)) if scores else None} "
          f"answered={len(scores)}/{n} failed={failed} wall={wall:.0f}s "
          f"pages/hour={n / wall * 3600:.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
