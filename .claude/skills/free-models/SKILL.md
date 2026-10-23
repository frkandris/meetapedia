---
name: free-models
description: Check which free LLM models each configured provider actually serves today against config/providers.yaml, and search for free-tier providers we are not using yet. Use when free-tier models 404, when adding or enabling a provider, when the fleet is short of capacity, or as a routine daily check that the catalogue has not gone stale.
---

# Free model availability check

Free-tier line-ups move weekly. On 2026-08-16 the router went live with a
catalogue written from vendor docs, and within hours **every** configured model
name was wrong — Groq had deprecated two, Gemini had closed `2.5-flash` to new
projects, all three OpenRouter `:free` slugs had left the free tier, and GitHub
Models answered `410` because the service is being retired.

So do not read docs. Ask the APIs.

## Run it

```bash
# Against the deployed app — the only place the provider keys live
ROUTER_API_KEY=<key> PYTHONPATH=. .venv/bin/python scripts/check_free_models.py --remote

# With provider keys in the local env (fuller: also surfaces NEW models)
PYTHONPATH=. .venv/bin/python scripts/check_free_models.py

# Only the ":free" tier, machine-readable
PYTHONPATH=. .venv/bin/python scripts/check_free_models.py --free-only --json
```

Exit code 1 means something configured is not listed upstream — worth checking,
not necessarily broken.

## Reading the output

- **UNLISTED** — configured in `providers.yaml`, absent from the provider's own
  `/models` list. **A hint, not a verdict.** On 2026-08-16 `open-mistral-nemo`
  was unlisted and answered requests perfectly, scoring 76 in our own
  measurement — providers keep serving aliases and legacy names they no longer
  advertise. Confirm with a real call before editing anything; the command is
  printed in the output.

  **Run the check just after 00:00 UTC.** That confirming call needs budget,
  and by mid-morning the crawler has spent it: three attempts on three separate
  days to verify `open-mistral-nemo` all came back `quota_exhausted` rather
  than an answer. That says nothing about the model — only that the check was
  run too late in the day.
- **NEW** — served upstream, not in our config. A candidate to add, *not* an
  instruction: a new model needs a `quality:` score, and the honest way to get
  one is `POST /v1/score`, not a guess.
- **OK** — present both places.

`--remote` asks `/v1/models/upstream`, which queries the providers themselves
from the server — so it sees NEW models too. It deliberately does **not** read
`/v1/models`: that lists what the router can route to *today*, which omits a
provider whose daily budget is spent or one parked behind `allow_paid`. Reading
that as "the models are gone" produced a false alarm on 2026-08-16 for exactly
those two cases.

A provider the server could not reach (bad key, network) reports an `error` and
has **no** GONE entries — absence of an answer is not evidence of removal.

## Then look outside the fleet

`check_free_models.py` can only ask providers we already hold a key for. It
cannot tell you that someone launched a free tier last month, and the fleet's
capacity is the sum of its providers — so run a search as part of the check:

```
WebSearch: "free tier LLM API <current year> no credit card OpenAI-compatible daily quota"
```

A candidate is worth the work only if all of these hold. Check them in this
order, because the first two disqualify most of what a search returns:

1. **OpenAI-compatible `/chat/completions`.** `OpenAICompatExtractor` then
   needs one catalogue entry and no code.
2. **A standing free tier, not a trial.** Expiring credits make the router's
   daily ledger meaningless and cost a debugging session when they run out.
3. **A published daily limit.** `rpd` is what the ledger plans against; without
   a number, the router cannot budget and will discover the ceiling by being
   refused.
4. **No card required.** Not a technical requirement — an operational one, and
   a good proxy for "this is a real free tier".

Known-good shape to compare against: Groq, Cerebras, Gemini, Mistral,
OpenRouter — all already in the catalogue. Names that recur in these lists and
are **not** in the fleet, as of 2026-08-18: **SambaNova**, **NVIDIA NIM**,
**SiliconFlow**. Each is a candidate, none is a decision: a new provider needs
a key, a catalogue entry, and a `quality:` score from `POST /v1/score` before
it is trusted in the routing order.

Aggregator sites are a starting point, not a source — they are often stale or
affiliate-driven. Confirm limits against the provider's own documentation, and
then against a real call.

### While you are there, check our numbers against theirs

`rpd` in `providers.yaml` was written from vendor docs and is not
self-correcting. On 2026-08-18 a search reported Groq's free tier at **1,000
requests/day** while we had `rpd: 14400` configured — if theirs is right, the
router plans for fourteen times the real allowance and spends the day being
refused. A wrong `rpd` is worse than a conservative one: the ledger budgets
against it.

## Measure a candidate before it enters the catalogue

`POST /v1/score` scores configured models only. `scripts/score_candidates.py`
scores NEW ones with a provider's existing key, on the same golden set, with
the configured models as the baseline — run it in the container over stdin
(usage in its docstring). One call per model per page, so keep `--pages`
small, and run candidates **one at a time**: OpenRouter's `:free` tier is
20/min across all models, and a batch makes every model after the first look
broken.

It also catches the opposite of UNLISTED: **listed but not served.** On
2026-10-23 the configured `nvidia/nemotron-3-super-120b-a12b:free` was still in
OpenRouter's list while every call returned 404 "Function … Not Found" from
NVIDIA behind it; `check_free_models.py` reported it OK. Only a call says a
model works.

## Acting on it

Edit `config/providers.yaml` **in the repository** and deploy. It is *not* a
persisted volume — that claim lived here until 2026-08-18, when a settings
change made in git reached production and one made through `/admin/config`
vanished at the next deploy. The catalogue is still config rather than code
because it is data, not because it can be hot-edited.

After changing anything:

1. Deploy (which restarts the app), so a fresh preflight probes the new names.
2. Read the result: `curl -H "Authorization: Bearer $ROUTER_API_KEY" \
   "https://kozossegek.com/v1/logs?grep=preflight"`
3. Score any newly added model before trusting its position in the routing
   order: `POST /v1/score?provider=<name>&pages=3`.

## The recurring trap

Three times on 2026-08-16, an indirect signal was read as fact and was wrong:

- Coolify's `running:healthy` did **not** mean the site was reachable;
- absence from `/v1/models` did **not** mean a model was retired (its daily
  quota was spent);
- absence from a provider's `/models` list did **not** mean a model was gone.

Each cost real time. When a check says something is missing, make the actual
call before acting on it.

## What this does not tell you

Availability is not quality, and it is not quota. A model can be present,
serve requests, and still be a poor extractor — or be present and out of daily
budget (`GET /v1/quota`). Three separate questions, three separate checks.

Background: `docs/wiki/pages/decisions/free-tier-model-router.md` and
`docs/wiki/pages/operations/ai-provider-quota-runbook.md`.
