---
type: Runbook
title: Setting Up the Local GPU Machine From Scratch
description: Exact steps to turn a freshly installed Apple Silicon Mac back into the `localgpu` provider — llama.cpp, the model, two launchd agents, and retaking the existing named tunnel.
tags: [operations, local-inference, llama-cpp, cloudflare-tunnel, launchd, providers]
timestamp: 2026-09-25
resource: config/providers.yaml
---

# Setting Up the Local GPU Machine From Scratch

*The machine is disposable and the tunnel hostname is not: a reinstall re-creates the server and the agents in minutes, but the tunnel and the key must be **retaken**, not replaced.*

Why this page exists separately from [[our-own-gpu-in-the-fleet]]: that page records
*why* the provider looks the way it does. This one is the sequence to type when the
machine has been wiped — done 2026-09-17 on a reinstalled M3/16 GB, where the provider
had been answering 100% errors for days because nothing was listening.

## The two things you must not re-create

**The tunnel.** `cloudflared tunnel login` writes a new `cert.pem`, but the tunnel
itself (`meetapedia-gpu`, id `fa2bf00d-…`) still exists in the account with its DNS
record. Fetch its credentials instead of creating a second one:

```bash
cloudflared tunnel login                     # browser, pick the meetapedia.com zone
cloudflared tunnel list                      # confirm meetapedia-gpu exists
ID=fa2bf00d-bb5a-4b16-a04a-50b88c4d4b21
cloudflared tunnel token --cred-file ~/.cloudflared/$ID.json $ID
```

A second tunnel would need a second DNS record, and `gpu.meetapedia.com` can only point
at one — the reason the named tunnel was chosen over a quick one in the first place.

**The key.** `LOCAL_GPU_KEY` in Coolify is the *server's* copy and survives the
reinstall. Take the machine to the server's value; do not mint a new one and expect the
server to follow. Minting one first is what happened here, and the symptom is
misleading: `/v1/models` and `/v1/quota` answer 200 (they only read the catalogue),
while every completion comes back `error code: 502` from Cloudflare. The app log names
it exactly — `api_request_failed … "Invalid API Key" … status=401` followed by
`gateway_upstream_unavailable … model=localgpu`. Changing the server's copy instead
costs a redeploy and buys nothing.

## The sequence

```bash
brew install llama.cpp cloudflared
mkdir -p ~/models && curl -L -o ~/models/Qwen3-4B-Q4_K_M.gguf \
  https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/main/Qwen3-4B-Q4_K_M.gguf
mkdir -p ~/.meetapedia && chmod 700 ~/.meetapedia
# write the server's LOCAL_GPU_KEY value (Coolify → env) into this file:
chmod 600 ~/.meetapedia/localgpu.key
```

`--api-key-file`, not `--api-key`: the plist is world-readable and `ps` shows every
argument, so the literal key would be visible to any process on the machine.

Two launchd agents in `~/Library/LaunchAgents`, both `RunAtLoad` + `KeepAlive`, both
wrapped in `caffeinate -i`, logging to `~/Library/Logs/meetapedia/`:

- `com.meetapedia.llama` → `caffeinate -i llama-server -m ~/models/Qwen3-4B-Q4_K_M.gguf
  --alias qwen3-4b-q4km --host 127.0.0.1 --port 8080 -c 20480 -np 2 -fa on
  -ctk q8_0 -ctv q8_0 -ngl 99
  --api-key-file ~/.meetapedia/localgpu.key
  --chat-template-kwargs '{"enable_thinking":false}'`
- `com.meetapedia.tunnel` → `caffeinate -i cloudflared tunnel --config
  ~/.cloudflared/config.yml run`, with `config.yml` routing `gpu.meetapedia.com` to
  `http://127.0.0.1:8080`.

`--alias qwen3-4b-q4km` makes `/v1/models` report the name the catalogue uses, so
`localgpu:qwen3-4b-q4km` resolves at the gateway. `--host 127.0.0.1` because the tunnel
is the only intended door in.

`-c` is the total shared by the `-np` slots (10,240 each: a ~4.6K-token prompt plus
`max_output_tokens: 4000`); `-fa on` is required for the q8_0 V cache. Changed from
`-c 8192` on 2026-09-25 — see [[our-own-gpu-in-the-fleet]].

Load them with `launchctl bootstrap gui/$(id -u) <plist>`. `launchctl kickstart -k
gui/$(id -u)/com.meetapedia.llama` restarts one with the arguments launchd already
holds, so after **editing** a plist, `launchctl bootout gui/$(id -u)/com.meetapedia.llama`
and bootstrap it again (retry the bootstrap if it answers error 5: the bootout has not
finished).

## Verifying, in the order that isolates faults

```bash
K=$(tr -d '\n' < ~/.meetapedia/localgpu.key)
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/v1/models            # 401
curl -s -H "Authorization: Bearer $K" http://127.0.0.1:8080/v1/models               # the alias
curl -s -H "Authorization: Bearer $K" https://gpu.meetapedia.com/v1/models          # through the tunnel
# end to end, through production's own router (ROUTER_API_KEY):
curl -s https://meetapedia.com/v1/chat/completions -H "Authorization: Bearer $ROUTER" \
  -H 'Content-Type: application/json' \
  -d '{"model":"localgpu","max_tokens":80,"messages":[{"role":"user","content":"ping"}]}'
```

The last one is the only real proof: it returns `x_router: {provider: localgpu, model:
qwen3-4b-q4km, quality: 73}`, and `~/Library/Logs/meetapedia/llama.log` shows the
matching `launch_slot_` line. Measured 2026-09-17 on the rebuilt machine: a full
8,000-char page with the real system prompt took **57.6 s** (4,129 prompt tokens at 287
tok/s, 1,094 generated at 25.4 tok/s) — inside Cloudflare's 100 s origin timeout, and
matching the 2026-09-06 numbers.

**`Python-urllib` gets a Cloudflare 403** at `gpu.meetapedia.com`, while
`python-httpx` (what the server actually sends) gets 200. A hand-written probe script
can therefore fail on a healthy provider; set a `User-Agent` before concluding
anything. Verified 2026-09-17.

## The machine is a provider, so absence must stay legible

`configured` requires both `LOCAL_GPU_URL` and `LOCAL_GPU_KEY`, so a sleeping machine is
*absent* rather than failing — see [[free-tier-model-router]]. What the reinstall showed
is the other half: with the variables still set and nothing listening, the provider is
present and broken, and `extractor_preflight_ok` prints `retired=1` with `localgpu`
missing from `live=[…]` while every other entry reads `(no budget)`. That line is the
fastest health check there is.

Note the agents load at *login*, not at boot: a rebooted machine that nobody logs into
has no provider. `caffeinate -i` keeps it awake while logged in but does not survive a
closed lid.

## Caveat learned the same day

A provider that comes back mid-window used not to rejoin the **enrichment** run.
`main.py:_enrich_body` built its chain once per run, and with
`schedule.worker_enabled: true` that run is unbounded — so `localgpu`, retired by the
circuit breaker while it was 401ing, stayed retired and every batch logged
`all providers rate limited` even after the machine was verifiably serving the
`ai_only` pipeline (which had rebuilt its own chain at its next preflight). A container
restart was the only cure. Fixed the same day: every pause in the enrichment loop now
rebuilds the chain and logs `enrich_chain_rebuilt`, so a machine that comes back is
picked up within one pause instead of at the next restart. See
[[free-tier-model-router]] for the chain's retirement rules and [[cost-saver-schedule]]
for the windows.
