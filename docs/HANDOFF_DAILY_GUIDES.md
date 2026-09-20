# Daily data guides — handoff

Branch: `feat/daily-data-guides`

## Outcome

The continuous worker now attempts the day's remaining guide quota before any extraction, collection, or enrichment work. The default is at most 10 new pages per UTC day. It uses no DataForSEO request. Each article body uses one call from the existing quota-aware model fleet; deterministic code supplies and renders the facts.

Candidates follow `pipeline.country_priority`: Hungary first, then Germany, Indonesia, Sweden, then unlisted countries. If Hungary has no more eligible city-topic pairs, the same run continues into the next country. Hungarian pages publish on közösségek.com under `/utmutatok/`; international pages publish in English on meetapedia.com under `/guides/`.

## Quality and safety

- Minimum 8 visible communities.
- At least 60% have a meaningful description (50+ characters).
- At least 3 structured comparison dimensions have data for 2+ communities.
- The daily 10 is a ceiling, not a forced quota.
- The writer sees a bounded JSON fact packet and cannot browse; invalid JSON or prose outside 300–800 words is rejected.
- The row records writer model + prompt version; unknown evidence dimensions, model-written URLs and numeric claims absent from the fact packet are rejected.
- Titles, summaries, counts, links, comparison cards and schema remain deterministic; the page discloses AI assistance.
- Unique `(site, city, topic)` and per-day DB counting make restarts idempotent.
- Each article snapshots at most 20 records; indexes paginate at 24 rows.
- Sitemap entries and `Article`/breadcrumb structured data are domain-scoped.

Settings are under `schedule.guides_*` in `config/settings.yaml`. The durable design note is `docs/wiki/pages/seo/daily-data-guides.md`.
The prompt-design research and source links are in `docs/research/ai-article-brief.md`.

## Deployment check

After Coolify deploy, verify `/utmutatok` and `/guides` on their respective domains. On the first worker iteration, logs should show `daily_guides_published` with a count and slugs. A zero count is valid when no pair passes the gates or the UTC-day cap is already full. Confirm new URLs appear in the matching `/sitemap.xml` and never in the other domain's sitemap.

Before leaving the feature unattended, manually review the first 20 published pages for factual fidelity, useful local language and repetitive phrasing. Thereafter review a random weekly sample and keep the prompt version fixed until a small golden-set eval has compared the replacement.
