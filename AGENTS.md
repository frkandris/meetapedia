# AGENTS.md

<!-- GENERATED FILE — do not edit directly.
     Source: CLAUDE.md · Regenerate: PYTHONPATH=. .venv/bin/python scripts/sync_agents_md.py -->

This file provides guidance to coding agents (Codex, Copilot, and friends) working
with code in this repository. It is a verbatim copy of CLAUDE.md.

## Commands

```bash
# Run tests (the whole suite passes — nothing to ignore)
PYTHONPATH=. .venv/bin/pytest

# Run a single test file
PYTHONPATH=. .venv/bin/pytest tests/test_store.py

# Lint
.venv/bin/ruff check scraper/ tests/

# Install dev deps
pip install -e ".[dev]"

# Keep AGENTS.md in sync after editing this file (a test enforces it)
PYTHONPATH=. .venv/bin/python scripts/sync_agents_md.py
```

**No local dev environment.** The app runs on Hetzner, managed by Coolify. Do not attempt to start the server locally for verification — read templates and code directly instead. The app is a FastAPI server (uvicorn) on port 8000 in production. `ADMIN_PASSWORD` env var must be set or the admin UI is inaccessible.

## Architecture

The scraper discovers community groups for each `(city, topic)` pair:

1. **Search** (`search.py`): DataForSEO only (`DataForSEOClient`, live or standard mode). Quota errors (`SearchQuotaError`) permanently skip the provider for the run via `FallbackSearchClient` (kept as a single-provider wrapper so a fallback can be re-added with one line).
2. **Fetch** (`fetch.py`): `httpx` + `trafilatura` to extract clean page text. Blocked domains (Facebook, Instagram, TikTok, LinkedIn, YouTube, Reddit, Twitter/X) return `None` immediately. `playwright_domains` in `settings.yaml` controls Playwright-fetched domains (currently empty — the Playwright fetcher is dormant).
3. **Extract** (`extract.py`): `FallbackExtractor` is the single failure path (typed errors, circuit breaker, retry). Its `primaries` list comes from `pipeline.build_extractor()` — **the one place a chain is assembled**. With `router.enabled` in `config/providers.yaml` that is the free-tier fleet ordered best-quality-first and vetoed per provider by the persisted quota ledger; otherwise it is the single `DeepSeekExtractor`.
4. **Store** (`store.py` → `db.py`): Upsert to SQLite `communities` table, merging `source_urls` on conflict.

The full run is orchestrated by `pipeline.py:run_pipeline()`. Modes:
- `full`: search → fetch → extract → enrich (default; labelled "Smart" in the UI)
- `ai_only`: re-extract from cached page texts, no web requests
- `search_only`: search + fetch + cache raw text, zero LLM calls (the saver collector)

(The former `revalidate`, `recategorize`, and description-maintenance admin flows were deleted 2026-07-23 — the focus is low-cost world indexing. Historical `revalidate` rows may still appear in `runs`.)

**Pipeline city priority**: `_saver_city_groups(cities, priority)` in `main.py` (not `pipeline.py`) returns one group per named country plus a trailing "everything else" group. The order comes from `pipeline.country_priority` in `settings.yaml` — **Hungary → Germany → Indonesia → Sweden → rest** since 2026-08-16, when the 1000+ inhabitant import left Hungary with 973 unprocessed settlements on the primary market. Description enrichment walks the same groups, for the same reason. A worker pass ends when the work signal flips, so a country behind a large backlog may not be reached in that pass: this list *is* the expansion priority.

**Done-pair pre-filter**: `run_pipeline()` calls `get_fully_processed_pairs(db_path, current_fp)` (one SQL query) before inner loops and passes the complement as `pairs_filter`. Pairs with a `search_cache` entry AND all `cache_pages` at the current `extract_fingerprint` are skipped entirely — no loop iteration, no log entry. Fully-covered cities should not appear in the log.

**Scheduler**: there is none — one continuous worker (`_worker_loop` in `main.py`, `schedule.worker_enabled`) is the only thing that runs the pipeline. It picks `ai_only` while free quota remains (it expires at 00:00 UTC) and `search_only` once that is gone; the quota coming back is a `should_stop` condition the collector watches, not a scheduled event, so extraction resumes on its own at midnight. **The twin cron windows were deleted 2026-09-20.** They had not been registered with APScheduler since the worker shipped on 2026-08-18 — `saver_enabled`, `extract_cron`/`extract_until`, `search_cron`/`search_until`, `cron`/`cron_enabled` and `auto_run_on_startup` were all still in `settings.yaml`, still read by code, and none of them could change what the process did. `worker_enabled: false` now means *run nothing*, a maintenance switch with no other path behind it. `run_pipeline(stop_at=…)` still exists and is still tested; nothing in production passes it. `ai_only` loads raw pages one pair at a time; never restore the old whole-cache materialization because it can OOM at production scale. **Description enrichment** (`schedule.enrich_enabled`, `_enrich_run`) is started only by the worker after its daily guide step, which also restarts it if it stops; do not add a separate boot task because that races guide publication for quota. It shares the same provider budget via the quota ledger, is bounded per round and idempotent/resumable (the `long_description` marker), and deliberately does NOT reserve the run coordinator (coexists with `ai_only`).

**Daily data guides run first.** Once per UTC day the worker calls `guides.publish_daily_guides()` before enrichment or either pipeline mode. It materializes at most `schedule.guides_daily_limit` (10) new city×topic guides from existing records, with minimum community, description-coverage and comparison-dimension gates. The cap is shared across both sites and restart-safe in `data_guides`; candidates follow `pipeline.country_priority` (Hungary first, then Germany, Indonesia, Sweden, then the rest). Hungarian guides publish at `/utmutatok/*` on kozossegek.com, all other countries at `/guides/*` on meetapedia.com. One routed fleet call writes each article from a bounded fact packet; deterministic code owns titles, counts, cards, links and structured data. It makes no search calls and may publish fewer than ten rather than weaken the gates or accept malformed AI output.

**The fact packet is ordered by what it costs to lose, and capped at `_PACKET_BUDGET_CHARS` (6,000).** `communities` — the names — comes first and `comparison_dimensions` last, because the smallest context in the fleet is `localgpu`'s 8,192 tokens and llama.cpp *trims* an over-long prompt instead of refusing it. Hungarian runs about 1.4 characters per token against an English-trained vocabulary, so a packet that looks small is not. Nine of the first ten published guides named **zero** groups for exactly this reason: the community list was the tail, and the tail is what fell off. The dimension tables are the right thing to lose instead — the page prints them in cards under the prose either way. Never append a new key after `communities`.

**The prose gate is mechanical, and every threshold is calibrated on the ten v1 articles it rejects.** `_decode_article` refuses a repeated sentence, a three-word phrase occurring more than four times, fewer than three named groups, a section under 40 words, a Hungarian guide without Hungarian function words, an untraceable number, and a link. The word floor is 260, not the English-shaped 350 that made v1 pad: a dense Hungarian draft of this article is ~330 words where its English twin is over 400. Each refusal increments `guide_rejected_<reason>` *and* `guide_rejected_by_<model>` for the day and logs the city and topic — a day that publishes nothing must say whether the corpus ran out of candidates or the writer kept failing, because those need opposite fixes. The fleet's quality scores are measured for extraction, which is not this skill: a 4B model scoring 73 returned a 132-word stub where a 120B scoring 62 wrote 359 usable words naming seven groups.

**A guide whose `prompt_version` is not current is rewritten in place before any new guide is published** — same slug, same `published_at`, new body via `replace_data_guide`. Improving the writer is what makes a published article stale, and fixing a page people already read beats adding an eleventh. Rewrites and new publications share the one daily budget.

**Topic tiering**: cities with `topic_tier: core` in `cities.yaml` (260 small Swedish kommuner, ~1,976 German Städte under 100k, and 968 Hungarian settlements under 10k added 2026-08-16) only run `pipeline.core_topics` from `settings.yaml`; tiered-out pairs are fully frozen (no search, no re-extraction). `_tier_allows()` in `pipeline.py` is the single gate, also used by `/admin/api/search/jobs`.

**Adding a country**: use `scripts/import_cities.py <country> --min-pop N --apply --write-coords`. It is additive (never rewrites existing entries), resolves accent-fold slug collisions with a `(Region)` suffix, and tiers small settlements. A new locale needs three things or it degrades silently: `search_terms` in `topics.yaml`, an entry in `LOCALE_TO_DATAFORSEO_LOCATION` (`search.py`), and i18n labels.

**Model routing**: `config/providers.yaml` holds the free-tier fleet (Groq, Gemini, Mistral, OpenRouter, Cloudflare Workers AI; Cerebras and GitHub Models are in the file but `enabled: false`) with per-model quality scores. The two paid providers, DeepSeek and `openrouter_paid`, are **`enabled: false` since 2026-09-05** on top of `allow_paid: false` — see the ceiling paragraph below. Routing happens **before** generation, never as a cheap-first cascade. **Every model in the fleet shares one `fingerprint_model`** — the fingerprint keys the extraction cache, so letting it vary per model would invalidate ~74K cached extractions. Which model actually ran lives in `cache_pages.extract_model`/`extract_quality`, outside every key; read it with `pipeline._served_by()`, never `extractor.model` (that names the head of the chain and lies after failover). `QuotaLedger` persists per-day spend in `provider_usage` and lowers a provider's ceiling when a 429 proves the published limit wrong. Admin view: `/admin/providers`.

**Paid providers need a ceiling, not just a permission.** `router.allow_paid` says paid models *may* be used; `router.daily_budget_usd` says how much, and **0 (the default) means no paid calls at all** — `paid_allowed()` requires both. A free provider stops us with a 429; nothing stops a paid one, which is why `allow_paid: true` alone burned ~$60 in four days (2026-08-24..27, see `docs/wiki/pages/post-mortems/`). `provider_usage.cost_usd` accumulates from the provider's own reported token split × the model's `usd_per_1m_in`/`usd_per_1m_out`; **refused calls are charged too**, because a truncated answer is billed in full. `build_extractors` refuses to build an unpriced paid model — it would report $0.00 against the ceiling. Reaching the ceiling removes paid providers from routing until 00:00 UTC; it never aborts a run. `ModelRouter.done_for_today()` — money, requests *and* tokens — is what `preflight()` must ask before probing, or every run spends one paid call proving what the ledger already knows. **Currently 0.00, and since 2026-09-05 the reason is the opposite of what it was.** It used to be that there was nothing to spend. Now the OpenRouter account holds **~$10.80** — bought to cross OpenRouter's $10 all-time-purchase threshold, which lifts the `:free` daily cap from 50 to 1000 — and `openrouter_paid` **shares `OPENROUTER_API_KEY`** with the free entry. So the money is real, reachable, and must not be spent: draining it loses the free-tier capacity it bought, and a *negative* balance makes OpenRouter answer 402 on the free models too. DeepSeek's own account still holds $0.69. **Three switches now guard this, not one** — `allow_paid: false`, `daily_budget_usd: 0.00`, and `enabled: false` on both paid providers. Turning paid back on means reversing all three deliberately; flipping one and finding nothing happens is the designed behaviour, not a bug.

**Our own GPU is a provider, not a special case.** `localgpu` in `providers.yaml` is a model on the operator's Mac (llama.cpp + Qwen3-4B), reached through the same catalogue and ordered by the same measured quality — **73** on the hu golden set, above Qwen3-8B's 67 and gpt-oss-20b's 65 on the identical sample. The smallest candidate won, which is this file's own LLMStructBench caveat proving itself: measure the small model first. Its point is not the score: on 2026-09-05 every free provider's allowance was spent by 22:30 UTC, and a provider at 67 that is always there beats one at 74 that has been gone since lunchtime. Three things about it differ from a hosted provider and each has a reason. **`base_url_env` (`LOCAL_GPU_URL`) holds the address**, because a tunnel to a laptop gets a new hostname on every reconnect and `config/` is not persisted — a URL in the YAML would make each reconnection a deploy; `configured` requires the address as well as the key, so a machine that is asleep is *absent* rather than built-and-failing at connect time. **`timeout_seconds: 600`** against the global 60: a full `max_text_chars` page measured 66.5 s, and a timeout is scored as a failure, so the global would not report a slow provider — it would retire a working one through the circuit breaker. **`max_output_tokens: 4000`** against the fleet's 1,500, the one place that rule inverts, because nothing is reserved or billed locally. Use llama.cpp, never Ollama, on Apple Silicon: Ollama's MXFP4 packaging does not reach Metal (14.5 vs 27-29 tok/s generation, measured on the same weights). Two runtime settings are load-bearing and silent when wrong: `--chat-template-kwargs '{"enable_thinking":false}'` (Qwen3 ignores `--reasoning-budget 0` and reasons in plain text, which becomes `ExtractorContentError` — the quarantine trigger), and `-c 8192` (a short context window trims the prompt rather than failing, and the page text is the tail). See `docs/wiki/pages/decisions/our-own-gpu-in-the-fleet.md`.

**External LLM gateway**: `/v1/chat/completions` + `/v1/models` + `/v1/quota` (`scraper/web/api.py`) expose the router to other software with the OpenAI wire format. Bearer auth from comma-separated `ROUTER_API_KEY`; **unset = the gateway 401s everything**, never open. Deliberately general purpose — no project prompt or schema is injected. Gateway calls spend the same daily ledger as the crawler.

**Search cost rules**: every *successful* search is saved to `search_cache` — even empty results and Full Refresh runs (an unsaved empty search is re-paid every run). Provider failures raise (`SearchQuotaError` / `ExtractorUnavailableError`) and are NOT cached: the pair/page is skipped with a `search_failed`/`extract_failed` pair-log counter and retried next run. Never convert these errors to empty results — caching an empty extraction under the current fingerprint is permanent silent data loss. `FallbackSearchClient.search_all(stop_after=…)` stops issuing paid queries once enough unique URLs are collected. Production uses `dataforseo_mode: standard` with `standard_priority: 2` (~$1.2/1K, normally ≤1 minute); normal priority may legally exceed the client's 5-minute polling window and must not be used with the current sequential collector.

**Extractor failure rules**: `run_pipeline()` calls `extractor.preflight()` — one live mini-extraction — before any pair loop, so a broken model name or revoked key fails the run immediately instead of one skipped page at a time (`search_only` skips it: no LLM). During a run, `FallbackExtractor` opens a circuit breaker after `_FAILURE_THRESHOLD` (20) *consecutive* failed calls; one success resets the counter. `providers_down` (providers configured but all dead) aborts the run with the reason in the pair log's `extract_error` → run detail banner + daily email. `exhausted` alone must NOT abort — it is also true when no API key is set, which is a deliberate no-LLM run.

**A failed extraction is never cached, and after three tries it is never retried either.** Caching a failure would record "0 communities" permanently under the current fingerprint — unrecoverable. The missing half of that rule was a bound: a page that fails *deterministically* was re-attempted by every run forever, walking the whole fleet each time (~21 pages × ~30 runs = most of one day's paid bill). `_Quarantine` in `pipeline.py` counts failures per `(url_hash, fingerprint)` in `extract_failures` and stops attempting a page at `pipeline.extract_max_page_failures` (3; 0 disables). **Only `ExtractorContentError` counts** — a model answered and the answer was unusable (truncated, malformed JSON) — and `FallbackExtractor._call` raises it only when every provider that answered agreed and nothing transient happened in the same call. Outages, 429s and spent quotas must never reach it. The fingerprint is half the key, so any prompt or model change releases everything automatically; `/admin/quarantine` releases one page by hand. `get_fully_processed_pairs(quarantine_threshold=…)` treats a held page as done so its pair leaves the loop.

**`max_tokens` is capacity, not headroom — and it is per model.** Free tiers
charge `prompt + max_tokens` against a per-minute token window **before
generating** — Groq's is 8,000. Sending no cap (as we did until 2026-08-19)
reserves the model's maximum on every call: roughly one request per minute,
which is why Groq stopped at 354 calls in a day and 838 of Gemini's 1,205 came
back 429, and why an 8,000-char prompt returned a plain HTTP 413 instead of a
truncated answer. The resolution order is model → provider → the global
`deepseek.max_output_tokens`; a paid reasoning endpoint gets 4,000 without
handing the same number to Groq. Do not raise the *global* one "for safety" —
that makes runs slower, not safer. If `llm_output_truncated` appears for a
free provider, cut `max_text_chars` first: past a page's useful content that
half of the sum buys nothing. Learned on a sibling
project, where measuring a model on the *real* workload — not a synthetic
prompt — also reversed the catalogue's quality ranking, so treat
`providers.yaml` scores as measured-for-English-extraction, not universal.

**Cache**: everything goes through `cache.py` (a thin facade over `db.py`). Each scraped URL gets a row in `cache_pages`. The extraction cache is fingerprint-keyed: SHA-256[:12] of `SYSTEM_PROMPT + model_name`. Changing either invalidates all cached extractions automatically.

**Web app** (`web/app.py`): single FastAPI app serving two domains from one container. Public router (`_fastapi`) and admin router (`admin`, gated by `_BasicAuth` ASGI middleware). `_detect_site(request)` reads the `Host` header and returns `"meetapedia"` or `"kozossegek"`. `lang_context(request)` injects site-aware variables (`site`, `site_name`, `site_url`, `lang`, `locale`, `map_url`, `about_url`, `explore_url`, `submit_url`, `map_center`) into every public template. `_site_cities(request)` filters cities by domain (HU-only vs. all). Shared runtime state lives in `web/state.py:app_state` singleton.

`app_state.cities` and `app_state.topics` are **dataclass objects** — always use `city.name`, `city.country` (NOT `city["name"]`). Dict-style access causes a 500 on any route that touches them.

`app_state.current_city` / `current_topic` are set by the `on_pair_start` callback during pipeline runs (cleared in `finally`). Consumed by `/admin/api/coverage/current` for the live jump-to-active feature.

## Key Files

| File | Purpose |
|---|---|
| `scraper/main.py` | Entry point: scheduler + uvicorn |
| `scraper/pipeline.py` | Run orchestration |
| `scraper/extract.py` | LLM prompts, schemas, extractors |
| `scraper/db.py` | All SQLite access; `init_db()` is safe to call repeatedly |
| `scraper/models.py` | `CommunityRecord` pydantic model with auto-cleanup validator |
| `scraper/web/app.py` | All HTTP routes (~3700 lines) |
| `scraper/duplicates.py` | Duplicate detection; admin UI at `/admin/duplicates` |
| `scraper/wrong_city.py` | Wrong-city detection (text mentions another known city); admin UI at `/admin/wrong-city`; both under the "Data quality" nav group |
| `scraper/playwright_fetch.py` | Playwright-based page fetcher; `playwright_domains` in `settings.yaml` is currently empty (social domains are blocked, not Playwright-fetched) |
| `scraper/false_positives.py` | CRUD + prompt injection for false positive rules |
| `scraper/providers.py` | Free-tier LLM provider catalogue loader + generic OpenAI-compatible extractor |
| `scraper/router.py` | Quota-aware model router: `QuotaLedger` (persisted per-day budget) + `ModelRouter` (pre-generation selection) |
| `scraper/web/api.py` | Public OpenAI-compatible gateway at `/v1/*` (Bearer auth via `ROUTER_API_KEY`) |
| `scraper/web/static/js/listing.js` | Shared public-site widgets: accent-insensitive autocomplete + A-Z/free-text list filter |
| `scraper/web/schema.py` | JSON-LD schema generation for public pages |
| `scraper/web/i18n.py` | Translations; `lang_context(request)` injects `t`, `lang`, `topic_labels` etc. into every public template |
| `config/cities.yaml` | City list: `name`, `country`, `locale`, `search_variants` |
| `config/topics.yaml` | Topic list: `name`, per-locale `search_terms` |
| `config/settings.yaml` | Model/API/cache/schedule config; `pipeline.country_priority` orders the saver windows |
| `config/providers.yaml` | Free-tier provider catalogue: models, quality scores, rate limits, router policy |
| `scraper/web/templates/_listing_filter.html` | `filter_bar()` macro — import it and call it above any listing; listing.js auto-wires it |
| `scraper/web/templates/coverage.html` | City×topic matrix; JS class toggle (`.active-row`, `.active-topic`) drives live cell states — use CSS `<style>` block, not Tailwind, for JS-dynamic styles |
| `docs/wiki/` | LLM wiki (Karpathy + OKF + llm-wiki-seed): hacks, post-mortems, decisions, integrations, runbooks |

## LLM Wiki

`docs/wiki/` is the persistent knowledge base. Before non-trivial work, skim
`docs/wiki/index.md` for relevant pages (plus `glossary.md`/`faq.md`). The maintenance
rules — when to capture what, page format, same-commit discipline — live in
`docs/wiki/CLAUDE.md` and `docs/wiki/SCHEMA.md`. Wiki updates land in the **same
commit** as the code change that triggered them; validate with
`PYTHONPATH=. .venv/bin/python scripts/lint_wiki.py` before committing.

## Important Patterns

**i18n**: all public templates receive `t('key')` via `lang_context(request)`. Translation keys live in `i18n.py` in two dicts (English base, then per-language overrides merged on top). New keys need both English (required) and Hungarian (primary market). Missing keys fall back to English silently.

**Database init**: `db.py:init_db()` uses `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE ... ADD COLUMN` guards everywhere. It is safe to call on every request. Call it at the start of any route that touches a table that might not exist on older production DBs.

**Jinja2 macros**: macros must be defined **before** they are called in templates. Jinja2 does not hoist macro definitions. Defining a macro after its call site causes `UndefinedError` at render time — silently if the calling branch is never reached (e.g., inside `{% if records %}`).

**Topic labels in templates**: `topic_labels` dict comes from `lang_context` (i18n-aware). `TOPIC_LABELS` in `app.py` is the English fallback. Both are compatible; `lang_context` overrides the explicit kwarg if passed last via `**lang_context(request)`. **That override only protects the key named `topic_labels`.** Any other context key built from `TOPIC_LABELS` — `all_topic_names` on the community page, `topic_label` on the person page — ships English to every language, and the two fallback dictionaries are not even the same: `app.py`'s leaves `hagyomanyorzes`, `baby` and `kisallat` in Hungarian, so the bug showed as a *mixed* list rather than an English one (reported 2026-09-20). Resolve `lang_context(request)` into a local once and build every label from `_lang["topic_labels"]`.

**Extraction prompt overrides**: `extract.py:get_prompt(key)` checks `_PROMPT_OVERRIDES` first. Admins can edit prompts live from `/admin/prompts`. The fingerprint system means any prompt change triggers re-extraction on next run.

**False positives**: stored in `false_positives` table. `build_prompt_section(all_fps, city, topic)` appends them to the extraction system prompt. Call `get_false_positives(_db())` to load them.

**Community identity**: `community_id` = SHA-256[:12] of `name.lower()|city.lower()`. Stable across re-runs. `record_key` = `norm(name)|norm(city)|norm(topic)` (unique DB key).

**Never server-render a list proportional to the database — public templates included.** The Tailwind CDN JIT scans the full initial DOM before the page becomes visible, which is why this was first written for admin pages; the `logs.html` → `/admin/api/logs/history` pattern is the reference. Scoping it to admin cost more than the JIT ever did: a hidden city dropdown was 76% of every one of 42,091 community pages and `/helyszinek` shipped 15.5 MB in 34 s on the event loop (2026-08-21, see `docs/wiki/pages/post-mortems/`). On public pages the cost is crawl budget and availability, not one operator's patience. Load lists from a JSON endpoint (`/api/cities.json`) or link into a per-city page; `tests/test_community_page_weight.py` and `tests/test_public_page_weight.py` hold the line.

**Public listing UX**: every listing page (cities, venues, people, explore) sorts alphabetically server-side and imports `_listing_filter.html` for the shared A-Z bar + free-text filter. `/static/js/listing.js` auto-attaches to each `[data-mp-filter]` block, so listing pages carry no JS of their own; items opt in with `data-name="Real Name"` (the raw name — `MpText.norm()` folds accents and the A-Z bar needs the original first letter). Two traps: `hidden` alone loses to any Tailwind `display` utility, so `public_base.html` declares `[hidden] { display: none !important; }`; and `listing.js` is `defer`red, so page scripts using `MpAutocomplete` must run inside `DOMContentLoaded`. Never use `<datalist>` for suggestions — iOS Safari ignores it entirely.

**Outclick tracking never redirects.** Community-detail external CTAs retain their literal `http(s)` `href` and carry `data-outclick`; `/static/js/outclick.js` sends a best-effort `POST /api/outclick` beacon without preventing navigation. The endpoint records only URLs already stored on that visible community and stores no visitor identifier. Never restore the old `/out?url=…` redirect: Google crawled thousands of internal redirect URLs and stopped seeing the direct destination links.

**Stop/cancel pattern**: long-running routes (pipeline runs) must use `asyncio.create_task()` and store the task in `app_state._run_task`. `BackgroundTasks` (FastAPI) cannot be cancelled. `asyncio.CancelledError` is a `BaseException` in Python 3.8+, so `except Exception` will NOT catch it — always use `finally` for cleanup.

**CSS build**: `scraper/web/static/css/app.css` is gitignored. Docker builds it from `input.css` via `pytailwindcss` at image build time. For local dev, maintain `app.css` manually. Committing `input.css` changes is sufficient for production.

**Playwright vs. blocked ordering**: `fetch_and_clean()` checks `playwright_fetcher.matches(url)` *before* `_is_blocked()`. A domain in both lists gets fetched by Playwright, not blocked. Keep social-media domains out of `playwright_domains` entirely.

**Person + venue extraction skip**: in `_run_full` and `_run_ai_only`, both the person AND venue cache lookups and AI calls are skipped entirely when `community_names` is empty for a URL. No communities → no persons/venues to extract (the majority of URLs yield 0 communities). Venue/person cache read/write uses `canonical_venue_fingerprint` / `canonical_person_fingerprint` (always primaries[0]) so fallback-provider extractions don't re-run when DeepSeek recovers.

**settings.yaml schedule flags**: worker, guide quality/limit, enrichment, and report settings remain. `report_cron` (the daily email) is the only cron in the system. Startup no longer runs a pipeline directly: the worker starts on boot, publishes any remaining daily guide quota first, then picks the right pipeline work. Its finished pairs and guide rows are already persisted, so interrupted work is simply pending again.

**Two-domain nav active-state**: nav links in `public_base.html` use `or` prefix checks for both HU and EN paths (e.g. `_p.startswith('/terkep') or _p.startswith('/map')`). Add both prefixes when introducing a new route that exists on both domains.

## Adding Things

**New city**: add to `config/cities.yaml` (name, country, locale, search_variants). Also add coordinates to `CITY_COORDS` dict in `app.py` for the map page.

**New topic**: add to `config/topics.yaml` (name, per-locale search_terms). Add to `TOPIC_ICONS` and `TOPIC_LABELS` dicts in `app.py`. Add label to `get_topic_labels()` in `i18n.py` for each supported language.

**New i18n key**: add to the English dict first, then add Hungarian translation. Other languages fall back to English automatically.

**New DB column**: add `ALTER TABLE ... ADD COLUMN IF NOT EXISTS ...` guard inside `init_db()` in `db.py`. The guard makes it safe to deploy without manual migration.

## Deployment

Runs on Coolify (Hetzner) via Docker. Persist only `/app/data` (SQLite). **`/app/config` is not persisted** — it ships in the image, so `settings.yaml`, `providers.yaml`, `cities.yaml` and `topics.yaml` come from git and an edit made on the server is lost at the next deploy (established 2026-08-18: a repository change reached production, and the admin UI's settings edit did not survive). The settings editor at `/admin/config` is read-only for that reason. Do not mount a volume over the entire `/app/` tree. Required env vars: `ADMIN_PASSWORD`. Optional API keys: `DEEPSEEK_API_KEY`, `DATAFORSEO_LOGIN`, `DATAFORSEO_PASSWORD`. Email notifications (`/subscribe`, `/report-not-community`, `/suggest-edit`, `/claim-community`): `RESEND_API_KEY`, `FEEDBACK_EMAIL` (recipient), `RESEND_FROM` (sender, e.g. `noreply@kozossegek.com`). All optional — missing = silent no-op.
