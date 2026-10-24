#!/bin/sh
# Run bench.py against several llama-server configurations on port 8090.
# Pause the governor first (governor.py pause) so the GPU is not shared.
# Usage: tools/localgpu-governor/bench_matrix.sh PAGES.json OUT_DIR
set -eu
PAGES=$1; OUT=$2; mkdir -p "$OUT"
LS=${LLAMA_SERVER:-$HOME/opt/llama-b11189/llama-b11189/llama-server}
M=${MODEL:-$HOME/models/Qwen3.5-4B-Q6_K.gguf}
D=${DRAFT:-$HOME/models/Qwen3.5-0.8B-Q8_0.gguf}
KEY=${KEY_FILE:-$HOME/.meetapedia/localgpu.key}
HERE=$(cd "$(dirname "$0")" && pwd)
PY=${PY:-$HERE/../../.venv/bin/python}
COMMON="-m $M --alias qwen3.5-4b-q6k --host 127.0.0.1 --port 8090 -fa on -ctk q8_0 -ctv q8_0 -ngl 99 --api-key-file $KEY --reasoning off"

run() {  # label slots conc extra...
  label=$1; slots=$2; conc=$3; shift 3
  "$LS" $COMMON -c $((10240 * slots)) -np "$slots" "$@" > "$OUT/$label.server.log" 2>&1 &
  pid=$!
  for _ in $(seq 1 60); do curl -sf http://127.0.0.1:8090/health >/dev/null && break; sleep 1; done
  PYTHONPATH="$HERE/../.." "$PY" "$HERE/bench.py" --url http://127.0.0.1:8090 --key-file "$KEY" \
    --pages "$PAGES" --concurrency "$conc" --repeat 2 --label "$label" --save "$OUT/$label.json"
  kill "$pid"; wait "$pid" 2>/dev/null || true; sleep 2
}

case "${SET:-1}" in
  1)
    run baseline_np2      2 2
    run np4               4 4
    run ngram_np2         2 2 --spec-type ngram-simple
    run draft08_np2       2 2 -md "$D" -ngld 99 --spec-draft-n-max 8
    ;;
  2)  # tuning the draft model
    run baseline_np1      1 1
    run draft08_np1       1 1 -md "$D" -ngld 99 --spec-draft-n-max 8
    run draft08_np2_n4    2 2 -md "$D" -ngld 99 --spec-draft-n-max 4
    run draft08_np2_n16   2 2 -md "$D" -ngld 99 --spec-draft-n-max 16
    ;;
  3)  # A/B/A/B on AC: drift (battery, heat) cancels out
    run ab1_baseline_np2  2 2
    run ab1_draft08_np2   2 2 -md "$D" -ngld 99 --spec-draft-n-max 8
    run ab2_baseline_np2  2 2
    run ab2_draft08_np2   2 2 -md "$D" -ngld 99 --spec-draft-n-max 8
    ;;
esac
