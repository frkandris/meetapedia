# localgpu governor

Runs the `localgpu` provider (llama-server + Qwen3.5-4B) on a Mac its owner is
also working on, and steps back when they need the machine.

| state | when | llama-server |
|---|---|---|
| `full` | no input for 5 min, on AC, ≥25% memory free | 2 slots, normal priority |
| `low` | the owner is using the Mac | 1 slot, `taskpolicy -b` |
| `off` | battery, <12% memory free or critical pressure, thermal limit, or `pause` | stopped (the ~4-5 GB is freed) |

Going down is immediate (after letting calls in flight finish, up to 90 s);
going up waits for two agreeing checks (20 s apart). The Mac is kept awake
(`caffeinate -i`) only while the model is running.

Production adapts on its own: it reads `/props` → `total_slots` before sending
work, so `low` gets one call at a time, and it treats the tunnel's 502/530
while the server is `off` as *absent for three minutes* rather than as failure
(`ExtractorAbsentError` in `scraper/extract.py`).

Measured on this M3 / 16 GB on 2026-10-23, on battery, 8,000-char page:
`full` 285 tok/s prefill, 19.0 tok/s generation; `low` 297 / 15.1.

## Install

Prerequisites and the tunnel are in
`docs/wiki/pages/operations/local-gpu-machine-setup.md`. Then:

```sh
tools/localgpu-governor/install.sh
```

It copies `governor.py` to `~/.meetapedia/bin/` (so moving the repository does
not stop the provider), and installs two launchd agents:
`com.meetapedia.governor` and `com.meetapedia.tunnel`. It removes the old
always-on `com.meetapedia.llama` agent if present. Re-run it after editing
`governor.py`.

## Use

```sh
/usr/bin/python3 ~/.meetapedia/bin/localgpu-governor.py status   # state + signals
/usr/bin/python3 ~/.meetapedia/bin/localgpu-governor.py pause    # stop it now
/usr/bin/python3 ~/.meetapedia/bin/localgpu-governor.py resume
```

Logs: `~/Library/Logs/meetapedia/governor.log` (state changes with the signals
behind them), `llama.log`, `tunnel.log`.

Thresholds are flags on `run` (`--idle`, `--check`) and constants at the top of
`governor.py` (`MIN_FREE_PCT`, `FULL_FREE_PCT`). The kernel's memory-pressure
*level* is used only at "critical": on this machine it sat at "warning" with
46% free, and gating on it would have kept the model off whenever the owner
worked.
