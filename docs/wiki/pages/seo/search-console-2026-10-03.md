---
type: Analysis
title: Search Console Baseline 2026-10-03
description: First API-pulled baseline — kozossegek has one indexed page in a 120-URL sample and 55 impressions in 90 days, while 94% of meetapedia's clicks come from Hungarian pages Google indexed before the July 301 and has not recrawled since.
tags: [seo, search-console, indexing, baseline, measurement]
timestamp: 2026-10-03
resource: scraper/web/app.py
---

# Search Console Baseline 2026-10-03

*Pulled through the Search Console API (read-only service account, see the session memory), 2026-07-03 – 2026-10-01, `dataState: all`; the URL sample is 120 random kozossegek sitemap URLs from [[bot-crawl-audit-2026-10-03]] plus the same paths on meetapedia.*

Supersedes the export-based [[search-console-2026-09-05]] as the number to measure against.

## Totals, 90 days

| | Clicks | Impressions | Queries | Pages with impressions |
|---|---:|---:|---:|---:|
| meetapedia.com | 153 | 3,568 | 331 | 1,697 |
| kozossegek.com | 5 | 55 | 3 | 46 |

meetapedia by half-month (clicks/impressions): Jul 20/521, 17/472 · Aug 22/676, 37/669 ·
Sep 32/746, 24/454. Flat, slightly down in late September. kozossegek: 17 of its 55
impressions are `site:kozossegek.com` — someone checking, not searching.

## Where meetapedia's traffic actually comes from

**145 of its 154 page-level clicks and 2,217 of 4,046 impressions land on Hungarian-city
pages** — URLs that have answered a 301 to kozossegek.com since 2026-07-26
([[seo-cross-domain-canonical]]). Country split agrees: Hungary 139 clicks, everyone else 14.
The international corpus ranks at positions 60–90 (662 of 859 query impressions), so it
earns almost nothing.

## URL Inspection, 120 sitemap URLs

| kozossegek.com | count | meetapedia.com twin | count |
|---|---:|---|---:|
| URL is unknown to Google | 80 | URL is unknown to Google | 89 |
| Crawled – currently not indexed | 38 | **Submitted and indexed** | 29 |
| Submitted and indexed | 1 (home) | Alternate with proper canonical | 1 |
| Page with redirect | 1 | error | 1 |

- **All 23 sampled guides (`/utmutatok/*`) are unknown to Google.**
- The 29 indexed meetapedia twins carry Google's canonical *on meetapedia* although their
  declared canonical is kozossegek. Their last crawls are mostly June–July: Google has not
  been back since the 301, so it still serves the pre-redirect copy.
- kozossegek's last crawl dates spread from May to October; 80 URLs were never crawled.
- **Sitemaps:** kozossegek's was last downloaded **2026-09-05** (33,933 URLs then, 53,804 now,
  1 warning, 0 indexed reported); meetapedia's on 2026-10-02 — submitted as
  `http://meetapedia.com/sitemap.xml`, 104,894 URLs.

## What this means

1. The binding constraint is **crawl and trust**, not targeting. A page-two pool for the
   playbook's "query at positions 8–40 with no dedicated page" step does not exist:
   four queries qualify, and the only real one (`språkcafe göteborg`, position 9.8, 24
   impressions) already lands on its own topic page.
2. The HU traffic that remains is living on borrowed time. When Google recrawls those
   meetapedia URLs it will follow the 301 to a domain where it currently declines to index
   the same page ("crawled – not indexed"). The 301 was right as a consolidation signal;
   it has not made Google trust kozossegek, and it may remove the last HU clicks. This is an
   open decision, not a fix — see the decision list below.
3. Google has not refetched kozossegek's sitemap in four weeks, so nothing published there
   since (all guides, the person cleanup) is being discovered through it.

## Open decisions (owner)

- Resubmit `https://kozossegek.com/sitemap.xml` and `https://meetapedia.com/sitemap.xml`
  (https, not http) in the Search Console UI — the restricted API user cannot.
- Request indexing by hand for the home page's top links and the guides index.
- Whether to keep the HU 301 direction, or to make meetapedia.com the Hungarian home that
  Google already prefers. Either way, do not flip it back and forth: a second domain move
  restarts the consolidation clock.
