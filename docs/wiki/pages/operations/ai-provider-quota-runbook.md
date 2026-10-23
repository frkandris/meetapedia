---
type: Runbook
title: Operating the AI Provider Fleet
description: How to bring a free LLM provider online, read the quota page, and react when one dies or changes its model names.
tags: [operations, llm, providers, quota, runbook]
timestamp: 2026-08-16
resource: config/providers.yaml
---

# Operating the AI Provider Fleet

*Adding capacity is one env var; every failure mode shows up on one admin page.*

Design rationale lives in [[free-tier-model-router]]. This page is the operator's view.

## Bringing a provider online

1. Sign up and create an API key.
2. Set the env var named in the **Key** column of `/admin/providers` (Coolify →
   Environment Variables, **runtime only** — a build-time secret broke the image
   build once, [[2026-07-ga4-env-buildtime-failure]]).
3. Redeploy. The provider appears as `ready` on the next page load.

Env vars per provider: `GROQ_API_KEY`, `CEREBRAS_API_KEY`, `GEMINI_API_KEY`,
`MISTRAL_API_KEY`, `OPENROUTER_API_KEY`, `GITHUB_MODELS_TOKEN`,
`DEEPSEEK_API_KEY`. Every one is optional and independent — a provider with no
key is skipped silently, which is the normal state, not an error.

## Reading /admin/providers

| Column | Means |
|---|---|
| Quality | 0-100 score on our extraction task. Seeded from public benchmarks until `scripts/score_providers.py` measures it. |
| Used today | Requests spent this UTC day, **including failed ones** — a rejected request still consumed a slot. |
| Budget | 95% of the effective daily limit, leaving headroom for enrichment and admin work. |
| `observed cap` | A 429 taught us the real limit. This number only ever goes down. |
| Status | `ready` / `rate limited` / `budget spent` / `no API key` / `disabled in config`. |

The **quality mix** panel shows cached pages grouped by the model that extracted
them. Rows below the current best score are what an upgrade sweep would revisit.

## Reading the logs

`GET /v1/logs?grep=model_retired` (Bearer `ROUTER_API_KEY`) returns the recent
in-memory log lines without a platform login — see [[router-gateway-api]]. It
needs a running app; a container that will not start is a Coolify deployment-log
job.

## When a provider starts failing

The run log names it (`extractor_model_retired`, `extractor_preflight_retired`,
`extractor_rate_limited`) and the row's `last_error` shows the message.

**Model names go stale fast — this is the normal failure, not an exception.** On
2026-08-16, within an hour of enabling six providers: Groq had deprecated
`qwen3-32b` and `llama-4-scout`, Gemini closed `gemini-2.5-flash` to new
projects, all three OpenRouter `:free` slugs had left the free tier, and GitHub
Models answered `410 github_models_retirement_brownout` — the service itself is
being retired. A 404/410 now retires that model for the run
(`ExtractorModelError`) instead of costing one wasted request per page.

- **Wrong model name** (`HTTP 400`) — the usual cause. Fix the `model:` line in
  `config/providers.yaml` **in git**, then deploy — config/ is not a persisted
  volume, so an edit made on the server is lost at the next deploy. The
  fleet preflight retires such a model for the run rather than burning a wasted
  request on every page.
- **Revoked key** (`HTTP 401`) — rotate it in Coolify.
- **Persistent 429s** — the ledger has already lowered `observed_limit`; nothing
  to do. To force a re-probe, delete that day's row:
  `DELETE FROM provider_usage WHERE day='YYYY-MM-DD' AND provider='groq';`
- **Every model dead** — the run aborts with the reason attached, exactly as the
  single-provider path did ([[extractor-circuit-breaker]]).

## First live rollout — 2026-08-16

Six providers enabled at once. What the first preflight actually returned, as a
calibration for what to expect:

| Provider | Outcome |
|---|---|
| Groq | 3/3 models live |
| Gemini | 2/2 live — but only after the shipped 2.5-flash names were replaced |
| Mistral | 2/2 live |
| Cerebras | `HTTP 402 billing limit` until the account was activated; then 2/3 (one model name did not exist) |
| OpenRouter | live but `no budget` — an earlier faulty run had spent the day's 50 requests |
| GitHub Models | `410 github_models_retirement_brownout` — service retiring, disabled in config |

**Every shipped model name was wrong.** The catalogue was seeded from vendor
docs and round-up articles; on the day it went live, Groq had deprecated two of
them, Gemini had closed 2.5-flash to new projects, and all three OpenRouter
`:free` slugs had left the free tier. Treat a fresh catalogue entry as a guess
until a preflight confirms it, and read `?grep=preflight_ok` after enabling
anything.

The same preflight showed both recovery paths working: one Gemini model came
back `(recovered)` after a timeout retry, another was held as `(rate limited)`
rather than retired. Without those, a network blip would have dropped the
highest-quality model for the whole window.

## Changing priority or scoring

- Reorder by editing `quality:` values, or better: run
  `PYTHONPATH=. .venv/bin/python scripts/score_providers.py --apply` on the
  server, where the database with the golden set lives. Dry run first.
- Paid capacity needs **three** switches since 2026-09-05, not one, and setting
  only the first is a silent no-op — `configured` fails quietly, so you get no
  paid capacity *and* no error:
    1. `router.allow_paid: true` (the permission),
    2. `router.daily_budget_usd: <amount>` (the ceiling; 0 blocks everything),
    3. `enabled: true` on `deepseek` / `openrouter_paid` in the same file.
  All three are deliberately off. Note that `openrouter_paid` shares
  `OPENROUTER_API_KEY` with the *free* OpenRouter entry, and that key's account
  holds ~$10.80 bought to unlock the free tier's 1000/day — spending it down
  loses that capacity, and going negative makes OpenRouter 402 the free models
  too. See [[paid-spend-guard]].
- `router.enabled: false` disables routing entirely; extraction reverts to the
  single configured provider with no other behaviour change.
- `upgrade_min_gain` / `upgrade_max_per_run` bound the re-extraction sweep. Raise
  the gain to make upgrades rarer; set the max to 0 to stop them.

## What NOT to do

- **Do not give a provider its own `fingerprint_model`.** Every model in the
  fleet must share one, or the first routed run invalidates ~74K cached
  extractions and re-pays for them. See [[extraction-fingerprint-cache]].
- **Do not raise an `observed_limit` by hand** to "unlock" a provider. It was
  recorded because the provider refused at that count.
- **Do not enable a paid provider to clear a backlog.** The backlog is bounded
  by the run window, not by capacity; paying for it changes the cost model
  ([[cost-optimization-2026-07]]).

## A provider that fails every call

A 100% failure rate is a catalogue question, not a bug in the router. Make the real call and read the error: `billing limit (HTTP 402)` means the free tier is gone, not that the model is. Since 2026-08-23 the ledger blocks a 402 provider until the next UTC midnight and persists it, so the worker stops rebuilding an extractor around a dead provider every few minutes — see [[2026-08-cerebras-free-tier-ended]].

## Free-tier audit, 2026-10-23

`check_free_models.py` in the container reported every configured model
present; a real call disagreed. `nvidia/nemotron-3-super-120b-a12b:free` was
listed by OpenRouter and answered 404 "Function … Not Found" (NVIDIA retired
it behind the listing) — replaced by `nvidia/nemotron-3-ultra-550b-a55b:free`,
measured at **60** on the Hungarian sample `7ba469cf2df8` with
`scripts/score_candidates.py`. `thinkingmachines/inkling:free` answers 403
outside "agentic harnesses". Both Gemma `:free` models were refused upstream
(429 from Google behind OpenRouter) — re-score after 00:00 UTC, as is
`open-mistral-nemo` (still unlisted; the confirming call hit
`quota_exhausted`). Outside the fleet nothing adds real capacity: NVIDIA NIM
publishes no daily limit and counts serving a public site as production
(Enterprise licence), SambaNova's free tier is reported at ~20 requests/day per
model, and Cerebras reportedly ended its no-card tier (ours is already
`enabled: false`).
