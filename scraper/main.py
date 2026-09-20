import argparse
import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path

import structlog
import uvicorn
import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .cache import CacheManager
from .config import CONFIG_DIR, load_config
from .db import (backfill_records_count, get_last_run,
                 init_db)
from .pipeline import (WORKER_EXTRACT, next_worker_action, run_pipeline, worker_after_run,
                        worker_outcome)
from .router import build_router
from .web.app import app as web_app, templates
from .web.log_stream import broadcaster
from .web.state import app_state

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
#: Fallback for a malformed `report_cron` — the only cron left in the system.
_REPORT_CRON = "30 4 * * *"


def broadcast_processor(logger, method, event_dict):
    broadcaster.add_line({k: str(v) for k, v in event_dict.items()})
    return event_dict


def configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            broadcast_processor,
            structlog.dev.ConsoleRenderer(),
        ]
    )


def _build_version() -> str:
    version_file = BASE_DIR / "VERSION"
    if version_file.exists():
        ts = version_file.read_text().strip()
        if ts:
            return "v." + ts
    return "v.unknown"


def _cron_fields(cron_expr: str, fallback: str = _REPORT_CRON) -> tuple[str, str, str, str, str]:
    fields = cron_expr.split()
    if len(fields) == 5:
        return fields[0], fields[1], fields[2], fields[3], fields[4]

    log = structlog.get_logger()
    log.warning("invalid_cron_expression", cron=cron_expr, fallback=fallback)
    fallback_fields = fallback.split()
    if len(fallback_fields) != 5:
        raise ValueError(f"Fallback cron must have 5 fields: {fallback}")
    return (
        fallback_fields[0],
        fallback_fields[1],
        fallback_fields[2],
        fallback_fields[3],
        fallback_fields[4],
    )


def _settings_schedule() -> dict:
    """The full schedule: block from settings.yaml ({} on any error)."""
    try:
        settings = yaml.safe_load((CONFIG_DIR / "settings.yaml").read_text(encoding="utf-8")) or {}
        schedule = settings.get("schedule", {})
        return schedule if isinstance(schedule, dict) else {}
    except Exception:
        return {}


def _settings_country_priority() -> list[str] | None:
    """pipeline.country_priority from settings.yaml, or None to use the default.

    Lets an operator re-order expansion markets by editing config (which is a
    mounted volume in production) instead of shipping a code change.
    """
    try:
        settings = yaml.safe_load((CONFIG_DIR / "settings.yaml").read_text(encoding="utf-8")) or {}
        order = (settings.get("pipeline") or {}).get("country_priority")
        if isinstance(order, list) and all(isinstance(c, str) for c in order) and order:
            return order
    except Exception:
        pass
    return None


# Order the worker walks countries in. A pass ends when the work signal flips,
# so a country listed after one with a large unfinished backlog may not be
# reached in that pass — this list *is* the expansion priority.
# Overridable from settings.yaml as `pipeline.country_priority`.
#
# 2026-08-16: Hungary moved to the front. It used to sit last because it was
# fully indexed and the done-pair pre-filter fast-skipped it; the 1000+ inhabitant
# import added 973 unprocessed settlements, making it the largest available
# content gain on the primary (kozossegek.com) market. Germany follows as the
# active international expansion, then Indonesia (opened 2026-08-16).
DEFAULT_COUNTRY_PRIORITY = ["Hungary", "Germany", "Indonesia", "Sweden"]


def _saver_city_groups(cities: list, priority: list[str] | None = None) -> list[list]:
    """Group cities into the order the worker should process them.

    Returns one group per named country, then a final group with everything
    else. Countries named but absent from the city list yield empty groups,
    which the caller skips.
    """
    order = priority or DEFAULT_COUNTRY_PRIORITY
    groups = [[city for city in cities if city.country == country] for country in order]
    named = set(order)
    groups.append([city for city in cities if city.country not in named])
    return groups


async def _sleep(seconds: float) -> None:
    """The one place the enrichment loop waits on the clock.

    Named so a test can substitute it, exactly as `extract._sleep` is — see that
    docstring for why this is measured rather than stylistic. Here the stake is
    a loop whose pauses are 75 and 900 seconds: without the seam, one test of
    the rate-limit branch would cost more wall-clock time than the whole suite.
    """
    await asyncio.sleep(seconds)


#: How long enrichment waits out a per-minute limit before trying again.
#: Longer than the 60s windows the free tiers publish, short enough that a
#: 9.5-hour budget is not spent asleep.
_ENRICH_RATE_LIMIT_PAUSE_S = 75

#: How long enrichment waits when there is nothing to do — the pool is empty,
#: or the daily quota is spent. Long enough to be cheap, short enough that a
#: midnight reset or a fresh batch of extractions is picked up promptly.
_ENRICH_IDLE_PAUSE_S = 900


async def _enrich_body(cfg, enrich_batch,
                       _build_extractor, free_quota_available) -> None:
    """Run enrichment rounds until the pool empties or the task is cancelled.

    There is no window. The twin-cron schedule that gave this one was deleted
    on 2026-09-20 (see [[continuous-worker]]); what remains is the worker, and
    under it the loop ends only when cancelled.

    Module level, not a closure inside `main()`, so the suite can reach it: the
    signature already injected `enrich_batch` and `_build_extractor` as if it
    were testable, and it was not — `free_quota_available` completes the set.
    Every wait goes through `_sleep`, the same seam `extract.py` uses, so a test
    substitutes a virtual clock instead of spending 15 real minutes per pause.
    """
    log = structlog.get_logger()
    extractor = _build_extractor(app_state.pipeline_cfg)
    if extractor.exhausted:
        log.info("enrich_skipped", reason="no_extractor")
        return
    # Probe the fleet before the batch, as run_pipeline does. Without it a
    # stale model name costs one wasted request *per record* for the whole
    # window — on 2026-08-16 every enrich call fanned out across four dead
    # models before giving up.
    try:
        await extractor.preflight()
    except Exception as exc:
        log.warning("enrich_skipped", reason="preflight_failed", error=str(exc))
        return

    async def _pause(seconds: float, reason: str) -> None:
        """Wait, then start the next round on a freshly built chain.

        A provider retired by the circuit breaker stays retired for the life
        of one extractor, and this run has no life span under
        `worker_enabled` — so a provider that comes back is never noticed.
        On 2026-09-17 `localgpu` spent the day answering 401 (its machine had
        been reinstalled with a different key), was retired here, and every
        batch went on reporting "all providers rate limited" for 20 minutes
        after the machine was verifiably serving the `ai_only` pipeline —
        which had rebuilt its own chain at its next preflight. A container
        restart was the only cure.

        Rebuilding is a config and ledger read, no HTTP, and it happens only
        on a path that is already sleeping for 75 s or more. Deliberately no
        preflight: model names cannot change without a deploy, so the probe
        that is right once per run would here be one wasted call per pause.
        """
        nonlocal extractor
        await _sleep(seconds)
        extractor = _build_extractor(app_state.pipeline_cfg)
        log.info("enrich_chain_rebuilt", reason=reason)

    # The same country priority the pipeline walks, for the same reason.
    # `get_enrichment_candidates` orders by id, and the international
    # records were imported first — so an unscoped enrichment spends the
    # whole budget on the secondary market before reaching a Hungarian
    # record. On 2026-08-20 that was 488 international records updated and
    # three Hungarian ones.
    _groups = [g for g in _saver_city_groups(app_state.cities or [],
                                             _settings_country_priority()) if g]
    scope = {c.name for c in (_groups[0] if _groups else [])}
    if not scope:
        return
    limit = int(cfg.get("enrich_batch_limit") or 200)
    # No deadline: the batch stops when the fleet runs out of quota and waits
    # out per-minute limits, which is the only thing that ever needed a clock.
    # The parameter stays on enrich_batch for the off-peak pricing case, should
    # a paid provider ever return.
    deadline = None
    total = 0
    try:
        while True:
            # Deliberately NOT yielding to extraction. That was tried on
            # 2026-08-21 and reverted the same day: enrichment was taking
            # two thirds of the free budget, which looked like the problem
            # until the traffic numbers were put next to it. 42,091
            # community pages, 68% of them with no long description, and 34
            # visitors a day — the marginal value of page 42,092 is close
            # to zero, and making 28,795 thin pages rankable is not.
            #
            # The share was never the fault. Spending it on the secondary
            # market was, and the country priority above fixes that.
            stats = await enrich_batch(
                app_state.db_path, extractor, scope, limit=limit,
                fetch_missing=False,
                blocked_domains=app_state.pipeline_cfg.fetch_blocked_domains,
                deadline=deadline)
            total += stats["enriched"]  # count before any early exit
            if stats.get("stopped_at_deadline"):
                # Unreachable while `deadline` is None; kept because
                # enrich_batch owns the contract, not this loop.
                log.info("enrich_deadline_reached", enriched_this_window=total)
                break
            if stats["pool"] == 0:
                log.info("enrich_complete", enriched_this_window=total,
                         scope_size=len(scope))
                # This market is caught up; hand the budget to the next one
                # rather than idling while a lower-priority backlog waits.
                _groups = _groups[1:]
                if _groups:
                    scope = {c.name for c in _groups[0]}
                    log.info("enrich_scope_advanced", cities=len(scope))
                    continue
                # Caught up. Extraction keeps adding communities, so wait
                # for them rather than ending — there is no cron that would
                # start this again.
                await _pause(_ENRICH_IDLE_PAUSE_S, "caught_up")
                continue
            # Provider down: enrich_batch fails fast and leaves candidates
            # unmarked, so pool stays nonzero — bail out instead of tight-looping.
            if stats.get("stopped_rate_limited"):
                # A per-minute limit is the fleet asking us to slow down,
                # and waiting it out is right — unless there is no daily
                # budget left to wait for. A spent allowance also answers
                # 429, and on 2026-08-18 that had enrichment retry every 75
                # seconds for hours: 37 batches, zero records, every attempt
                # another refused call.
                if not free_quota_available():
                    log.info("enrich_waiting_for_quota_reset",
                             enriched_this_window=total)
                    await _pause(_ENRICH_IDLE_PAUSE_S, "quota_reset")
                    continue
                log.info("enrich_waiting_out_rate_limit", enriched_this_window=total)
                await _pause(_ENRICH_RATE_LIMIT_PAUSE_S, "rate_limited")
                continue
            # `stopped_no_provider` is the batch's own verdict and must be
            # honoured even when it enriched a few records first — otherwise
            # a batch that managed five before the fleet went quiet simply
            # starts another doomed one.
            if (stats.get("stopped_no_provider")
                    or extractor.exhausted
                    or (stats["enriched"] == 0 and stats["failed"] > 0)):
                log.warning("enrich_aborted_provider_down", enriched_this_window=total)
                # Out of daily quota, most likely. It returns at 00:00 UTC
                # and this is the only thing waiting for it — sleeping is
                # how enrichment resumes on the new day without a cron.
                await _pause(_ENRICH_IDLE_PAUSE_S, "provider_down")
                continue
            await _sleep(1)  # yield to the event loop between rounds
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.error("enrich_run_failed", error=str(exc))
    finally:
        app_state._enrich_running = False
        app_state._enrich_task = None


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-once", action="store_true")
    args = parser.parse_args()

    configure_logging()
    log = structlog.get_logger()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # History goes to the persisted volume, next to the database. The ring in
    # memory holds a few minutes under the worker's load, which is not a log.
    broadcaster.attach_file(DATA_DIR / "logs")
    db_path = DATA_DIR / "scraper.db"
    init_db(db_path)
    #: The migration is resumable, so a retry costs only the rows still NULL.
    _BACKFILL_ATTEMPTS = 5
    _BACKFILL_RETRY_S = 120
    # Off the startup path: ~97 s for the production corpus, and the done-pair
    # filter reads the blob for whatever this has not reached, so nothing waits
    # on it. Chunked, so the crawler keeps its turn at the writer lock.
    async def _backfill_once() -> None:
        # Retried: a transient write lock or one malformed blob used to end the
        # migration for the lifetime of the process. Reads stay correct either
        # way — the filter falls back to the blob — but the rows it has not
        # reached are exactly the ones that keep the scan slow, so giving up
        # silently leaves the performance fix half-applied until a restart.
        for attempt in range(_BACKFILL_ATTEMPTS):
            try:
                await asyncio.to_thread(backfill_records_count, db_path)
                return
            except Exception as exc:  # noqa: BLE001 — never stop boot for this
                log.warning("records_count_backfill_failed",
                            attempt=attempt + 1, error=str(exc))
                await asyncio.sleep(_BACKFILL_RETRY_S)
    app_state._backfill_task = asyncio.create_task(_backfill_once())

    cities, topics, pipeline_cfg = load_config(db_path)
    cache = CacheManager(db_path)

    app_state.cities = cities
    app_state.topics = topics
    app_state.pipeline_cfg = pipeline_cfg
    app_state.cache_manager = cache
    app_state.db_path = db_path
    app_state.version = _build_version()
    templates.env.globals["app_version"] = app_state.version

    persisted = get_last_run(db_path)
    if persisted:
        app_state.last_run_at = persisted
        log.info("restored_last_run_at", last_run_at=persisted.isoformat())

    if args.run_once:
        await run_pipeline(cities, topics, pipeline_cfg, cache=cache)
        return

    def _on_progress(phase: str | None, url: str | None) -> None:
        app_state.current_phase = phase
        app_state.current_url = url

    def _on_pair_start(city: str, topic: str) -> None:
        app_state.current_city = city
        app_state.current_topic = topic

    def _release_enrich() -> None:
        """Hand the enrichment slot back on an early return."""
        app_state._enrich_running = False
        app_state._enrich_task = None

    async def _enrich_run() -> None:
        """SEO description enrichment. Started by the worker, which also restarts
        it if it ever stops. Processes bounded rounds until no candidates remain;
        idempotent/resumable via the long_description marker. Does NOT reserve the
        pipeline slot — it deliberately coexists with the ai_only extractor
        (_merge_source_urls keeps enriched fields safe if both touch a row)."""
        if app_state._enrich_running:
            log.info("enrich_skipped", reason="already_running")
            return
        # Claimed here, before the first await. The flag used to be set after
        # the extractor was built, so two starts — the boot task and the worker's
        # self-healing restart — could both pass the check and run in parallel,
        # doubling provider calls and leaving one uncancellable through
        # `_enrich_task`.
        app_state._enrich_running = True
        app_state._enrich_task = asyncio.current_task()
        cfg = _settings_schedule()
        if not app_state.db_path or not app_state.pipeline_cfg:
            _release_enrich()
            return
        from .enrich import enrich_batch
        from .web.app import _build_extractor
        try:
            return await _enrich_body(cfg, enrich_batch,
                                      _build_extractor, _free_quota_available)
        finally:
            # Everything below the slot claim runs under this, including the
            # cancellable preflight: a stop during it used to leave
            # `_enrich_running` true for the life of the process.
            _release_enrich()

    scheduler = AsyncIOScheduler()

    # ── Continuous worker ─────────────────────────────────────────────────────
    #: Nothing here is clock-bound. The twin windows existed for two reasons and
    #: both are gone: DeepSeek's off-peak discount (extraction moved to a free
    #: fleet) and a belief that DataForSEO was cheaper at certain hours (it is
    #: not — priced by queue, verified 2026-08-18). What is left is one real
    #: constraint: only one pipeline run at a time. So the work chooses itself.
    #:
    #:   free quota left  → extract, because it expires at 00:00 UTC
    #:   none left        → collect, because that is what money buys
    #:
    #: The daily reset is the only time anything happens "at" a time, and even
    #: that is not scheduled: quota returning is simply a condition the collector
    #: is watching for, so extraction resumes on its own.

    #: Idle wait between iterations. Long enough not to spin, short enough that a
    #: quota reset or a finished run is picked up promptly.
    _WORKER_IDLE_SECONDS = 60
    #: After an extraction pass that found nothing, how long before asking again.
    #: Without it the worker would relaunch an empty run every minute.
    _WORKER_EXTRACT_RETRY_S = 900
    #: Consecutive extraction passes producing no records before standing aside
    #: regardless of what the work signal says. A backstop, not the mechanism.
    _WORKER_EMPTY_LIMIT = 3

    #: The quota answer is cached for this long. `should_stop` is consulted
    #: between every pair, and building a router parses the provider catalogue
    #: and reads the ledger — per pair, with eight searches in flight, that is a
    #: real cost for a number that changes on the scale of minutes.
    _QUOTA_CACHE_SECONDS = 30.0
    _quota_cache: dict = {"at": 0.0, "value": True}

    def _free_quota_available() -> bool:
        """True when some provider still has daily budget. Never raises."""
        import time as _t
        now = _t.monotonic()
        if now - _quota_cache["at"] < _QUOTA_CACHE_SECONDS:
            return bool(_quota_cache["value"])
        try:
            cfg = app_state.pipeline_cfg
            if cfg is None:
                return False
            mr = build_router(
                app_state.db_path,
                temperature=cfg.deepseek_temperature,
                timeout_seconds=cfg.deepseek_timeout,
                max_text_chars=cfg.deepseek_max_text_chars,
                rate_limit_seconds=cfg.deepseek_rate_limit_seconds,
                fingerprint_model=cfg.deepseek_fingerprint_model or cfg.deepseek_model,
            )
            value = bool(mr and mr.enabled and mr.has_capacity())
        except Exception as exc:
            log.warning("worker_quota_check_failed", error=str(exc))
            # Assume there is quota: a broken check must not park the extractor
            # for the rest of the day.
            value = True
        _quota_cache.update(at=now, value=value)
        return value

    async def _worker_loop() -> None:
        from .web.app import _build_extractor, launch_pipeline_run
        from .guides import publish_daily_guides
        import time as _time
        extract_idle_until = 0.0
        empty_extractions = 0
        empty_collections = 0
        guides_checked_day = ""
        guides_retry_at = 0.0
        log.info("worker_started")
        while True:
            try:
                if app_state.is_running:
                    # A manual or control-API run owns the slot. Leave it alone.
                    await asyncio.sleep(_WORKER_IDLE_SECONDS)
                    continue
                # Publication is deliberately first in the daily queue. It
                # spends a small, bounded number of fleet calls on data we
                # already own; the DB makes the cap restart-safe across sites.
                utc_day = datetime.now(timezone.utc).date().isoformat()
                if (schedule_cfg.get("guides_enabled")
                        and guides_checked_day != utc_day
                        and _time.monotonic() >= guides_retry_at
                        and not getattr(app_state, "worker_paused", False)):
                    try:
                        writer = _build_extractor(app_state.pipeline_cfg)
                        if writer.exhausted:
                            raise RuntimeError("no model available for daily guides")
                        published = await publish_daily_guides(
                            app_state.db_path,
                            app_state.cities or [],
                            writer,
                            limit=int(schedule_cfg.get("guides_daily_limit") or 10),
                            min_communities=int(schedule_cfg.get("guides_min_communities") or 8),
                            min_description_ratio=float(
                                schedule_cfg.get("guides_min_description_ratio") or 0.6),
                            min_dimensions=int(schedule_cfg.get("guides_min_dimensions") or 3),
                            country_priority=_settings_country_priority(),
                        )
                        guides_checked_day = utc_day
                        log.info("daily_guides_published", count=len(published),
                                 slugs=[g["slug"] for g in published])
                    except Exception as exc:  # fleet/quota failure: retry, do not block worker
                        guides_retry_at = _time.monotonic() + _WORKER_EXTRACT_RETRY_S
                        log.warning("daily_guides_deferred", error=str(exc),
                                    retry_s=_WORKER_EXTRACT_RETRY_S)
                if (schedule_cfg.get("enrich_enabled")
                        and not app_state._enrich_running
                        and not getattr(app_state, "worker_paused", False)):
                    # Self-healing: enrichment used to be started only at boot,
                    # so once stopped it stayed stopped until the next deploy.
                    app_state._enrich_boot_task = asyncio.create_task(_enrich_run())
                if getattr(app_state, "worker_paused", False):
                    # Stopped by an operator. Without this the worker simply
                    # started another run the moment the cancelled one ended,
                    # so /v1/control/stop could not actually stop anything.
                    await asyncio.sleep(_WORKER_IDLE_SECONDS)
                    continue

                quota = _free_quota_available()
                extract_ready = _time.monotonic() >= extract_idle_until
                mode = next_worker_action(
                    is_running=False, paused=False,
                    quota=quota, extract_ready=extract_ready)
                if mode == WORKER_EXTRACT:
                    # Stop when the budget is gone — collection is what is left
                    # to do, and it costs money rather than a daily allowance.
                    def _preempt() -> bool:
                        return not _free_quota_available()
                else:
                    # Stop when the budget comes back. At 00:00 UTC the ledger
                    # rolls over and this turns true on its own, which is the
                    # whole of "start extraction after the reset".
                    def _preempt() -> bool:
                        return (_free_quota_available()
                                and _time.monotonic() >= extract_idle_until)

                finished = asyncio.Event()
                outcome: dict = {}

                def _on_finished(pair_logs: list, total_new: int) -> None:
                    outcome.update(worker_outcome(pair_logs, total_new))
                    finished.set()

                cfg = app_state.pipeline_cfg
                started, reason = launch_pipeline_run(
                    mode,
                    # The cache flags are the whole point of a saver run and the
                    # launcher's defaults are the admin form's ("Full Refresh").
                    # Without these, search_only would re-buy every search we
                    # already own and ai_only would re-extract every done page —
                    # which also means it would never look idle, so the worker
                    # would never collect again.
                    skip_scraped=bool(getattr(cfg, "cache_skip_scraped", True)),
                    skip_extracted=bool(getattr(cfg, "cache_skip_extracted", True)),
                    should_stop=_preempt, on_finished=_on_finished)
                if not started:
                    log.info("worker_run_skipped", mode=mode, reason=reason)
                    await asyncio.sleep(_WORKER_IDLE_SECONDS)
                    continue

                log.info("worker_run_started", mode=mode, quota=quota)
                # Wait on the task, not only on the callback: a task cancelled
                # before it ever ran never reaches its finally, and the callback
                # would never fire. asyncio.wait returns for done, cancelled and
                # failed alike, and never re-raises.
                run_task = getattr(app_state, "_run_task", None)
                if run_task is not None:
                    await asyncio.wait({run_task})
                else:
                    await finished.wait()
                # Held from before the await: the run's finally releases the
                # coordinator and clears app_state._run_task, so asking
                # afterwards always said "not cancelled".
                was_cancelled = bool(run_task is not None and run_task.cancelled())
                log.info("worker_run_finished", mode=mode, pairs=outcome.get("pairs"),
                         worked=outcome.get("worked"), fetched=outcome.get("fetched"),
                         new_records=outcome.get("new"), cancelled=was_cancelled)

                after = worker_after_run(
                    mode=mode,
                    worked=int(outcome.get("worked") or 0),
                    fetched=int(outcome.get("fetched") or 0),
                    new_records=int(outcome.get("new") or 0),
                    cancelled=was_cancelled,
                    empty_extractions=empty_extractions,
                    empty_collections=empty_collections,
                    empty_limit=_WORKER_EMPTY_LIMIT,
                    idle_s=_WORKER_IDLE_SECONDS,
                    retry_s=_WORKER_EXTRACT_RETRY_S)
                if mode == "ai_only" and after.extract_cooldown:
                    # Log the decision, not the counter: the old line fired on
                    # every pass once the counter sat at the limit, which said
                    # nothing new, and stayed silent when a single empty pass
                    # parked extraction for the same quarter hour.
                    log.info("worker_extraction_idle",
                             consecutive_empty=after.empty_extractions,
                             parked_s=after.extract_cooldown)
                empty_extractions = after.empty_extractions
                empty_collections = after.empty_collections
                if after.extract_cooldown is not None:
                    # 0 releases extraction; anything else parks it. Never a
                    # ratchet: pushing the cooldown forward on every empty pass
                    # kept extraction off even after the quota reset.
                    extract_idle_until = (
                        _time.monotonic() + after.extract_cooldown
                        if after.extract_cooldown else 0.0)
                if after.sleep:
                    await asyncio.sleep(after.sleep)
            except asyncio.CancelledError:
                log.info("worker_stopped")
                raise
            except Exception as exc:
                log.error("worker_iteration_failed", error=str(exc))
                await asyncio.sleep(_WORKER_IDLE_SECONDS)

    scheduler.start()
    app_state.scheduler = scheduler
    schedule_cfg = _settings_schedule()
    worker_enabled = bool(schedule_cfg.get("worker_enabled"))
    if worker_enabled:
        # The only thing that runs the pipeline. `worker_enabled: false` now
        # means "run nothing" — a maintenance switch, not a fallback to the
        # twin crons, which were deleted on 2026-09-20 after 33 days in which
        # nothing registered them. martinfowler.com/articles/feature-toggles.html
        app_state._worker_task = asyncio.create_task(_worker_loop())
        if schedule_cfg.get("enrich_enabled"):
            # Enrichment coexists with extraction (it does not take the run
            # slot) and has no window to wait for, so it simply runs. The worker
            # restarts it if it ever stops.
            app_state._enrich_boot_task = asyncio.create_task(_enrich_run())

    if _settings_schedule().get("report_enabled"):
        async def _daily_report_job() -> None:
            from .report import send_daily_report
            hu = {c.name for c in (app_state.cities or []) if c.country == "Hungary"}
            try:
                await send_daily_report(db_path, hu)
            except Exception as exc:
                log.error("daily_report_failed", error=str(exc))
        rm, rh, rd, rmo, rdow = _cron_fields(
            str(_settings_schedule().get("report_cron") or _REPORT_CRON), _REPORT_CRON)
        scheduler.add_job(_daily_report_job, CronTrigger(
            minute=rm, hour=rh, day=rd, month=rmo, day_of_week=rdow), misfire_grace_time=3600)
        log.info("scheduler_report_enabled", cron=_settings_schedule().get("report_cron"))

    if not worker_enabled:
        log.warning("worker_disabled", version=app_state.version,
                    hint="schedule.worker_enabled is off — nothing will run")

    config = uvicorn.Config(
        web_app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=8000,
        log_level="warning",
        loop="asyncio",
    )
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
