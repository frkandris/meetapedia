---
type: Decision
title: The Local GPU Governor and Provider Absence
description: Our own GPU may be off, asleep or throttled by its owner; production treats that as absence, not failure, and a governor sizes the model to what the owner leaves free.
tags: [providers, router, local-inference, llama-cpp, launchd, macos]
timestamp: 2026-10-23
resource: tools/localgpu-governor/governor.py
---
# The Local GPU Governor and Provider Absence
*macOS cannot give a process only the spare GPU, so the governor changes the slot count instead of a priority — and production has to read "nobody home" as absence.*

Background: [[our-own-gpu-in-the-fleet]].

The dedicated machine was switched off at 09:34 UTC on 2026-10-23, and the cost
of that was not the lost capacity — it was that nothing noticed. `LOCAL_GPU_URL`
was still set, so the provider stayed *configured*; every call ended in
Cloudflare's HTTP 530, enrichment logged `enrich_call_failed` on every batch,
the daily guides deferred every fifteen minutes, and the ledger booked 6,623
"used" requests that never reached a model.

Two changes, which only work together:

- **Absence is its own outcome.** For a provider whose address comes from
  `base_url_env` (`self_hosted`), a connect error or 502/503/504/521-523/530
  raises `ExtractorAbsentError` (`scraper/extract.py`). The chain skips the
  provider for `_ABSENT_SECONDS` (180) across every extractor in the process,
  hands the ledger slot back (`QuotaLedger.unreserve_call`), and counts neither
  a failure nor a rate limit — a 429 would have been read as evidence about the
  day's ceiling. Before each call the extractor reads `/props` → `total_slots`
  at most once a minute and sizes the provider's gate to it (`_SlotGate`, never
  above `max_concurrency`), so the same request doubles as the presence check.
- **The model runs on a Mac its owner is working on**, started and stopped by
  `tools/localgpu-governor/governor.py`: `full` (2 slots) after 5 idle minutes
  on AC, `low` (1 slot, `taskpolicy -b`) while the owner works, `off` on
  battery, under 12% free memory, at thermal limits, or when paused. macOS has
  no way to give a process only the spare GPU — background QoS covers CPU and
  I/O, not Metal, and the weights stay wired — so a governor that changes the
  slot count, rather than a priority, is the mechanism. The kernel's
  memory-pressure *level* sat at "warning" with 46% free, so only "critical"
  counts. Measured the same day on the M3 / 16 GB, on battery: `full` 285 tok/s
  prefill and 19.0 tok/s generation, `low` 297 and 15.1. A stopped model behind
  the tunnel answers 502, which production now reads as absent.

Setup is in [[local-gpu-machine-setup]].

## First evening in service (2026-10-23)

- **It oscillated.** Two loaded slots took free memory from ~54% to ~24%, under
  the 25% needed for `full`, so it fell to `low`, freed the memory and climbed
  back: 36 llama-server restarts in five hours, each cancelling the calls in
  flight (the ledger's last error read `cancelled`). `desired_state` now takes
  the current state: 25% gates *entering* `full`, staying needs only
  `MIN_FREE_PCT` (12%), because the reading already includes the model's own
  cache.
- **The old machine had been failing for eleven days before it was switched
  off** (corrected 2026-10-24; this first read "two days" from 10-21/10-22
  alone). Production's ledger: 7,402 good calls on 10-08, ~3,600–3,900 on
  10-09/10-10, then from 10-12 to 10-22 about 28,000 calls a day, every one a
  failure (HTTP 530), zero tokens. The daily report's "~28,000 enrichment calls
  a day" were those failures — calls counted, success never checked.
- **Throughput, same ledger:** the new machine's first night (2026-10-24,
  00:00–07:00 UTC, `full` throughout, no sleep in `pmset -g log`) returned
  4.15 M tokens in ~7 hours — ~590 K/hour against the old machine's best day
  (10-08) at ~540 K/hour; 123–255 pages extracted an hour. On 10-23, with absence handling
  and this machine, ~450 calls succeeded (687K tokens) and 53 pages were
  extracted by it in the first hours.

## Tuning measured 2026-10-24: the machine is near its optimum

`tools/localgpu-governor/bench.py` sends production's exact request (system
prompt, `extraction_user_prompt`, schema as grammar) over 12 cached Hungarian
golden pages ×2; `bench_matrix.sh` runs configurations on port 8090.

- **Profile of real traffic (llama.log, 3,738 requests):** generation is 83% of
  the time; llama-server already reuses the shared prompt prefix (median 701
  new prompt tokens, 3 s). Median 197 generated tokens at ~16 tok/s per slot.
- **On AC, interleaved A/B/A/B** (battery runs drifted by tens of percent and
  are not comparable): current 2 slots 252/259 pages/h; Qwen3.5-0.8B draft
  model (`-md`, n-max 8) 265/264 — **+3%**, outputs identical within the
  baseline's own run-to-run variance. Not worth 0.8 GB more.
- **Worse:** 4 slots (140 vs 181 on battery), 1 slot (99), n-gram/prompt-lookup
  speculation (123; 18% acceptance). MLX is not faster than llama.cpp for
  small dense models per published M3 comparisons.
- **Pipeline:** 71% of extracted pages yield no community (89% in the world
  wave's first day), but near-empty answers cost 3.5 s each — 8% of GPU time;
  a pre-filter would buy little. Runaway answers to the 4,000-token cap: a
  2,000 cap would save ~5% of generation time, 1,000 ~9% but truncates real
  many-group pages.
- **The Hetzner server is not a candidate:** 4 shared AMD EPYC-Rome vCPUs
  (AVX2, no AVX-512), 7.5 GB RAM with ~4 GB free, no GPU, and the same cores
  serve the site. A GPU server (GEX45) is ~€214/month + €209 setup.
- **Qwen3.5-2B is not the shortcut (2026-10-24).** Scored on this Mac with
  `tools/localgpu-governor/score_local.py` over production's 40-page Hungarian
  golden set (same pages, expected names and generic-token list as
  `/v1/score`), 2 slots, on AC: 4B Q6_K **81** at 311 pages/h; 2B Q6_K **60**
  at 753 pages/h. 2.4× the speed for 21 points — the level of the fleet's
  weakest models. The 4B stays. The same day the localgpu output cap went from
  4,000 to 2,000 tokens (`providers.yaml`).
