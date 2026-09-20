---
type: SEO
title: Structured Data
description: Which public page types emit JSON-LD, what each object claims, and why the listing pages deliberately do not get an ItemList.
tags: [seo, schema, json-ld, structured-data, indexing]
timestamp: 2026-09-20
resource: scraper/web/schema.py
---

# Structured Data

*One tag in `public_base.html` emits whatever `schema_json` a route sets, so a
new page type cannot ship without markup by forgetting a `{% block head %}`.*

## What each page type declares

| Page | Object | Built by |
|---|---|---|
| Community | `MusicGroup` / `SportsClub` / `DanceGroup` / `PerformingGroup` / `Organization` by topic | `community_to_schema` |
| City, explore | the same objects, one per community, in an `@graph` | `records_to_jsonld` |
| Venue | `Place`, narrowed by `venue_type` (`Library`, `Park`, `PlaceOfWorship`, `CafeOrCoffeeShop`, …) | `venue_to_schema` |
| Person | `Person` with `memberOf` per community they lead | `person_to_schema` |
| Home | `WebSite` + `SearchAction` + `Organization` | `site_jsonld` |
| Every page | `BreadcrumbList` | `breadcrumb_jsonld` |

Before the 2026-09-20 audit only the community page and explore had anything,
and the community object carried eight properties. Venue and person pages —
the two shapes a search engine understands best, a named place in a named town
and a named person — had none.

## Two rules the generators follow

**Nothing is inferred.** A group with no stated founding year gets no
`foundingDate`. Structured data is read by machines that cannot tell a guess
from a fact, so a confident wrong answer is worse than an absent one.
`test_nothing_is_invented_for_a_bare_record` holds this.

**`mainEntityOfPage` belongs to a page with one subject.** `records_to_jsonld`
passes the page URL through only when it was given exactly one record;
on a city listing it would assert that all forty communities are what the page
is about.

`url` is the group's own website, never ours — our page is `mainEntityOfPage`
and, absent a site of their own, `@id`.

## Why the listing pages have no ItemList

`/helyszinek`, `/varosok` and `/emberek` deliberately emit no `ItemList`. An
ItemList of 7,676 venues is a server-rendered list proportional to the database
in the document — exactly what [[2026-08-boilerplate-outweighed-the-content]]
cost us 15.5 MB and 34 seconds of event loop to learn, and what
`tests/test_public_page_weight.py` now forbids. The per-city pages carry the
real entities, and the listings exist to link into them.

See [[indexing-strategy]] for canonical tags and thin-page rules, and
[[seo-cross-domain-canonical]] for which domain a HU city page belongs to.
