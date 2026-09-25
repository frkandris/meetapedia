---
type: Decision
title: Our Own GPU in the Fleet
description: A laptop running Qwen3-4B scores 73 on our task — above the 8B and the 20B — and its allowance never runs out, which is the hole it fills.
tags: [providers, router, local-inference, llama-cpp, quota, measurement]
timestamp: 2026-09-25
resource: config/providers.yaml
---

# Our Own GPU in the Fleet

*Measured, not assumed: an 8B model on a 16 GB MacBook agrees with the incumbent extraction as often as the free fleet's middle, and has no daily limit to spend.*

> Sharpened by [[free-model-catalogue-churn]]: a hosted free model can also be withdrawn
> outright, and on 2026-09-21 the best-scoring one was, three days after it was added. This
> provider cannot be — which is a second, quieter reason it earns its place.
>
> The `-c 8192` window has a second edge, found on 2026-09-21: llama.cpp trims an
> over-long prompt instead of refusing it, and what it trims is the tail. See
> [[2026-09-guides-named-nobody]] for what that silently removed, and what now
> goes last in a prompt as a result.

## The question, and the number that actually mattered

Not "is a local model as good as the cloud" but "is it good enough to be worth
having". Both halves were measured on 2026-09-05/06 against the Hungarian golden
set — sample `d13dfe914a92`, 16 pages, 17 expected communities, exported with
[[exporting-a-golden-set]] and verified identical to the server's by fingerprint.

| model, on this machine | score | answered | weights |
|---|---|---|---|
| **Qwen3-4B Q4_K_M** | **73** | 16/16 | 2.5 GB |
| Qwen3-8B Q4_K_M | 67 | 15/16 | 5.0 GB |
| gpt-oss-20b MXFP4 | 65 | 16/16 | 12.1 GB |

**The smallest model won**, and it is also the one that runs fastest and leaves
the machine usable. Do not read that as "4B beats 20B": read it as the thing
`providers.yaml` already warns about in its own header — LLMStructBench
(arXiv:2602.14743) found prompting strategy outweighs model size for JSON
extraction, and this is our prompt doing the work. The practical lesson is to
measure the small model first, not last.

For scale, the free fleet on its own (older, differently-sampled) numbers runs
52-80, with `mistral-small` at 80 and Groq's gpt-oss-20b at 67. So this sits
near the fleet's top rather than its middle — and the cloud comparison on *this*
sample is still outstanding, because on 2026-09-05 every free provider's daily
allowance was already spent by 22:30 UTC and the same was true again at 08:49
the next morning.

**That last sentence is the decision.** A provider scoring 67 with no allowance
is worth more than one scoring 74 that has been unavailable since lunchtime.
Routing is by quality, so the better free models still go first; this one is what
remains when they are gone.

Memory settled the rest. On a 16 GB machine that its owner is actually using —
Chrome, a chat app, Spotlight indexing — the 8B ran at **5.4 tok/s** against
15.8-16.6 on an idle one, because everything else was paging and unified memory
means that steals the bandwidth the GPU needs. At 5.4 tok/s a single extraction
takes ~109 s, past the ceiling below. The 4B leaves 2.5 GB more headroom and does
not have the problem. gpt-oss-20b's appeal was that Cloudflare and Groq serve the
*same weights*, which would have isolated "where it runs" from "which model it
is" — worth measuring, not worth running.

## The runtime was the variable, not the hardware

The first attempt measured **145 s per page** and looked like a verdict on the
machine. It was a verdict on the packaging. Ollama ships gpt-oss as MXFP4, and
that path does not reach Metal on Apple Silicon:

| | Ollama (MXFP4) | llama.cpp (GGUF) |
|---|---|---|
| prefill | 124 tok/s | **290 tok/s** |
| generation | 14.5 tok/s | **27-29 tok/s** |
| `ollama ps` | `19%/81% CPU/GPU` | — |

Same machine, same weights. Two further things were needed and both are
non-obvious:

- **`iogpu.wired_limit_mb=13500`.** The default GPU budget on 16 GB is ~11.8 GiB
  and gpt-oss-20b is 12.1 GB, so Metal answered
  `kIOGPUCommandBufferCallbackErrorOutOfMemory` at load. Not persistent across a
  reboot. Qwen3-8B does not need it; it is recorded because the diagnosis took
  the longest.
- **`-c 8192`.** The extraction prompt is 3.1-4.4K tokens depending on tokenizer
  (2,852-char system prompt plus `max_text_chars: 8000` of page text) and the cap
  is 4,000 generated. Ollama's VRAM-derived default is 4,096, and a runtime short
  of context *trims the prompt* rather than failing — the page text is the tail
  of the message, so it trims exactly the part the answer depends on. A trimmed
  prompt still scores. It scores the truncation.

## How it is wired in

`localgpu` in `config/providers.yaml`, reached like any other provider. Nothing
in the pipeline knows it is ours.

- **Both the address and the key come from the environment** (`base_url_env:
  LOCAL_GPU_URL`, `api_key_env: LOCAL_GPU_KEY`), and `configured` is false unless
  both are set. That is the correct state whenever the machine is asleep, closed,
  or off — see [[free-tier-model-router]] for what `configured` gates. `base_url`
  is deliberately empty: a stale default would send every call in a run to a dead
  host. The address is env-borne because a Cloudflare quick tunnel gets a new
  hostname on every reconnect and `config/` is not a persisted volume, so a URL
  in the YAML would make each reconnection a code deploy.
- **`timeout_seconds: 600`**, against the global 60. A full 8,000-char page
  measured 66.5 s end to end through the tunnel, so the global would fail every
  call — and a timeout is scored as a *failure*, so it would not report a slow
  provider, it would retire a working one through the circuit breaker. Raising
  the global instead is the wrong fix: it would let a genuinely hung hosted call
  hold a slot for as long as the slowest local model is allowed.
- **`max_output_tokens: 4000`**, against the fleet's 1,500. This is the one place
  that rule inverts. The 1,500 exists because Groq reserves `prompt + max_tokens`
  against an 8,000-token minute window *before* generating; nothing is reserved
  here and nothing is billed, so the cap costs only seconds actually spent.
- **`max_concurrency: 1`**, against unlimited everywhere else — and this is the
  setting that made it work at all. `pipeline.extract_concurrency: 4` is right
  for hosted APIs, where the wait is network latency and four requests overlap
  for free. On one GPU they do not overlap: they share it. Four concurrent pages
  returned four answers each about four times slower (27 tok/s alone against
  **1.85 tok/s** with four slots busy), which bought nothing and pushed every
  call past **Cloudflare's 100-second origin timeout** — HTTP 524 on extractions
  that were otherwise fine. That 100 s is the real ceiling regardless of
  `timeout_seconds: 600`, on every non-Enterprise plan.

  The limit is shared by every extractor for that provider, not held per
  object — the first version was per-instance and limited nothing, because the
  pipeline's chain and `_enrich_run`'s own chain are separate extractors for the
  same provider in the same process. Three of them each politely allowed
  themselves one call.

  The queue belongs on our side of the wire. Waiting on the semaphore holds no
  HTTP connection open, so the proxy's clock does not start until the model is
  free. Serialising at the far end instead (`llama-server --parallel 1`) would
  do the opposite: the request sits in the origin's own queue with the
  connection open and times out exactly as before.

Rebuilding the machine itself — including retaking the tunnel and the key after a
reinstall — is [[local-gpu-machine-setup]].

On the machine: `llama-server` and `cloudflared` run as launchd agents
(`com.meetapedia.llama`, `com.meetapedia.tunnel`) with `KeepAlive`, under
`caffeinate -i` so the provider does not vanish when the laptop is left alone.
`llama-server --api-key` means the tunnel answers 401 without the bearer token.

**Use a named tunnel, not a quick one.** `cloudflared tunnel --url` needs no
account and is the obvious way to start, but its hostname is random and it gets
a *new* one on every reconnect. The first night's tunnel ran 8 hours, dropped,
and then failed to retake its own name — 22 consecutive `control stream
encountered a failure while serving`. The failure mode is the problem: the
server's `LOCAL_GPU_URL` keeps pointing at a hostname that no longer resolves to
anything, `configured` is still true because the variable is still set, and every
call fails at connect time until a human notices. A named tunnel
(`gpu.meetapedia.com`, created 2026-09-06) keeps its hostname across restarts,
reboots and network changes, so the variable is set once and `base_url_env` stops
being a maintenance burden. Costs one browser login; nothing else changes.

The hostname sits on the public brand domain deliberately — the name should say
which project the provider belongs to, and there is nothing to leak behind it:
Cloudflare proxies it (so the machine's own IP is never exposed) and it answers
401 to everything without the key. Credentials are `~/.cloudflared/cert.pem` and
`~/.cloudflared/<tunnel-id>.json`, outside the repository and secret.

## Two ways the runtime can lie about quality

Both were hit here, and both look like a bad model in the score.

**A thinking model that reasons in plain text.** Qwen3-4B ignores llama.cpp's
generic `--reasoning-budget 0` (which works on gpt-oss) and emits `"Okay, let's
tackle this..."` as `content` — no `<think>` tags, no `reasoning` field, so
neither the runtime nor `_json_items` can separate it. All 16 golden pages came
back as `ExtractorContentError`, scoring `n/a`. In production that error is
precisely what `_Quarantine` counts, so a misconfigured local model would retire
real pages permanently while looking merely unlucky. The fix reaches the model's
own template: `--chat-template-kwargs '{"enable_thinking":false}'`.

**A context window shorter than the prompt.** Covered above under `-c 8192`: the
runtime trims rather than fails, and it trims the page text.

The general rule both point at: when adding a model, look at the *shape* of one
answer before trusting any score. `n/a` and `0` look alike in a summary table and
mean opposite things.

## What this does not settle

- **The Cloudflare ceiling is close.** The scored run averaged **89 s/page**
  against a 100 s origin timeout, and that run went over loopback where no
  timeout applied. Through the tunnel a slow page will 524. That failure is
  benign — `ExtractorUnavailableError`, not `ExtractorContentError`, so it never
  reaches the quarantine and the page is retried next run — but it caps how much
  this provider can actually contribute. Getting off Cloudflare's HTTP proxy
  (a reverse tunnel to the server itself) is the fix if it ever matters enough.
- **Throughput does not transfer.** These are this M3's numbers, under this
  machine's load. The Hetzner
  box has no GPU; the same model there would be far slower. This machine
  contributes as a machine, not as a proof about the server.
- **The same-sample cloud comparison is still owed.** Scheduled for the next
  00:02 UTC window, before the `ai_only` run starts consuming the day.
- **The scores measure agreement with the incumbent extraction**, not ground
  truth — see [[measuring-extraction-quality]]. A model that finds a real club
  the incumbent missed is scored down for it.
- **Constrained decoding is available and not yet used.** The project's own
  `EXTRACTION_SCHEMA`, sent as a per-request
  `response_format: {"type": "json_schema", ...}`, is enforced at the sampler and
  returns clean JSON with every field — verified 2026-09-06. Only the
  *server-level* `--json-schema-file` flag fails (`Failed to initialize
  samplers: std::exception`), so the schema is fine and the flag is not.
  Meanwhile `response_format: {"type": "json_object"}` — what `json_mode: true`
  sends — is **silently ignored** by llama.cpp, so the prompt is doing all the
  work today.

  Not adopted yet, deliberately. It would change the payload of every extraction
  call, which is the highest-risk shared path in `extract.py`, and it would need
  a per-call-site schema (extraction, venue, person and enrichment each have
  their own) rather than one flag. The failure it prevents is already *recovered*
  by `_json_items` unwrapping fences; the gain is preventing them instead, which
  does not justify an unreviewed change to that path.

## 2026-09-25: the server, measured from the outside

Read from the running `llama-server` (`/props`, `/slots`, build b10964):
`total_slots: 4`, every slot reporting `n_ctx: 8192` — and two concurrent
4.3-4.6K-token prompts answered **HTTP 500 "Context size has been exceeded."**
within seven seconds. The four slots share one 8,192-token KV cache. That, not
the GPU, is what the 2026-09-06 "1.85 tok/s with four slots" measured, and it
also means one long page can run out of room on its own: a 4,570-token prompt
plus `max_output_tokens: 4000` is 8,570.

Changed the same day, all measured on the same real pages:

- **Streaming** (`stream: true`). A page with twelve communities took 95-97 s,
  at Cloudflare's 100 s silent-origin limit; streamed, the first token resets it.
- **Grammar-enforced schema** (`json_schema: true` → `response_format:
  json_schema` with `EXTRACTION_SCHEMA`). Every answer valid, same communities.
- **Compact output** (in `_API_EXTRACT_SUFFIX`, outside the fingerprint): omit
  empty fields, no indentation. 1,488 → 1,219, 422 → 194 and 321 → 282 output
  tokens, same communities found. Output tokens are most of a call's time here.

Still `max_concurrency: 1`. Raising it needs the server restarted with room for
two full calls — per slot at least prompt + 4,000:

    llama-server -m ~/models/Qwen3-4B-Q4_K_M.gguf --jinja \
      --chat-template-kwargs '{"enable_thinking":false}' \
      -np 2 -c 20480 -fa on -ctk q8_0 -ctv q8_0

(`-c` is the total across slots; q8_0 KV halves its memory to ~1.5 GB.)

Applied the same day to the launchd agent (keeping `--alias`, `--api-key-file`,
`-ngl 99` and the thinking switch): `/props` reports 2 slots at `n_ctx: 10240`, an
unauthenticated request still 401s, and a test call answered clean JSON with no
reasoning text. `max_concurrency` is still 1 — the room exists now; two parallel
real pages have not been measured yet.

