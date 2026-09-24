"""Staged description enrichment (short + long).

For thin community pages, generates a one-line `short_description` (cards/meta) and
a ~200-word `long_description` (page body) from the community's own page text — the
biggest re-indexing lever (see docs/wiki `description-enrichment-plan`). The two
fields are separate from the extractor's `description`, so a re-extraction can't
revert them (`_merge_source_urls` preserves them). Bounded + admin-triggered — never
an auto-run — so the corpus changes gradually (the 2026-06 corpus-churn lesson) and
a human reviews output before scaling.

`long_description` present is the durable "already enriched" marker.
"""
from __future__ import annotations

from datetime import datetime, timezone

import structlog

from .db import (
    get_enrichment_candidates,
    mark_enrichment_attempted,
    update_community_enrichment,
)
from .extract import ExtractorContentError
from .fetch import fetch_and_clean

log = structlog.get_logger()

MAX_BATCH = 500          # hard ceiling per run; enrichment is gradual by design
_MIN_LONG_WORDS = 60     # accept a long_description only if it is genuinely richer
_MIN_LONG_CHARS = 200    # char floor so CJK output (few spaces) isn't wrongly rejected
_MAX_SHORT_CHARS = 140

_REFUSAL_MARKERS = (
    "i cannot", "i can't", "as an ai", "i'm sorry", "sajnos nem", "nem tudok",
    "insufficient information", "nincs elég", "cannot generate",
)


def _is_refusal(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in _REFUSAL_MARKERS)


def validate(short: str, long: str) -> tuple[str, str] | None:
    """Return cleaned (short, long) if acceptable, else None. Guards against junk,
    refusals, and over-long 'short' text before anything is published."""
    short, long = (short or "").strip(), (long or "").strip()
    # word floor for space-delimited languages, char floor for CJK (few/no spaces)
    long_enough = len(long.split()) >= _MIN_LONG_WORDS or len(long) >= _MIN_LONG_CHARS
    if not long_enough or _is_refusal(long) or _is_refusal(short):
        return None
    if not short:
        # derive a minimal short from the long's first sentence if the model omitted it
        short = long.split(".")[0].strip()
    short = short[:_MAX_SHORT_CHARS].strip()
    if not short:
        return None
    return short, long


def _count_attempts(db_path, n: int) -> None:
    """`n` enrichment provider attempts, on the UTC day they were made.

    Attempts, not successes, because that is the unit a provider's daily
    allowance is denominated in: a refused call spends a slot too. The report
    subtracts this from the fleet's total attempts to get extraction's share,
    so counting successes here and dividing by an attempt budget would mix two
    units and overstate capacity — on a day with 47% refusals, materially.

    Per attempt, not per batch: the batch total was written after the loop, so
    a cancelled run — which the admin stop route makes routine — left the
    records enriched, the ledger charged, and the counter empty. Per attempt
    also stamps the right day for a batch running through midnight, which the
    ledger already does; a batch-end stamp moved an evening's spend into the
    next morning's report.

    **Zero is a real answer.** This used to floor at `max(1, n)`, so a
    description that never reached a provider — the fleet paced out, the breaker
    open, the quota spent, all of which make `write_descriptions` raise before
    any call — still booked one attempt. Those are exactly the moments a busy
    refusing fleet produces in bulk, so the phantom count grew with the refusal
    rate: 252 unaccounted attempts on 2026-09-10, 464 on 2026-09-11, against a
    ledger that had seen neither. The caller decides what a missing counter
    means; this function only records what it is given.
    """
    n = int(n)
    if n <= 0:
        return
    from .db import bump_daily_counter
    bump_daily_counter(db_path, datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                       "enrich_attempts", n)


async def enrich_batch(
    db_path, extractor, city_names: set[str], limit: int = 20,
    dry_run: bool = False, fetch_missing: bool = True,
    blocked_domains: list[str] | None = None,
    deadline: datetime | None = None,
) -> dict:
    """Enrich up to `limit` un-enriched communities in `city_names` (those without a
    long_description). Uses cached source raw_text, or fetches the page fresh when
    missing (if `fetch_missing`). Returns stats + before/after samples for review.

    `deadline` (UTC) is a hard off-peak cutoff: the managed job passes its
    window-end so a batch started near the boundary stops issuing paid LLM calls
    the moment the discount window closes, rather than running a whole `limit`-long
    round of hundreds of calls into peak pricing (`stopped_at_deadline` in stats)."""
    limit = max(0, min(limit, MAX_BATCH))
    pool = get_enrichment_candidates(
        db_path, set(city_names), min(MAX_BATCH, max(limit * 3, limit)))
    stats = {"pool": len(pool), "enriched": 0, "skipped": 0, "no_source": 0,
             "failed": 0, "dry_run": dry_run, "stopped_at_deadline": False,
             "stopped_no_provider": False, "stopped_rate_limited": False,
             "samples": []}
    for c in pool:
        if stats["enriched"] >= limit:
            break
        if deadline is not None and datetime.now(timezone.utc) >= deadline:
            # off-peak window closed mid-round — stop before any further paid call
            stats["stopped_at_deadline"] = True
            break
        text = c.get("raw_text")
        if not text and fetch_missing:
            # try each source in turn — the first may be blocked/dead while a
            # later one is reachable
            for url in c.get("source_urls") or []:
                try:
                    text = await fetch_and_clean(url, blocked_domains or [])
                except Exception as exc:
                    log.warning("enrich_fetch_failed", url=url, error=str(exc))
                    text = None
                if text and len(text) >= 300:
                    break
        if not text or len(text) < 300:
            stats["no_source"] += 1
            if not dry_run:
                mark_enrichment_attempted(db_path, c["record_key"])
            continue
        # `calls_made` counts *provider* attempts, and one logical call can walk
        # several providers down the fallback chain. The ledger counts the same
        # attempts, so the report can only subtract like from like — counting
        # one per description under-subtracts exactly as often as the fleet
        # fails over, which on 2026-08-23 was 858 attempts in 1,794.
        # Whether the extractor counts provider attempts at all is a property
        # of the object, not of how this call went — so it is asked once, here,
        # and not inferred later from a delta of zero. A zero delta on a real
        # extractor means "no provider was called", which is information; on a
        # stub that has no counter it would mean nothing at all.
        _tracks_attempts = hasattr(extractor, "calls_made")
        _attempts_before = int(getattr(extractor, "calls_made", 0) or 0)
        try:
            res = await extractor.write_descriptions(
                c["name"], c["city"], c["topic"], c.get("locale", "hu"), text)
        except ExtractorContentError as exc:
            # Every model that answered wrote something unusable for this page.
            # Same verdict as an answer that fails `validate`: mark it, so the
            # next batch does not pay the whole fleet to learn it again.
            log.info("enrich_unusable_answer", name=c["name"], city=c["city"], error=str(exc))
            stats["skipped"] += 1
            if not dry_run:
                mark_enrichment_attempted(db_path, c["record_key"])
            continue
        except Exception as exc:
            log.warning("enrich_call_failed", name=c["name"], city=c["city"], error=str(exc))
            stats["failed"] += 1
            # A dead fleet is not a per-record problem: on 2026-08-17 the
            # breaker opened and the loop logged 368 identical failures in
            # seconds, re-fetching a source page for each one. Nothing is
            # marked, so the records come back next round — but the fetches
            # are spent, and we hammer third-party sites for nothing.
            if getattr(extractor, "rate_limited_out", False):
                # Every provider is inside a per-minute window. That is a wait,
                # not an ending: on 2026-08-18 a 60-second limit ended the whole
                # 9.5-hour enrichment window after 73 records. Stop the batch so
                # the caller can pause, and say plainly that it may come back.
                stats["stopped_rate_limited"] = True
                log.info("enrich_paused_rate_limited", failed=stats["failed"])
                break
            if (getattr(extractor, "providers_down", False)
                    or getattr(extractor, "quota_exhausted", False)
                    or getattr(extractor, "exhausted", False)):
                stats["stopped_no_provider"] = True
                log.warning("enrich_stopped_no_provider", failed=stats["failed"])
                break
            continue  # transient provider error — do NOT mark; retry next batch
        finally:
            # `finally`, so the attempts are recorded however the call ended —
            # including `asyncio.CancelledError`, which is a BaseException and
            # never reaches the `except Exception` above.
            _count_attempts(
                db_path,
                (int(getattr(extractor, "calls_made", 0) or 0) - _attempts_before)
                if _tracks_attempts else 1)
        ok = validate(res.get("short_description", ""), res.get("long_description", ""))
        if not ok:
            stats["skipped"] += 1
            if not dry_run:
                mark_enrichment_attempted(db_path, c["record_key"])
            continue
        short, long = ok
        if not dry_run:
            update_community_enrichment(db_path, c["record_key"], short, long)
        stats["enriched"] += 1
        if len(stats["samples"]) < 10:
            stats["samples"].append({
                "name": c["name"], "city": c["city"], "topic": c["topic"],
                "old": c["description"], "short": short, "long": long,
                "old_words": len((c["description"] or "").split()),
                "long_words": len(long.split()),
            })
    # Named so a day's total can be counted without inference. "Why did we
    # modify existing communities instead of processing new ones?" took a
    # correlation between two report rows to answer on 2026-08-20; this makes
    # it a grep.
    log.info("enrich_batch_done", **{k: v for k, v in stats.items() if k != "samples"})
    if stats["enriched"]:
        log.info("enrich_records_updated", count=stats["enriched"],
                 cities_in_scope=len(city_names),
                 sample_city=next(iter(sorted(city_names)), ""))
    return stats
