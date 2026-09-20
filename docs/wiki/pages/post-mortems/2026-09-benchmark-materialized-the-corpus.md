---
type: Post-mortem
title: The benchmark script materialized the whole corpus
description: A read-only measurement script loaded every cached page's text into memory on production, and was killed with 237 MB of RAM left on an 8 GB host.
tags: [post-mortem, memory, sqlite, benchmark, jev, cache-pages]
timestamp: 2026-09-20
resource: scripts/benchmark_joinability_gate.py
---

# The benchmark script materialized the whole corpus

*A read-only script is not automatically a safe one: this one held four
gigabytes of page text to keep two thousand pages of it.*

## Symptom

`scripts/benchmark_joinability_gate.py local --per-class 1000` was started in
the production container on 2026-09-20 to measure a page-level gate. It printed
nothing for over a minute. `free -m` on the host read **6,416 MB used of 7,751,
237 MB free** and falling, on a machine also running the scraper worker and a
6 GB SQLite file. It was killed before the OOM killer chose for us — and the
OOM killer would not necessarily have chosen the benchmark.

## Root cause

`load_sample()` selected in Python:

```python
rows = conn.execute("""SELECT ..., json_extract(data, '$.raw_text'), records_count
                       FROM cache_pages WHERE extracted_at IS NOT NULL ...""").fetchall()
```

One `fetchall()` over **128,072 extracted pages**, each carrying ~30 KB of page
text in its `data` blob — roughly 4 GB resident — in order to sort them and keep
2,000. The sampling itself was correct and deterministic; it was simply done in
the wrong place.

Two things made this easy to miss. The script is genuinely read-only, so it
looked harmless in review — the handoff describes it as "no runner mutates the
scraper database", which is true and beside the point. And it was never run
against production data before: the local checkout has no database, and the unit
test built thirty 800-byte rows, where the same code is instant.

## Fix

Selection happens on keys, text is fetched only for the pages chosen.

- `records_count >= 0` is what "has been extracted" means (`-1` is the
  scraped-but-not-extracted sentinel), and `scraped_at IS NOT NULL` repeats the
  `idx_cache_pages_done` partial index's own clause — a partial index is only
  eligible when the query's WHERE matches it. The key read is then served
  entirely from the index: `SCAN cache_pages USING INDEX idx_cache_pages_done`.
- The chosen keys are resolved by primary key, with
  `substr(json_extract(data,'$.raw_text'), 1, ?)` truncating **in SQL**. The
  runners see at most `max_chars` anyway; carrying whole pages into Python and
  slicing them there was the second half of the cost.
- The ordering is unchanged, so a given `--seed` still selects the same pages.

Measured on synthetic corpora with production-sized blobs, which is the only
way this class of bug shows up at all:

| corpus | old | new |
|---|---|---|
| 20,000 rows / 739 MB | 1.08 s, **888 MB** peak | 0.15 s, **86 MB** |
| 70,000 rows / 2,585 MB | (not run — it was already the bug) | 2.18 s, **100 MB** |

The point is the second row: memory is flat as the corpus grows, where the old
shape was linear in it. Extrapolated to the real 128,072 rows the old path
wanted about 5.7 GB.

`tests/test_cost_audit_scripts.py` now traces the statements the sampler
executes and fails if any query that touches `json_extract` is not restricted
to `url_hash IN (…)`.

## Lessons

- **"Read-only" is not "safe".** The review question for a script pointed at
  production is not only what it writes, but what it holds.
- CLAUDE.md already forbids this shape for `ai_only` — *"never restore the old
  whole-cache materialization because it can OOM at production scale"*. The rule
  was written for the pipeline and is about the table, not about the caller. A
  benchmark is not an exception to it.
- A fixture whose rows are 800 bytes cannot exercise a bug whose cause is that
  rows are 30 KB. The synthetic database has to carry realistic blob sizes or it
  proves nothing — the same lesson as [[done-pair-url-hash-not-city-topic]].
