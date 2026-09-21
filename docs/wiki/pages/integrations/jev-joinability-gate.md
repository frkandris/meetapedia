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

## The local baseline, and what it took to measure it

The free model is the bar Jev has to clear, and getting an honest number from
it took three runs on the same corpus:

1. **74.6% recall** — and a threshold column that moved half a point end to
   end. That second fact was the real finding: naive Bayes sums one
   log-probability per n-gram, so the score scaled with page length and every
   page sat on the ±50 clamp. It measured the scoring, not the corpus.
2. **98.9%**, after dividing the log-odds per n-gram and fitting Platt scaling
   on a calibration split held back by hostname. Same model, same data.
3. **99.46% recall while rejecting 13.6% of negatives** on a 1,200-page
   held-out set — where the 406-page run had said 26.8% of work saved. The
   smaller number rested on two false negatives. Quote the larger run:
   **11.1% of all extraction work, free.**

The lesson worth keeping: a gate score has to be calibrated or its threshold is
a decoration, and a held-out set of a few hundred pages will tell you whatever
you hope to hear.


## Reaching Jev

TypeSafe's own console was waitlisted on 2026-09-20 and cleared the same night.
Two routes exist; the benchmark takes `--provider`:

- **Cloudflare Workers AI** (`--provider cloudflare`), `typesafe/jev` through
  `POST /accounts/{id}/ai/run`. Partner models bill from the **AI Gateway's
  prepaid balance** — not the Workers Paid plan and not the neuron allowance
  (error 2021, *"add money to your gateway or use BYOK"*). Two facts only the
  live service gave: Workers AI nests the answer twice
  (`result.result.answers`), and concurrency 6 draws 429 — one rate limit ended
  a 1,200-page run at page 422, since `asyncio.gather` loses every in-flight
  page with the first exception. Concurrency 3 holds.
- **TypeSafe directly**, `TYPESAFE_API_KEY`, once off the waitlist.

Measured price, from real `usage`: 2,136 input tokens per page — $0.11 per
1,000 pages, $0.09/day for new pages, $11.49 to gate the entire corpus once.


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

What it means for throughput — and the arithmetic here is easy to get wrong,
because **a page is not a call**. Venue and person extraction are skipped when
a page yields no communities, so an empty page costs 1 call and a useful one
costs 3. The corpus average is 1.363. At ~2,100 free fleet calls a day:

```
                          calls/page   pages/day   backlog
no gate                        1.363       1,540    83 days   1.00x
local gate (13.6% rejected)    1.252       1,677    76 days   1.09x
Jev @ 0.06 (40.8% rejected)    1.030       2,040    63 days   1.32x
Jev @ 0.10 (65.5% rejected)    0.828       2,538    50 days   1.65x
```

Note the gap between "33.4% of pages skipped" and "24.5% of calls saved": the
gate discards the *cheapest* pages, and what remains is richer in the
three-call kind. Any estimate that treats a skipped page as a saved call —
including the first version of this table — overstates the gain by about half.

## Two things the numbers understate

**The label is wrong more often than Jev is — all six times, in fact.** The
false negatives at threshold 0.06 were read one by one against what the
extractor claimed to find on each page:

| Jev | page | extractor's record | verdict |
|---|---|---|---|
| 0.03 | `szallas.hu/tornyospalca/wellness` | "Wellness Center Tornyospálca" | hotel spa, not a group |
| 0.03 | `szabolcsveresmart.hu/…int_szocialis` | "Kisvárdai Család- és Gyermekjóléti Központ" | an institution, not joinable |
| 0.04 | `funiq.hu/1494-nemesnadudvar` | **"Nemesnádudvar"** | the village's own name |
| 0.04 | `utazzitthon.hu/latnivalo/nagyrecse` | "Nagyrecse Fitness Club" | sightseeing page; the club is invented |
| 0.04 | `utazzitthon.hu/latnivalo/taszar` | **"Taszár"** | the settlement's name again |
| 0.04 | `filharmonia.hu/…zenes-estek…` | "Zenés Estek a Kastélykertben" | a concert series |

Six of six are incumbent errors. **Jev's true recall on this sample at 0.06 is
100%**; the measured 98.9% is an artifact of grading it against the extractor
it corrects.

**0.06 is where the boundary actually is.** Above it the picture is mixed —
"Belvárosi Jógastúdió" (0.09) and "AquAnett Úszóiskola" (0.07) are a studio and
a paid school, which the `joinable` rule excludes anyway — but at **0.10 real
communities start falling out**: "Heves Megyei Fotóklub" and "Nagyrábé Senior
Citizens' Association" are genuine associations Jev scores too low. So the
33.4% saving is available at no real cost, and the 53.6% at 0.10 is not.

The corollary is that the gate is also a data-quality instrument: the same
$0.0001 question that skips an extraction flags a false positive among the
45,888 records already stored.

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

## The A/B run, 2026-09-21: the gate ships, the merge does not

525 pages of the production corpus, in its own proportions (80.6% with no
communities), each one compared against the extraction already cached for it.
Two changes measured together — a Jev gate at 0.06, then one combined call in
place of three.

```
gate:       84 pages rejected (16%) — one real loss
calls:      709 -> 419              = 40.9% saved
            1.360 -> 0.798 calls per page
            1,544 -> 2,632 pages/day (1.71x), backlog 83 -> 49 days
communities: 132 -> 235   (+78%)
venues:       97 -> 216   (+123%)
errors:     22 / 525 (4.2%) — rate limits and 5xx on the fleet
Jev cost:   $0.115 per 1,000 pages
```

**The gate is confirmed.** One genuine loss in 84 rejections — 98.8% — which
matches the 1,200-page measurement where every page below 0.06 turned out to be
an incumbent error. Nothing here argues against shipping it.

**The merge is refuted, and the counts are why it took 500 pages to see.** At
25 pages the extra communities looked like better granularity: eight timetable
rows of one German sports club replaced by the club itself. At 525 the pattern
is different. What the combined call adds:

    Kör · Senioren · Babykurse · Musikgarten · Könyvbemutató – Balatonfűzfő ·
    A hívek imája Máriacellben · KolorFund innovációs fórum · kita-osaka-cc

One-word fragments, a book launch, a prayer, a forum, course names — none of
them a joinable group, and every one excluded by the three-condition rule in
the standalone prompt. Meanwhile it loses real ones:

    Vilnius Chess Club · Katholische Pfarrgemeinde St. Ulrich Geislingen ·
    Seniorenclub (AWO Friedberg) · Freiwilligenagentur mit Herz & Hand ·
    北大阪サイクリングクラブ

So +78% is not sharper recognition. It is a looser filter. The standalone
prompt spends its whole attention on what counts as a joinable group; merged,
the same model divides that attention three ways, and the strict part is what
dilutes first. Add the 8,192-token ceiling that excludes `localgpu` — the one
provider whose allowance never runs out — and the merge costs more than the
two calls it saves.

**Decided 2026-09-21: implement the gate, revert the merge.** The combined
prompt, `extract_all`, and the A/B harness are removed with this commit; the
numbers above are the record of why, and re-deriving them would cost another
$0.06 and six hours.

If the merge is ever revisited, the experiment to run is not this one: split
the prompt differently — communities alone, venues and people together — so
the strict judgment keeps a call to itself.
