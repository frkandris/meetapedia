---
type: Concept
title: Free Model Catalogue Churn
description: A free model is a loan, not an asset — three have now been withdrawn with the same 404 sentence, and a retired entry keeps costing one call per run until it is deleted.
tags: [providers, router, openrouter, free-tier, measurement]
timestamp: 2026-09-21
resource: config/providers.yaml
---

# Free Model Catalogue Churn

*The fleet's best free model on Thursday was a 404 on Sunday. That is the normal case, not the incident.*

## The pattern

Three withdrawals, one sentence:

| Model | Added | Withdrawn | Lifetime | Quality at withdrawal |
|---|---|---|---|---|
| `openai/gpt-oss-20b:free` | — | 2026-09-05 | — | 45, the weakest of three |
| `deepseek/deepseek-v4-flash-0731:free` | 2026-09-18 | 2026-09-21 | **3 days** | 76, the *best* we had |
| `open-mistral-nemo` | recurring | intermittent | — | flaps rather than dies |

The first two returned the same body:

```
HTTP 404 {"error":{"message":"This model is unavailable for free.
The paid version is available now - use this slug instead: <slug>"}}
```

That is a withdrawal, not an outage: the model exists, the free tier does not.
It is distinguishable from the `open-mistral-nemo` case — which disappears from
the catalogue listing and comes back — because a real call agrees with the
listing. Check both before deleting: a model absent from the listing but
answering calls is a listing bug, and the reverse is a retirement.

## Why a retired entry is deleted, not disabled

`enabled: false` would leave the entry documented and inert. It is deleted
instead because the cost is not zero: `extractor.preflight()` probes the chain
before every run, so a dead model spends one call per run proving what the 404
already said. The tombstone comment carries the knowledge; the entry would only
carry the waste.

The paid slug the error helpfully suggests is *not* the fix. `allow_paid: false`
and `daily_budget_usd: 0.00` are both deliberate, and the OpenRouter balance is
there to buy free-tier capacity, not to be spent — see CLAUDE.md's ceiling
paragraph.

## What survives the model

The 0731 entry cost two evenings: a `max_output_tokens` of 4,000, then 8,000,
each raise justified by counting `llm_output_truncated` failures. Three days
later the model was gone — and the rule those evenings established was not:
**a reasoning model emits its thinking as billed output, so the global 1,500
cap strangles it before the JSON closes.** That rule is why the same snapshot
answered 4 of 14 at the global cap and 14 of 20 at 8,000, and it will apply to
whatever replaces it. See [[measuring-extraction-quality]].

So the loss is the capacity, not the measurement. The practical consequence for
routing is that **quality ranking must not be treated as stable inventory**: the
fleet is ordered best-first, and the head of that order is the entry most likely
to vanish, because the models good enough to be worth withdrawing are the ones a
provider notices it is giving away.

## Rule

Measure a free model before trusting it, expect to measure its replacement, and
when a real call returns the 404 sentence, delete the entry the same day and
leave a tombstone comment where it stood. Do not port the measurement's *numbers*
to the next model; port its *method*.

## See also

- [[our-own-gpu-in-the-fleet]] — the provider that cannot be withdrawn, which is its whole point
- [[measuring-extraction-quality]] — why answer rate is not the quality score
