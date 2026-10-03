---
type: Analysis
title: Search Console Findings on 2026-09-05
description: Fresh exports distinguish kozossegek's persistent indexing collapse from meetapedia's crawl backlog and weak search visibility, without proving a single algorithmic cause.
tags: [seo, search-console, indexing, measurement]
timestamp: 2026-09-05
resource: scraper/web/app.py
---

# Search Console Findings on 2026-09-05

*Superseded as the baseline by [[search-console-2026-10-03]], pulled from the API.*

*The two domains need different diagnoses; the earlier fixes are present, but kozossegek's search visibility has not recovered.*

## Sources and scope

User-provided files in Downloads, read without modification:

- `kozossegek.com-Performance-on-Search-2026-09-05.xlsx`
- `meetapedia.com-Performance-on-Search-2026-09-05.xlsx`
- `kozossegek.com-Coverage-2026-09-05.xlsx`
- `meetapedia.com-Coverage-2026-09-05.xlsx`

Performance filters are Web / Last 16 months, but populated daily series start
2026-05-08 (kozossegek) and 2026-05-14 (meetapedia), ending 2026-09-03.
Coverage is All known pages, with its latest snapshot dated **2026-08-28**.
Do not present the latter as a live September index count or as the share of
current canonical pages indexed: it also includes historical/duplicate URLs.

Daily Chart totals reconcile exactly with Countries and Devices:
kozossegek 902 clicks / 45,071 impressions; meetapedia 197 / 8,266.
Both Queries and Pages contain exactly 1,000 data rows. They are truncated,
whole-period tables, not evidence of which queries/pages performed in August.
Query anonymization is another reason not to reconcile query rows to totals.
See [Google's report limitations](https://support.google.com/webmasters/answer/17010575?hl=en).

## What changed and what did not

| Metric | kozossegek.com | meetapedia.com |
|---|---:|---:|
| Clicks, 2026-08-07 through 2026-09-03 | 2 | 53 |
| Impressions, same 28 days | 14 | 1,247 |
| CTR, clicks / impressions | 14.29% | 4.25% |
| Approx. impression-weighted position from rounded daily rows | 3.29 | 30.62 |
| Indexed, 2026-08-28 | 2,432 | 19,048 |
| Not indexed, 2026-08-28 | 34,941 | 44,706 |
| Crawled, currently not indexed | 23,812 | 437 |
| Discovered, currently not indexed | 7,334 | 36,166 |
| Blocked by robots.txt | 1,056 | 7,310 |
| Page with redirect | 1,910 | 392 |
| Duplicate without user-selected canonical | 720 | 5 |
| Alternate page with proper canonical | 33 | 281 |
| Excluded by noindex | 43 | 43 |
| Server error (5xx) | 32 | 36 |
| Not found (404) | 1 | 33 |
| Google chose a different canonical | 0 reported | 3 |

Sources: performance Chart!A93:E120 (HU), Chart!A87:E114 (meetapedia);
coverage Chart!A84:D84 and Critical issues!A2:D10 / A2:D11 respectively.
All exclusion reasons sum exactly to each coverage chart's not-indexed total.

**The HU break starts on May 31, earlier than some wiki summaries imply.**
May 25–30 produced 471 clicks / 25,686 impressions in six days. May 30 alone:
71 / 4,760; May 31: 2 / 223. June produced 30 clicks, July 4, August 3.
The current problem is near-absence of impressions, not primarily CTR.
An average position of 3.29 on just 14 impressions does not indicate recovery.

Meetapedia grew from 37 clicks / 939 impressions in the preceding 28 days to
53 / 1,247, but remains small. Its index is much larger than kozossegek's,
while its dominant exclusion is **not yet crawled**, not crawled-and-unindexed.
Across the full export 175 of its 197 clicks originated in Hungary; this is
historical country evidence, not proof that today's HU redirects are broken.
Visible international queries include `kampsport göteborg` (93 impressions,
position 83.37), `dresdner schachbund` (84, 65.75), and `yogakurs graz` (30, 73.3).

## Live checks, 2026-09-05

Read-only GET requests with curl; small sample, not an uptime or Googlebot audit:

- Both robots.txt files returned 200; city/detail paths are not broadly blocked.
  The excluded `/community/` prefix is present. The 7,310 blocked meetapedia URLs
  cannot be classified without the reason-specific URL export.
- Both sitemaps returned 200 and parsed: kozossegek **33,933 URLs / 4,205,335 bytes**;
  meetapedia **38,120 / 5,015,569 bytes**. One fetch each took about 5 seconds;
  that is total request time, not a TTFB measurement.
- `/kazincbarcika/magyar-maltai-szeretetszolgalat-kazincbarcikai-csoport`
  on meetapedia returned **301** to the same HU path on kozossegek, then 200 with
  a matching canonical and no robots noindex meta tag.
- kozossegek `/emod/emodi-pincegazdak-egyesulete` returned 200, self-canonical,
  no noindex meta, about **49.6 KB** HTML. Its body has a substantial description,
  so not every remaining indexing failure can be explained as a missing paragraph.
- meetapedia `/graz` returned 200, self-canonical, about **172 KB**; English intro
  with German community text. Budapest returned 200, self-canonical, about **315 KB**.
  Language targeting and page weight warrant testing, not a claim of proven cause.

Python urllib requests returned 403 while curl GET succeeded. This is a
client-dependent observation, **not proof that verified Googlebot is blocked**.
Verify crawler access using GSC live inspection and server/Cloudflare logs.

## Corrections to earlier interpretations

- [[2026-06-seo-traffic-collapse]] and [[2026-06-search-index-collapse]] record
  important historical observations, but their assertions of an algorithmic
  quality penalty, shuffle as a proven trigger, and boilerplate as a definitive
  cause exceed what these exports establish. They remain hypotheses.
  [Google defines crawled-not-indexed](https://support.google.com/webmasters/answer/7440203?hl=en)
  as a state, not an explanation or proof of a penalty.
- [[seo-cross-domain-canonical]] documents a fix; a sampled live 301 now confirms
  it. The whole-period page table cannot establish whether Google has recrawled
  and consolidated the current redirect targets.
- [[indexing-strategy]]'s old thin-page exclusion description was superseded by
  commit `04282ff` on 2026-08-21: details now pass `page_noindex=False`, and
  `_build_sitemap` includes visible communities without descriptions.
- [[2026-08-boilerplate-outweighed-the-content]]'s hidden dropdown fix is present
  in code and the sampled detail document is substantially smaller. This does
  not imply sitewide crawl/index recovery; the coverage export ends only a week
  after the August 21 changes.

## Evidence still needed

The reason-specific URL exports arrived later on the same day (see below).
Still needed: recent 28-day Pages/Queries exports;
URL Inspection's selected canonicals and last-crawl dates for representative
pages; Crawl Stats host status and response times; current Manual Actions and
Security Issues screens. The four exports do not contain these.

The next useful experiment is a small cohort of existing canonical pages with
verified, practical joining information, compared with unchanged similar pages.
Longer generated text, blanket noindex removal, or more submitted URLs alone
are not evidence of added value. No production settings or content were changed
during this investigation.

## Follow-up: ten drilldown exports

Files: `{domain}-Coverage-Drilldown-2026-09-05.xlsx` and suffixes `(1)` through
`(4)` for each domain, from the user's Downloads. Classification uses Metadata's
Issue field, not the filenames. Meetapedia `(3)` and `(4)` have identical parsed
tables, charts and metadata: crawled-not-indexed was exported twice. There are
nine distinct reports; the three requested categories are all present.

Each Table has at most 1,000 examples, so these are **not representative random
samples** and their proportions must not be extrapolated to the full index.
Excel crawl dates use the 1899-12-30 epoch. Both discovered-not-indexed tables
contain serial 25569 throughout (1970-01-01), a missing-date sentinel, not evidence
of an actual crawl. Drilldown last-crawl dates reach August 29 even though Chart
ends August 28; preserve the distinct timestamps instead of forcing alignment.

| Domain / reason | Exported examples | Finding |
|---|---:|---|
| HU robots-blocked `(3)` | 1,000 / 1,056 | 907 `/source/`, 83 legacy `/out?`, 10 `/set-lang?`; none in current sitemap |
| Meetapedia robots-blocked, unsuffixed | 1,000 / 7,310 | 999 `/set-lang?`, one `/source/`; none in current sitemap |
| HU duplicate `(4)` | 720 / 720 | 709 `/kozosseg-bekuldes?` URLs; all 720 last-crawled May 13–June 1; none in current sitemap |
| HU crawled-not-indexed, unsuffixed | 1,000 / 23,812 | 979 in current sitemap; last-crawled August 17–29; 712 on/after August 21, 541 on/after August 23 |
| HU discovered `(1)` | 1,000 / 7,334 | All in current sitemap; 225 venues, 281 people, 10 single-segment paths, 484 other city-scoped paths |
| Meetapedia discovered `(1)` | 1,000 / 36,166 | All in current sitemap; 30 single-segment paths, 970 city/topic-or-community paths |

The 541 later HU crawls weaken an explanation based solely on Google not yet
returning after the August changes. They do not prove which code revision Google
saw or how long reconsideration should take. The HU sample includes 182 person
and 187 venue pages: this is not exclusively a community-description problem.

### Live verification of examples

GET checks on September 5, no redirect following for initial status:

- The duplicate example `/kozosseg-bekuldes?city=Magl%C3%B3d&topic=vallalkozas`
  now returns 200 and canonicalizes to `/kozosseg-bekuldes`. The old duplicate
  report is therefore not proof that today's canonical tag is missing.
- HU crawled examples `/vasvar/mi-idonk-noi-klub` and `/eger/egri-sanga` return
  200, self-canonical, without robots noindex meta. Both already have substantial
  descriptions. `/pecs/voisingers-pecs` instead returns 302 to `/pecs` and is not
  in the current sitemap: report labels do not necessarily match current state.
- Meetapedia's `/aachen/buchklub`, `/aachen/filmklub`, and
  `/aachen/grune-hochschulgruppe-aachen-e-v` are sitemap-listed and return 200,
  self-canonical, without noindex meta. Discovery in these examples is already
  solved; submitting the same URLs again does not establish crawl capacity.
- Data-quality review candidates: `/aba/helyszin/mta-szekhaz` labels the Academy
  headquarters as Aba; `/mohacs/mohacsi-tobbcelu-kistersegi-tarsulas-idosek-klubja-dunaszekcso`
  has Mohács in the title but Dunaszekcső in its own address paragraph. Check the
  source before editing locations; these are observed inconsistencies, not a
  measured explanation of Google's decision.

### Confirmed sitemap/routing mismatch

Intersecting the redirect exports with current sitemaps found 13 Meetapedia URLs
and one HU URL. Live checks show **11 of those Meetapedia URLs still redirect**:
10 `/<city>/community-general` paths and `/explore` → `/felfedezes`. The other two
Meetapedia URLs (`/cities`, a Hannover chess detail) and the HU detail now return
200. Do not remove all historical redirect examples mechanically.

The full current sitemap contains 12 `community-general` paths on Meetapedia and
nine on kozossegek; only the ten in the Meetapedia intersection were HTTP-tested.
The code explains the mismatch: `_build_sitemap` iterates stored DB topics and
`_topic_url_slug` generates a fallback slug for unknown names, but
`public_city_segment` only accepts configured topics. `community_general` is
absent from `config/topics.yaml`, so that route falls through to a city redirect.
One affected detail also exposes the internal topic name in its title:
`/cologne/freiwilligendienste` → `Freiwilligendienste – community_general Cologne`.

This is a concrete repair target, not sufficient evidence that eleven redirects
caused the sitewide collapse. Align sitemap topic eligibility with route
eligibility and reconcile legacy topic labels separately; avoid blindly deleting
communities under the old topic. [Google's sitemap guidance](https://developers.google.com/search/docs/crawling-indexing/sitemaps/build-sitemap)
calls for preferred canonical URLs.

The `/cities` GET returned about 1.01 MB of HTML; investigate this separately from
the corrected community-detail dropdown. A single response-size measurement is
not an uptime or crawl-budget diagnosis. No SEO code or production data was
changed in this follow-up.

## Implemented after the user's repair request

On September 5, the user authorized fixes, verification, commit and push.
`_build_sitemap` now emits topic listings only for configured topics, while
keeping the detail URLs stored under retired topics. Meetapedia's static entries
now use `/rolunk` and `/felfedezes`, where the application actually renders those
pages, instead of their redirecting English aliases. Existing public URLs and
database records are unchanged.

Community details under retired topics no longer link to missing topic listings
in visible navigation or JSON-LD breadcrumbs. Titles use the localized Other
label when a topic has no translation. The hidden report form retains the real
stored topic so moderation still identifies the original record. Related listings
on community details now use the same centered width and mobile gutters as the
main card, fixing the screenshot supplied during the investigation.

`tests/test_sitemap_routes.py` checks both domains with configured and legacy
topic data: all generated sitemap URLs return 200 without following redirects
and self-canonicalize; legacy community details remain listed and indexable,
without broken topic links or corrupted report identity. Tests use in-process
TestClient, not a locally started production server.
