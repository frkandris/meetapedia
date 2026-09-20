---
type: Decision
title: Scheduler and Cost-Saver Cron Configuration (removed)
description: REMOVED 2026-09-20 — the twin cost-saver crons and the startup-recovery plan were deleted after 33 days in which no code registered them; one cron (the daily report) is left.
tags: [scheduler, cron, decision, main]
timestamp: 2026-09-20
resource: scraper/main.py
---

# Scheduler and Cost-Saver Cron Configuration (removed)

*This page described a scheduler that registered three independent settings' worth
of jobs. Since 2026-09-20 APScheduler registers exactly one: the daily report
email. Everything else is [[continuous-worker]].*

## What it used to say

- `schedule.saver_enabled: true` registered the `search_only` collector and the
  `ai_only` off-peak extractor.
- `schedule.report_enabled: true` registered the daily summary email.
- `schedule.cron_enabled: false` kept the older combined `full` run available.

## Why it went

The worker shipped on 2026-08-18 and [[continuous-worker]] explains what replaced
the windows. What that change did **not** do was remove the losing side. For 33
days `_cron_run` sat in `main.py` fully written and never called; `_startup_plan`,
`_startup_until`, `_startup_window`, `_within_window` and `_next_window_end` were
live code reachable only through a `_startup_run` that returns immediately under
`worker_enabled`; and eight keys in `settings.yaml` — `saver_enabled`, `cron`,
`cron_enabled`, `auto_run_on_startup`, `extract_cron`, `extract_until`,
`search_cron`, `search_until` — were read on every boot and could not change what
the process did.

That is worse than dead code that looks dead. An operator reading `settings.yaml`
would have concluded that extraction runs 00:30→10:00, and the daily report's own
wording implied it; a reader of `_startup_plan` would have concluded that a deploy
resumes an interrupted collection. Both were false, and both were documented.

`run_pipeline(stop_at=…)` survives the deletion: it is still the mechanism for a
time-boxed run and is still covered by tests. Nothing in production passes it.

## What is left

- One cron: `report_cron`, the daily summary email. `_cron_fields` now falls back
  to that expression, because there is no other.
- `worker_enabled: false` means **run nothing** — a maintenance switch, not a
  fallback to a second scheduler.
- Startup runs nothing at all. The worker starts on boot and picks the right work
  immediately, so an interrupted run needs no recovery: its finished pairs are
  cached and its unfinished ones are pending again. That is what keeps a deploy
  from costing a collection run — see [[continuous-worker]].

Commit `65e5c09..` removed it; the code is in git history.
