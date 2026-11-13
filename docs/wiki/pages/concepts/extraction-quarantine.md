---
type: Concept
title: The Extraction Quarantine
description: After three content failures at one fingerprint a page stops being re-extracted — the bound that the never-cache-a-failure rule was missing, and which only became expensive once retries cost money.
tags: [extraction, cache, cost, pipeline, quarantine]
timestamp: 2026-08-27
resource: scraper/pipeline.py
---

# The Extraction Quarantine

*"Never record this outcome" needed a partner that says "and stop asking".*

## The rule it completes

A failed extraction is never cached. Caching it would write "this page has 0
communities" under the current fingerprint, and the page would never be looked
at again — unrecoverable, silent data loss. That rule is correct and is stated
in three places in `extract.py`.

Its cost is that a page which fails *deterministically* is re-attempted by every
run, forever, and each attempt walks the entire provider fleet. For sixteen
months that was free. On 2026-08-26 it was roughly ten thousand charged calls
for 335 pages of output ([[2026-08-paid-fallback-burned-the-budget]]).

## What it stores, and what it refuses to store

Not "this page is empty" — that is the data loss. **"This page failed N times at
this fingerprint."** One row per (url_hash, fingerprint) in `extract_failures`.

Because the fingerprint is half the key, every prompt change and every model
change releases the whole quarantine automatically — and that is exactly the
change that could produce a different answer. Nothing else could.

## Only the answer being unusable counts

`_Quarantine.note()` ignores anything that is not an `ExtractorContentError`:

| failure | counts? | why |
|---|---|---|
| truncated answer (`finish_reason: length`) | yes | the model answered; what it said was unusable |
| invalid / malformed JSON | yes | same |
| timeout, 500, connection reset | **no** | we never heard back — says nothing about the page |
| 429 | **no** | the API is alive and asking us to slow down |
| spent quota, 402 | **no** | the designed end of a window |
| an unexpected exception in our own parser | **no** | our bug, not the page's |

`FallbackExtractor._call` raises `ExtractorContentError` only when **every**
provider that answered failed that way and nothing transient happened in the
same call. One flaky network error anywhere makes the whole attempt
uncountable. Conservative on purpose: the counter rising slowly costs a few
retries, and rising wrongly quarantines the corpus over one bad afternoon.

`ExtractorContentError` subclasses `ExtractorUnavailableError`, so every existing
handler, the circuit breaker and the never-cache rule treat it exactly as
before. What it adds is attribution, not new control flow. One behaviour does
change: a content failure ends the call. `_call`'s second round exists for
transient errors, and re-sending an identical prompt to the same fleet cannot
make an answer fit a cap it already overflowed — while a truncated call is
charged in full. The round also waits out a pacing window, up to fifteen
minutes, to give a provider that never got a turn one; that is worth it when
the alternative is losing the page and not worth it here, because three
separate runs will each offer the page a differently-paced fleet before the
quarantine takes it.

## Where it is consulted

- `_run_ai_only` and `_run_full` skip a held page before any call, and count it
  as `extract_quarantined` rather than `extract_failed` — this run is not
  failing, it is declining to pay for a failure already established.
- `get_fully_processed_pairs` treats a held page as done, so its pair stops
  returning to the loop on every run of the day to skip the same page again.
- A success calls `forgive()`, which writes only for a page that actually has a
  row.

## Operator surface

`/admin/quarantine` lists what is held, the last error for each, and a Release
button (clears every fingerprint, because the operator is saying "try this", not
"try this once"). The daily report states the count — not as an error, but as a
number that must not grow quietly: a rising count means the token cap or the
prompt is wrong for a whole class of pages, and nothing else in the report would
say so. That is the same reasoning as the ceiling in [[paid-spend-guard]]: a
guard nobody can see is a guard nobody trusts.

`pipeline.extract_max_page_failures: 0` disables it entirely.

The collector has the same rule one level up: a searched pair whose every URL
fails to fetch is retried three passes, then counts as collected — see
[[2026-11-collector-stuck-on-dead-pairs]].
