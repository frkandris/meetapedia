#!/usr/bin/env python3
"""Rank recent paid models by what one dollar buys in communities extracted.

Takes OpenRouter's public catalogue (`curl https://openrouter.ai/api/v1/models
-o models.json`) and turns each model's per-token price into:

    pages per $1        = 1 / (in_tokens * price_in + out_tokens * price_out)
    communities per $1  = pages per $1 * communities per page

The unit numbers are measured, not assumed — re-measure them with
`scripts/daily_work.py` (communities per page) and the llama-server timing log
of `tools/localgpu-governor/bench.py` (tokens). Defaults are the 2026-10-22..
11-01 production figures: 0.40 communities per extracted page; ~2,500 input
tokens (system prompt + up to 8,000 chars of page) and ~300 output tokens.

Price is not quality: a model must still be scored on the golden set
(`POST /v1/score`) before it goes into `providers.yaml`.

    .venv/bin/python scripts/model_cost_per_community.py models.json --since 2026-09-15
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("catalogue")
    ap.add_argument("--since", default="", help="only models created on/after YYYY-MM-DD")
    ap.add_argument("--in-tokens", type=float, default=2500)
    ap.add_argument("--out-tokens", type=float, default=300)
    ap.add_argument("--per-page", type=float, default=0.40)
    ap.add_argument("--max-out-price", type=float, default=2.0, help="USD per 1M output tokens")
    ap.add_argument("--top", type=int, default=30)
    args = ap.parse_args()

    since = (datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc).timestamp()
             if args.since else 0)
    rows = []
    for m in json.loads(Path(args.catalogue).read_text())["data"]:
        p = m.get("pricing") or {}
        try:
            pin, pout = float(p.get("prompt", 0)), float(p.get("completion", 0))
        except ValueError:
            continue
        if pin <= 0 or pout <= 0 or pout * 1e6 > args.max_out_price:
            continue  # free variants are routed separately; skip the expensive tier
        if (m.get("created") or 0) < since or ":" in m["id"]:
            continue
        page = args.in_tokens * pin + args.out_tokens * pout
        rows.append((args.per_page / page, 1 / page, m))
    rows.sort(key=lambda r: -r[0])
    print(f"{'model':48} {'created':10} {'$in/M':>6} {'$out/M':>6} {'ctx':>7} "
          f"{'pages/$':>8} {'comm/$':>7} {'$/1k comm':>9}")
    for comm, pages, m in rows[:args.top]:
        p = m["pricing"]
        print(f"{m['id'][:48]:48} "
              f"{datetime.fromtimestamp(m['created'], timezone.utc):%Y-%m-%d} "
              f"{float(p['prompt']) * 1e6:6.3f} {float(p['completion']) * 1e6:6.3f} "
              f"{m.get('context_length') or 0:7} {pages:8.0f} {comm:7.0f} {1000 / comm:9.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
