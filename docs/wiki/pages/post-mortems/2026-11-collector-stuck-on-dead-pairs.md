---
type: Post-mortem
title: The Collector Spent a Week Re-fetching Dead Pairs
description: Searched pairs whose every URL failed to fetch were never marked collected, so from 2026-11-06 they filled every 15-minute collector turn and the 10,637 unsearched pairs behind them were never reached.
tags: [worker, collector, fetch, search, done-pairs]
timestamp: 2026-11-13
resource: scraper/db.py
---
# The Collector Spent a Week Re-fetching Dead Pairs

*A pair that could never be fetched was never finished, and it stood at the head of the queue.*

## Symptom

From 2026-11-07 to 2026-11-13 the pipeline added almost no communities: 0–3
extracted pages a day against 1,200–2,800 before, and no paid search at all
(`dataforseo_microusd` last written on 11-06). Nothing logged an error. The
worker alternated as designed — an empty `ai_only` pass (300 in a row), then a
`search_only` turn — and every turn ended with `fetched=0 new_records=0`.

## Root cause

`search_only` skips pairs whose `search_cache.collected_at` is set, and the
pipeline sets it only when at least one URL was fetched (or the search was
empty). A pair whose every result failed — only Instagram/YouTube, 403s, broken
certificates — stayed uncollected on purpose, "so a later run retries". Nothing
bounded the retry.

Measured in production on 11-13: **1,022** searched pairs with no fetched page,
spread across the priority order (Hungary 107, Germany 138, China 83, India 234,
Nigeria 114, Brazil 65…), and **10,637** pairs never searched (Japan 1,067,
Philippines 594, Turkey 540…). The collector walks cities in
`pipeline.country_priority` order, ~2 s per dead pair, and its turn lasts as
long as extraction is parked — 15 minutes. Each turn re-fetched ~400 dead pairs
and closed in Brazil. As the world waves added countries, the dead set grew
until, around 11-06, it no longer fitted in a turn; the unsearched pairs behind
it were never reached, so nothing new was searched, fetched or extracted.

The done-pair filter had the same hole for `ai_only`: a pair with URLs but no
cached page was never "done", so extraction iterated it too.

## Fix

- `search_cache.collect_attempts` counts passes in which every selected URL
  failed; `db.note_failed_collection` marks the pair collected after
  `pipeline.collect_max_pair_attempts` (3; 0 disables) and logs
  `collect_pair_given_up` once. A new search resets the count.
- `get_fully_processed_pairs` treats a collected pair with no fetched page as
  done: there is nothing to extract.

Expected recovery: each turn retires the ~400 dead pairs it reaches after their
third failure, so the backlog clears in a few hours and the collector moves on
to unsearched pairs. `scripts/repair_undecoded_pages.py`-style reopening still
works: clearing `collected_at` puts a pair back.

## Lesson

"Retry later" needs a bound wherever the retry sits in a priority queue: an
unbounded retry at the head of the order starves everything behind it while
every individual pass looks healthy. Same rule as the extraction quarantine
([[extraction-quarantine]]): remember having tried.
