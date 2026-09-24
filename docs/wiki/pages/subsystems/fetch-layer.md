---
type: Subsystem
title: Fetch Layer
description: An SSRF-safe httpx fetcher validates public DNS and redirects before trafilatura/html2text turns HTML into clean text.
tags: [fetch, trafilatura, httpx, blocked-domains]
timestamp: 2026-07-10
resource: scraper/fetch.py
---

# Fetch Layer

*`fetch_and_clean(url, …)` returns clean page text or `None`; `fetch_many` runs it concurrently under a semaphore, capped at `max_pages`.*

See [[search-layer]] for where URLs come from, [[extraction-layer]] for what consumes the text.

## Extraction pipeline

Two-tier: `trafilatura.extract(include_comments=False, include_tables=False)` first; if the result is missing or `< min_text_length` (100), fall back to `html2text` (links + images ignored). Returns `None` if the fallback is also too short, if HTTP status ≥ 400, if `content-type` lacks `text/html`, or if URL safety rejects the initial/redirect target.

## Safety gates and ordering

Before httpx runs, `fetch_and_clean` applies [[server-side-url-safety]] and the configured blocked-domain list. Every HTTP redirect is checked again.

Blocked domains (`twitter, x, facebook, instagram, tiktok, linkedin, youtube, reddit`) are login-walled/bot-hostile and return no useful text. They are still valid as `social_links` values on extracted records. Matching uses exact host/subdomain boundaries through `host_matches_domain`.

Blocked URLs are filtered **twice** (pipeline pre-filter + `_is_blocked` inside `fetch_and_clean`) — belt-and-suspenders so cached-URL paths can't slip a blocked URL through.

## Concurrency

The pipeline bounds fetches with one `asyncio.Semaphore(fetch.max_concurrent)` and fetches at most `search.max_pages_per_topic` URLs per pair. (`fetch_many` was removed 2026-09-24 — nothing called it.)

## Encoding and binary bodies

The fetcher offers only `Accept-Encoding: gzip, deflate`. Until 2026-09-24 it also offered `br` without the `brotli` package installed: servers answered in Brotli, httpx passed the bytes through undecoded, html2text accepted them, and ~28% of `cache_pages` were binary noise. `looks_undecoded()` now refuses a body whose U+FFFD share exceeds 2%, and `scripts/repair_undecoded_pages.py` reopens the affected pages.

## Playwright fetcher (removed)

Removed 2026-09-24. It had been dormant since social domains moved to `blocked_domains` (2026-05-15: Chromium on login-walled sites cost 91% CPU / 43 GB disk I/O per run), and the Docker image never installed `playwright`, so enabling it would have crashed every run.

## Shared User-Agent

The Chrome 124 UA string lives in `fetch._HEADERS`, the one place it is set.
