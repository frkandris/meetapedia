---
type: SEO
title: Daily data-derived guides
description: The worker materializes at most ten AI-written, fact-grounded city-topic guides per UTC day, following country priority across both domains without new search spend.
tags: [seo, guides, scheduler, structured-data]
timestamp: 2026-09-20
resource: scraper/guides.py
---

# Daily data-derived guides

*The worker gives an LLM a bounded fact packet and publishes only validated, useful comparison articles rather than arbitrary prose.*

## Publication contract

`main.py:_worker_loop()` runs `publish_daily_guides()` before enrichment and before choosing extraction or collection. Enrichment has no separate boot task: the worker is its only launcher, after the guide attempt, so startup cannot race the two workloads for the first free quota. The database counts publications by UTC date, so a restart fills only the remainder of the shared daily cap. Each article uses one logical completion through the existing quota-aware free fleet; failover may spend multiple provider attempts, and those attempts are recorded as `guide_attempts`. There is no new search request.

Candidates follow `pipeline.country_priority`: Hungary, Germany, Indonesia, Sweden, then every other configured country. Hungarian rows belong to `kozossegek` and render at `/utmutatok/{slug}`; all others belong to `meetapedia` and render in English at `/guides/{slug}`. Both indexes are paginated and each domain's sitemap includes only its own guide rows.

## Quality gates

A city-topic pair needs at least eight visible communities, meaningful descriptions on at least 60%, and at least three structured comparison dimensions with two or more known values. These thresholds live in `config/settings.yaml`. Ten is a ceiling, not a quota: if only four pairs qualify, four pages publish.

Each row stores a bounded snapshot of at most twenty communities and its comparison facts. That prevents page weight from growing with the corpus and stops later crawler changes from silently rewriting an indexed article. The page links to the live city-topic listing for current results.

The writing prompt separates trusted editorial rules from an explicitly delimited JSON fact packet. It defines the reader's task, language, evidence boundary, forbidden claims, output schema and length, then asks for a silent factual self-check. Code rejects malformed output, prose outside 300–800 words, unknown evidence dimensions, model-written URLs and numeric claims absent from the packet. It stores writer model and prompt version, allows at most twice the remaining publication quota in attempts, and keeps deterministic control of titles, summaries, counts, links and comparison cards. Pages disclose AI assistance. The first 20 pages require manual sampling before unattended operation; thereafter the handoff prescribes a weekly random sample and golden-set eval before prompt changes.

## Persistence and indexing

`db.py:init_db()` owns the `data_guides` table. Its unique `(site, city, topic)` constraint makes publication idempotent, while `published_at` implements the daily cap. Guide pages emit `Article` JSON-LD, breadcrumbs, self-canonicals, internal links and sitemap `lastmod` values. This complements [[indexing-strategy]] and obeys the bounded-DOM rule in [[tailwind-cdn-jit-large-lists]].
