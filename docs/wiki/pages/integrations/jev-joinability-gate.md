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
