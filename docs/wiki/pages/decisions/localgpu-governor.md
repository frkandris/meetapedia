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
- **The old machine had been failing for two days before it was switched
  off.** Production's ledger: 14,193 localgpu calls on 2026-10-21 and 27,759 on
  10-22, every one a failure (HTTP 530). The daily report's "~28,000
  enrichment calls a day" were those failures. On 10-23, with absence handling
  and this machine, ~450 calls succeeded (687K tokens) and 53 pages were
  extracted by it in the first hours.
