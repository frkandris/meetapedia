---
type: Subsystem
title: Extraction Layer
description: DeepSeek LLM extraction of communities, venues, and persons from page text, with four prompt families, live-editable prompts, and fingerprint-keyed caching.
tags: [extraction, llm, deepseek, prompts, fingerprint]
timestamp: 2026-07-10
resource: scraper/extract.py
---

# Extraction Layer

*`FallbackExtractor` (DeepSeek only since the 2026-07 provider cleanup) turns clean page text into `CommunityRecord` / `VenueRecord` / `PersonRecord` objects via four separate prompt+schema families.*

See [[extraction-provider-fallback-chain]], [[extraction-fingerprint-cache]], [[community-record]], [[joinable-quality-gate]].

## Providers

`_ApiExtractor` is the shared OpenAI-compatible base; `DeepSeekExtractor` (`api.deepseek.com/v1`, `deepseek-chat`) is the **only** provider since the 2026-07 cleanup (GroqExtractor removed). Per-provider state on the wrapper: `_exhausted[i]` (permanent, HTTP 402) and `_blocked_until[i]` (temporary, HTTP 429 with `Retry-After`, default 60 s); a new `FallbackExtractor` is built each run.

**Errors:** HTTP 402 raises `ExtractorQuotaError`, 429 raises `ExtractorRateLimitError`, and network/other HTTP failures raise `ExtractorUnavailableError`. The wrapper can wait out a short rate limit and retries a transient failure once; the pipeline never caches a failed call as an empty result. See [[non-quota-errors-drop-page]].

## Effective config beats class defaults

Runtime config (settings.yaml) overrides class defaults — the YAML wins, a latent surprise if settings are trimmed. Community extraction uses `temperature` (0.1); venue/person/enrich hard-code `temperature 0.0` (only community extraction is stochastic).

## Four prompt+schema families

`SYSTEM_PROMPT`+`EXTRACTION_SCHEMA` (community), `VENUE_SYSTEM_PROMPT`+`VENUE_SCHEMA`, `PERSON_SYSTEM_PROMPT`+`PERSON_SCHEMA`, `ENRICH_SYSTEM_PROMPT`+`ENRICH_SCHEMA`. **The schema dicts are documentation only** — the API request uses `response_format: {"type": "json_object"}` plus a hand-written JSON-shape suffix string (`_API_EXTRACT_SUFFIX`), not JSON-schema grammar — `json_object` buys valid JSON, **not** an object, so all three
parsers route through `_json_items()`, which rejects a non-dict top level as an
unavailable extraction ([[2026-07-llm-bare-array-run-abort]]). The `SYSTEM_PROMPT` repeatedly instructs "output field values in the **original language of the page**" (see [[llm-prompt-language-bias]]).

## Live-editable prompts

`get_prompt(key)` returns `_PROMPT_OVERRIDES.get(key) or PROMPT_KEYS[key]()`. The admin `/admin/prompts` page writes overrides to the DB; every extractor method calls `get_prompt()` at call time, so edits take effect without restart. **Trap: `get_prompt` uses `or`**, so an override set to `""` silently reverts to the default — you cannot blank a prompt. `enrich_user` has no override key (hard-coded inline). See [[get-prompt-empty-override-falls-back]].

## False-positive injection

Negative examples are appended to the system message **at call time, after** `get_prompt(...)`: `get_prompt("extraction_system") + false_positive_examples + _API_EXTRACT_SUFFIX`. Both full and cache-only extraction pass the pair-scoped section. Because it is outside the global fingerprint, `false_positives.add/remove` explicitly invalidates the affected community cache rows (or all rows for a global rule). See [[false-positive-injection]].

## Person extraction skip

Persons are extracted only when `community_names` is non-empty for a page (people are meaningful only relative to a known community). Zero communities → no person LLM call, saving a request per page (most pages yield 0 communities). A second cache layer skips the call when persons were already extracted at the current fingerprint.

## Enrichment

A record with no website/social/contact and `confidence ≥ 0.7` gets a second pass: search `"name" city`, fetch top results, and `enrich()` fills **only empty fields** via `model_copy(update=…)`. Enrichment happens only in `_run_full`, not `ai_only`.

## Fingerprints

Each extractor exposes `model_fingerprint` / `venue_fingerprint` / `person_fingerprint` (SHA-256[:12] of `prompt + model`) plus `canonical_fingerprint`. The canonical one always uses `primaries[0]` so pages extracted by the *fallback* provider still store under the primary's key — see [[canonical-fingerprint-provider-shift]] and [[extraction-fingerprints]].

## Failure handling

`FallbackExtractor` turns provider trouble into typed errors and never returns an empty
result on failure ([[non-quota-errors-drop-page]]). Beyond per-call retry, two run-level
guards exist since 2026-07-25: a `preflight()` live check before any pair loop, and a
circuit breaker that marks every provider exhausted after 20 consecutive failed calls.
`providers_down` (as opposed to `exhausted`, which is also true with no API key
configured) is what aborts a run. See [[extractor-circuit-breaker]].

Topics reach the model as readable phrases with a `{topic_rule}` paragraph, never as slugs — see [[2026-10-topic-slug-in-prompt]].
