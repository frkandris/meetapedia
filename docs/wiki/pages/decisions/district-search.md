---
type: Decision
title: Searching Large Cities District by District
description: Every city got 36 topics × five pages — London 62 communities, Lagos 31; a pilot searches ten large cities by district, and never-yielding domains stop taking fetch slots.
tags: [search, coverage, dataforseo, cities, cost]
timestamp: 2026-10-23
resource: scraper/pipeline.py
---
# Searching Large Cities District by District

*A city of nine million got the same five pages per topic as a town of fifty thousand.*

## The measurement (2026-10-23, production, read-only)

Every city: 36 topic pairs, three queries, at most five fetched pages per pair
(`search.max_pages_per_topic`). English-language cities hit exactly 5.0 URLs per
pair — `stop_after` ended the search after the first query.

| city | communities | pages | pages with a community |
|---|---:|---:|---:|
| London | 62 | 207 | 24 |
| New York | 55 | 124 | 26 |
| Los Angeles | 47 | 145 | 31 |
| Chicago | 67 | 138 | 27 |
| Toronto | 40 | 125 | 31 |
| Lagos | 31 | 139 | 23 |
| Cairo | 39 | 138 | 24 |
| Delhi | 69 | 129 | 31 |
| Mumbai | 78 | 144 | 48 |
| Beijing | 69 | 119 | 27 |
| *Berlin* | *228* | *280* | *64* |
| *Tokyo* | *542* | *165* | *97* |

Big-city results are tourism portals and listicles: London's most-fetched
domains were visitlondon.com (14 pages), Wikipedia, timeout.com, lonelyplanet —
none yielded a community. Tokyo did well because tunagate.com lists many clubs
per page. Corpus-wide, domains with ≥15 extracted pages and no community ever
took 8,357 of 170,230 pages (4.9%, 208 domains; dancenow.net in 607 cities).

## What changed

- **Never-yielding domains stop taking fetch slots.** At the start of a search
  run `db.get_low_yield_domains` (≥ `fetch.low_yield_min_pages`, 15) and
  `fetch.low_yield_patterns` (tourism portals by `re:` host pattern, timeout,
  lonelyplanet, getyourguide, tripadvisor, viator) join `fetch_blocked_domains`
  for that run — out of `stop_after`, the URL list and the fetch, so the next
  query's results take the slot. Free.
- **Pilot: ten cities searched by district** (`search_districts` in
  cities.yaml, set by `scripts/set_search_districts.py`; 175 districts). Each
  pair adds one query per district — "choir Hackney London", "合唱团 东城区 北京"
  — and two fetch slots per district, at most 60 (`_pages_for`). Pairs searched
  before are deepened in place: only the missing district queries run,
  concurrently, and their URLs join the cached ones; a failed query is not
  recorded as run, so only it is retried. The done-pair filter judges these
  pairs on their full page budget (`max_pages_by_city`), not the first five.
- Lists are hand-written: Wikidata P150 is empty for seven of the ten.

Cost: ~6,300 queries ≈ $3.7, inside the $1/day ceiling — about four days, during
which the world expansion's searches slow down.

## How it will be judged

Same query as the baseline above, after the pilot pairs have been searched,
fetched and extracted (baseline saved 2026-10-23: 557 communities in the ten).
Expand to every city ≥1M (core topics ~$20, all topics ~$100) only if the
pilot's gain per dollar beats the world expansion's.
