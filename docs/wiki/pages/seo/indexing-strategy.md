---
type: Architecture
title: SEO Indexing Strategy
description: Canonical tags, thin-page noindex, domain-scoped sitemaps, and robots rules that keep the two-domain directory from cannibalizing its own search rankings.
tags: [seo, canonical, sitemap, robots, noindex, jsonld]
timestamp: 2026-07-09
resource: scraper/web/app.py
---

# SEO Indexing Strategy

*Two domains serve overlapping content, so the indexing strategy centers on telling Google which copy is canonical and keeping thin/duplicate pages out of the index. See [[seo-cross-domain-canonical]] and the [[2026-06-seo-traffic-collapse]] post-mortem.*

## Canonical tags

`public_base.html` emits `<link rel="canonical" href="{{ canonical_base or site_url }}{{ path }}">`. `canonical_base` is set only for city-scoped pages (via `_canonical_base`); other pages self-canonicalize to their own `site_url`. The cross-domain rule for Hungarian pages is the load-bearing part — see [[seo-cross-domain-canonical]].

## Thin-page noindex

**Correction, 2026-09-05:** the detail-page and sitemap exclusions described
below are historical. Commit `04282ff` (2026-08-21) made every visible community
detail indexable (`page_noindex=False`) and included undescribed communities in
the sitemap. The empty city+topic explore guard remains. See
[[search-console-2026-09-05]] for fresh measurements and the distinction between
indexability and actual indexing. The following text preserves the earlier policy.

`public_base.html` adds `<meta name="robots" content="noindex">` when `page_noindex` is true:

- **Explore**: `city and topic and total == 0` — a city+topic combo with zero communities.
- **Community detail**: the record has no non-empty `description`.

Rationale: stop Google indexing empty/thin programmatic pages, a major trigger of the mass "Crawled – currently not indexed" devaluation. Thickening the *non-empty-but-thin* descriptions (the bigger population) is the complementary lever — planned, staged, and deferred to supervised runs in [[description-enrichment-plan]].

## Sitemap scoping

**Update, 2026-09-05:** topic listing URLs are emitted only for configured
topics, matching `public_city_segment`. Communities stored under retired topics
keep their detail URLs in the sitemap. Meetapedia uses `/rolunk` and `/felfedezes`
for its about/explore entries because `/about` and `/explore` redirect there.
Legacy-topic details omit invalid topic breadcrumbs/links but preserve their
stored identity for reports. Regression coverage: `tests/test_sitemap_routes.py`.

`GET /sitemap.xml` is domain-scoped via `lang_context` + `_site_cities`:

- **HU cities are removed from the meetapedia sitemap** (`site_city_names -= _hu_city_names()`) — a sitemap must list only canonical URLs, and HU pages canonicalize to kozossegek.
- **Thin community pages are skipped** (no description) — consistent with `page_noindex`.
- Venue/person URLs are emitted only for kozossegek.
- **Country landing pages** (`/cities/<slug>`, meetapedia only) are listed for countries with live content, minus Hungary — see [[country-landing-pages]].
- Order-preserving dedup via `dict.fromkeys`; `changefreq weekly`, no `priority`.
- **`<lastmod>` on community pages** (2026-07-26): `get_community_lastmods()` supplies
  each community URL's `updated_at` date, keyed by `(city, public_slug)` and resolved
  the same way as the public route (`ORDER BY topic, id`, first-wins) so the date
  matches the record actually served. `updated_at` only advances on a real content
  change — `_bulk_upsert_communities` compares a content fingerprint that excludes the
  volatile `extracted_at` — so a fingerprint re-extraction does not churn every page's
  `<lastmod>` (the [[2026-06-seo-traffic-collapse]] stability lesson). See [[persistence-layer]].

## robots.txt and other signals

Per-domain `Sitemap:` line. Disallows `/admin, /source/, /api/, /set-lang, /unsubscribe, /community/, /healthz, /kereses`; special-cases `facebookexternalhit` with `Allow: /` for link previews. `/set-lang` also sends `X-Robots-Tag: noindex, nofollow`.

## Breadcrumbs

`schema.py:breadcrumb_jsonld` emits a `BreadcrumbList` (Home → City → Topic →
Community, and the venue/person/global-topic variants) into every city-scoped page's
`<head>`; the route builds the trail via `_crumbs()` and passes it as `breadcrumbs`.
Topic labels are localized (`get_topic_labels(lang)`), and multi-topic explore filters
add no topic crumb (no canonical URL). `public_base.html` renders **only** the JSON-LD —
no visible base nav — because each page template already shows its own visible bar
(avoids a double breadcrumb). This reinforces the site hierarchy for SERP breadcrumbs
and internal linking without altering page content.

## JSON-LD

`schema.py:records_to_jsonld` builds a `@graph` of schema.org types (`SportsClub`/`MusicGroup`/`DanceGroup`/`PerformingGroup`, default `Organization`) mapped from topic, injected into `<head>` on community and explore pages. It escapes `</` → `<\/` to prevent script-tag breakout.

## hreflang

`i18n.py:_hreflang_alternates(path)` emits `rel=alternate hreflang` (hu / en /
x-default) in `<head>` for the **shared static pages with clean site-aware URLs**:
home (`/`↔`/`), map (`/terkep`↔`/map`), people (`/emberek`↔`/people`) — the pages
whose two URLs each serve on their own domain and 301 to the twin otherwise. The
list (`_STATIC_ALTERNATES`) deliberately **excludes** content pages (communities are
country-specific and HU ones 301 to kozossegek) and the not-yet-URL-localized static
pages (about/explore/cities/submit still share a single HU path or redirect EN→HU —
listing them would emit wrong alternates). Extend the list once those URLs are fully
localized.

## Structured data

Canonical tags tell Google which URL is the page; JSON-LD tells it what the
page is about. Which page types emit what, and why the listings deliberately
carry no `ItemList`, is in [[structured-data]].
