---
type: Subsystem
title: Web App (routes, auth, state)
description: One FastAPI app with a public router and an /admin router gated by pure-ASGI Basic auth; Hungarian paths are canonical and English paths redirect.
tags: [web, fastapi, routing, auth, app-state, admin]
timestamp: 2026-07-23
resource: scraper/web/app.py
---

# Web App (routes, auth, state)

*A single `_fastapi` app serves both domains; `app = _BasicAuth(_fastapi)` wraps it so `/admin/*` is gated. See [[two-domain-single-container]] and [[i18n-and-site-detection]].*

## Auth: pure-ASGI `_BasicAuth`

`_BasicAuth` is pure ASGI (not Starlette `BaseHTTPMiddleware`) specifically so it does **not buffer SSE** (the admin log stream). It guards only `/admin`-prefixed paths; everything else passes through. Missing `ADMIN_PASSWORD` → 503. Credentials compared with `hmac.compare_digest` (constant-time). Writes additionally require same-origin (`_same_origin_admin_write`: `Origin`/`Referer` netloc must equal `Host`) — a CSRF guard on top of auth.

## Routing: Hungarian canonical, English alias

The Hungarian path is the canonical handler; the English path 301/302-redirects to it (`/explore`→`/felfedezes`, `/map`→`/terkep`, `/about`→`/rolunk`, `/venues`→`/helyszinek`, `/people`→`/emberek`, `/submit-community`→`/kozosseg-bekuldes`). `_render_explore` is the shared renderer.

**Route ordering is load-bearing.** The two greedy catch-alls `/{city_slug}/{segment}` and `/{city_slug}` are registered **last**, after all literal/deeper routes (`/felfedezes/{topic_slug}`, `/{city_slug}/helyszin/…`, `/{city_slug}/ember/…`). Starlette matches in registration order, so literals win. `public_city_segment` disambiguates a topic slug from a community-name slug by trying `_topic_from_url_slug` first, then `_find_community_by_slug`, else 302 to the city page. When adding a route that exists on both domains, add both prefixes to nav active-state checks.

## `app_state` singleton

Module-global dataclass. **`cities` and `topics` are lists of config objects, not dicts** — always `c.name` / `c.country` / `c.locale`; dict-style access 500s any route touching them (see [[2026-05-coverage-page-500]]). The route bodies also use many function-local imports — never re-import a name that a different branch of the same function uses from the module level ([[function-local-import-shadowing]]). Pipeline callbacks mutate progress fields read by coverage, while `RunCoordinator` exclusively owns `is_running`, `_run_task`, and `current_run_mode`. `_home_stats_cache` (keyed by site) is invalidated after every run.

## Public discovery surfaces

The unfiltered `/{city}` page intentionally lists communities only. City-wide venue/person sections were removed in 2026-05; their stale `city_venues`/`city_persons` context queries were removed in 2026-07 after the old tests exposed the drift. A city + single-topic page still shows matching `topic_venues`. Full collections live at `/helyszinek` and `/emberek` (the latter deduplicates by normalized name+city), while community/detail pages link related entities directly. Do not restore city-wide entity scans unless the corresponding UI is intentionally restored too.

## Coverage page

City × topic matrix from `get_city_topic_states` + `get_fully_processed_pairs` at the current fingerprint. Five cell states: green (has communities), blue ✓ (done, 0 results), amber ~ (searched but stale fingerprint), gray · (never searched), pulsing ▶ (actively processing). It paginates 50 cities at a time; JS polls `/admin/api/coverage/current` every 3 s to move the highlight and refreshes the previous cell via `/admin/api/coverage/cell`. `POST /admin/api/restamp-fingerprints` bulk-updates stale fingerprints to current without reprocessing. See [[2026-06-coverage-amber-cells]].

## Queue and runs

Admin I/O ops (scrape/extract/enrich) go through an in-process queue (`queue_items` + `_queue_fns` + a worker task); manual cache-detail buttons use `priority=True`. Pipeline, scheduled, and startup runs share one coordinator-owned slot and task-identity cleanup. See [[shared-run-task-slot]] and [[asyncio-task-cancellation]].

## Admin Inbox nav

User-submitted items (edit requests, not-community reports, community submissions) live under one **Inbox** dropdown with live pending-count badges. The counts come from `count_pending_interactions` (db.py) exposed as a Jinja **global callable** (`templates.env.globals["inbox_counts"]` in app.py) so every admin page renders fresh counts without per-route context plumbing — the same globals mechanism as `app_version`, but a callable because a startup-time value would go stale. Not-community reports have no status column: every stored row is pending, handling deletes the row. Approving a submission stays optimistic (status='approved' immediately) but the background scrape re-queues it to 'pending' on failure so it cannot silently vanish with no community created. Venue edit requests are applied on approve for `closed` (row deleted — venues have no hidden flag) and `name_correction` (key recomputed); `wrong_info` is notes-only and remains a manual fix. **Approval always re-resolves the record from the identity the admin sees** (entity_name/city/topic → recomputed record key): the form's `record_key` is client-controlled, and trusting it would let a submitter pair an innocuous displayed name with another record's key so the approval mutates an unrelated record. Never mutate by a client-supplied key on any future moderation flow. See [[admin-simplification-2026-07]] for what was removed around it.

## Tailwind CDN JIT

Both admin and public load the Tailwind CDN JIT runtime (not a build). All utility classes must appear as **complete literal strings** at load; JS that rebuilds cells hard-codes full class strings (`bg-green-100 text-green-700`) rather than templating them, because a computed `bg-${c}-100` would never generate. See [[tailwind-cdn-jit-large-lists]].

## Flags

`request_city_en` ignores its form and just 301s to `/varosok` (stub). `_render_explore` still performs per-topic/per-city DB reads in some branches (N+1 risk on large data); the homepage's heavier totals are memoized in `_home_stats_cache` per site and invalidated after a run.

## Cross-edition surfaces

`public_base.html` renders a language strip above `<main>` linking the same page on the
other domain, and a footer link to the sister home page. The About page (`/rolunk`, EN
`/about` 302s to it) credits the author, links the GitHub repo, and explains the
kozossegek ↔ meetapedia relationship. See [[sister-site-cross-links]].

Visual identity (palette, typeface, page head, About content) since 2026-10-23:
[[site-visual-identity]].
