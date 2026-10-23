---
type: Decision
title: The World Expansion, in Waves by City Size
description: Every country DataForSEO can search, largest population first, in waves of decreasing city size, with the search placed by country and capped at $5 a day.
tags: [expansion, cities, search, dataforseo, cost, wikidata]
timestamp: 2026-10-23
resource: scripts/import_world.py
---
# The World Expansion, in Waves by City Size

*The crawler ran out of work, not money: by 2026-10-11 every configured
city–topic pair was searched, and twelve days passed with no new search.*

## The situation that forced it

On 2026-10-23 the corpus held 89,469 communities from 63 countries and 3,876
cities; 43,456 pairs were done and the 25 left could not be fetched. The worker
alternated `ai_only` (nothing to extract) and `search_only` (nothing to search)
every twenty minutes, and the daily report showed 0 new communities for twelve
days. Measured across the 54,326 searched pairs: 2.9 queries and 9.8 URLs per
pair, ~0.4 cents per pair all-in ($213 of DataForSEO spend).

## The decision

Every country in DataForSEO's location list, **largest population first**, in
**waves by city size**: ≥100,000 people first, later ≥15,000, then ≥1,000.
Each wave adds only `pipeline.core_topics` (`topic_tier: core`). Hungary,
Germany, Indonesia and Sweden stay first (`pipeline.country_priority`); they
were imported deeper by [[importing-city-lists]] and the wave skips them.

Waves rather than strictly country-by-country, because at ≥1,000 people India
alone is tens of thousands of settlements; country-major order would reach the
small countries in years. Within a wave the country order is still population.

Wave 1 (2026-10-23): 3,169 new cities in 146 countries, ~19,000 pairs, an
estimated $40-75 of search.

## What had to change for it to be safe

- **The search is placed by the city's country.** `LOCALE_TO_DATAFORSEO_LOCATION`
  maps a *language*, so every Spanish-speaking city searched from Spain and
  every English one from the United States. `config/countries.yaml` (generated
  by `scripts/import_world.py`) carries each country's `dataforseo_location`,
  and `DataForSEOClient.search(country=…)` prefers it; the language table is
  the fallback for countries outside the file.
- **Countries DataForSEO cannot place are not imported**: no Russia, Iran,
  Taiwan, Hong Kong, Cuba, Syria or Sudan in its 213 locations (checked
  2026-10-23). A search run from another country buys another country's SERP.
- **A daily ceiling on search spend**, `search.daily_budget_usd: 1.0` (the
  operator lowered it from 5.0 the same day, with `search.core_queries_per_pair:
  1` — one paid query per core pair instead of up to three, ~$11-20 for the wave)
  (`search.SearchBudget`). Nothing else stops DataForSEO — a free LLM answers
  429, a paid search API keeps charging. The spend is DataForSEO's own `cost`
  per response, kept in `daily_counters` (`dataforseo_microusd`); at the
  ceiling `SearchQuotaError` stops the run's search, caches nothing, and
  extraction carries on. Same semantics as the LLM ceiling: absent = no
  ceiling, 0 = no paid searches.
- **The order covers every country.** `main._settings_country_priority()`
  appends every other country by population after the named four, so the
  worker, enrichment and the daily guides walk the same order;
  `_saver_city_groups` groups in one pass instead of one scan per country.
- **Search terms in every language a core city uses.**
  `scripts/add_core_search_terms.py` added 215 locale entries for the six
  core topics; a locale without terms falls back to English silently, and
  `test_every_search_language_has_terms_for_every_core_topic` now says so.
- **`zh` is sent as `zh-CN`** — DataForSEO has no bare `zh`.

## Found on the way

- **Norwegian searched in English from the US.** YAML 1.1 reads a bare
  `locale: no` as `False`; `str()` made it `"False"`, which matched the topic
  keys (also `"False"`) but is no DataForSEO language. `config.yaml_locale()`
  maps it back to `"no"`.
- **Wikidata's "city" class includes metropolitan areas** ("New York
  metropolitan area", "El Paso–Juárez"); they are subtracted. Subtracting
  "urban area" as well removed almost every US city through its class chain
  (6 left of 253), so names are filtered instead.
- **Duplicates under another spelling**: Hanover/Hannover, Nuremberg/Nürnberg,
  Ludwigshafen/"Ludwigshafen am Rhein". The importer checks the local-language
  label, prefixes within the country, and a 3 km radius; 10 km also removed
  real ≥100k suburbs (80 in Japan).

## Not settled

Countries already in `cities.yaml` keep their existing search language for new
cities (Egypt, Vietnam, Thailand, Malaysia search in English); whether the
local language finds more groups there is unmeasured. Wikidata's city typing is
uneven — Vietnam yields 9 cities ≥100k, India 262 of roughly 500.


Depth inside a city is a separate lever: see [[district-search]].
