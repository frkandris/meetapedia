---
type: Integration
title: Jev Joinability Gate
description: Jev can cheaply reject pages with no joinable community before generative extraction, but only after a recall-first shadow benchmark against a local classifier.
tags: [jev, extraction, classification, cost, benchmarking]
timestamp: 2026-09-20
resource: scripts/benchmark_joinability_gate.py
---

# Jev Joinability Gate

*Jev can cheaply reject pages with no joinable community before generative extraction, but only after a recall-first shadow benchmark against a local classifier.*

## Fit

The community extractor does two different jobs in one call: it decides whether
the page contains a joinable community, then generates names and profile fields.
Jev 1.13 returns typed probabilistic decisions but cannot generate those strings.
It can therefore be a gate, never a replacement:

    raw page -> Jev says no with high certainty -> skip extraction
             -> yes or uncertain             -> existing extractor

The gate's question spells out the same three-condition AND as
[[joinable-quality-gate]]: recurring, publicly open, and a group identity. The
three diagnostic questions travel in the same request, but production gating
must use the single complete question. Jev's own jaggedness guide says separate
questions and their negations do not obey probability identities.

At the published 2026-09-15 price of $0.042 per million input tokens, an
8,000-character page costs roughly $0.00006–0.00011 to judge. That buys paid
calls, not cash savings, while the downstream fleet is free: the benefit here
is reclaimed daily quota and latency. Positive pages pay both inputs, so the
scheme only wins when enough negative pages stop at the gate.

## Measurement before authority

`scripts/benchmark_joinability_gate.py` draws a deterministic balanced sample
from extracted `cache_pages`. `records_count > 0` is only agreement with the
incumbent extractor, not truth, so every low-scored incumbent positive needs
manual review before deployment. The report shows positive recall at a series
of rejection thresholds; overall accuracy is deliberately not the target.

The same script includes a dependency-free, local character n-gram Naive Bayes
baseline with a host-held-out split. This is the real buy-versus-build test: the
corpus already supplies tens of thousands of weak labels, and a sufficiently
safe local gate has no API cost, vendor dependency, or data transfer.

Run either side on the identical sample:

```bash
PYTHONPATH=. .venv/bin/python scripts/benchmark_joinability_gate.py local

TYPESAFE_API_KEY=... PYTHONPATH=. .venv/bin/python \
  scripts/benchmark_joinability_gate.py jev --cache data/jev-gate-cache.json
```

The Jev cache prevents a repeated benchmark from paying for the same page.
Nothing writes to the scraper database and no production path consults the
result yet. A later production implementation needs its own model-and-question
fingerprint, an auditable reversible negative cache, and uncertain cases must
always fall through to extraction. A target such as 99–99.5% positive recall is
only a starting criterion; manually confirmed false negatives decide the real
threshold.

## Known risks

Jev is early access. Its vendor documents literal interpretation, context rot
from irrelevant long state, adversarial-content sensitivity, and weaker
performance through indirection. Those are directly relevant to scraped web
pages. `max_text_chars` should stay identical across the two benchmark runners,
and results must be stratified by market before one global threshold is trusted.

## Inline enrichment companion measure

`scripts/report_inline_enrichment.py` reads the telemetry the pipeline already
stores in `cache_pages.enrich_log` and reports searches, approximate LLM calls,
successful records, and fields added. It turns disabling
`pipeline.enrich_communities` into a production-data decision rather than an
intuition. Description enrichment is a separate subsystem and is not evidence
that contact enrichment has no value.

# Citations

- TypeSafe, “Introducing System One Models & Jev”, 2026-09-15:
  <https://typesafe.ai/blog/introducing-system-one-models-and-jev>
- TypeSafe API quick start: <https://docs.typesafe.ai/introduction/quickstart>
- Jev 1.13 documented jaggedness:
  <https://docs.typesafe.ai/model-jaggedness/jev-1.13>

## Running the benchmark against production

The sampler reads keys from the `idx_cache_pages_done` partial index and
resolves text only for the pages it picked. That is not a detail: the first
version loaded every extracted page's text to keep 2,000 of them and had to be
killed on the production host with 237 MB of RAM free — see
[[2026-09-benchmark-materialized-the-corpus]]. Measured on that corpus,
**81.8% of extracted pages yield zero communities** (104,795 of 128,072), which
is the number the whole gate question turns on.

## First measurement, 2026-09-20: the local baseline

Run on production, 1,000 pages per class, held out by hostname:

```
threshold  skipped  neg_skipped  false_neg  positive_recall
0.01       189      141          48         74.603%
0.50       202      153          49         74.074%
```

Two things to read here, and the second matters more.

**Recall is 74.6% at its best**, against a target of 99%. A gate at this
quality would discard a quarter of the communities the extractor would have
found — not a trade, a loss.

**The threshold column is inert.** Moving it from 0.01 to 0.50 changes recall
by half a point. That is not a property of the corpus; it is a property of the
score. Naive Bayes adds one log-probability per n-gram and a page carries
thousands, so the raw sum scales with page length and every page lands on the
±50 clamp the scorer applied. Every page in the "worst positives" list scored
exactly `0.0000`, which is the bottom of the clamp, not a probability.

So the first number does not measure whether the corpus is separable. It
measures an uncalibrated score. The scorer now divides the log-odds by the
n-gram count — a rate rather than a total, comparable between a 400-character
page and an 8,000-character one — and fits Platt scaling on a calibration
split held back from the training data, disjoint by hostname from both the fit
and the held-out sets.

On a synthetic corpus of mixed-difficulty pages the difference is the whole
question: the old scorer put **0%** of pages between 0.02 and 0.98 and the
threshold moved nothing; the new one puts 27% there and the threshold moves
the decision. `tests/test_cost_audit_scripts.py` holds that property.

This also makes the local baseline comparable to Jev on the axis Jev is sold
on. A calibrated probability is what a recall target is expressed in, and
until this change the local runner did not produce one.

## Second measurement, 2026-09-20: the calibrated local baseline

Same sample, same seed, same model — only the score changed. Held out by
hostname: **406 pages, 189 of them positive.**

```
threshold  skipped  neg_skipped  false_neg  positive_recall
0.01        39       37           2          98.942%
0.05        73       71           2          98.942%
0.10        79       76           3          98.413%
0.20        95       91           4          97.884%
0.30       115      106           9          95.238%
0.40       166      139          27          85.714%
0.50       204      155          49          74.074%
```

**74.6% became 98.9% on the same data with the same model.** That settles what
the first run actually measured: the score, not the corpus. It also means the
threshold is now a control — the column spans 99% to 74% instead of moving half
a point end to end.

Translated onto the real corpus, where 81.8% of extracted pages are negative:

| threshold | recall | negatives rejected | share of all pages skipped |
|---|---|---|---|
| 0.05 | 98.94% | 32.7% | **26.8%** |
| 0.10 | 98.41% | 35.0% | 28.7% |
| 0.20 | 97.88% | 41.9% | 34.3% |
| 0.30 | 95.24% | 48.8% | 40.0% |

So a free, dependency-free local model at threshold 0.05 removes about **a
quarter of all extraction work** for roughly **1% of communities lost**. That is
the bar Jev now has to beat — not "is a gate viable", which is answered, but
"is a paid gate enough better than a free one to be worth the money and the
vendor".

Two cautions before anyone ships this:

- **The sample is small where it matters.** 98.94% rests on **two** false
  negatives out of 189 positives. The confidence interval on that is wide; a
  `--per-class 3000` run costs nothing but time and should come first.
- **The label is weak, and visibly so.** The lowest-scored positives include a
  `theguardian.com` article, a `szallas.hu` booking page and an Instagram post.
  Those are pages where the incumbent extractor claims a community and may
  itself be wrong — so some of the "false negatives" are the gate being right.
  Manual review of that list is not optional.

## Third measurement: the larger sample, and why it matters

`--per-class 3000`. Held out by hostname: **1,200 pages, 560 positive, 640
negative** — three times the evidence of the run above.

```
threshold  skipped  neg_skipped  false_neg  positive_recall
0.01        16       15           1          99.821%
0.02        26       24           2          99.643%
0.05        90       87           3          99.464%
0.10       162      155           7          98.750%
0.20       216      202          14          97.500%
0.30       299      277          22          96.071%
0.40       465      389          76          86.429%
```

| threshold | recall | negatives rejected | share of all pages skipped |
|---|---|---|---|
| 0.05 | 99.46% | 13.6% | **11.1%** |
| 0.10 | 98.75% | 24.2% | 19.8% |
| 0.20 | 97.50% | 31.6% | 25.8% |
| 0.30 | 96.07% | 43.3% | 35.4% |

**The larger sample halved the answer, and then halved it again.** At the
recall a gate actually needs, the 406-page run put the saving at 26.8%; on
1,200 pages it is **11.1%**. Both numbers are from the same model on the same
corpus. The first was two false negatives away from a different conclusion,
which is what a sample that small buys.

This is the run to quote. It is also the one that makes the Jev question
sharp rather than academic:

- The free local gate buys **~11% of extraction work at 99.5% recall**, or
  ~20% if 98.75% is acceptable.
- For Jev to be worth money and a vendor dependency it has to clear that by a
  wide margin — 40-50% of pages at the same recall would be three to four
  times the free option and obviously worth a few dollars. Matching 11% would
  not be.

The measurement to run next is therefore Jev on this exact sample: same seed,
same `--per-class 3000`, so the two tables are read side by side.

## Reaching Jev without the waitlist

TypeSafe's own console put us on a waitlist on 2026-09-20. Two routes exist
that use credentials this project already holds, and the benchmark takes
`--provider` to choose:

- **Cloudflare Workers AI** (`--provider cloudflare`) serves the same model as
  `typesafe/jev` through `POST /accounts/{id}/ai/run`, authenticated with the
  `CLOUDFLARE_API_TOKEN` the extraction fleet already uses. Same questions,
  same answer fields; the envelope differs — the payload nests under `input`
  and the answer may nest under `result`, and both shapes are handled.
- **OpenRouter** also lists Jev, but through a chat-completions surface that
  does not express typed questions. Not worth adapting while Cloudflare's
  native route exists.

**Measured 2026-09-20: the Cloudflare route answers 402 Payment Required.**
Jev is a third-party partner model on Workers AI and is not covered by the
free neuron allowance. The smoke test is what established this, and it also
ruled out the obvious alternative explanation: the same token, in the same
container, at the same minute, got **200** from `@cf/openai/gpt-oss-20b`. So
it is the model that costs money, not the day's quota that ran out — and the
extraction fleet's allowance was never touched.

**Resolved the same day: it is the AI Gateway's own prepaid balance.** The
second 402 said so exactly — *"Insufficient balance; add money to your gateway
or use BYOK"* (code 2021). Not the Workers Paid plan, not the neuron
allowance: partner models bill through **AI Gateway Unified Billing**, topped
up at AI → AI Gateway → Credits Available → Manage. With credit on the
account the same call answers 200, and `gatewayMetadata.keySource: "Unified"`
confirms which purse it came from. The BYOK alternative the error offers is
closed to us — it wants the TypeSafe key the waitlist is withholding.

Two things the live service taught that no amount of reading would have:

- **Workers AI nests the answer twice**, not once:
  `{result: {state, result: {answers}, gatewayMetadata}}`. A single unwrap
  leaves `answers` missing, which is why the runner refuses an answerless
  response loudly instead of scoring it as a zero.
- **Concurrency 6 hits 429** on this gateway — it ended a 1,200-page run after
  422 pages, because `asyncio.gather` takes every in-flight page down with the
  first exception. The runner now retries 429 and 5xx with doubling backoff,
  and 3 is the concurrency that has held.

The measured cost, from real `usage` numbers (2,136 input tokens per page at
`max_text_chars: 8000`): **$0.11** for the 1,200-page comparison, **$0.09** a
day for ~1,000 new pages, **$11.49** to gate the whole 128,072-page corpus
once. If the gate works, its price is not the question.

Were credit not an option, the remaining routes would be:

1. **Wait for the TypeSafe waitlist.** Free, unknown latency; reports suggest
   hours rather than weeks.
2. **Put a payment method on Cloudflare** (Workers Paid, $5/month plus neuron
   usage). The benchmark itself is a couple of dollars at most.
3. **A gateway account** — Vercel AI Gateway, Netlify, AIMLAPI — each needs its
   own signup and its own wire format.

None is urgent. The free local gate already buys ~11% of extraction work at
99.5% recall, and Jev only becomes interesting if it clears that by a wide
margin. The measurement is worth a few dollars; it is not worth a rushed
decision.

## The comparison, 2026-09-20: Jev wins, by 2-5x

Both runners on the **same 1,200 held-out pages** (560 positive, 640 negative),
same seed, same hostname split — that is what `--held-out-only` is for. Jev's
thresholds below are recomputed from the cached responses, so the finer grid
cost nothing extra.

| threshold | recall | negatives rejected | share of all pages skipped |
|---|---|---|---|
| 0.030 | 100.00% | 5.5% | 4.5% |
| 0.035 | 99.64% | 19.1% | 15.6% |
| 0.045 | 98.93% | 31.1% | 25.4% |
| 0.060 | 98.93% | 40.8% | **33.4%** |
| 0.100 | 96.43% | 65.5% | 53.6% |
| 0.200 | 92.50% | 80.8% | 66.1% |

Against the local model at the same recall:

| recall | local | Jev | |
|---|---|---|---|
| 99.64% | 3.1% | 15.6% | **5.0x** |
| 98.93% | 11.1% | 33.4% | **3.0x** |
| 96.43% | 25.8% | 53.6% | **2.1x** |

At **0.06 — 98.9% recall and a third of all extraction work removed** — the
free model buys 11%. That is the margin the question was asked about, and it
is wide enough to settle it: a paid gate is worth it here.

What it means for throughput, at ~2,100 free fleet calls a day:

```
no gate      2,100 pages/day   backlog 61.0 days
local gate   2,362 pages/day   backlog 54.2 days   (1.12x)
Jev @ 0.06   3,153 pages/day   backlog 40.6 days   (1.50x)
```

## Two things the numbers understate

**The label is wrong more often than Jev is.** The lowest-scored "false
negatives" are `szallas.hu/tornyospalca/wellness`, `utazzitthon.hu/latnivalo/…`
(a sightseeing page), `filharmonia.hu/nyari-programok/…` (a concert series).
These are pages where the incumbent extractor claimed a community and Jev says
there is none — and on inspection Jev is right. So the measured recall is a
floor, and the gate doubles as a quality signal: the same call that saves an
extraction also flags a probable false positive already in the corpus.

**Non-English held up.** TypeSafe's own documentation warns that accuracy is
best in English and to test before relying on it elsewhere. This corpus is
Hungarian, German and Swedish, and the results above are from it. The warning
was worth heeding; it did not bite.

## What a production gate still needs

The preconditions listed earlier have not moved — a dedicated fingerprint, an
auditable and reversible negative cache separate from the extraction cache,
uncertain/error/429 always falling through to extraction, never writing a
permanent empty extraction from a classifier result, per-market thresholds if
calibration drifts, daily cost accounting against an explicit budget, an admin
release path, and a regression test proving an outage cannot classify a page
as empty.

One more, learned here: **the threshold belongs in config, not in code**, and
0.06 is a starting point rather than a settled value. The band between 0.03
and 0.06 is where recall trades hardest against saving, and it should be
re-measured whenever the extraction prompt changes — the labels move with it.
