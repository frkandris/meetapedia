---
type: Subsystem
title: Daily Report Email
description: report.py builds one email per UTC day — traffic, per-site diffs, new data guides, run outcomes, and current stock totals — sent via Resend at 04:30 UTC or on demand.
tags: [subsystem, report, email, traffic, analytics]
timestamp: 2026-09-20
resource: scraper/report.py
---

# Daily Report Email

*One email per day answers "what happened yesterday and where does the database
stand" — visitors, diffs, runs, and totals, split Hungarian / international.*

## Pipeline

1. **Trigger**: cron `30 4 * * *` UTC (`schedule.report_enabled` in settings) or
   `POST /admin/api/send-daily-report`. Default day = yesterday (UTC).
2. **Data**: `get_daily_summary(db, start_iso, end_iso, hu_cities)` in `scraper/db.py`
   computes per-scope diffs (new/changed communities via `community_history` joins
   with the `__created__`/MIN(changed_at) guard from
   [[history-created-sentinel-overcounting]], venues, persons, pages, searches),
   run outcomes with `search_failed`/`extract_failed` counters plus the persisted
   top-level `runs.error`, and a `stock` dict —
   current totals per scope (communities, venues, persons, cached/extracted pages,
   covered pairs). Scope split: city ∈ `hu_cities` → `hu`, else `intl`.
3. **Traffic**: [[ga4-reporting]] numbers are primary (visitors/sessions/pageviews per
   site); the server-side counter (`traffic_daily`/`traffic_visitors` tables, fed by a
   bot-filtering HTTP middleware hashing `day|ip|ua`) is the fallback and footnote.
4. **Render**: `build_report_html()` — sections: Látogatók (GA4), Változások (diff
   table), Futások (runs with failure notes), Új adatútmutatók (daily guide links,
   site split and writer model), Állomány (current stock table). Labels
   are self-explanatory Hungarian ("város–téma páros", never bare "pár").
5. **Send**: [[resend-email]] from `info@kozossegek.com` to `REPORT_EMAIL` (fallback
   `FEEDBACK_EMAIL`). Subject: `[közösségek] Napi összefoglaló {day} — {n} új
   közösség, {m} látogató`.

## Design points

- **Diffs AND stock**: the Változások table shows what changed yesterday; the
  Állomány table (added 2026-07-09) shows absolute current totals in the same
  Magyar/Nemzetközi/Össz layout — both views in one email.
- `build_report_html` accepts summaries without a `stock` key (falls back to the old
  totals) so it never breaks on older data shapes.
- Middleware counts **public HTML GETs only** — bot user-agents, `/admin`, static
  assets, and utility paths are excluded.
- Everything degrades silently: no Resend key → skip with log; no GA4 env → server
  counter; empty day → zeros, email still sent.
- The guide block is always present. It links every guide published on the reported
  UTC day to the correct domain and shows the writer model; zero is explicit because
  it distinguishes a quiet guide day from a missing report section. See
  [[daily-data-guides]].
- The AI-spend line separates provider attempts into extraction, enrichment and
  guide writing. Guide attempts are measured around every routed completion and
  persisted even when the call fails; only the remainder is labelled
  `egyéb (preflight, átjáró)`.
- Scheduled/startup exceptions are HTML-escaped and displayed as `futási hiba`; a
  zero-pair failed run therefore carries its actionable cause in the email.
- A run row with `finished_at=NULL` and no stored error is conservatively rendered
  as unfinished (`still running, container restart, or OOM`) because the database
  alone cannot distinguish those states. Provider-level failures make scheduled
  runs unsuccessful; their pair-log counts are shown once, without a duplicate
  top-level error string.
- Since 2026-07-31 the run line has **three** states, read from `runs.outcome`
  ([[run-outcome-three-states]]): ✅ clean, ⚠️ finished with retryable item
  failures, ❌ aborted. Before that a single transient DataForSEO timeout out of
  1414 pairs rendered ❌, identical to a dead provider. The failure clause now
  says the next run retries the pairs, which is what actually happens — nothing
  is retried inside the failing run.
- Since 2026-07-24 the history joins count `DISTINCT` entity ids/rows: a
  community indexed under several topics shares one `community_id`, and the
  plain `COUNT(*)` join multiplied new/changed counts by the number of topic
  rows — the emailed numbers were inflated.
- Since 2026-07-23 `get_daily_summary` also lifts the first `search_error` out of
  the pair logs and the run row renders it as `· ok: <original provider error>`,
  so a search-provider outage is diagnosable from the email alone
  ([[2026-07-search-provider-down-noise]]).
