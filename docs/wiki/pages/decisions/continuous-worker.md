---
type: Decision
title: Continuous Worker
description: Why the twin time windows were deleted, what decides the work now, and the ten defects the reviews found in getting there.
tags: [schedule, pipeline, worker, operations, control-api]
timestamp: 2026-09-20
resource: scraper/main.py
---

# Continuous Worker

*Nothing is clock-bound any more. The work chooses itself.*

## Why the windows went

The twin schedule — collect 10:30-23:50, extract 00:30-10:00 — existed for two
reasons, and by 2026-08-18 neither was true:

- **DeepSeek's off-peak discount.** Extraction moved to a free-tier fleet whose
  allowance resets at 00:00 UTC ([[free-tier-model-router]]), so the discount
  window stopped meaning anything.
- **A belief that DataForSEO was cheaper at certain hours.** It is not.
  Verified against their published pricing: the price is set by *queue*, not by
  clock — $0.6/1K normal, $1.2/1K priority, $2/1K live. There was never a
  cheaper hour.

What remained was one real constraint: only one pipeline run at a time. That is
a condition, not a schedule.

## What decides the work

    free quota left  ->  extract, because it expires at 00:00 UTC
    none left        ->  collect, because that is what money buys

The daily reset is the only thing that happens "at" a time, and even that is
not scheduled. The collector runs with a `should_stop` predicate that asks "has
the quota come back?", so at midnight it winds up between pairs and extraction
takes over on its own. `run_pipeline` gained that predicate alongside `stop_at`
precisely because a deadline cannot express a condition.

A restart is indistinguishable from a continuation: the worker starts on boot
and picks the right work immediately, and every finished pair is already cached
(the fingerprint-keyed extraction cache and `search_cache`). **This is what stopped a deploy from
costing a collection run** — on 2026-08-17 three collector runs died as `run
cancelled` with 0 pairs because deploys landed mid-window, and the day yielded
206 pairs instead of ~800. Startup recovery is skipped entirely now; there is
nothing to recover.

Shipped behind `schedule.worker_enabled`, an ops toggle configured in source
control, with the twin crons left in place and disabled until the worker has
proven itself ([feature toggles](https://martinfowler.com/articles/feature-toggles.html),
[strangler fig](https://martinfowler.com/bliki/StranglerFigApplication.html)).

## Operating it

`/v1/control/{status,run,stop,resume}`, Bearer auth. Deliberately **not** part
of the OpenAI-compatible surface: `/v1/chat/completions` is a published
interface other software depends on, so the operator endpoints get their own
prefix and their own key — `CONTROL_API_KEY`, falling back to `ROUTER_API_KEY`
with a warning ([published interface](https://martinfowler.com/bliki/PublishedInterface.html)).

`GET /v1/backlog` answers "is there work?" directly, and echoes the settings the
running process actually has. Both exist because those questions kept being
answered by inference from a log buffer that holds a few minutes.

`launch_pipeline_run()` is the one place a run is reserved, started, recorded
and classified. The admin form, the control API and the worker all call it.

## What the reviews found

Ten defects across three rounds, and the shape is worth keeping:

| | |
|---|---|
| The worker inherited the launcher's defaults, which are the admin form's **Full Refresh** | would have re-bought all 45,570 pairs of search, and re-extracted every finished page — so the extractor never looks idle and collection never runs |
| "Did extraction find work?" was `len(pair_logs)` | `ai_only` logs a pair even with no cached pages, and every never-searched pair is in the filter, so an empty pass looked busy |
| Stop cancelled the run but did not pause the worker | the next run started within the minute; nothing could be stopped |
| The cancellation check read `_run_task` after the run's `finally` cleared it | a cancelled pass was always read as "found nothing" |
| Enrichment could run twice, and a cancel during preflight leaked its slot forever | the guard was set after an `await` |
| An empty collection pass cleared the extraction cooldown | a caught-up system alternated empty runs with no pause, writing a run record each time |
| Fixing that made the cooldown a **ratchet** | every empty pass pushed it 15 minutes further out, so extraction never resumed after the quota reset |
| The admin Run button did not clear the pause | one run, then paused until a restart |

Two of these were introduced by the previous round's fix. Both times the
mistake was the same: changing when a flag is set without asking what else
reads it.

## The one that was not the worker's fault

Minutes after the collector went to eight-way concurrency, the container ran
out of file descriptors:

```
providers_config_unreadable  [Errno 24] Too many open files
quota_ledger_unreadable      unable to open database file
```

Every HTTP request opened its own client, and the standard-mode search opened
one **per poll** — up to 150 per search at normal priority. The visible symptom
was SQLite and a YAML file failing to open: socket exhaustion three levels from
where it hurt. Clients are pooled per event loop now (per loop, because a client
holds connections belonging to the loop that created them).

Concurrency did not cause this. It revealed it — one connection per request was
always wrong.

## Two measures, not one (2026-08-23)

`pages_worked` answers "did extraction do anything?" — found, minus served from cache, minus failed. The collector branch consulted the same function, and a `search_only` run extracts nothing, so both subtrahends are always zero and it degrades to *URLs the search returned*. A pass that downloaded nothing because every URL was already cached still reported all of them as work, cleared the extraction cooldown, and let extraction start again immediately.

That is the 2026-08-22 report: around 200 runs, `ai_only` and `search_only` alternating every three or four minutes for twenty hours, and the same page shows **0 pages downloaded, 0 pairs searched**, 78 pages extracted against the previous day's 387.

`pages_fetched` is the collector's own measure — downloaded, minus the ones that came from the cache. And when both halves come back empty three times running the worker now sleeps for the extraction retry interval rather than polling every minute: nothing changes a caught-up system except the quota rolling over at midnight, or an operator.

This is the *third* wrong answer to "was that pass worth anything?" in a week. The pattern in all three: a signal that is correct for one run mode read as if it were general.

The bookkeeping lives in `pipeline.worker_after_run` and the measurement in
`pipeline.worker_outcome`, both pure functions, for the same reason
`next_worker_action` does. Inside the loop's closure the only way to check
either was to assert on source text, and review rounds proved the point twice:
reverting the collector to consult `worked` left every test in
`tests/test_worker_idle.py` green, and so did passing `fetched=worked` in the
callback. The mapping between a run's pair logs and the numbers the decision
reads is the thing that has been wrong every single time, so it is a function
with its own test now.

A cancelled pass counts for nothing, in both halves. The old loop declined to
*park* on cancellation but still counted it toward the empty streak, so three
interruptions — a quota running out mid-run, an operator pressing stop — parked
extraction for a quarter of an hour on no evidence at all. Writing the rule
down as a function is what made the contradiction with its own comment visible.

## The losing side, deleted 2026-09-20

Shipping the worker did not remove what it replaced. For 33 days both existed:
`_cron_run` was fully written and never called, `_startup_plan` and its four
window helpers were reachable only through a `_startup_run` that returns
immediately under `worker_enabled`, and eight keys in `settings.yaml` were read
on every boot without being able to change anything.

The cost of leaving it was not the ~300 lines. It was that `settings.yaml` said
extraction runs 00:30→10:00 and `_startup_plan` said a deploy resumes an
interrupted collection, and an operator had no way to tell that neither was
true any more. A toggle that cannot change behaviour still teaches.

`worker_enabled` stays, with a different meaning: `false` runs nothing. There is
no second scheduler behind it. See [[scheduler-disabled-no-cron]] for the
headstone.

## First work of the UTC day

Since 2026-09-20 the worker checks [[daily-data-guides]] before choosing either
pipeline mode. That publication step shares the provider ledger but not the run
coordinator: its small daily cap is deliberately completed before the open-ended
extraction or collection backlog can occupy the day.
