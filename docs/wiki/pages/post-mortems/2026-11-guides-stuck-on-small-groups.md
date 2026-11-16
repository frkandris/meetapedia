---
type: Post-mortem
title: No Guide Was Published for Eight Days
description: From 2026-11-08 every guide pass spent its draft cap refusing small or dimensionless Hungarian groups before any model call, so the 1,191 eligible German candidates behind them were never reached.
tags: [guides, worker, priority, gates]
timestamp: 2026-11-16
resource: scraper/guides.py
---
# No Guide Was Published for Eight Days

*The cap meant to bound fleet calls was spent by refusals that made none.*

## Symptom

`data_guides` gained its last rows on 2026-11-08. Every day after, the worker
ran the guide step dozens of times: 560–900 `guide_refused:*` counters a day,
but `guide_attempts` (fleet calls) only 1–7. No error, and each pass reported a
retry as designed.

## Root cause

`publish_daily_guides` walks the unpublished city/topic inventory in
`country_priority` order and stops a pass after `limit * 2` (20) attempts. The
counter was bumped **before** `_draft_guide`, so a group refused by the
record-only gates — fewer than 8 communities, under 60% described, fewer than
3 comparable dimensions — used a slot exactly like a model draft.

Measured in production on 2026-11-16: of 29,855 candidates, 7,791 are Hungarian
and sort first. Only 332 of those have 8+ groups, and 331 of the 332 have no
three comparable dimensions; the one that passes sits at position 293. Each
pass refused twenty of them, remembered them for the day, and was retried 15
minutes later — 45 passes a day reached position ~900 and never left Hungary.
Germany (1,191 eligible), Sweden (292) and the US (178) were never offered.

A second effect: those pre-call refusals were also charged to
`writer.last_model`, so a model could be dropped mid-pass after three
refusals it never wrote.

## Fix

- The inventory row already carries `community_count` and `described_count`;
  a candidate failing those two gates is skipped in the loop — no query, no
  `guide_refused` counter, no cap slot.
- `_PRE_CALL_REFUSALS` (`too_few_communities`, `too_few_descriptions`,
  `too_few_dimensions`): such a refusal is still remembered for the day but
  spends no cap slot and is not charged to a model. The cap now bounds what it
  was for — fleet calls.
- Regression test: `test_groups_refused_before_any_call_do_not_spend_the_pass_cap`.

## Lesson

The same shape as [[2026-11-collector-stuck-on-dead-pairs]], a week later: a
per-pass bound that counts cheap, certain refusals lets the head of a priority
queue starve everything behind it while every pass looks healthy. Count only
what the bound exists to limit.
