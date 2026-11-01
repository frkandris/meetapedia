---
type: Integration
title: GA4 Reporting
description: The daily email reads visitor/session/pageview numbers from the GA4 Data API via a service account; property 536914034 covers both domains, split by hostName.
tags: [integration, analytics, ga4, service-account, reporting]
timestamp: 2026-11-01
resource: scraper/report.py
---

# GA4 Reporting

*Google Analytics is the source of truth for visitor numbers in the daily email; the
server-side counter remains as a bot-filtered footnote and fallback.*

## Contract

- **GA4 property**: `536914034` (account `394132821`) — one property tracks both
  kozossegek.com and meetapedia.com; `fetch_ga4_traffic()` splits rows by the
  `hostName` dimension (`www.` stripped, substring-matched to a site).
- **GCP project**: `kozossegek-report` (ID `august-button-501918-d2`), Google
  Analytics Data API enabled.
- **Service account**: `ga4-report@august-button-501918-d2.iam.gserviceaccount.com`,
  added as **Viewer** on the GA4 property (no GCP project roles needed).
- **Env vars** (both required, else GA4 is skipped and the email falls back to the
  server counter): `GA4_PROPERTY_ID`, `GA4_CREDENTIALS_JSON` — the full service
  account key JSON pasted as the value.
- **API call**: `POST https://analyticsdata.googleapis.com/v1beta/properties/{id}:runReport`
  with dims `[hostName]`, metrics `[activeUsers, sessions, screenPageViews,
  engagedSessions]`, then dims `[sessionSource]` for the top sources by engaged
  sessions (`_sources`); bearer token from `google.oauth2.service_account` (scope `analytics.readonly`).

## Failure model

`fetch_ga4_traffic()` returns `None` on ANY error (missing env, bad key, API failure)
and logs `ga4_fetch_failed` — the [[daily-report]] then renders the server-side
counter instead. GA4 can never break the email.

## Hard-won rules

- **`GA4_CREDENTIALS_JSON` must be a runtime-only env var in Coolify.** Marking it
  "Available at Buildtime" injects it as a Dockerfile `ARG`, and the multiline JSON
  breaks the build — see [[2026-07-ga4-env-buildtime-failure]].
- The SA key JSON is created and pasted by the owner; sessions must not handle the
  key material.
- GA4 numbers usually exceed the server counter (SPA-less full pageloads, but the
  counter's bot filter is stricter and it only counts public HTML).
- **Engaged sessions are the headline, not users** (since 2026-11-01). From
  2026-10-29 a headless Chrome on Windows, from Singapore/Hong Kong/Frankfurt
  datacentres, walked the meetapedia sitemap one page per "user" at 0.0–1 s:
  27 → 326 → 1,159 → 3,657 GA4 users a day, all `(direct)`, ~13 engaged sessions.
  The subject line now counts `engagedSessions` (10 s, a conversion or a second
  page); raw users stay in the table so a crawler stays visible. The real
  traffic it hid: chatgpt.com sent 58 users (48 s average engagement) in the same
  eleven days, Google organic 15. `scripts/ga4_breakdown.py` breaks a spike down
  by source, country, landing page and device.
