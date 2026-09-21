import asyncio
import hashlib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, NamedTuple

import structlog

from .extract import (DeepSeekExtractor, ExtractorContentError,
                      ExtractorUnavailableError, FallbackExtractor,
                      get_extract_fingerprint, get_person_fingerprint,
                      get_venue_fingerprint)
from .false_positives import build_prompt_section
from .false_positives import load as load_false_positives
from .fetch import fetch_and_clean
from .search import (DataForSEOClient, FallbackSearchClient, SearchQuotaError,
                     SearchUnavailableError, build_queries)
from .db import get_daily_counters_with_prefix, get_search_cache, save_search_cache, mark_search_collection_complete, get_collected_pairs, get_covered_pairs, upsert_venues, upsert_persons, delete_leader_persons_for_community, load_cache_page, find_community_by_id, get_fully_processed_pairs, get_upgradable_pages
from .router import build_router
from .store import save_results

if TYPE_CHECKING:
    from .cache import CacheManager
    from .gate import JoinabilityGate
    from .models import CommunityRecord

log = structlog.get_logger()


def _needs_enrichment(record: "CommunityRecord") -> bool:
    return not record.website and not record.social_links and not record.contact


_TITLE_PREFIXES = frozenset({"dr", "dr.", "prof", "prof.", "ifj", "ifj.", "id", "id."})


def _is_name_segment(s: str) -> bool:
    """Return True if s looks like a name (not a role/description)."""
    s = s.strip()
    if not s:
        return False
    first = s.split()[0].rstrip(".").lower()
    if first in _TITLE_PREFIXES:
        return True
    return bool(re.match(r"^[A-ZÁÉÍÓÖŐÚÜŰ]", s))


def _parse_leader_field(leader: str) -> list[tuple[str, str]]:
    """
    Parse a free-text leader field into (name, role_description) pairs.

    Handles patterns like:
    - "Name, role"                    → one person
    - "Name1, Name2, Name3 (role)"   → multiple people, collective role
    - "Name1, role1; Name2, role2"   → two people with individual roles
    - "Name1, Name2"                  → multiple people, no role
    """
    leader = leader.strip()

    # Extract collective role from trailing parentheses when it has no uppercase
    # e.g. "Name1, Name2 (játékmesterek)" — but NOT "Name (Yang Yajian alias)"
    collective_role = ""
    m = re.search(r"\(([^)]+)\)\s*$", leader)
    if m and not re.search(r"[A-ZÁÉÍÓÖŐÚÜŰ]", m.group(1)):
        collective_role = m.group(1).strip()
        leader = leader[: m.start()].strip().rstrip(",").strip()

    results: list[tuple[str, str]] = []
    for seg in re.split(r"\s*;\s*", leader):
        seg = seg.strip()
        if not seg:
            continue

        comma_parts = [p.strip() for p in seg.split(",")]
        name_parts: list[str] = []
        role_parts: list[str] = []

        for part in comma_parts:
            if not part:
                continue
            if role_parts or not _is_name_segment(part):
                role_parts.append(part)
            else:
                name_parts.append(part)

        role_desc = ", ".join(role_parts).strip() or collective_role
        if not name_parts:
            continue
        if len(name_parts) == 1:
            results.append((name_parts[0], role_desc))
        else:
            # Multiple names in one segment — collective role applies to each
            for name in name_parts:
                results.append((name, collective_role))

    return results


def _persons_from_leaders(records: "list[CommunityRecord]", city_name: str, topic_name: str) -> list:
    from .models import PersonRecord
    from datetime import datetime, timezone
    extracted_at = datetime.now(timezone.utc).isoformat()
    persons = []
    for rec in records:
        if not rec.leader:
            continue
        for name, role_desc in _parse_leader_field(rec.leader):
            name = name.strip()
            if not name:
                continue
            try:
                persons.append(PersonRecord(
                    name=name,
                    role="leader",
                    bio=role_desc or None,
                    city=city_name,
                    topic=topic_name,
                    community_name=rec.name,
                    community_id=rec.community_id or "",
                    source_url=rec.source_url,
                    extracted_at=extracted_at,
                ))
            except Exception:
                pass
    return persons


async def _enrich_record(
    record: "CommunityRecord",
    searxng: "FallbackSearchClient",
    extractor: Any,
    config: "PipelineConfig",
    semaphore: asyncio.Semaphore,
    on_progress: "Callable[[str | None, str | None], None] | None" = None,
    timing: "dict | None" = None,
    enrich_log_entry: "dict | None" = None,
    enrich_fp_examples: str = "",
) -> "CommunityRecord":
    """timing dict accumulates {"scrape": s, "extract": s, "count": n} across calls."""
    query = f'"{record.name}" {record.city}'
    if enrich_log_entry is not None:
        enrich_log_entry["search_query"] = query
    try:
        results = await searxng.search_all([query], locale=record.locale, num_results=3)
    except Exception:
        return record

    for result in results[:2]:
        url_log: dict = {"url": result.url, "fetched": False, "success": False}
        if enrich_log_entry is not None:
            enrich_log_entry["research_urls"].append(url_log)

        if on_progress:
            on_progress("enrich_scrape", record.source_url)
        t0 = time.monotonic()
        text = await fetch_and_clean(
            result.url, config.fetch_blocked_domains,
            config.fetch_timeout, config.fetch_min_text_length,
            semaphore,
        )
        if timing is not None:
            timing["scrape"] += time.monotonic() - t0
        if on_progress:
            on_progress(None, None)
        if not text:
            continue

        url_log["fetched"] = True

        if on_progress:
            on_progress("enrich_extract", record.source_url)
        t0 = time.monotonic()
        try:
            enriched = await extractor.enrich(record, text, false_positive_examples=enrich_fp_examples)
        except ExtractorUnavailableError as exc:
            log.warning("enrich_unavailable_skipped", community=record.name, reason=str(exc))
            return record  # best-effort: record proceeds unenriched
        finally:
            if timing is not None:
                timing["extract"] += time.monotonic() - t0
            if on_progress:
                on_progress(None, None)

        if enriched.website or enriched.social_links or enriched.contact:
            if enrich_log_entry is not None:
                fields: list[str] = []
                if not record.website and enriched.website:
                    fields.append("website")
                if not record.contact and enriched.contact:
                    fields.append("contact")
                if not record.social_links and enriched.social_links:
                    fields.append("social_links")
                enrich_log_entry["enriched"] = True
                enrich_log_entry["fields_added"] = fields
                url_log["success"] = True
            if timing is not None:
                timing["count"] += 1
            log.info("enriched", community=record.name, city=record.city, source=result.url)
            return enriched

    return record


def _should_stop(stop_at, should_stop: "Callable[[], bool] | None" = None) -> bool:
    """True when this run should wind up between pairs.

    Two independent reasons, checked at the same points. `stop_at` is a
    deadline; `should_stop` is a question the caller answers — "has the fleet's
    quota come back, so extraction should take over from collection?" — which is
    what lets the scheduler pre-empt a long run without giving it a clock.
    """
    if should_stop is not None:
        try:
            if should_stop():
                return True
        except Exception as exc:  # a broken predicate must not end a run
            log.warning("should_stop_failed", error=str(exc))
    return _window_closed(stop_at)


def _window_closed(stop_at) -> bool:
    """True when a stop_at deadline is set and has passed (aware UTC datetime)."""
    if stop_at is None:
        return False
    from datetime import datetime, timezone
    return datetime.now(timezone.utc) >= stop_at


def _new_pair_log(city_name: str, topic_name: str, queries: list[str]) -> dict:
    """Full-key pair log. run_detail.html sums/iterates these keys with strict
    Jinja Undefined — every pair log (including failure entries) must carry all
    of them."""
    return {
        "city": city_name,
        "topic": topic_name,
        "queries": queries,
        "search_cache_hit": False,
        "urls_found": 0,
        "fetched_urls": [],
        "fetch_failed": 0,
        "cache_hits_scrape": 0,
        "cache_hits_extract": 0,
        "records_extracted": 0,
        "search_failed": False,
        "search_error": None,
        "extract_failed": 0,
        "extract_error": None,
        # Pages a joinability gate declined to extract. Not a failure: nothing
        # went wrong, this run chose not to spend a call.
        "gate_skipped": 0,
        # True only on the marker entry a provider-death abort leaves behind.
        # An ordinary per-pair failure sets search_failed/extract_failed and the
        # loop continues — see classify_run_outcome().
        "aborted": False,
    }


#: Run outcomes, coarsest last. Persisted in `runs.outcome`.
RUN_OK = "ok"            # everything the run attempted succeeded
RUN_WARNING = "warning"  # finished, but some pairs/pages failed and will be retried
RUN_ABORTED = "aborted"  # stopped early: dead provider, or a top-level exception


#: How many times a pair's skipped pages may be re-offered when the fleet turns
#: out to be healthy after all. A small bound: the point is to recover from a
#: momentary stop, not to retry into one.
_MAX_EXTRACT_ROUNDS = 3


#: One writer at a time. SQLite allows exactly one anyway, and moving these
#: calls into threads removed the accidental serialisation the event loop used
#: to provide: `save_results` reads, merges and replaces a whole topic
#: non-atomically, so two overlapping saves could drop one side's extractions.
#: Serialising costs nothing real — the writes are short and no longer hold the
#: loop, which is the part that mattered.
_db_write_lock: "asyncio.Lock | None" = None


async def _off_loop(fn, *args, **kwargs):
    """Run a blocking database call without stopping the world.

    SQLite calls are synchronous, and the pipeline made fourteen of them
    directly inside coroutines. Serially that was tolerable; with eight
    searches and four extractions in flight it pushed `/healthz` to six
    seconds — and the Docker liveness probe kills a container that answers
    slowly, which makes Traefik drop the route and every visitor gets a 404.
    That is the 2026-08-18 outage, three steps upstream of where it hurt.

    The event loop also serves the public site. Anything that holds it for
    hundreds of milliseconds is taking that time from visitors.
    """
    global _db_write_lock
    if _db_write_lock is None:
        _db_write_lock = asyncio.Lock()  # created inside the loop that uses it
    async with _db_write_lock:
        return await asyncio.to_thread(lambda: fn(*args, **kwargs))


class _Quarantine:
    """Pages that keep failing the same way, and are no longer paid to re-fail.

    An extraction that fails is never cached — caching it would record "this
    page has 0 communities" permanently under the current fingerprint, and that
    is unrecoverable data loss. The price of that rule is that a page which
    fails *deterministically* is attempted again by every run: on 2026-08-26,
    ~21 pages against ~30 runs, each walking the whole provider fleet. While
    the fleet was free that was wasted quota; with paid providers on it was
    most of the day's bill, and none of it could ever succeed.

    So this is not a cache of the failure — it is a memory of *having tried*.
    The fingerprint is part of the key, so any prompt or model change releases
    every page automatically, which is exactly the change that could produce a
    different answer. Only `ExtractorContentError` counts: a model answered and
    what it said was unusable. An outage, a 429 or a spent quota says nothing
    about the page, and counting those would quarantine the corpus over one bad
    afternoon.
    """

    def __init__(self, cache, fingerprint: str, threshold: int):
        self.cache = cache
        self.fingerprint = fingerprint
        #: 0 or less disables the quarantine — the escape hatch if it is ever
        #: found to be hiding work that would now succeed. A run with no cache
        #: at all (`_run_full` allows one) disables it the same way: there is
        #: nowhere to read the counts from and nowhere to write them.
        self.threshold = int(threshold or 0) if cache is not None else 0
        self._counts: dict[str, int] = {}
        if self.threshold > 0:
            try:
                self._counts = cache.extract_failure_counts(fingerprint)
            except Exception as exc:  # a broken quarantine must not stop a run
                log.warning("extract_quarantine_unreadable", error=str(exc))

    @property
    def size(self) -> int:
        if self.threshold <= 0:
            return 0
        return sum(1 for c in self._counts.values() if c >= self.threshold)

    def holds(self, url: str) -> bool:
        if self.threshold <= 0:
            return False
        return self._counts.get(self.cache.url_hash(url), 0) >= self.threshold

    async def note(self, url: str, error: BaseException) -> None:
        """Count one content failure against a page."""
        if self.threshold <= 0 or not isinstance(error, ExtractorContentError):
            return
        try:
            count = await _off_loop(self.cache.note_extract_failure, url,
                                    self.fingerprint, str(error))
        except Exception as exc:
            log.warning("extract_quarantine_write_failed", url=url, error=str(exc))
            return
        self._counts[self.cache.url_hash(url)] = count
        if count == self.threshold:
            # Once, at the moment it happens. The page is silent from here on,
            # so this line and the admin list are the only places it appears.
            log.warning("extract_page_quarantined", url=url, failures=count,
                        threshold=self.threshold, reason=str(error)[:200])

    async def forgive(self, url: str) -> None:
        """The page just extracted — drop whatever it had accumulated.

        Only writes for a page that actually has a row: a success is the common
        case and must not cost a database write.
        """
        if self.threshold <= 0:
            return
        if self._counts.pop(self.cache.url_hash(url), 0):
            try:
                await _off_loop(self.cache.clear_extract_failure, url, self.fingerprint)
            except Exception as exc:
                log.warning("extract_quarantine_clear_failed", url=url, error=str(exc))


async def _extract_pair_pages(
    extractor, pages, *, city, topic, fp_section, concurrency, on_progress,
) -> "tuple[dict[str, Any], tuple[str, bool] | None]":
    """Community extraction for one pair's uncached pages, several at a time.

    Returns `({url: (records, model, quality) | Exception}, stop, deferred)`.
    `stop` is why it gave up, or None if the fleet was fine at the end;
    `deferred` holds urls that never got a turn. Both are needed: a page can be
    left over with no stop reason at all — each round a call retires the last
    provider, the queue steps aside, and a sibling already in flight revives it —
    and "the fleet was never usable" must stay distinguishable from "this page
    kept losing the race", because only the first is a failure.

    The pages of a pair are independent — nothing one extracts affects another —
    so the serial loop was not expressing a constraint, only its own shape. It
    cost the 2026-08-17 window 3.3 extractions/min against a fleet whose
    combined ceiling is 185 calls/min (arXiv:2504.07347: idle capacity under a
    solvable constraint is lost throughput). Concurrency is bounded by
    `pipeline.extract_concurrency` and, above that, by the router refusing a
    provider whose slot is already claimed.
    """
    results: dict[str, Any] = {}
    if not pages:
        return results, None, set()
    sem = asyncio.Semaphore(max(1, int(concurrency or 1)))
    pending = list(pages)
    skipped: list = []

    async def _one(url: str, text: str) -> None:
        async with sem:
            # Asked inside the slot, not once up front: the fleet can spend its
            # last quota on the page ahead of this one, and attempting the rest
            # would only collect identical failures.
            if _stop_reason(extractor) is not None:
                skipped.append((url, text))
                return
            if on_progress:
                on_progress("extract", url)
            try:
                results[url] = await _extract_traced(
                    extractor, text=text, city=city.name, topic=topic.name,
                    locale=city.locale, source_url=url,
                    false_positive_examples=fp_section,
                )
            except ExtractorUnavailableError as exc:
                results[url] = exc
            finally:
                if on_progress:
                    on_progress(None, None)

    # A stop seen mid-flight is a snapshot, not a verdict: a request already in
    # flight can revive the very provider whose failures caused it, and a
    # rate-limit pause seen early would otherwise mask a real outage that
    # happened later. The state after everything has landed is the one that
    # counts — and if it says the fleet is fine, the pages that stepped aside
    # get their turn instead of being written off.
    for _round in range(_MAX_EXTRACT_ROUNDS):
        skipped = []
        await asyncio.gather(*[_one(u, t) for u, t in pending])
        stop = _stop_reason(extractor)
        if stop is not None or not skipped:
            return results, stop, {u for u, _ in skipped}
        pending = skipped
    log.info("extract_pages_deferred", city=city.name, topic=topic.name,
             pages=len(skipped))
    return results, _stop_reason(extractor), {u for u, _ in skipped}


def _stop_reason(extractor) -> tuple[str, bool] | None:
    """Why extraction cannot continue: (reason, is_outage), or None if it can.

    Pure — callers decide what to record. An earlier version marked the pair log
    itself, which meant asking the question from a context with no pair log
    silently discarded the answer.

    The community branch guards on these flags before calling, but venue and
    person extraction did not — and since the flags describe a single attempt,
    a pause raised by a venue call was cleared by the next call before anything
    read it, so the pass walked every remaining page instead of stopping.

    Only `providers_down` is an outage; the other two are the window ending
    normally and must not be reported as a failure.
    """
    if getattr(extractor, "providers_down", False):
        return (getattr(extractor, "failure_reason", None)
                or "no extraction provider available"), True
    if getattr(extractor, "rate_limited_out", False):
        return "all providers rate limited", False
    if getattr(extractor, "quota_exhausted", False):
        return "free-tier daily quota spent", False
    return None


def _mark_stop(extractor, pair_log: dict) -> str | None:
    """Record a stop on the pair log and return its reason, or None to carry on."""
    stop = _stop_reason(extractor)
    if stop is None:
        return None
    reason, is_outage = stop
    if is_outage:
        pair_log["extract_error"] = reason
        pair_log["aborted"] = True
    return reason


#: What the continuous worker should do next. Strings rather than an enum
#: because they are also the run modes the pipeline already speaks.
WORKER_WAIT, WORKER_EXTRACT, WORKER_COLLECT = "wait", "ai_only", "search_only"


def next_worker_action(*, is_running: bool, paused: bool, quota: bool,
                       extract_ready: bool) -> str:
    """Choose the next action from the four facts that decide it.

    Extracted from the worker loop so the choice can be tested as a choice.
    It was previously three nested conditions inside a closure, and the only
    way to check it was to assert on the source text — which passes when the
    logic is wrong and fails when a variable is renamed.

    The rule itself: free quota expires at midnight and collection costs money,
    so extraction goes first whenever there is budget and work to spend it on.
    """
    if is_running or paused:
        return WORKER_WAIT
    return WORKER_EXTRACT if (quota and extract_ready) else WORKER_COLLECT


def pages_worked(pair_logs: "list[dict]") -> int:
    """Pages this run newly extracted and cached.

    The worker's "was that pass worth anything?" question, and it has been
    answered wrongly twice:

    * `len(pair_logs)` — `ai_only` logs a pair even when it has no cached
      pages, and every never-searched pair is in the filter, so an empty pass
      looked busy and extraction ran forever while the collector never did;
    * `urls_found > cache_hits_extract` — a page that *fails* counted, and a
      failure caches nothing, so the next run found the identical state. One
      permanently failing page drove about a hundred runs on 2026-08-18.

    Found, minus served from cache, minus failed, minus quarantined. The last
    one for exactly the reason in the second bullet: a quarantined page caches
    nothing either, so counting it as outstanding would keep the worker
    starting extraction passes that can only skip it again — the same empty
    loop, one guard later.

    A module-level function because it is the part worth testing, and testing
    it through the worker loop means asserting on source text rather than on
    the answer.
    """
    return sum(
        max(0, int(p.get("urls_found") or 0)
               - int(p.get("cache_hits_extract") or 0)
               - int(p.get("extract_failed") or 0)
               - int(p.get("extract_quarantined") or 0))
        for p in pair_logs)


def worker_outcome(pair_logs: "list[dict]", total_new: int) -> dict:
    """What a finished run did, in the terms the worker's decision needs.

    Here rather than in the loop's callback so the mapping is testable. It is
    the mapping that has been wrong every time: `worked` is an extraction
    measure and `fetched` a collection one, and reading either through the
    other is what produced ~200 empty runs on 2026-08-22 and ~100 on 08-18.
    """
    return {
        "pairs": len(pair_logs),
        "worked": pages_worked(pair_logs),
        "fetched": pages_fetched(pair_logs),
        "new": total_new,
    }


class WorkerAfterRun(NamedTuple):
    """What the worker should carry into its next iteration."""
    empty_extractions: int
    empty_collections: int
    #: Seconds to hold extraction back; 0 releases it, None leaves it as it was.
    extract_cooldown: float | None
    #: Seconds to sleep before looking again; 0 means loop straight on.
    sleep: float


def worker_after_run(*, mode: str, worked: int, fetched: int, new_records: int,
                     cancelled: bool, empty_extractions: int, empty_collections: int,
                     empty_limit: int, idle_s: float, retry_s: float) -> WorkerAfterRun:
    """The worker's post-run bookkeeping, as a function of what the run did.

    Lifted out of `_worker_loop` for the same reason `next_worker_action` was:
    in place, the only way to check it was to assert on the source text of a
    closure — which passes when the logic is wrong and fails when a variable is
    renamed. Reverting the collector to consult `worked` left every test green.
    """
    cooldown: float | None = None
    sleep = 0.0
    if mode == "ai_only":
        if cancelled:
            # A cancelled pass says nothing about whether work exists — the
            # quota ran out mid-run, or an operator stopped it. It must not
            # park extraction, and it must not count toward the empty streak
            # either: three interruptions would park it on no evidence at all.
            # The old loop parked on neither but still counted, which the rule
            # it documented did not survive being written down as a function.
            return WorkerAfterRun(empty_extractions, empty_collections, None, 0.0)
        # Independent of the "worked" signal, which has been wrong twice.
        # Whatever it says, an extraction pass producing no records three times
        # running is not making progress and must stand aside for the collector.
        empty_extractions = 0 if new_records else empty_extractions + 1
        if empty_extractions >= empty_limit or not worked:
            # Either backstop parks it for the same quarter of an hour: an
            # empty pass means there is nothing cached to extract yet.
            cooldown = retry_s
    elif mode == "search_only":
        if fetched:
            # Downloaded, not merely found. Clearing this on "URLs the search
            # returned" ran ~200 empty runs on 2026-08-22.
            empty_collections = 0
            cooldown = 0.0
        elif not cancelled:
            empty_collections += 1
            # A caught-up system should idle, not poll: nothing changes either
            # half except the quota rolling over at midnight, or an operator.
            sleep = retry_s if empty_collections >= empty_limit else idle_s
    return WorkerAfterRun(empty_extractions, empty_collections, cooldown, sleep)


def pages_fetched(pair_logs: "list[dict]") -> int:
    """Pages this run newly downloaded — the collector's "was that worth it?".

    Not `pages_worked`. That one subtracts extraction cache hits and extraction
    failures, and a `search_only` run extracts nothing, so both subtrahends are
    always zero and it degrades to `urls_found` — a pass that downloaded
    *nothing* because every URL was already cached still reported every one of
    them as work.

    That is what kept the worker awake on 2026-08-22: the collector cleared the
    extraction cooldown on every pass, extraction restarted immediately, and
    the pair alternated every three or four minutes for twenty hours. The daily
    report has both halves of the proof on one page — around 200 runs, and
    "Letöltött oldal: 0".

    Fetched, minus the ones that came from the cache.
    """
    return sum(
        max(0, len(p.get("fetched_urls") or ())
               - int(p.get("cache_hits_scrape") or 0))
        for p in pair_logs)


def _persist_attempts(extractor, db_path) -> None:
    """Record this run's provider attempts under `extract_attempts`.

    Called wherever a run ends — normal exit, preflight abort, cancellation —
    because a counter written only on the happy path is the bug it was added to
    fix. Idempotent per run only in the sense that each run's extractor is
    fresh; calling it twice for the same extractor would double-count, so it is
    called once per exit path.
    """
    if db_path is None:
        return
    attempts = int(getattr(extractor, "calls_made", 0) or 0)
    if attempts <= 0:
        return
    from datetime import datetime as _dt, timezone as _tz

    from .db import bump_daily_counter
    bump_daily_counter(db_path, _dt.now(_tz.utc).strftime("%Y-%m-%d"),
                       "extract_attempts", attempts)


def _log_throughput(extractor, run_mode: str, db_path=None) -> None:
    """Report where the window's time went, on every exit path.

    Also persists the run's provider attempts under `extract_attempts`, so the
    daily report can state a per-page cost without having to *infer* which
    calls were extraction's. Inferring it — "everything that is not
    enrichment" — silently charges extraction for `preflight()`, which runs
    once per run against every provider, and for the `/v1/chat/completions`
    gateway, which is other people's software entirely. With the worker
    starting a run every twenty minutes, preflight alone is a few hundred
    attempts a day against a fleet that manages under two thousand.

    The yield of a serial chain is decided by two numbers: compare
    `calls_per_min` against the fleet's combined rpm ceiling (185 as configured
    on 2026-08-17). A large gap is latency the chain idles on, which concurrency
    would recover; `wait_s` approaching `call_s` instead means pacing binds and
    concurrency would only hit the same limits harder.
    """
    if extractor is not None and getattr(extractor, "calls_made", 0):
        log.info("extractor_throughput", run_mode=run_mode, **extractor.throughput())
    _persist_attempts(extractor, db_path)


def classify_run_outcome(pair_logs: list[dict], run_error: str | None = None) -> str:
    """Three-state run outcome from the pair logs plus any top-level error.

    A single transient DataForSEO timeout out of 1414 pairs used to make a run
    ❌, indistinguishable from a provider outage that killed the window
    (2026-07-30 daily report). Item-level failures are `warning`: nothing was
    cached, the pair is retried next run. Only an abort is `aborted`.
    """
    if run_error:
        return RUN_ABORTED
    if any(p.get("aborted") for p in pair_logs):
        return RUN_ABORTED
    if any(p.get("search_failed") or p.get("extract_failed") for p in pair_logs):
        return RUN_WARNING
    return RUN_OK


def _tier_allows(city: "CityConfig", topic_name: str, core_topics: list[str]) -> bool:
    """topic_tier='core' cities only run core_topics; empty core_topics disables tiering."""
    return city.topic_tier != "core" or not core_topics or topic_name in core_topics


@dataclass
class CityConfig:
    name: str
    locale: str
    search_variants: list[str]
    country: str = ""
    # "full" = all topics; "core" = only PipelineConfig.core_topics (small towns
    # where most niche topics can't yield results — cuts search + LLM spend).
    topic_tier: str = "full"


@dataclass
class TopicConfig:
    name: str
    search_terms: dict[str, list[str]]


@dataclass
class PipelineConfig:
    search_results_per_query: int
    search_max_pages: int
    search_rate_limit: float
    fetch_timeout: int
    fetch_min_text_length: int
    fetch_max_concurrent: int
    fetch_blocked_domains: list[str]
    db_path: Path
    fetch_playwright_domains: list[str] = field(default_factory=list)
    dataforseo_login: str = ""
    dataforseo_password: str = ""
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-chat"
    # Cache-identity pin: fingerprints derive from this name when set, so a
    # provider-side model rename doesn't invalidate the whole extraction cache.
    deepseek_fingerprint_model: str = ""
    deepseek_temperature: float = 0.1
    deepseek_timeout: int = 60
    deepseek_max_text_chars: int = 8000
    #: Cap on generated tokens. Capacity, not headroom — see
    #: `_ApiExtractor.max_output_tokens`.
    deepseek_max_output_tokens: int = 1500
    deepseek_rate_limit_seconds: float = 1.0
    cache_skip_scraped: bool = True
    cache_skip_extracted: bool = True
    search_cache_ttl_days: int = 7
    enrich_communities: bool = True
    dataforseo_mode: str = "live"  # "standard" = queued task_post/task_get API
    dataforseo_priority: int = 1  # standard mode: 1=normal, 2=high priority
    # Topics still searched for topic_tier="core" cities; empty = tiering disabled.
    core_topics: list[str] = field(default_factory=list)
    #: Pages whose community extraction may be in flight at once. 1 reproduces
    #: the serial chain exactly. Lives in settings.yaml, which is a mounted
    #: volume in production, so it can be turned down without a deploy.
    extract_concurrency: int = 1
    #: Pairs whose *search* may be in flight at once. The collector spends
    #: 45-70s per pair waiting on a queued DataForSEO task; 1 is the serial
    #: collector. Raising this is what makes `standard_priority: 1` viable —
    #: half the price for a slower queue only pays off if the wait overlaps.
    search_concurrency: int = 1
    #: Content failures at one fingerprint after which a page stops being
    #: re-extracted. A failed extraction is deliberately never cached, so
    #: without this a page whose answer will not fit the token cap is retried by
    #: every run against every provider — ~21 pages × 30 runs a day, and once
    #: paid providers were on, most of the bill. 0 disables the quarantine.
    extract_max_page_failures: int = 3

    #: The joinability gate (scraper/gate.py). Off by default and with a zero
    #: budget, because it spends real money — the same two-lock shape the paid
    #: extraction providers have, for the same 2026-08 reason.
    gate_enabled: bool = False
    gate_threshold: float = 0.06
    gate_model: str = "typesafe/jev"
    gate_provider: str = "cloudflare"
    gate_account_id: str = ""
    gate_daily_budget_usd: float = 0.0


def _url_hash(url: str) -> str:
    """The one spelling of this hash the whole system shares.

    CLAUDE.md names it as triplicated across cache, db, pipeline and web; this
    delegates rather than adding a fourth copy that could drift by a byte.
    """
    from .cache import CacheManager
    return CacheManager.url_hash(url)


def build_gate(config: "PipelineConfig") -> "JoinabilityGate":
    """The one place a gate is assembled, mirroring build_extractor.

    Off unless both locks are open: `gate.enabled` is the permission and
    `gate.daily_budget_usd` the amount, and the gate itself treats 0 as "no
    calls at all". The 2026-08 paid-provider post-mortem is why it takes two.
    """
    from .gate import JoinabilityGate
    return JoinabilityGate(
        config.db_path,
        enabled=config.gate_enabled,
        threshold=config.gate_threshold,
        model=config.gate_model,
        provider=config.gate_provider,
        account_id=config.gate_account_id,
        max_text_chars=config.deepseek_max_text_chars,
        daily_budget_usd=config.gate_daily_budget_usd,
        timeout_seconds=config.deepseek_timeout,
    )


async def _extract_traced(extractor, **kwargs):
    """Extract one page and report which model served it, as one operation.

    Prefer the extractor's own traced call, which carries provenance out with
    the result. Anything extractor-shaped that lacks it — admin flows, test
    doubles — falls back to reading the mutable attributes afterwards, which is
    only correct while nothing else can call the chain in between. Concurrent
    callers must use an extractor that provides `extract_traced`.
    """
    traced = getattr(extractor, "extract_traced", None)
    if traced is not None:
        return await traced(**kwargs)
    records = await extractor.extract(**kwargs)
    model, quality = _served_by(extractor)
    return records, model, quality


def _served_by(extractor) -> tuple[str, int | None]:
    """(model, quality) of the model that actually served the last call.

    `extractor.model` only names the head of the chain and lies whenever
    failover or routing picked something else. Read defensively: callers pass
    anything extractor-shaped, including test doubles that carry neither field.
    """
    model = getattr(extractor, "last_model", "") or getattr(extractor, "model", "")
    quality = getattr(extractor, "last_quality", None)
    return model, (int(quality) if quality else None)


def build_extractor(config: "PipelineConfig") -> FallbackExtractor:
    """The one place an extractor chain is assembled.

    With `router.enabled` in config/providers.yaml the chain is the free-tier
    fleet, ordered best-quality-first and vetoed per provider by the persisted
    quota ledger. Otherwise it is the original single DeepSeek provider,
    unchanged.

    **Every extractor in the fleet is pinned to the same `fingerprint_model`.**
    The fingerprint is the extraction cache key; letting it vary per model would
    invalidate the whole cache the moment routing picked a different one and
    re-pay for ~74K extractions already done. Which model actually ran is
    recorded in `cache_pages.extract_model`, deliberately outside every key.
    """
    fingerprint_model = config.deepseek_fingerprint_model or config.deepseek_model

    router = build_router(
        config.db_path,
        temperature=config.deepseek_temperature,
        timeout_seconds=config.deepseek_timeout,
        max_text_chars=config.deepseek_max_text_chars,
        max_output_tokens=config.deepseek_max_output_tokens,
        rate_limit_seconds=config.deepseek_rate_limit_seconds,
        fingerprint_model=fingerprint_model,
    )
    if router.enabled:
        # The FULL fleet, not `order()`: that filters on live quota, and the
        # chain is built once for a run that can span 8 hours and cross midnight
        # UTC. A provider whose budget was spent at 16:35 would otherwise be
        # absent for the whole window, so the ledger's day-rollover could never
        # readmit it. Per-call availability is enforced by `_available()`, which
        # asks the ledger every time.
        fleet = router.all_extractors()
        if fleet:
            log.info("extractor_routed",
                     fleet=[f"{e.provider}:{e.model}" for e in fleet[:8]],
                     total=len(fleet))
            return FallbackExtractor(primaries=fleet, router=router)
        log.warning("model_router_no_capacity_falling_back")

    primaries = []
    if config.deepseek_api_key:
        primaries.append(DeepSeekExtractor(
            api_key=config.deepseek_api_key,
            model=config.deepseek_model,
            temperature=config.deepseek_temperature,
            timeout_seconds=config.deepseek_timeout,
            max_text_chars=config.deepseek_max_text_chars,
        max_output_tokens=config.deepseek_max_output_tokens,
            rate_limit_seconds=config.deepseek_rate_limit_seconds,
            fingerprint_model=config.deepseek_fingerprint_model or None,
        ))
    return FallbackExtractor(primaries=primaries)


def build_guide_writer(config: "PipelineConfig", *, give_up_at: int = 3,
                       now=None) -> FallbackExtractor:
    """The extraction fleet, minus the models that cannot write a guide today.

    The fleet is ordered by a quality score measured for **structured
    extraction**, and writing a readable 400-word article is a different skill.
    Measured on the live fleet 2026-09-21: `qwen3-4b` (score 73) answered a
    guide packet with a 132-word stub, `nemotron-3-super-120b` (58) returned an
    empty message twice, and `gpt-oss-120b` (62) wrote 359 usable words naming
    seven groups. Ordering by the extraction score therefore picks the worse
    writer first, and with the prose gate failing closed that spends the whole
    daily guide budget on drafts nothing will accept.

    Rather than hand-maintain a second score for models that change week to
    week, this reads what the gate already recorded: a model whose drafts were
    refused `give_up_at` times today is skipped for the rest of the day. Same
    shape as the router's rule for a provider that refuses every call, and it
    resets at 00:00 UTC on its own.

    Fails open. If every model is blocked the full fleet is returned, because a
    draft that might be refused is worth more than a day with no attempt at all.
    """
    from datetime import datetime, timezone

    writer = build_extractor(config)
    day = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date().isoformat()
    refused = get_daily_counters_with_prefix(config.db_path, day, "guide_rejected_by_")
    blocked = {model for model, count in refused.items() if count >= give_up_at}
    if not blocked:
        return writer
    keep = [e for e in writer.primaries if getattr(e, "model", "") not in blocked]
    if not keep:
        log.info("guide_writer_all_models_refused", blocked=sorted(blocked))
        return writer
    log.info("guide_writer_skipping_models", blocked=sorted(blocked),
             kept=[f"{e.provider}:{e.model}" for e in keep[:6]])
    return FallbackExtractor(primaries=keep, router=getattr(writer, "router", None))


async def run_pipeline(
    cities: list[CityConfig],
    topics: list[TopicConfig],
    config: PipelineConfig,
    cache: "CacheManager | None" = None,
    run_mode: str = "full",
    skip_scraped: bool | None = None,
    skip_extracted: bool | None = None,
    run_communities: bool = True,
    run_venues: bool = True,
    run_persons: bool = True,
    on_progress: Callable[[str | None, str | None], None] | None = None,
    on_pair_start: "Callable[[str, str], None] | None" = None,
    stop_at: "Any | None" = None,
    should_stop: "Callable[[], bool] | None" = None,
    allow_upgrade: bool = False,
) -> tuple[list[dict], int]:
    """stop_at: optional aware datetime (UTC) — pair loops stop gracefully once
    reached, so a run can be boxed into a time window (e.g. DeepSeek off-peak).

    allow_upgrade: permit the quality-upgrade sweep when this pass finds nothing
    left to collect. Off by default because `_cron_run` calls run_pipeline once
    per country group: an already-finished leading group would otherwise start
    re-extracting and could eat the whole remaining window before the groups
    behind it — with genuinely uncollected pages — are ever reached. Only the
    caller knows whether every group is done, so only the caller may enable it."""
    # run_mode="search_only": search + fetch + cache raw text, zero LLM calls.
    # Pairs collect cheaply (DataForSEO standard mode); a later ai_only run
    # extracts the cached pages when DeepSeek is in its off-peak window.
    if run_mode == "search_only":
        run_communities = run_venues = run_persons = False
    _skip_scraped = skip_scraped if skip_scraped is not None else config.cache_skip_scraped
    _skip_extracted = skip_extracted if skip_extracted is not None else config.cache_skip_extracted

    extractor: FallbackExtractor = build_extractor(config)
    # The chain may sit out a 429 for up to 15 minutes; past the window end that
    # sleep finishes nothing and delays the collector window behind it.
    extractor.deadline = stop_at

    run_stats: dict[str, dict[str, int]] = {}
    total_new = 0
    pair_logs: list[dict] = []

    # Pre-compute done pairs (searched + all pages extracted with current FP) to skip entirely.
    # Tier-filtered: "core"-tier cities only pair with core_topics (see CityConfig.topic_tier),
    # which also keeps the catch-up pass from re-searching tiered-out pairs.
    all_pairs = {(c.name, t.name) for c in cities for t in topics
                 if _tier_allows(c, t.name, config.core_topics)}
    # The cache-identity model, NOT whichever model the router happens to pick.
    # build_extractor() pins every provider in the fleet to this same name for
    # exactly this reason: fingerprints key the extraction cache, so letting one
    # vary per model would invalidate ~74K cached extractions the first time
    # routing chose a different provider.
    model = config.deepseek_fingerprint_model or config.deepseek_model
    current_fp = get_extract_fingerprint(model)
    venue_fp = get_venue_fingerprint(model)
    person_fp = get_person_fingerprint(model)
    done_pairs: set[tuple[str, str]] = set()
    _filter_started = time.monotonic()
    if _skip_extracted:
        # Off the loop like the writes. These are the two largest reads in the
        # system — `get_fully_processed_pairs` loads every `cache_pages` row
        # (199,537 of them) and JSON-parses each one — and they run at the start
        # of every pass, which under the worker is every twenty minutes. Today's
        # sweep moved the *writes* off the event loop and left these, which is
        # why the site kept 404ing after it: a read that scans 200,000 rows
        # holds the loop exactly as hard as a write.
        if run_mode == "search_only":
            done_pairs = await _off_loop(
                get_collected_pairs, config.db_path, config.search_max_pages)
        else:
            done_pairs = await _off_loop(
                get_fully_processed_pairs,
                config.db_path,
                current_fp,
                venue_fp,
                person_fp,
                run_communities=run_communities,
                run_venues=run_venues,
                run_persons=run_persons,
                max_pages=config.search_max_pages,
                quarantine_threshold=config.extract_max_page_failures,
            )
    if _skip_extracted:
        # Timed because moving it to a thread is only half a fix: the SQLite
        # call releases the GIL, but building a dict per row for 199,537 rows
        # does not, so a slow scan still starves the loop that serves the site.
        # If this reads in seconds, the query needs to get cheaper rather than
        # move again.
        log.info("done_pair_filter", pairs=len(done_pairs),
                 seconds=round(time.monotonic() - _filter_started, 2))
    pairs_to_run = all_pairs - done_pairs
    skipped = len(all_pairs) - len(pairs_to_run)
    if skipped:
        log.info("pairs_skipped_fully_processed", count=skipped, remaining=len(pairs_to_run))

    if not pairs_to_run:
        log.info("pipeline_all_pairs_done", run_mode=run_mode)
        # Nothing new to collect — the one condition under which spending free
        # quota on re-extraction is worth it. This is the *only* reachable call
        # site: every path below this point has new work pending by definition.
        if allow_upgrade and run_mode == "ai_only" and cache is not None:
            # The normal preflight sits below this early return, so the sweep
            # would otherwise get none of it — and it is exactly the pass that
            # needs it, walking up to upgrade_max_per_run pages that would each
            # burn a wasted request on a retired model name.
            try:
                await extractor.preflight()
            except Exception as exc:
                log.warning("quality_upgrade_preflight_failed", error=str(exc))
                return pair_logs, total_new
            upgrade_new, upgrade_logs = await _run_quality_upgrade(
                cities, topics, config, extractor, cache, current_fp,
                stop_at=stop_at, should_stop=should_stop, on_progress=on_progress,
            )
            total_new += upgrade_new
            pair_logs += upgrade_logs
        _log_throughput(extractor, run_mode, config.db_path)
        return pair_logs, total_new

    # Preflight: one live extraction before the pair loops start. A provider-side
    # breaking change (retired model name, revoked key, unparseable response)
    # otherwise burns the whole run window one skipped page at a time — the
    # 2026-07-24 off-peak window produced 5 records from 1368 pages before anyone
    # noticed. search_only never calls the LLM, so it skips the check.
    if run_mode != "search_only" and (run_communities or run_venues or run_persons):
        try:
            await extractor.preflight()
        except Exception as exc:
            log.error("extractor_preflight_failed", run_mode=run_mode, error=str(exc))
            _persist_attempts(extractor, config.db_path)
            raise ExtractorUnavailableError(
                f"extractor preflight failed, no work attempted: {exc}") from exc

    if run_mode == "ai_only":
        total_new, pair_logs = await _run_ai_only(
            cities, topics, config, extractor, cache, _skip_extracted, run_stats, on_progress,
            run_communities=run_communities, run_venues=run_venues, run_persons=run_persons,
            on_pair_start=on_pair_start, pairs_filter=pairs_to_run, stop_at=stop_at,
                should_stop=should_stop,
        )
    else:
        reai_new, reai_logs = (0, [])
        if run_mode != "search_only":  # search_only never touches the LLM
            reai_new, reai_logs = await _run_ai_only(
                cities, topics, config, extractor, cache, _skip_extracted, run_stats, on_progress,
                run_communities=run_communities, run_venues=run_venues, run_persons=run_persons,
                on_pair_start=on_pair_start, pairs_filter=pairs_to_run, stop_at=stop_at,
                should_stop=should_stop,
            )
        search_client = _build_search_client(config)
        if (_stop := _stop_reason(extractor)) and run_mode == "full":
            # `_run_ai_only` above already gave up. Running the full chain now
            # pays DataForSEO for searches whose extractions cannot run. Skip
            # the pass, not the run: the duplicate scan and the run summary
            # below still have to happen.
            log.info("full_pass_skipped_extractor_stopped", reason=_stop[0])
            full_new, full_logs = 0, []
        else:
            full_new, full_logs = await _run_full(
                cities, topics, config, extractor, cache, _skip_scraped, _skip_extracted,
                run_stats, on_progress,
                run_communities=run_communities, run_venues=run_venues,
                run_persons=run_persons,
                on_pair_start=on_pair_start, pairs_filter=pairs_to_run, stop_at=stop_at,
                should_stop=should_stop,
                search_client=search_client,
            )
        total_new = reai_new + full_new
        pair_logs = reai_logs + full_logs

    if run_mode in ("full", "search_only"):
        if search_client.exhausted:
            # The main pass aborted on a dead provider — a catch-up pass with the
            # same client would only add another abort marker.
            log.warning("catchup_skipped_search_provider_down",
                        reason=getattr(search_client, "failure_reason", None))
            covered = uncovered = None
        else:
            covered = await _off_loop(get_covered_pairs, config.db_path)
            uncovered = all_pairs - covered - done_pairs
        # `full` mode's catch-up re-runs the whole search → fetch → extract
        # chain. Entering it with a fleet that already stopped means paying
        # DataForSEO for pages nothing can extract, so the same reasoning that
        # skips it on a dead search provider applies to a dead extractor.
        if uncovered and run_mode == "full" and (_stop := _stop_reason(extractor)):
            log.info("catchup_skipped_extractor_stopped", reason=_stop[0])
            uncovered = None
        if uncovered and not _should_stop(stop_at, should_stop):
            log.info("catchup_pass_start", pairs=len(uncovered))
            catchup_new, catchup_logs = await _run_full(
                cities, topics, config, extractor, cache,
                _skip_scraped, _skip_extracted, run_stats, on_progress,
                run_communities=run_communities, run_venues=run_venues,
                run_persons=run_persons, pairs_filter=uncovered,
                on_pair_start=on_pair_start, stop_at=stop_at,
                should_stop=should_stop,
                search_client=search_client,
            )
            total_new += catchup_new
            pair_logs += catchup_logs
            log.info("catchup_pass_complete", new_records=catchup_new, pairs=len(uncovered))

    log.info("pipeline_complete", run_mode=run_mode, total_new_records=total_new)
    _log_throughput(extractor, run_mode, config.db_path)
    if run_mode != "search_only":
        try:
            from .duplicates import detect_all
            await asyncio.to_thread(detect_all, config.db_path)
        except Exception as exc:
            log.warning("post_run_duplicate_scan_failed", error=str(exc))
    failed_search = sum(1 for p in pair_logs if p.get("search_failed"))
    failed_extract = sum(p.get("extract_failed", 0) for p in pair_logs)
    if failed_search or failed_extract:
        log.warning("run_completed_with_failures",
                    search_failed_pairs=failed_search,
                    extract_failed_pages=failed_extract,
                    note="failed items were NOT cached and will be retried next run")
    return pair_logs, total_new


def _build_search_client(config: PipelineConfig) -> FallbackSearchClient:
    search_primaries: list = []
    if config.dataforseo_login and config.dataforseo_password:
        search_primaries.append(DataForSEOClient(
            config.dataforseo_login, config.dataforseo_password,
            rate_limit_seconds=config.search_rate_limit,
            mode=config.dataforseo_mode,
            standard_priority=config.dataforseo_priority,
        ))
    log.info("search_client", primaries=[type(p).__name__ for p in search_primaries])
    return FallbackSearchClient(primaries=search_primaries)


async def _prefetch_searches(
    searxng, pairs, config, *, concurrency: int,
) -> dict:
    """Issue several pairs' searches at once, keyed by `(city, topic)`.

    The collector's cost is almost entirely DataForSEO round trips: a pair takes
    45-70 seconds, nearly all of it waiting for a queued task. Pairs are
    independent, so that wait was the loop's shape rather than a constraint —
    the same mistake, and the same fix, as [[concurrent-extraction]].

    Queries within a pair stay sequential on purpose: `search_all` stops issuing
    them once `stop_after` unique urls are collected, which typically skips the
    third. Running them together would buy latency with money.

    Returns `{(city, topic): results | Exception}`. Anything missing simply
    falls through to a live call at the normal call site, so a failure here can
    only cost time, never correctness.

    Successful searches are written to `search_cache` here rather than only at
    the call site. The run can stop — window closed, provider gone — with pairs
    still unconsumed, and an unsaved paid search is re-paid on every future run.
    The call site writes the same row again; it is an upsert.
    """
    out: dict = {}
    if not pairs:
        return out
    sem = asyncio.Semaphore(max(1, int(concurrency or 1)))

    async def _one(city, topic) -> None:
        async with sem:
            if searxng.exhausted:
                return
            terms = topic.search_terms.get(city.locale) or topic.search_terms.get("en", [])
            queries = build_queries(city.name, city.search_variants, terms)
            try:
                results = await searxng.search_all(
                    queries, locale=city.locale,
                    num_results=config.search_results_per_query,
                    stop_after=config.search_max_pages * 2,
                )
            except (SearchQuotaError, SearchUnavailableError) as exc:
                out[(city.name, topic.name)] = exc
                return
            out[(city.name, topic.name)] = results
            # Logged here as well as at the call site: once searches moved into
            # the prefetch, `search_done` stopped appearing for almost every
            # pair and the collector became invisible in the logs.
            log.info("search_done", city=city.name, topic=topic.name,
                     urls=len(results), via="prefetch")
            from .fetch import _is_blocked as _url_blocked
            try:
                await _off_loop(save_search_cache,
                    config.db_path, city.name, topic.name,
                    [r.url for r in results
                     if not _url_blocked(r.url, config.fetch_blocked_domains)],
                    queries)
            except Exception as exc:  # a cache write must not lose the result
                log.warning("prefetch_search_cache_write_failed",
                            city=city.name, topic=topic.name, error=str(exc))

    await asyncio.gather(*[_one(c, t) for c, t in pairs])
    return out


async def _run_full(
    cities: list[CityConfig],
    topics: list[TopicConfig],
    config: PipelineConfig,
    extractor: FallbackExtractor,
    cache: "CacheManager | None",
    skip_scraped: bool,
    skip_extracted: bool,
    run_stats: dict,
    on_progress: Callable[[str | None, str | None], None] | None,
    run_communities: bool = True,
    run_venues: bool = True,
    run_persons: bool = True,
    pairs_filter: set[tuple[str, str]] | None = None,
    on_pair_start: "Callable[[str, str], None] | None" = None,
    stop_at: "Any | None" = None,
    should_stop: "Callable[[], bool] | None" = None,
    search_client: "FallbackSearchClient | None" = None,
) -> tuple[int, list[dict]]:
    searxng = search_client if search_client is not None else _build_search_client(config)
    semaphore = asyncio.Semaphore(config.fetch_max_concurrent)

    pw_fetcher = None
    if config.fetch_playwright_domains:
        from .playwright_fetch import PlaywrightFetcher
        pw_fetcher = PlaywrightFetcher(config.fetch_playwright_domains)
        await pw_fetcher.start()

    all_fps = load_false_positives(config.db_path)
    enrich_fp_section = build_prompt_section(all_fps, fp_type="enrichment")
    quarantine = _Quarantine(cache, extractor.canonical_fingerprint,
                             config.extract_max_page_failures)
    gate = build_gate(config)
    total_new = 0
    pair_logs: list[dict] = []

    # Pairs this run will walk, in order. The prefetch reads ahead along it —
    # batching by *city* looked natural but a core city has six topics and most
    # are already cached, so a batch was typically one or two pairs and
    # `search_concurrency: 8` never engaged. Measured 0.16 pairs/min on
    # 2026-08-18, below even the serial collector it replaced.
    _walk = [(c, t) for c in cities for t in topics
             if (pairs_filter is None or (c.name, t.name) in pairs_filter)
             and _tier_allows(c, t.name, config.core_topics)]
    _walk_pos = 0
    prefetched: dict = {}

    async def _refill_prefetch() -> None:
        """Put another `search_concurrency` searches in flight, across cities."""
        nonlocal _walk_pos
        want = max(1, int(config.search_concurrency or 1))
        batch: list = []
        while len(batch) < want and _walk_pos < len(_walk):
            c, t = _walk[_walk_pos]
            _walk_pos += 1
            if skip_scraped and config.search_cache_ttl_days > 0:
                hit = await _off_loop(get_search_cache, config.db_path, c.name,
                                      t.name, config.search_cache_ttl_days)
                if hit is not None:
                    continue  # the loop serves this one from cache
            batch.append((c, t))
        if batch:
            prefetched.update(await _prefetch_searches(
                searxng, batch, config, concurrency=want))

    # Early exits use this flag instead of return so the Playwright fetcher
    # cleanup below always runs.
    aborted = False
    # `aborted` means something broke; `stopped` means the window is simply
    # over — the fleet spent its free quota, or every provider is inside a
    # back-off. Both must leave the *city* loop: without this a quota-spent
    # pause only ended the current city's topics and the next city went on
    # paying DataForSEO and fetching pages for extractions that cannot run.
    stopped = False

    for city in cities:
        if aborted or stopped:
            break
        run_stats[city.name] = {}
        for topic in topics:
            if pairs_filter is not None and (city.name, topic.name) not in pairs_filter:
                continue
            # Tier guard (belt-and-suspenders — all_pairs is already tier-filtered,
            # but direct _run_full callers may pass no filter).
            if not _tier_allows(city, topic.name, config.core_topics):
                continue
            if _should_stop(stop_at, should_stop):
                log.info("run_window_closed", city=city.name, topic=topic.name)
                aborted = True
                break
            await asyncio.sleep(0)
            if on_pair_start:
                on_pair_start(city.name, topic.name)
            log.info("processing_pair", city=city.name, topic=topic.name)

            terms = topic.search_terms.get(city.locale) or topic.search_terms.get("en", [])
            queries = build_queries(city.name, city.search_variants, terms)

            # Popped here, before either branch. The prefetch writes its result
            # to `search_cache`, so by the time the loop arrives the pair takes
            # the cache-hit path — which never popped, and the dict grew one
            # result list per prefetched pair for the whole run. At production
            # scale that is the same OOM this pipeline already learned once.
            _pre = prefetched.pop((city.name, topic.name), None)

            use_search_cache = skip_scraped and config.search_cache_ttl_days > 0
            search_cache_hit = False
            cached_urls = get_search_cache(config.db_path, city.name, topic.name,
                                           config.search_cache_ttl_days) if use_search_cache else None
            if cached_urls is not None:
                urls = cached_urls[:config.search_max_pages]
                urls_found = len(urls)
                search_cache_hit = True
                log.info("search_cache_hit", city=city.name, topic=topic.name, urls=len(urls))
            else:
                if searxng.exhausted:
                    # Provider down/quota gone for the rest of the run: abort instead
                    # of walking every remaining pair (a dead provider once produced
                    # 4972 per-pair "failures" from 3 real errors). One marker entry
                    # keeps the run visibly failed; nothing was cached, so every
                    # unsearched pair is retried next run.
                    reason = getattr(searxng, "failure_reason", None)
                    pair_logs.append({**_new_pair_log(city.name, topic.name, queries),
                                      "search_failed": True, "search_error": reason,
                                      "aborted": True})
                    log.warning("search_provider_down_run_aborted", city=city.name,
                                topic=topic.name, reason=reason)
                    aborted = True
                    break
                try:
                    if _pre is None and not _should_stop(stop_at, should_stop):
                        await _refill_prefetch()
                        _pre = prefetched.pop((city.name, topic.name), None)
                    if isinstance(_pre, BaseException):
                        raise _pre
                    search_results = _pre if _pre is not None else await searxng.search_all(
                        queries, locale=city.locale, num_results=config.search_results_per_query,
                        stop_after=config.search_max_pages * 2,
                    )
                except (SearchQuotaError, SearchUnavailableError) as exc:
                    pair_logs.append({**_new_pair_log(city.name, topic.name, queries),
                                      "search_failed": True, "search_error": str(exc)})
                    log.warning("search_unavailable_pair_skipped", city=city.name,
                                topic=topic.name, reason=str(exc))
                    continue
                from .fetch import _is_blocked as _url_blocked
                search_results = [
                    r for r in search_results
                    if not _url_blocked(r.url, config.fetch_blocked_domains)
                ]
                log.info("search_done", city=city.name, topic=topic.name, urls=len(search_results))
                all_urls = [r.url for r in search_results]
                urls = all_urls[:config.search_max_pages]
                urls_found = len(search_results)
                # Always record the search — even in Full Refresh mode and even when it
                # found nothing. An unsaved empty result is re-paid on every run (and
                # twice per run via the catch-up pass); an empty cached list correctly
                # marks the pair as done. The full (uncapped) URL list is stored; reads
                # apply [:search_max_pages].
                await _off_loop(save_search_cache, config.db_path, city.name, topic.name, all_urls, queries)

            fetched: list[tuple[str, str]] = []
            pair_log = {**_new_pair_log(city.name, topic.name, queries),
                        "search_cache_hit": search_cache_hit, "urls_found": urls_found}

            urls_to_fetch = []
            for url in urls:
                if cache and skip_scraped:
                    cached_text = cache.get_scraped(url)
                    if cached_text:
                        log.debug("cache_hit_scrape", url=url)
                        fetched.append((url, cached_text))
                        pair_log["cache_hits_scrape"] += 1
                        pair_log["fetched_urls"].append(url)
                        continue
                urls_to_fetch.append(url)

            async def _fetch_one(url: str) -> tuple[str, str | None, float]:
                if on_progress:
                    on_progress("scrape", url)
                t0 = time.monotonic()
                text = await fetch_and_clean(
                    url, config.fetch_blocked_domains,
                    config.fetch_timeout, config.fetch_min_text_length,
                    semaphore,
                    playwright_fetcher=pw_fetcher,
                )
                dur = time.monotonic() - t0
                if on_progress:
                    on_progress(None, None)
                return url, text, dur

            for url, text, scrape_dur in await asyncio.gather(*[_fetch_one(u) for u in urls_to_fetch]):
                if text:
                    if cache:
                        await _off_loop(cache.save_scraped, url, text, city.name, topic.name,
                                           duration_s=scrape_dur, source_queries=queries)
                    fetched.append((url, text))
                    pair_log["fetched_urls"].append(url)
                else:
                    pair_log["fetch_failed"] += 1

            log.info("fetch_done", city=city.name, topic=topic.name, pages=len(fetched))
            # An empty SERP is a valid terminal result. If URLs were returned but
            # every fetch failed, leave the marker NULL so a later run retries
            # instead of suppressing the pair until the long search-cache TTL.
            if not urls or fetched:
                await _off_loop(mark_search_collection_complete, config.db_path, city.name, topic.name)

            if not run_communities and not run_venues and not run_persons:
                # search_only is a strict collection mode: after the fetch batch it
                # must not read extraction caches, upsert entities, or run duplicate
                # detection. Failed URLs are recorded above but the pair is terminally
                # collected; an interrupted pair remains unmarked and is resumed.
                run_stats[city.name][topic.name] = 0
                pair_logs.append(pair_log)
                continue

            records = []
            extract_dead = False
            for url, text in fetched:
                community_names: list[str] = []

                # ── Community extraction (with fingerprint cache) ────────────
                community_cache_hit = False
                if cache and skip_extracted:
                    cached_records = cache.get_extracted(url, fingerprint=extractor.canonical_fingerprint)
                    if cached_records is not None:
                        log.debug("cache_hit_extract", url=url)
                        records.extend(cached_records)
                        community_names = [r.name for r in cached_records]
                        pair_log["cache_hits_extract"] += 1
                        pair_log["records_extracted"] += len(cached_records)
                        community_cache_hit = True

                if not community_cache_hit and run_communities:
                    if getattr(extractor, "rate_limited_out", False):
                        # Every provider is inside a back-off window. Waiting it
                        # out page by page burns the window on sleep, so stop
                        # this pass cleanly and let the next one resume — the
                        # daily budget is untouched, nothing is cached, and the
                        # pages are retried. Not an abort: the providers are
                        # alive.
                        log.info("extract_paused_all_rate_limited",
                                 city=city.name, topic=topic.name)
                        extract_dead = True
                        break
                    if getattr(extractor, "quota_exhausted", False):
                        # The free-tier fleet spent its daily allowance. That is
                        # the designed end of a window, not an outage: stop
                        # cleanly with no `aborted` flag, so the run detail and
                        # the daily email do not report a provider failure.
                        # Nothing is cached, so the pages retry tomorrow.
                        log.info("extract_stopped_quota_spent", city=city.name,
                                 topic=topic.name)
                        extract_dead = True
                        break
                    if extractor.providers_down:
                        # Every configured LLM provider died this run (quota gone
                        # or the circuit breaker opened). Abort instead of walking
                        # the remaining pages one by one: nothing is cached, so
                        # every skipped page is retried next run, and the run is
                        # visibly failed with the reason attached.
                        pair_log["extract_failed"] += 1
                        pair_log["extract_error"] = getattr(extractor, "failure_reason", None)
                        pair_log["aborted"] = True
                        extract_dead = True
                        break
                    if extractor.exhausted:
                        # No LLM configured at all (deliberate no-key setup):
                        # leave the page un-extracted, cached raw text stays.
                        # Do NOT cache an empty result.
                        pair_log["extract_failed"] += 1
                        continue
                    if quarantine.holds(url):
                        # Failed the same way `extract_max_page_failures` times
                        # at this fingerprint. Counted apart from `extract_failed`:
                        # it is not this run failing, it is this run declining to
                        # pay for a failure already established.
                        pair_log["extract_quarantined"] = (
                            pair_log.get("extract_quarantined", 0) + 1)
                        continue
                    if not await gate.allows(_url_hash(url), url,
                                             text, city.name, topic.name):
                        # The gate said no, with a calibrated probability below
                        # the configured threshold. Counted apart from every
                        # kind of failure: nothing went wrong, this run
                        # declined to spend an extraction on a page a $0.0001
                        # question says has nothing on it. Nothing is written
                        # to the extraction cache — the decision lives in
                        # `gate_decisions`, keyed by a fingerprint that
                        # re-opens it on any change, and is releasable by hand
                        # at /admin/gate.
                        pair_log["gate_skipped"] = pair_log.get("gate_skipped", 0) + 1
                        continue
                    if on_progress:
                        on_progress("extract", url)
                    t0 = time.monotonic()
                    try:
                        # Provenance comes out *with* the result. Reading it
                        # afterwards was correct only as long as nothing else
                        # could call the chain in between — and _enrich_record
                        # goes through the same chain, so the page would be
                        # stamped with the enricher's provider: the score that
                        # then drives or blocks the upgrade sweep.
                        extracted, _model, _quality = await _extract_traced(
                            extractor,
                            text=text, city=city.name, topic=topic.name,
                            locale=city.locale, source_url=url,
                            false_positive_examples=build_prompt_section(
                                all_fps, city=city.name, topic=topic.name
                            ),
                        )
                    except ExtractorUnavailableError as exc:
                        pair_log["extract_failed"] += 1
                        await quarantine.note(url, exc)
                        log.warning("extract_unavailable_page_skipped", url=url, reason=str(exc))
                        # The guards above run *before* the call, so a stop
                        # caused by the last page of a pair would never be seen:
                        # the loop would end and the run be filed as a warning
                        # even though this very call opened the breaker.
                        if (_reason := _mark_stop(extractor, pair_log)):
                            log.info("extract_stopped_after_page", url=url, reason=_reason)
                            extract_dead = True
                            break
                        continue
                    finally:
                        extract_dur = time.monotonic() - t0
                        if on_progress:
                            on_progress(None, None)

                    joinable = [r for r in extracted if r.joinable]
                    if len(joinable) < len(extracted):
                        log.info("joinability_filtered", url=url,
                                 kept=len(joinable), removed=len(extracted) - len(joinable))

                    enrich_timing = {"scrape": 0.0, "extract": 0.0, "count": 0, "needed": False}
                    final_records = []
                    enrich_logs: list[dict] = []
                    for record in joinable:
                        log_entry: dict = {
                            "community_name": record.name,
                            "search_query": None,
                            "research_urls": [],
                            "enriched": False,
                            "fields_added": [],
                        }
                        if (config.enrich_communities and not extract_dead
                                and _needs_enrichment(record)
                                and (record.confidence or 0.0) >= 0.7):
                            enrich_timing["needed"] = True
                            record = await _enrich_record(
                                record, searxng, extractor, config, semaphore,
                                on_progress, enrich_timing, log_entry,
                                enrich_fp_examples=enrich_fp_section,
                            )
                        enrich_logs.append(log_entry)
                        final_records.append(record)
                        # `_enrich_record` swallows provider errors by design —
                        # enrichment is best-effort and a record without a
                        # website is still a record. But the swallow also hid
                        # the fleet dying: if the last thing a run did was an
                        # enrichment call, the outage was never recorded and the
                        # run was filed as clean.
                        #
                        # Flag it and stop enriching; do NOT break. The loop
                        # still has to append every extracted record, because
                        # `final_records` is what gets cached under this
                        # fingerprint — a short list here would be cached as the
                        # page's permanent, silently incomplete extraction.
                        if not extract_dead and (_reason := _mark_stop(extractor, pair_log)):
                            log.info("extract_stopped_during_enrich", reason=_reason)
                            extract_dead = True

                    if cache:
                        await _off_loop(cache.save_extracted, url, final_records, duration_s=extract_dur,
                                             fingerprint=extractor.canonical_fingerprint,
                                             model=_model, quality=_quality)
                        if enrich_timing["needed"]:
                            await _off_loop(cache.mark_enrich_scraped, url, enrich_timing["scrape"])
                            await _off_loop(cache.mark_enrich_extracted, url, enrich_timing["count"],
                                                        enrich_timing["extract"], model=extractor.model)
                            await _off_loop(cache.save_enrich_log, url, enrich_logs)

                    # NB: no per-URL save_results — the pair-final batch save below
                    # covers it; saving per URL re-ran O(n²) dedup + a full topic
                    # DELETE+reinsert + a city-wide duplicate scan for every page.
                    records.extend(final_records)
                    total_new += len(final_records)
                    pair_log["records_extracted"] += len(final_records)
                    log.info("extracted", url=url, found=len(extracted), kept=len(final_records))
                    community_names = [r.name for r in final_records]

                # ── Venue extraction (with fingerprint cache) ────────────────
                # Gated on community_names like person extraction: pages with no
                # communities (the majority) skip the venue LLM call entirely.
                # Cost gate: skip the venue LLM call on 0-community pages — but only
                # when the communities pass ran; a venues-only run (run_communities
                # off) must extract unconditionally or it would be a no-op.
                if run_venues and (community_names or not run_communities) and not (cache and cache.get_venue_extracted(
                        url, fingerprint=extractor.canonical_venue_fingerprint) is not None):
                    try:
                        _topic_slugs = [t.name for t in topics]
                        venues = await extractor.extract_venues(
                            text, city.name, city.locale, url, valid_topics=_topic_slugs)
                        if venues:
                            await _off_loop(upsert_venues, config.db_path, [v.model_dump() for v in venues])
                            log.info("venues_extracted", url=url, found=len(venues))
                        if cache:
                            await _off_loop(cache.save_venue_extracted, url, [v.model_dump() for v in venues],
                                                       fingerprint=extractor.canonical_venue_fingerprint,
                                                       model=extractor.model)
                    except Exception as exc:
                        # Counted like a failed community extraction: nothing was
                        # cached, so the page is retried — and the daily report
                        # must not show a venue-blind run as a clean ✓.
                        pair_log["extract_failed"] = pair_log.get("extract_failed", 0) + 1
                        log.warning("venues_extract_error", url=url, error=str(exc))
                        if (_reason := _mark_stop(extractor, pair_log)):
                            log.info("extract_paused_mid_page", url=url, reason=_reason)
                            extract_dead = True
                            break

                # ── Person extraction (with fingerprint cache) ───────────────
                if community_names:
                    _person_cache = cache.get_person_extracted(
                        url, city.name, topic.name,
                        fingerprint=extractor.canonical_person_fingerprint) if cache else None
                    if _person_cache is not None:
                        if _person_cache:
                            log.debug("person_cache_hit", url=url, cached=len(_person_cache))
                    elif run_persons:
                        try:
                            persons = await extractor.extract_persons(
                                text, city.name, topic.name, city.locale, url, community_names,
                            )
                            if persons:
                                await _off_loop(upsert_persons, config.db_path, [p.model_dump() for p in persons])
                                log.info("persons_extracted", url=url, found=len(persons))
                            else:
                                log.info("persons_extract_zero", url=url,
                                         communities=len(community_names))
                            if cache:
                                await _off_loop(cache.save_person_extracted, url, city.name, topic.name,
                                                            [p.model_dump() for p in persons],
                                                            fingerprint=extractor.canonical_person_fingerprint,
                                                            model=extractor.model)
                        except Exception as exc:
                            pair_log["extract_failed"] = pair_log.get("extract_failed", 0) + 1
                            log.warning("persons_extract_error", url=url, error=str(exc))
                            if (_reason := _mark_stop(extractor, pair_log)):
                                log.info("extract_paused_mid_page", url=url, reason=_reason)
                                extract_dead = True
                                break

            # ── Synthesize PersonRecords from community leader fields ────────
            if run_persons:
                leader_persons = _persons_from_leaders(records, city.name, topic.name)
                # Communities that still yield synthesized leaders get the full
                # replace (legacy behavior). Everything else only drops rows
                # explicitly marked origin='leader_field', so a stale synthesized
                # leader disappears while independently AI-extracted leader
                # persons survive.
                synth_names = {p.community_name for p in leader_persons}
                for rec in records:
                    await _off_loop(delete_leader_persons_for_community,
                        config.db_path, rec.name, city.name,
                        only_synthesized=rec.name not in synth_names)
                if leader_persons:
                    await _off_loop(upsert_persons, config.db_path,
                                    [{**p.model_dump(), "origin": "leader_field"}
                                     for p in leader_persons])
                    log.info("persons_from_leaders", city=city.name, topic=topic.name,
                             found=len(leader_persons))

            count = await _off_loop(save_results, city.name, topic.name, records, config.db_path)
            run_stats[city.name][topic.name] = count
            pair_logs.append(pair_log)

            if extract_dead:
                # Three different things set `extract_dead`, and only one of
                # them is an outage. `aborted` is set solely by the
                # providers_down branch, so it — not the flag — decides how the
                # run is reported. The bracket access here also used to raise
                # KeyError on the other two paths, which turned a clean pause
                # into a crashed run.
                if pair_log.get("aborted"):
                    log.warning("extract_provider_down_run_aborted", city=city.name,
                                topic=topic.name, reason=pair_log.get("extract_error"))
                    aborted = True
                else:
                    log.info("extract_window_stopped", city=city.name, topic=topic.name)
                    stopped = True
                break

    if pw_fetcher:
        await pw_fetcher.stop()
    return total_new, pair_logs


async def _run_quality_upgrade(
    cities: list[CityConfig],
    topics: list[TopicConfig],
    config: PipelineConfig,
    extractor: FallbackExtractor,
    cache: "CacheManager",
    fingerprint: str,
    *,
    stop_at: "Any | None" = None,
    should_stop: "Callable[[], bool] | None" = None,
    on_progress: Callable[[str | None, str | None], None] | None = None,
) -> tuple[int, list[dict]]:
    """Re-extract pages whose cached result came from a weaker model.

    The operator's policy, and the one the research supports: **new work always
    outranks re-work**. Free daily allowances do not roll over, so a request not
    spent by midnight UTC is simply lost — but a request spent re-doing a page
    while unprocessed pages exist is worse than lost, because it delays new
    coverage. Hence the three gates:

      1. Only when the normal pass had nothing left to collect — this function
         is called from that branch alone.
      2. Only when an available model beats the cached one by at least
         `upgrade_min_gain` points — below that the expected gain does not
         justify the request (the marginal-quality-per-cost condition in
         arXiv:2605.06350).
      3. Bounded by `upgrade_max_per_run` and by the run window, so a sweep can
         never eat into the next day's collection.

    Two exclusions the candidate query cannot express:

    * **Pages with unknown quality are left alone.** ~74K rows predate the
      router and carry NULL, meaning "extracted by the paid incumbent", which
      scores *above* every free model. Treating NULL as 0 would have the sweep
      overwrite good DeepSeek output with weaker free-model output — a
      downgrade dressed as an upgrade.
    * **Tier-frozen pairs stay frozen.** `topic_tier: core` cities run only
      `core_topics`; re-extracting a tiered-out pair spends quota on work the
      pipeline deliberately does not do.

    A failed re-extraction leaves the existing cached result untouched: the old
    answer is strictly better than no answer.

    **Known limitation — the sweep can only add, never remove.** `save_results`
    merges by `record_key` and rewrites the union, so a false positive the
    better model correctly rejects survives in the database. Dropping bad
    records is a real reason to re-extract, and this does not deliver it;
    removals still go through the admin not-community flow. Fixing it means a
    per-source-URL reconciliation in `store.py`, which is a larger change than
    this sweep should carry.
    """
    router = getattr(extractor, "router", None)
    if router is None or not getattr(router, "enabled", False):
        return 0, []

    settings = router.catalogue.router
    best = router.best_available_quality()
    if best <= 0:
        log.info("quality_upgrade_skipped", reason="no free capacity left today")
        return 0, []
    threshold = router.upgrade_threshold()

    by_city = {c.name: c for c in cities}
    candidates = get_upgradable_pages(
        config.db_path, threshold, max(0, settings.upgrade_max_per_run), fingerprint,
        cities=list(by_city))
    if not candidates:
        log.info("quality_upgrade_nothing_to_do", threshold=threshold, best=best)
        return 0, []

    # Tier gate, mirroring run_pipeline and _run_full. Pages whose topic is
    # unknown to this run's config are skipped too: we cannot tell whether they
    # are frozen. (The city restriction is already applied in SQL.)
    topic_names = {t.name for t in topics}

    def _allowed(page: dict) -> bool:
        city = by_city.get(page.get("city") or "")
        topic = page.get("topic") or ""
        if city is None or topic not in topic_names:
            return False
        return _tier_allows(city, topic, config.core_topics)

    eligible = [p for p in candidates if _allowed(p)]
    if len(eligible) != len(candidates):
        log.info("quality_upgrade_tier_filtered",
                 kept=len(eligible), dropped=len(candidates) - len(eligible))
    candidates = eligible
    if not candidates:
        return 0, []

    log.info("quality_upgrade_start", pages=len(candidates),
             best_available=best, below_quality=threshold)
    all_fps = load_false_positives(config.db_path)
    upgraded = failed = 0
    total_new = 0
    pending: dict[tuple[str, str], list] = {}

    stopped: tuple[str, bool] | None = None
    for page in candidates:
        if _should_stop(stop_at, should_stop):
            log.info("quality_upgrade_window_closed", upgraded=upgraded)
            break
        # Re-checked every page: the fleet's budget drains as we spend it, and
        # once the best remaining model no longer clears the bar the sweep must
        # stop rather than downgrade a page it already extracted well.
        if router.best_available_quality() - settings.upgrade_min_gain < page["q"]:
            log.info("quality_upgrade_budget_spent", upgraded=upgraded)
            break
        await asyncio.sleep(0)
        entry = load_cache_page(config.db_path, page["url_hash"])
        text = (entry or {}).get("raw_text")
        if not text:
            continue
        city, topic = page.get("city") or "", page.get("topic") or ""
        # cache_pages rows carry no locale, so the old `entry.get("locale")`
        # always fell through to "hu" — which _parse_communities stamps onto
        # every record, relabelling German and Indonesian communities as
        # Hungarian. The city config is the authority, as in _run_full.
        locale = by_city[city].locale or "en"
        if on_progress:
            on_progress("extract", page["url"])
        try:
            records, model, quality = await _extract_traced(
                extractor,
                text=text, city=city, topic=topic,
                locale=locale, source_url=page["url"],
                false_positive_examples=build_prompt_section(all_fps, city=city, topic=topic),
            )
        except ExtractorUnavailableError as exc:
            # Keep the older, weaker result — it beats losing the page entirely.
            failed += 1
            log.warning("quality_upgrade_failed", url=page["url"], reason=str(exc))
            if (_stop := _stop_reason(extractor)):
                # Walking hundreds more pages against a fleet that has stopped
                # produces nothing but failure counters, and an outage here was
                # filed as a mere warning because the sweep has no other place
                # to record one.
                log.info("quality_upgrade_stopped", reason=_stop[0])
                stopped = _stop
                break
            continue
        finally:
            if on_progress:
                on_progress(None, None)

        if (quality or 0) <= page["q"]:
            # Failover handed the call to a model no better than the cached one.
            # Overwriting would be churn without gain.
            continue
        joinable = [r for r in records if r.joinable]
        await _off_loop(cache.save_extracted, page["url"], joinable, fingerprint=fingerprint,
                             model=model, quality=quality)
        if joinable:
            # Batched, never per page: save_results ends in a full topic
            # DELETE+reinsert, an O(n^2) dedup and a city-wide duplicate scan.
            # _run_full carries the same warning — doing it per URL is what made
            # an earlier version unusable at scale.
            pending.setdefault((city, topic), []).extend(joinable)
        upgraded += 1

    log.info("quality_upgrade_complete", upgraded=upgraded, failed=failed,
             new_records=total_new)
    for (city, topic), recs in pending.items():
        # save_results returns the pair's total stock, not the number added, so
        # the count comes from what we handed in — as at every other call site.
        await _off_loop(save_results, city, topic, recs, config.db_path)
        total_new += len(recs)

    if not (upgraded or failed):
        return total_new, []
    # Built from _new_pair_log, never hand-rolled: run_detail.html iterates
    # these keys under strict Jinja Undefined, and a missing one (it compared
    # `p.records_extracted > 0`) hard-fails the whole admin page.
    entry = _new_pair_log("—", "quality_upgrade", [])
    entry.update({
        "urls_found": len(candidates),
        "records_extracted": total_new,
        "extract_failed": failed,
        "cache_hits_extract": upgraded,
    })
    if stopped is not None and stopped[1]:
        entry["extract_error"] = stopped[0]
        entry["aborted"] = True
    return total_new, [entry]


async def _run_ai_only(
    cities: list[CityConfig],
    topics: list[TopicConfig],
    config: PipelineConfig,
    extractor: FallbackExtractor,
    cache: "CacheManager | None",
    skip_extracted: bool,
    run_stats: dict,
    on_progress: Callable[[str | None, str | None], None] | None,
    run_communities: bool = True,
    run_venues: bool = True,
    run_persons: bool = True,
    on_pair_start: "Callable[[str, str], None] | None" = None,
    pairs_filter: "set[tuple[str, str]] | None" = None,
    stop_at: "Any | None" = None,
    should_stop: "Callable[[], bool] | None" = None,
) -> tuple[int, list[dict]]:
    if not cache:
        log.warning("ai_only_mode_no_cache")
        return 0, []

    all_fps = load_false_positives(config.db_path)
    # Read once for the whole pass. A page that has failed `threshold` times at
    # this fingerprint is not attempted again: nothing about the run changes the
    # answer, and every attempt walks the fleet and is charged for it.
    quarantine = _Quarantine(cache, extractor.canonical_fingerprint,
                             config.extract_max_page_failures)
    gate = build_gate(config)
    log.info("ai_only_start", load_strategy="pair_by_pair",
             run_communities=run_communities, run_venues=run_venues,
             run_persons=run_persons, quarantined_pages=quarantine.size)

    total_new = 0
    pair_logs: list[dict] = []

    for city in cities:
        run_stats[city.name] = {}
        for topic in topics:
            if pairs_filter is not None and (city.name, topic.name) not in pairs_filter:
                continue
            if _should_stop(stop_at, should_stop):
                log.info("run_window_closed", city=city.name, topic=topic.name)
                return total_new, pair_logs
            await asyncio.sleep(0)
            if on_pair_start:
                on_pair_start(city.name, topic.name)
            pages = await asyncio.to_thread(
                cache.get_scraped_for_pair, city.name, topic.name
            )
            # Built from _new_pair_log, not by hand: run_detail.html iterates
            # these keys with strict Undefined, so a hand-written subset
            # renders as an error the moment the template touches a key this
            # branch happened not to set. It already omitted `aborted`,
            # `extract_error` and `extract_quarantined`.
            pair_log: dict = {
                **_new_pair_log(city.name, topic.name, []),
                "urls_found": len(pages),
                "fetched_urls": [url for url, _ in pages],
                "cache_hits_scrape": len(pages),
            }

            if not pages:
                log.info("ai_only_no_cache", city=city.name, topic=topic.name)
                run_stats[city.name][topic.name] = 0
                pair_logs.append(pair_log)
                continue

            log.info("ai_only_processing", city=city.name, topic=topic.name, pages=len(pages))
            extraction_fp_section = build_prompt_section(
                all_fps, city=city.name, topic=topic.name
            )
            records = []
            extract_dead = False

            # Cache first, then everything that missed — extracted together
            # rather than one page at a time. The loop below is unchanged; it
            # reads an answer that is already in hand instead of awaiting one.
            cached_by_url = {
                url: cache.get_extracted(url, fingerprint=extractor.canonical_fingerprint)
                for url, _ in pages
            }
            fresh_by_url: dict = {}
            pair_stop: "tuple[str, bool] | None" = None
            deferred_urls: set = set()
            # Held pages are counted, not attempted — and only when this pass
            # would have extracted communities at all. A venues-only run never
            # asks the question, so it must not report an answer.
            to_extract, quarantined_here, gated_here = [], 0, 0
            for _u, _t in pages:
                if cached_by_url.get(_u) is not None:
                    continue
                if run_communities and quarantine.holds(_u):
                    quarantined_here += 1
                    continue
                # Asked before the batch is assembled, not inside it: this pass
                # extracts several pages at once, and a page the gate rejects
                # should never occupy a slot in that batch.
                if run_communities and not await gate.allows(
                        _url_hash(_u), _u, _t, city.name, topic.name):
                    gated_here += 1
                    continue
                to_extract.append((_u, _t))
            if quarantined_here:
                pair_log["extract_quarantined"] = quarantined_here
            if gated_here:
                pair_log["gate_skipped"] = gated_here
            if run_communities and not extractor.exhausted:
                fresh_by_url, pair_stop, deferred_urls = await _extract_pair_pages(
                    extractor,
                    to_extract,
                    city=city, topic=topic, fp_section=extraction_fp_section,
                    concurrency=config.extract_concurrency, on_progress=on_progress,
                )
            if pair_stop is not None:
                # Recorded once, here, and not per url. A stop can arrive with
                # every page already attempted — nothing absent from the map to
                # notice it by — and counting it per url turned one outage into
                # one `extract_failed` per queued page. Setting `extract_dead`
                # before the loop also stops a page earlier in the list from
                # starting venue or person calls the fleet cannot serve.
                reason, is_outage = pair_stop
                if is_outage:
                    pair_log["extract_error"] = reason
                    pair_log["aborted"] = True
                log.info("extract_stopped_mid_pair", city=city.name,
                         topic=topic.name, reason=reason)
                extract_dead = True

            for url, text in pages:
                await asyncio.sleep(0)
                community_names: list[str] = []

                # ── Community extraction (with fingerprint cache) ────────────
                # Always read from cache for community_names (helps person extraction).
                # Only run fresh extraction when run_communities=True and cache misses.
                community_cache_hit = False
                cached = cached_by_url.get(url)
                if cached is not None:
                    log.debug("cache_hit_extract", url=url)
                    records.extend(cached)
                    community_names = [r.name for r in cached]
                    pair_log["cache_hits_extract"] += 1
                    pair_log["records_extracted"] += len(cached)
                    community_cache_hit = True

                if not community_cache_hit and run_communities:
                    if quarantine.holds(url):
                        # Not a failure of this run: it was decided on an
                        # earlier one and is counted separately, so the daily
                        # report shows a quarantined corpus as quarantined
                        # rather than as a run that keeps failing.
                        continue
                    outcome = fresh_by_url.get(url)
                    if outcome is None:
                        # Never attempted, for one of three reasons. The fleet
                        # stopped part-way (recorded above, once); the page kept
                        # losing the race for a slot (`deferred_urls` — nothing
                        # went wrong, it simply did not get a turn); or no
                        # provider was ever usable, which is the only one that
                        # is a failed page. Nothing was cached in any case, so
                        # the page is retried next pass.
                        if pair_stop is None and url not in deferred_urls:
                            pair_log["extract_failed"] = pair_log.get("extract_failed", 0) + 1
                        continue
                    if isinstance(outcome, BaseException):
                        pair_log["extract_failed"] = pair_log.get("extract_failed", 0) + 1
                        await quarantine.note(url, outcome)
                        log.warning("extract_unavailable_page_skipped", url=url,
                                    reason=str(outcome))
                        continue
                    extracted, _model, _quality = outcome

                    joinable = [r for r in extracted if r.joinable]
                    if len(joinable) < len(extracted):
                        log.info("joinability_filtered", url=url,
                                 kept=len(joinable), removed=len(extracted) - len(joinable))

                    await _off_loop(cache.save_extracted, url, joinable,
                                         fingerprint=extractor.canonical_fingerprint,
                                         model=_model, quality=_quality)
                    await quarantine.forgive(url)

                    records.extend(joinable)
                    total_new += len(joinable)
                    pair_log["records_extracted"] += len(joinable)
                    log.info("extracted", url=url, found=len(extracted), kept=len(joinable))
                    community_names = [r.name for r in joinable]

                # ── Venue extraction (with fingerprint cache) ────────────────
                # Gated on community_names — except for venues-only runs (see _run_full).
                if run_venues and not extract_dead and (
                        community_names or not run_communities) and cache.get_venue_extracted(
                        url, fingerprint=extractor.canonical_venue_fingerprint) is None:
                    try:
                        _topic_slugs = [t.name for t in topics]
                        venues = await extractor.extract_venues(
                            text, city.name, city.locale, url, valid_topics=_topic_slugs)
                        if venues:
                            await _off_loop(upsert_venues, config.db_path, [v.model_dump() for v in venues])
                            log.info("venues_extracted", url=url, found=len(venues))
                        await _off_loop(cache.save_venue_extracted, url, [v.model_dump() for v in venues],
                                                   fingerprint=extractor.canonical_venue_fingerprint,
                                                   model=extractor.model)
                    except Exception as exc:
                        # Counted like a failed community extraction: nothing was
                        # cached, so the page is retried — and the daily report
                        # must not show a venue-blind run as a clean ✓.
                        pair_log["extract_failed"] = pair_log.get("extract_failed", 0) + 1
                        log.warning("venues_extract_error", url=url, error=str(exc))
                        if (_reason := _mark_stop(extractor, pair_log)):
                            log.info("extract_paused_mid_page", url=url, reason=_reason)
                            extract_dead = True

                # ── Person extraction (with fingerprint cache) ───────────────
                if community_names:
                    _person_cache = cache.get_person_extracted(
                        url, city.name, topic.name, fingerprint=extractor.canonical_person_fingerprint)
                    if _person_cache is not None:
                        if _person_cache:
                            log.debug("person_cache_hit", url=url, cached=len(_person_cache))
                    elif run_persons and not extract_dead:
                        try:
                            persons = await extractor.extract_persons(
                                text, city.name, topic.name, city.locale, url, community_names,
                            )
                            if persons:
                                await _off_loop(upsert_persons, config.db_path, [p.model_dump() for p in persons])
                                log.info("persons_extracted", url=url, found=len(persons))
                            else:
                                log.info("persons_extract_zero", url=url,
                                         communities=len(community_names))
                            await _off_loop(cache.save_person_extracted, url, city.name, topic.name,
                                                        [p.model_dump() for p in persons],
                                                        fingerprint=extractor.canonical_person_fingerprint,
                                                        model=extractor.model)
                        except Exception as exc:
                            pair_log["extract_failed"] = pair_log.get("extract_failed", 0) + 1
                            log.warning("persons_extract_error", url=url, error=str(exc))
                            if (_reason := _mark_stop(extractor, pair_log)):
                                log.info("extract_paused_mid_page", url=url, reason=_reason)
                                extract_dead = True

            # ── Synthesize PersonRecords from community leader fields ────────
            if run_persons:
                leader_persons = _persons_from_leaders(records, city.name, topic.name)
                # Communities that still yield synthesized leaders get the full
                # replace (legacy behavior). Everything else only drops rows
                # explicitly marked origin='leader_field', so a stale synthesized
                # leader disappears while independently AI-extracted leader
                # persons survive.
                synth_names = {p.community_name for p in leader_persons}
                for rec in records:
                    await _off_loop(delete_leader_persons_for_community,
                        config.db_path, rec.name, city.name,
                        only_synthesized=rec.name not in synth_names)
                if leader_persons:
                    await _off_loop(upsert_persons, config.db_path,
                                    [{**p.model_dump(), "origin": "leader_field"}
                                     for p in leader_persons])
                    log.info("persons_from_leaders", city=city.name, topic=topic.name,
                             found=len(leader_persons))

            count = await _off_loop(save_results, city.name, topic.name, records, config.db_path)
            run_stats[city.name][topic.name] = count
            pair_logs.append(pair_log)

            if extract_dead:
                if pair_log.get("aborted"):
                    log.warning("extract_provider_down_run_aborted", city=city.name,
                                topic=topic.name, reason=pair_log.get("extract_error"))
                else:
                    # Alive, just out of budget or inside a back-off window:
                    # nothing is cached, the pages retry, and the run must not
                    # be reported as a provider failure.
                    log.info("extract_window_stopped", city=city.name, topic=topic.name)
                return total_new, pair_logs

    return total_new, pair_logs


async def scrape_submitted_url(
    db_path: Path,
    config: "PipelineConfig",
    city: str,
    topic: str,
    url: str,
) -> bool:
    extractor: FallbackExtractor = build_extractor(config)

    text = await fetch_and_clean(url, blocked_domains=[], timeout_seconds=15)
    if not text:
        log.warning("scrape_submitted_url_no_text", url=url)
        return False

    all_fps = load_false_positives(db_path)
    try:
        records = await extractor.extract(
            text=text, city=city, topic=topic, locale="hu", source_url=url,
            false_positive_examples=build_prompt_section(all_fps, city=city, topic=topic),
        )
    except ExtractorUnavailableError as exc:
        # BackgroundTasks has no error surface — log loudly; the submission stays
        # approved and can be re-run from the admin cache page.
        log.error("scrape_submitted_url_extract_failed", city=city, topic=topic,
                  url=url, reason=str(exc))
        return False
    # Same joinable gate as the main pipeline — without it these flows
    # published records the normal run would reject.
    records = [r for r in records if r.joinable]
    await _off_loop(save_results, city, topic, records, db_path)
    log.info("scrape_submitted_url_done", city=city, topic=topic, url=url, found=len(records))
    return True


async def reextract_community(
    db_path: Path,
    config: "PipelineConfig",
    community_id: str,
) -> bool:
    record = find_community_by_id(db_path, community_id)
    if not record:
        log.warning("reextract_community_not_found", community_id=community_id)
        return False

    source_url = record.get("source_url", "")
    if not source_url:
        log.warning("reextract_community_no_source_url", community_id=community_id)
        return False

    city = record.get("city", "")
    topic = record.get("topic", "")

    url_hash = hashlib.sha256(source_url.encode()).hexdigest()[:16]
    cached = load_cache_page(db_path, url_hash)
    text = cached.get("raw_text") if cached else None

    if not text:
        text = await fetch_and_clean(source_url, blocked_domains=[], timeout_seconds=15)
    if not text:
        log.warning("reextract_community_no_text", community_id=community_id, url=source_url)
        return False

    extractor: FallbackExtractor = build_extractor(config)

    all_fps = load_false_positives(db_path)
    try:
        records = await extractor.extract(
            text=text, city=city, topic=topic, locale=record.get("locale", "hu"), source_url=source_url,
            false_positive_examples=build_prompt_section(all_fps, city=city, topic=topic),
        )
    except ExtractorUnavailableError as exc:
        log.error("reextract_community_extract_failed", community_id=community_id,
                  reason=str(exc))
        return False
    # Same joinable gate as the main pipeline — without it these flows
    # published records the normal run would reject.
    records = [r for r in records if r.joinable]
    await _off_loop(save_results, city, topic, records, db_path)
    log.info("reextract_community_done", community_id=community_id, found=len(records))
    return True


