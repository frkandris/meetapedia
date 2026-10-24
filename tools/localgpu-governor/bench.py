#!/usr/bin/env python3
"""Throughput benchmark for a llama-server on the extraction workload.

Sends the requests production sends — the extraction system prompt, the user
message from `extract.extraction_user_prompt`, the community JSON schema as a
grammar — over real cached pages, `--concurrency` at a time, and reports
aggregate generated tokens per second, wall time and per-request latency. With
`--save`, it keeps every answer so two configurations can be compared for
identical output (speculative decoding should not change what is extracted).

Pages come from production's golden set (see the README for the export
command): {"system": "...", "pages": [{"url", "text", "city", "topic"}]}.

    python tools/localgpu-governor/bench.py --url http://127.0.0.1:8090 \\
        --key-file ~/.meetapedia/localgpu.key --pages .preview/bench/pages.json \\
        --concurrency 4 --repeat 2 --save out.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


async def main() -> int:
    import httpx

    from scraper.extract import EXTRACTION_SCHEMA, extraction_user_prompt

    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8090")
    ap.add_argument("--key-file", default=str(Path.home() / ".meetapedia/localgpu.key"))
    ap.add_argument("--pages", required=True)
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--max-tokens", type=int, default=4000)
    ap.add_argument("--save")
    ap.add_argument("--label", default="")
    args = ap.parse_args()

    data = json.loads(Path(args.pages).read_text(encoding="utf-8"))
    key = Path(args.key_file).expanduser().read_text().strip()
    jobs = [p for p in data["pages"] if p.get("text")] * args.repeat
    sem = asyncio.Semaphore(args.concurrency)
    results: list[dict] = []

    async with httpx.AsyncClient(timeout=900) as client:
        async def one(i: int, page: dict) -> None:
            body = {
                "model": "qwen3.5-4b-q6k", "temperature": 0.1, "max_tokens": args.max_tokens,
                "messages": [
                    {"role": "system", "content": data["system"]},
                    {"role": "user", "content": extraction_user_prompt(
                        page["topic"], page["city"], page["url"], page["text"][:8000])},
                ],
                "response_format": {"type": "json_schema", "json_schema": {
                    "name": "communities", "schema": EXTRACTION_SCHEMA}},
            }
            async with sem:
                t0 = time.monotonic()
                r = await client.post(f"{args.url}/v1/chat/completions", json=body,
                                      headers={"Authorization": f"Bearer {key}"})
                dt = time.monotonic() - t0
            j = r.json()
            results.append({
                "i": i, "url": page["url"], "seconds": dt,
                "completion_tokens": (j.get("usage") or {}).get("completion_tokens", 0),
                "content": ((j.get("choices") or [{}])[0].get("message") or {}).get("content", ""),
                "draft": (j.get("timings") or {}).get("draft_n"),
                "draft_accepted": (j.get("timings") or {}).get("draft_n_accepted"),
            })

        t0 = time.monotonic()
        await asyncio.gather(*[one(i, p) for i, p in enumerate(jobs)])
        wall = time.monotonic() - t0

    toks = sum(r["completion_tokens"] for r in results)
    lat = [r["seconds"] for r in results]
    drafted = sum(r["draft"] or 0 for r in results)
    accepted = sum(r["draft_accepted"] or 0 for r in results)
    print(f"{args.label:24} requests={len(results)} conc={args.concurrency} wall={wall:6.1f}s "
          f"gen_tokens={toks} agg_tok/s={toks / wall:5.1f} pages/hour={len(results) / wall * 3600:6.0f} "
          f"latency median={statistics.median(lat):5.1f}s max={max(lat):5.1f}s"
          + (f" draft_accept={accepted / drafted:.0%}" if drafted else ""))
    if args.save:
        Path(args.save).write_text(json.dumps(sorted(results, key=lambda r: r["i"]),
                                              ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
