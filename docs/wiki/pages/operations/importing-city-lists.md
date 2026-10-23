---
type: Runbook
title: Importing City Lists from Wikidata
description: scripts/import_cities.py adds a country's settlements above a population threshold without ever rewriting existing entries.
tags: [operations, cities, wikidata, import, expansion]
timestamp: 2026-08-16
resource: scripts/import_cities.py
---

# Importing City Lists from Wikidata

*One additive script turns "index every town in country X over N people" into a reviewable diff.*

## Usage

```bash
PYTHONPATH=. .venv/bin/python scripts/import_cities.py hungary  --min-pop 1000
PYTHONPATH=. .venv/bin/python scripts/import_cities.py hungary  --min-pop 1000 --apply --write-coords
```

Without `--apply` it is a dry run: counts, the collisions it would rename, and
a three-entry sample. `--write-coords` also splices `CITY_COORDS` entries into
`scraper/web/app.py`, which the map page needs.

Adding a country means adding one `CountrySpec` to `COUNTRIES`.

## Invariants

**Additive only.** Existing `- name:` entries are never rewritten, so hand-tuned
`search_variants` and `topic_tier` values survive re-runs. A settlement already
in the file is skipped even if Wikidata now disagrees about its population.

**Slug collisions are resolved, never ignored.** `public_slug()` accent-folds,
so Komló and Kömlő both become `komlo` and `_city_from_slug` would resolve every
link to whichever came first — one city's pages silently vanish. The importer
detects this and renames the *new* entry to `Kömlő (Heves)`, keeping the real
name in `search_variants` (searches must use the name people actually write).
Same convention as the 2026-07 German import; locked by
`tests/test_city_uniqueness.py`.

**Small settlements land as `topic_tier: core`.** Below `core_tier_below` an
entry only runs `pipeline.core_topics` — a village cannot yield a chess club and
a language exchange, and tiered-out pairs are fully frozen. Existing entries keep
whatever tier they already had.

## Two queries, not one

The settlement query and the region query are separate on purpose. Joining
`P131+` region resolution into the main query exceeds the public WDQS timeout
and returns a **truncated body** — which surfaces as a `JSONDecodeError`
halfway through, not as an error status. The importer treats a truncated
response as fatal (retrying cannot help) and only asks for regions for the
handful of names that actually collide.

Throttling (429/50x) is retried with `Retry-After`-aware backoff; without it the
script dies mid-write.

## Country notes

**Hungary** — `wdt:P939` (KSH settlement code) is exactly the set of Hungarian
settlements and nothing else, which beats any `P31/P279*` chain. Budapest's 23
districts carry their own KSH codes and are excluded by name.

**Indonesia** — the city tier is the `kota` (Q3199141), 93 of them, essentially
all above 100k. Asking for "human settlement in Indonesia" via `P31/P279*` times
the endpoint out with a 504. Labels come in English to match the rest of the
international list, with the `Kota ` prefix trimmed.

## After an import

1. `PYTHONPATH=. .venv/bin/pytest tests/test_city_uniqueness.py tests/test_search.py`
   — the second one catches a locale with no DataForSEO `location_code`, which
   would otherwise reproduce the 2026-07 outage (see
   [[2026-07-search-provider-down-noise]]).
2. If the new country introduces a locale, add its `search_terms` to
   `config/topics.yaml` — `pipeline.py` silently falls back to English terms,
   which finds a fraction of local groups.
3. Decide where the country sits in `pipeline.country_priority`
   (see [[cost-saver-schedule]]). The saver window is a hard time box: a country
   behind a large backlog may never be reached.

## History

- **2026-07-26** — ~2,057 German Städte (the pre-script, manual import).
- **2026-08-16** — every Hungarian settlement ≥1000 inhabitants (973 new, 968
  `core`), plus 83 Indonesian kota. Hungary moved to the front of
  `country_priority`: it had been last *because* it was fully indexed, and the
  import made it the largest unprocessed backlog on the primary market.
- **2026-10-23** — every other searchable country, ≥100,000 people, all
  `core`: 3,169 new cities in 146 countries, by `scripts/import_world.py` —
  one run for all countries, which also writes `config/countries.yaml`
  (DataForSEO location, search language, population order). See
  [[world-expansion]]. Next waves: the same command with `--min-pop 15000`,
  then `1000`.
