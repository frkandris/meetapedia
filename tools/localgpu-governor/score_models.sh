#!/bin/sh
# Score several GGUFs, one after another, on production's golden set, on this Mac.
#
#   tools/localgpu-governor/score_models.sh GOLDEN.json label=/path/model.gguf [label=/path/model.gguf ...]
#
# Pauses the governor (so the production slot is not sharing the GPU), serves
# each model on port 8090 with production's flags (2 slots x 10,240 context,
# q8_0 KV, reasoning off, JSON-schema grammar via score_local.py), scores it with
# score_local.py, and resumes the governor on exit — also when interrupted.
# Keep the baseline (the model production runs) in the same invocation: heat
# and battery state move pages/hour more than most model changes do.
# Server logs go to $OUT (default .preview/bench/).
set -u
GOLDEN=$1; shift
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
LS=${LLAMA_SERVER:-$HOME/opt/llama-b11189/llama-b11189/llama-server}
KEY=${KEY_FILE:-$HOME/.meetapedia/localgpu.key}
GOV=${GOVERNOR:-$HOME/.meetapedia/bin/localgpu-governor.py}
PY=${PY:-$ROOT/.venv/bin/python}
OUT=${OUT:-$ROOT/.preview/bench}
mkdir -p "$OUT"

/usr/bin/python3 "$GOV" pause
trap '/usr/bin/python3 "$GOV" resume' EXIT INT TERM
for _ in $(seq 1 30); do pgrep -f "llama-server -m .*--port 8080" >/dev/null || break; sleep 5; done

for spec in "$@"; do
  label=${spec%%=*}; model=${spec#*=}
  "$LS" -m "$model" --alias qwen3.5-4b-q6k --host 127.0.0.1 --port 8090 \
    -c 20480 -np 2 -fa on -ctk q8_0 -ctv q8_0 -ngl 99 \
    --api-key-file "$KEY" --reasoning off > "$OUT/$label.server.log" 2>&1 &
  pid=$!
  for _ in $(seq 1 120); do
    "$PY" -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8090/health')" 2>/dev/null && break
    sleep 1
  done
  (cd "$ROOT" && PYTHONPATH=. "$PY" tools/localgpu-governor/score_local.py \
    --golden "$GOLDEN" --label "$label" --concurrency 2 2>&1 | grep -vE "^\s*$|^20[0-9][0-9]-")
  kill "$pid"; wait "$pid" 2>/dev/null; sleep 2
done
