#!/usr/bin/env python3
"""Run `localgpu` on a Mac someone is also working on, and get out of their way.

macOS cannot give a process "only the spare GPU": background QoS throttles the
CPU and disk, not Metal, and the model's weights stay wired in unified memory
the whole time it is loaded. So instead of a priority, this is a governor. Every
`--check` seconds it reads four signals and runs llama-server in one of three
states:

    full  the owner has been idle `--idle` seconds, on AC, no memory pressure
          → two slots, normal priority (production's configured maximum)
    low   the owner is using the machine
          → one slot, background task policy
    off   on battery, memory short (critical, or under 12% free), thermal
          throttling, or `pause` was run
          → llama-server stopped: the ~4-5 GB it holds is given back

Production follows without being told: it reads `/props` → `total_slots`
before sending work, and treats the tunnel's 502/530 while the server is off
as "absent for a few minutes", not as failure (scraper/extract.py,
`ExtractorAbsentError`). Going down is immediate; going up waits for two
agreeing checks, so a glance at the screen does not restart the model.

Usage (paths default to the runbook's layout, see README.md):

    governor.py run                 # what the launchd agent executes
    governor.py status              # current state and the signals behind it
    governor.py pause | resume      # owner override, survives restarts

Standard library only, Python 3.9+, so it runs on the system /usr/bin/python3.
"""
from __future__ import annotations

import argparse
import json
import re
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Optional

HOME = Path.home()
STATE_DIR = HOME / ".meetapedia"
PAUSE_FILE = STATE_DIR / "localgpu.pause"
STATE_FILE = STATE_DIR / "localgpu.state.json"

SLOTS = {"full": 2, "low": 1}
#: Below this share of free memory the model is stopped: the owner's apps are
#: about to page, and on unified memory paging also starves the GPU.
MIN_FREE_PCT = 12
#: Full speed (the second slot's KV cache) only with this much free.
FULL_FREE_PCT = 25
#: Context per slot: a ~4.6K-token prompt plus `max_output_tokens: 4000`.
CTX_PER_SLOT = 10240


def log(msg: str, **kw) -> None:
    extra = " ".join(f"{k}={v}" for k, v in kw.items())
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg} {extra}".rstrip(), flush=True)


# ── signals ──────────────────────────────────────────────────────────────────

def _run(cmd: list) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return ""


def idle_seconds() -> float:
    """Seconds since the last keyboard/mouse/trackpad input."""
    m = re.search(r'"HIDIdleTime" = (\d+)', _run(["ioreg", "-c", "IOHIDSystem", "-d", "4"]))
    return int(m.group(1)) / 1e9 if m else 0.0


def on_ac_power() -> bool:
    return "AC Power" in _run(["pmset", "-g", "batt"])


def memory_pressure() -> int:
    """1 normal, 2 warning, 4 critical (kern.memorystatus_vm_pressure_level)."""
    try:
        return int(_run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"]).strip())
    except ValueError:
        return 1


def memory_free_pct() -> int:
    """`memory_pressure`'s own free percentage. The kernel's level is a poor
    gate on a 16 GB Mac: it sat at "warning" with 46% free on 2026-10-23, and
    gating on it would have kept the model off whenever the owner worked."""
    m = re.search(r"free percentage:\s*(\d+)%", _run(["memory_pressure"]))
    return int(m.group(1)) if m else 100


def thermally_limited() -> bool:
    out = _run(["pmset", "-g", "therm"])
    m = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", out)
    if m and int(m.group(1)) < 100:
        return True
    m = re.search(r"Thermal Warning Level\s*=\s*(\d+)", out)
    return bool(m and int(m.group(1)) > 0)


def read_signals() -> dict:
    return {"idle_s": round(idle_seconds()), "ac": on_ac_power(),
            "pressure": memory_pressure(), "free_pct": memory_free_pct(),
            "thermal": thermally_limited(),
            "paused": PAUSE_FILE.exists()}


def desired_state(sig: dict, idle_full_s: int, current: str = "off") -> "tuple[str, str]":
    if sig["paused"]:
        return "off", "paused by owner"
    if not sig["ac"]:
        return "off", "on battery"
    if sig["pressure"] >= 4 or sig["free_pct"] < MIN_FREE_PCT:
        return "off", f"memory short ({sig['free_pct']}% free)"
    if sig["thermal"]:
        return "off", "thermal limit"
    # Hysteresis. Entering `full` needs FULL_FREE_PCT free; staying in it only
    # needs more than MIN_FREE_PCT, because once two slots are loaded the
    # reading includes the model's own cache. Without this the governor
    # oscillated: full took free memory from ~54% to ~24%, under 25% it dropped
    # to low, freed it, went full again — 36 llama-server restarts in five
    # hours on 2026-10-23, each one cancelling the calls in flight.
    need = MIN_FREE_PCT if current == "full" else FULL_FREE_PCT
    if sig["idle_s"] >= idle_full_s and sig["free_pct"] >= need:
        return "full", f"idle {sig['idle_s']}s"
    return "low", f"in use (idle {sig['idle_s']}s)"


RANK = {"off": 0, "low": 1, "full": 2}


# ── llama-server ─────────────────────────────────────────────────────────────

class Server:
    def __init__(self, args):
        self.args = args
        self.proc: Optional[subprocess.Popen] = None
        self.awake: Optional[subprocess.Popen] = None
        self.state = "off"

    def _key(self) -> str:
        return Path(self.args.key_file).read_text().strip()

    def busy_slots(self) -> int:
        """Slots generating right now; 0 when unknown (fail towards switching)."""
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.args.port}/slots",
                headers={"Authorization": f"Bearer {self._key()}",
                         "User-Agent": "localgpu-governor"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                return sum(1 for s in json.load(resp) if s.get("is_processing"))
        except Exception:
            return 0

    def drain(self, limit_s: float) -> None:
        """Let calls in flight finish before a restart drops them: production
        scores a dropped call as a transient failure, a page it pays again."""
        deadline = time.monotonic() + limit_s
        while time.monotonic() < deadline and self.busy_slots():
            time.sleep(3)

    def start(self, state: str) -> None:
        n = SLOTS[state]
        cmd = [self.args.llama_server, "-m", self.args.model,
               "--alias", self.args.alias, "--host", "127.0.0.1", "--port", str(self.args.port),
               "-c", str(CTX_PER_SLOT * n), "-np", str(n),
               "-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0", "-ngl", "99",
               "--api-key-file", self.args.key_file, "--reasoning", "off"]
        if state == "low":
            # Background policy: efficiency cores and throttled I/O for the
            # CPU side. Metal work is not covered by it — the single slot is
            # what actually halves the GPU's share.
            cmd = ["taskpolicy", "-b"] + cmd
        log("llama_start", state=state, slots=n)
        out = open(Path(self.args.log_dir) / "llama.log", "ab")
        self.proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT)
        # Keep the Mac awake only while the model is up, never while it is off.
        self.awake = subprocess.Popen(["caffeinate", "-i", "-w", str(self.proc.pid)])
        self.state = state

    def stop(self, drain_s: float) -> None:
        if self.proc and self.proc.poll() is None:
            self.drain(drain_s)
            log("llama_stop", was=self.state)
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        self.state = "off"

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


# ── commands ─────────────────────────────────────────────────────────────────

def cmd_run(args) -> None:
    STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    Path(args.log_dir).mkdir(parents=True, exist_ok=True)
    server = Server(args)
    pending, agree = None, 0

    def _shutdown(*_):
        server.stop(drain_s=30)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    log("governor_start", idle_full_s=args.idle, check_s=args.check)
    while True:
        sig = read_signals()
        want, why = desired_state(sig, args.idle, server.state)
        if server.state != "off" and not server.alive():
            log("llama_exited", state=server.state)  # crashed: start it again
            server.state = "off"
        if want == server.state:
            pending, agree = None, 0
        elif RANK[want] < RANK[server.state]:
            # Down is immediate. Critical pressure and battery do not wait for
            # a long generation; the owner's keyboard waits for a short one.
            urgent = want == "off" and why != "paused by owner"
            server.stop(drain_s=10 if urgent else 90)
            if want != "off":
                server.start(want)
            log("state", now=server.state, reason=why, **sig)
            pending, agree = None, 0
        else:
            agree = agree + 1 if pending == want else 1
            pending = want
            if agree >= 2:
                server.stop(drain_s=90)
                server.start(want)
                log("state", now=server.state, reason=why, **sig)
                pending, agree = None, 0
        STATE_FILE.write_text(json.dumps(
            {"state": server.state, "reason": why, "signals": sig,
             "at": time.strftime("%Y-%m-%dT%H:%M:%S")}))
        time.sleep(args.check)


def cmd_status(args) -> None:
    if STATE_FILE.exists():
        print(STATE_FILE.read_text())
    else:
        print("no state yet (is the governor running?)")
    print("paused" if PAUSE_FILE.exists() else "not paused")


def cmd_pause(args) -> None:
    STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    PAUSE_FILE.touch()
    print("paused: the model stops within one check")


def cmd_resume(args) -> None:
    PAUSE_FILE.unlink(missing_ok=True)
    print("resumed")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command", choices=["run", "status", "pause", "resume"])
    ap.add_argument("--llama-server",
                    default=str(HOME / "opt/llama-b11189/llama-b11189/llama-server"))
    ap.add_argument("--model", default=str(HOME / "models/Qwen3.5-4B-Q6_K.gguf"))
    ap.add_argument("--alias", default="qwen3.5-4b-q6k",
                    help="the model name config/providers.yaml uses")
    ap.add_argument("--key-file", default=str(STATE_DIR / "localgpu.key"))
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--log-dir", default=str(HOME / "Library/Logs/meetapedia"))
    ap.add_argument("--idle", type=int, default=300,
                    help="seconds without input before running at full speed")
    ap.add_argument("--check", type=int, default=20, help="seconds between checks")
    args = ap.parse_args()
    {"run": cmd_run, "status": cmd_status,
     "pause": cmd_pause, "resume": cmd_resume}[args.command](args)


if __name__ == "__main__":
    main()
