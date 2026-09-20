"""A/B measurement: today's extraction path against a cheaper one.

The question, posed 2026-09-20 after the call structure was measured: a useful
page costs **5.82 LLM calls** today (communities, then venues, then persons,
then inline enrichment per record), against a free fleet that allows ~2,100 a
day. Three changes were proposed. This measures them together, on pages whose
current answer we already have, so quality can be compared and not just cost:

1. **A Jev gate.** One $0.0001 typed question decides whether the page is worth
   extracting at all. 81.8% of extracted pages yield nothing; measured recall
   at threshold 0.06 was 100% on a 1,200-page sample.
2. **Combined extraction.** One call returning communities, venues and people
   instead of three.
3. Inline enrichment is simply **not run** on either arm. Its cost is already
   measured (`report_inline_enrichment.py`: 2.82 calls and 2.03 paid searches
   per useful page, 42.5% yield) and running it here would spend DataForSEO
   money to re-learn that.

**Nothing here is stored.** No cache write, no `communities` row, no run
record. The output is a JSON report, and the pages keep whatever extraction
they already had — the control arm is read from that cache rather than
re-extracted, because paying twice to reproduce a known answer measures
nothing.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import structlog

from .db import load_cache_page
from .identity import public_slug

log = structlog.get_logger()

#: Cloudflare Workers AI serves Jev as a partner model, billed from the AI
#: Gateway's prepaid balance (see docs/wiki/.../jev-joinability-gate.md).
CF_URL_TEMPLATE = "https://api.cloudflare.com/client/v4/accounts/{account}/ai/run"
CF_ACCOUNT_ID = "809bd7dcd8939fa5e520a96ab4923429"
JEV_MODEL = "typesafe/jev"

#: Reviewed by hand on 2026-09-20: every page the gate rejected below this was
#: one where the *incumbent* extractor was wrong. 0.10 is where genuine
#: communities start to fall out.
DEFAULT_GATE_THRESHOLD = 0.06

#: Three lists in one answer need more room than one. The production cap is
#: 1,500; a truncated answer is a failed extraction, and this experiment must
#: not fail for a reason it introduced itself.
COMBINED_MAX_OUTPUT_TOKENS = 3000


@dataclass
class PageResult:
    url: str
    city: str
    topic: str
    #: What the current pipeline has cached for this page.
    baseline_names: list[str] = field(default_factory=list)
    baseline_venues: int = 0
    baseline_persons: int = 0
    #: What the experiment produced.
    gate_score: float | None = None
    gated_out: bool = False
    combined_names: list[str] = field(default_factory=list)
    combined_venues: int = 0
    combined_persons: int = 0
    llm_calls: int = 0
    jev_tokens: int = 0
    error: str | None = None
    seconds: float = 0.0


async def jev_gate(client: httpx.AsyncClient, url: str, text: str, city: str,
                   topic: str, account: str, token: str) -> tuple[float, int]:
    """One typed question. Returns (probability, input tokens billed)."""
    payload = {
        "model": JEV_MODEL,
        "input": {
            "state": {"city": city, "topic": topic, "source_url": url,
                      "page_text": text},
            "questions": {
                "has_joinable_community": {
                    "type": "noul",
                    "instructions": (
                        "Does this page provide evidence of at least one genuine "
                        "community group or club in or near the specified city and "
                        "relevant to the specified topic, where that same group meets "
                        "or organizes activities regularly, is open to new members "
                        "from the general public, and has a group identity rather than "
                        "being only a venue, commercial course, professional ensemble, "
                        "one-time event, annual event, news article, or directory entry "
                        "for another city?"
                    ),
                },
            },
        },
    }
    response = await client.post(
        CF_URL_TEMPLATE.format(account=account), json=payload,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
    )
    response.raise_for_status()
    body = response.json()
    for _ in range(3):                      # gateway envelope wraps the model's
        if "answers" in body or not isinstance(body.get("result"), dict):
            break
        body = body["result"]
    answers = body.get("answers") or {}
    if not answers:
        raise RuntimeError(f"no answers from Jev: {json.dumps(body)[:200]}")
    usage = (body.get("usage") or {}).get("input_tokens", 0)
    return float(answers["has_joinable_community"]["noul"]), int(usage)


def _names(records: list) -> list[str]:
    out = []
    for r in records:
        name = r.get("name") if isinstance(r, dict) else getattr(r, "name", "")
        if name:
            out.append(name)
    return out


def compare(baseline: list[str], found: list[str]) -> dict:
    """Name-level agreement between the two arms.

    Compared on `public_slug`, which folds accents and punctuation, rather than
    on the database's own `normalized_match_key`, which does not: that key is
    deliberately accent-sensitive so "Kör" and "Kor" stay distinct records. Two
    models writing the same Hungarian name with and without accents would then
    read as one community lost and one gained, which is a measurement artifact
    and not a difference in what was found.

    Kept to exact-after-folding on purpose: a fuzzy ratio would produce a
    number nobody can check by eye, and this table is going to be read by eye.
    """
    a = {public_slug(n) for n in baseline if n}
    b = {public_slug(n) for n in found if n}
    return {
        "baseline": len(a),
        "found": len(b),
        "kept": len(a & b),
        "lost": sorted(n for n in baseline if public_slug(n) in a - b),
        "new": sorted(n for n in found if public_slug(n) in b - a),
    }


async def run_page(page: dict, extractor, client: httpx.AsyncClient,
                   account: str, token: str, threshold: float,
                   valid_topics: list[str] | None) -> PageResult:
    url = page.get("url", "")
    result = PageResult(url=url, city=page.get("city", ""),
                        topic=page.get("topic", ""))
    text = page.get("raw_text") or ""
    result.baseline_names = _names(page.get("records") or [])
    result.baseline_venues = len(page.get("venues_data") or [])
    result.baseline_persons = sum(
        len(v) for v in (page.get("persons_data") or {}).values())

    started = time.monotonic()
    try:
        if token:
            score, tokens = await jev_gate(client, url, text[:8000], result.city,
                                           result.topic, account, token)
            result.gate_score, result.jev_tokens = score, tokens
            if score < threshold:
                result.gated_out = True
                result.seconds = time.monotonic() - started
                return result

        communities, venues, persons = await extractor.extract_all(
            text, result.city, result.topic, page.get("locale") or "hu", url,
            valid_topics=valid_topics,
            max_output_tokens=COMBINED_MAX_OUTPUT_TOKENS,
        )
        result.llm_calls = 1
        result.combined_names = _names(communities)
        result.combined_venues = len(venues)
        result.combined_persons = len(persons)
    except Exception as exc:                # every failure is data here
        result.error = f"{type(exc).__name__}: {exc}"[:200]
    result.seconds = time.monotonic() - started
    return result


def summarize(results: list[PageResult], threshold: float) -> dict:
    """The report. Cost is in calls, because calls are the scarce thing."""
    done = [r for r in results if not r.error]
    gated = [r for r in done if r.gated_out]
    ran = [r for r in done if not r.gated_out]

    # What the control arm cost for these same pages: one call for a page with
    # no communities, three for a page with any (venues and persons are skipped
    # when the first pass finds nothing — CLAUDE.md, "Person + venue extraction
    # skip"). Enrichment is excluded from both arms.
    baseline_calls = sum(3 if r.baseline_names else 1 for r in done)
    experiment_calls = sum(r.llm_calls for r in done)

    agreements = [compare(r.baseline_names, r.combined_names) for r in ran]
    kept = sum(a["kept"] for a in agreements)
    baseline_total = sum(a["baseline"] for a in agreements)
    found_total = sum(a["found"] for a in agreements)

    # A gated-out page whose baseline found communities is a real loss — that
    # is the number the whole gate stands or falls on.
    gate_losses = [r for r in gated if r.baseline_names]

    return {
        "pages": len(results),
        "errors": [r.error for r in results if r.error][:20],
        "error_count": sum(1 for r in results if r.error),
        "gate": {
            "threshold": threshold,
            "rejected": len(gated),
            "rejected_share": round(len(gated) / len(done), 4) if done else 0.0,
            "rejected_with_baseline_communities": len(gate_losses),
            "lost_urls": [r.url for r in gate_losses][:40],
            "jev_input_tokens": sum(r.jev_tokens for r in done),
            "jev_cost_usd": round(
                sum(r.jev_tokens for r in done) / 1e6 * 0.042, 4),
        },
        "calls": {
            "baseline": baseline_calls,
            "experiment": experiment_calls,
            "saved_share": round(1 - experiment_calls / baseline_calls, 4)
            if baseline_calls else 0.0,
        },
        "communities": {
            "baseline": baseline_total,
            "found": found_total,
            "kept": kept,
            "recall": round(kept / baseline_total, 4) if baseline_total else None,
            "extra": found_total - kept,
        },
        "venues": {
            "baseline": sum(r.baseline_venues for r in ran),
            "found": sum(r.combined_venues for r in ran),
        },
        "persons": {
            "baseline": sum(r.baseline_persons for r in ran),
            "found": sum(r.combined_persons for r in ran),
        },
        "lost_communities": [n for a in agreements for n in a["lost"]][:40],
        "new_communities": [n for a in agreements for n in a["new"]][:40],
        "seconds_total": round(sum(r.seconds for r in done), 1),
    }


async def run_ab_test(db_path: Path, pages: list[dict], extractor,
                      threshold: float = DEFAULT_GATE_THRESHOLD,
                      concurrency: int = 3,
                      valid_topics: list[str] | None = None,
                      account: str | None = None) -> dict:
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    account = account or CF_ACCOUNT_ID
    if not token:
        log.warning("ab_test_no_gate", reason="CLOUDFLARE_API_TOKEN unset")
    semaphore = asyncio.Semaphore(max(1, concurrency))
    results: list[PageResult] = []

    async with httpx.AsyncClient(timeout=120) as client:
        async def one(page: dict) -> None:
            async with semaphore:
                results.append(await run_page(
                    page, extractor, client, account, token, threshold,
                    valid_topics))

        await asyncio.gather(*(one(p) for p in pages))

    report = summarize(results, threshold)
    log.info("ab_test_complete", pages=report["pages"],
             saved=report["calls"]["saved_share"],
             recall=report["communities"]["recall"])
    return report


def load_pages(db_path: Path, url_hashes: list[str]) -> list[dict]:
    """The control arm, read from cache rather than re-extracted."""
    out = []
    for h in url_hashes:
        entry = load_cache_page(db_path, h)
        if entry and entry.get("raw_text") and entry.get("records") is not None:
            out.append(entry)
    return out
