"""Score models that are not in the catalogue yet, on our own extraction task.

`POST /v1/score` measures the configured fleet only, so a candidate found by
`check_free_models.py` (a NEW model) used to need a catalogue entry and a deploy
before it could be measured — and an entry with a guessed `quality:` is routed
by that guess from the moment it ships. This measures first: it builds an
extractor for each candidate with a provider's existing key and runs the same
`scoring.score_fleet` over the same deterministic golden set, alongside the
provider's configured models as the baseline.

Spends real allowance: one call per model per page. Runs inside the production
container (the keys and the cached pages live there):

    ssh meetapedia-prod "docker exec -i -w /app -e PYTHONPATH=/app <container> \
        python3 - --provider openrouter --locale hu --pages 4 \
        --model google/gemma-4-26b-a4b-it:free --model ..." < scripts/score_candidates.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from scraper.providers import OpenAICompatExtractor, load_catalogue
from scraper.scoring import score_fleet


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", required=True)
    ap.add_argument("--model", action="append", default=[], help="candidate; repeat")
    ap.add_argument("--pages", type=int, default=4)
    ap.add_argument("--locale", default="hu")
    ap.add_argument("--db", default="/app/data/scraper.db")
    ap.add_argument("--no-baseline", action="store_true")
    args = ap.parse_args()

    spec = next(p for p in load_catalogue().providers if p.name == args.provider)
    configured = [] if args.no_baseline else [m.model for m in spec.models]

    def _mk(model: str) -> OpenAICompatExtractor:
        return OpenAICompatExtractor(
            provider=spec.name, base_url=spec.url, api_key=spec.api_key, model=model,
            quality=0, timeout_seconds=spec.timeout_seconds or 120,
            max_output_tokens=spec.max_output_tokens or 1500,
            stream=spec.stream, json_schema=spec.json_schema,
            fingerprint_model="deepseek-chat")

    fleet = [_mk(m) for m in configured + args.model]
    out = asyncio.run(score_fleet(Path(args.db), fleet, pages=args.pages,
                                  locale=args.locale or None))
    print(json.dumps({k: out.get(k) for k in ("pages", "locale", "sample")}))
    for r in out.get("results", []):
        tag = "configured" if r.get("model") in configured else "CANDIDATE"
        print(f"{tag:10} {r.get('model')!s:55} score={r.get('score')} "
              f"answered={r.get('answered')} failed={r.get('failed')}")


if __name__ == "__main__":
    main()
