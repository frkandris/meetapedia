---
type: Analysis
title: Bot Crawl Audit 2026-10-03
description: A plain-HTTP, no-JavaScript crawl of a stratified sample of both sitemaps found clean canonicals and titles, a broken www host, missing AI-readable surfaces, and a quarter of all person pages making claims the corpus does not support.
tags: [seo, audit, crawl, persons, measurement]
timestamp: 2026-10-03
resource: scraper/web/app.py
---

# Bot Crawl Audit 2026-10-03

*Fetched the way an AI crawler does — one GET, no JavaScript, `OAI-SearchBot` user agent — 200 kozossegek and 162 meetapedia URLs, up to 40 per page type, drawn at random from the live sitemaps.*

## Sitemaps

| | kozossegek.com | meetapedia.com |
|---|---:|---:|
| URLs | 53,804 | 109,477 (index of parts) |
| without `lastmod` | 34,795 | 57,680 |
| foreign-host URLs | 0 | 0 |

Missing `lastmod` (topic listings, venues, people) is honest rather than wrong: no date is better
than a build date, which teaches Google to ignore the field.

## Clean

Every sampled URL answered 200 directly. 0 canonicals pointing elsewhere, 0 missing or doubled
canonicals, exactly one H1 everywhere, 0 noindexed pages in a sitemap, 0 JSON-LD parse errors, no
duplicate titles beyond the placeholder below, no fake ratings or review counts in structured data.
Every AI crawler user agent tested gets the page (Cloudflare does not block them).

## Defects found

1. **`https://www.meetapedia.com/` answers Cloudflare 526** (invalid origin certificate), and
   `http://meetapedia.com/` redirects with **307** rather than 301 (kozossegek does both right).
   Infrastructure, not code. **Fixed the same day in Cloudflare:** a Redirect Rule
   (`https://www.*` → `https://${1}`, 301, query kept) answers at the edge, so the origin never
   needs a www certificate, and *Always Use HTTPS* turned the 307 into a 301. Cause of the 526:
   the meetapedia zone is Full (strict) and Traefik has no www route, so it served its default
   certificate. The kozossegek zone is plain Full, which is also why its origin certificate —
   expired 2026-08-11, Let's Encrypt renewal failing behind the proxy — goes unnoticed.
2. **No `llms.txt`** (it 302'd to the home page) and no Markdown representation on either domain.
   Fixed: [[answer-engines]].
3. **Person pages making unsupported claims.** Of 34,222 stored person rows, **8,477** led a group
   that is not listed in that town — one Halásztelek retirement-club page put its leader in 228
   towns — so each such page linked to a group page that does not exist. **"Jane Smith"**, the
   example in the extraction prompt's description of `leader`, was a published person in 39 towns
   and the schema `member` of 66 groups. "Not specified" (425 rows), "Local organizers" (159) and
   "Az alapítvány vezetői" were people too, and 132 names had a parenthesis cut open. Fixed: see
   [[person-record]] — such rows are no longer published, submitted, or written.
4. **The meetapedia home page is 430 KB** of HTML against kozossegek's 157 KB. Not fixed here.

## Not a defect

The "doubled brand" check fired on 38 kozossegek guide titles only because the word *közösségek*
is both the brand and the noun ("Táncos közösségek Cegléd városában … – közösségek.com"). Titles
over 65 characters (42 / 14 sampled) are truncated in results but not wrong.

## Method

Throwaway script (stratified `random.sample` per page type, lxml parse of the raw response):
status, title, canonical, H1s, robots meta and header, internal link count, JSON-LD parse and the
claims it makes, words in `<main>`. Repeat it after a structural change; the numbers above are the
baseline.
