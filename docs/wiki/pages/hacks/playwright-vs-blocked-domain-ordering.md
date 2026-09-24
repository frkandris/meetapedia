---
type: Hack
title: Blocked Domains Precede Playwright
description: URL safety and blocked-domain checks run before Playwright, and browser requests repeat the public-address guard.
tags: [fetch, playwright, blocked-domains, ordering]
timestamp: 2026-07-10
resource: scraper/fetch.py
---

> **Obsolete (2026-09-24):** the Playwright fetcher was removed; only the blocked-domain check remains.


# Blocked Domains Precede Playwright

*Fixed 2026-07-10: Playwright no longer bypasses blocked-domain or SSRF checks.*

## Current rule

`fetch_and_clean()` validates public URL/DNS safety, then applies `_is_blocked()`, and only then delegates matching URLs to Playwright. A domain in both configuration lists is blocked. The browser context also intercepts every HTTP(S) request and aborts targets that resolve to non-public addresses.

## Historical failure mode

The old ordering checked `playwright_fetcher.matches(url)` first, so an accidental overlap bypassed the block and launched Chromium against login-walled sites. Configuration no longer carries that security invariant.

## Current state

`playwright_domains` in `settings.yaml` remains intentionally empty, so page fetching uses httpx by default.

## Related

- [[fetch-layer]]
- [[server-side-url-safety]]
