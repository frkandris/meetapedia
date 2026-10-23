---
type: Integration
title: Search Console Snapshot 2026-10-23
description: 28 days to 2026-10-21 — kozossegek.com 0 clicks and nothing indexed in the sample; meetapedia.com 50 clicks, 38 from Hungarian URLs that have 301'd since July; venue and person pages leave the sitemap.
tags: [seo, search-console, indexing, sitemap]
timestamp: 2026-10-23
resource: scripts/gsc_audit.py
---
# Search Console Snapshot 2026-10-23

*`scripts/gsc_audit.py` (read-only service account), window 2026-09-24 – 2026-10-21,
previous 28 days in brackets. Compare with [[search-console-2026-10-03]].*

| | Clicks | Impressions | Avg. position | URL Inspection, 24-URL sample |
|---|---:|---:|---:|---|
| kozossegek.com | **0** (1) | 9 (23) | 5.1 | 12 unknown, 12 crawled – not indexed, **0 indexed** |
| meetapedia.com | 50 (55) | 1,056 (1,175) | 22.3 | 19 unknown, 5 discovered – not indexed |

- **kozossegek.com is not in Google.** Its few impressions are `site:` queries.
  The sitemap index was downloaded once, on 2026-10-03, and Search Console
  shows no contents for it — the children were never processed.
- **meetapedia.com still earns from the Hungarian corpus it redirects away.**
  38 of its 50 clicks and 627 of 1,056 impressions are from Hungary, landing on
  URLs like `/budapest/bekasmegyeri-idosek-klubja` that have answered a 301 to
  kozossegek.com since 2026-07-26. Google keeps its old copy rather than follow.
- International pages rank at positions 40–100 and earn almost nothing.
- meetapedia's sitemap (140,445 URLs) is fetched daily; "indexed: 0" there is
  Search Console's sitemap counter, which it no longer fills — not a verdict.

## Changed the same day

Venue and person pages left both sitemaps (20,405 of kozossegek's 48,584
URLs). On a domain with nothing indexed, a sitemap that is 42% one-line pages
is what the domain is judged on. The pages stay live and indexable, reached
through the communities that link them. Reversible: if the next snapshot shows
kozossegek indexing, they can go back in batches.

## The decision this keeps pointing at (owner)

Three months after the 301, the evidence is one-directional: Google prefers
the Hungarian pages on meetapedia.com and declines to index them on
kozossegek.com. [[seo-cross-domain-canonical]] chose kozossegek as the
Hungarian home; the open question from [[search-console-2026-10-03]] —
keep that, or make meetapedia.com the Hungarian home Google already uses —
now has two more data points behind the second option. It is not a code
change to make casually: a second domain move restarts consolidation, so it
is made once or not at all.
