#!/bin/sh
# Install the localgpu governor and the tunnel as launchd agents on this Mac.
#
# Prerequisites (docs/wiki/pages/operations/local-gpu-machine-setup.md):
#   ~/opt/llama-b11189/llama-b11189/llama-server, ~/models/Qwen3.5-4B-Q6_K.gguf,
#   ~/.meetapedia/localgpu.key (production's LOCAL_GPU_KEY, mode 600),
#   ~/.cloudflared/config.yml + the meetapedia-gpu tunnel credentials.
#
# Idempotent: re-running replaces the agents with the current governor.py.
# Replaces the old always-on com.meetapedia.llama agent if one exists — the
# governor starts and stops llama-server itself.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
BIN="$HOME/.meetapedia/bin"
AGENTS="$HOME/Library/LaunchAgents"
LOGS="$HOME/Library/Logs/meetapedia"
UID_=$(id -u)
CLOUDFLARED=$(command -v cloudflared)

mkdir -p "$BIN" "$AGENTS" "$LOGS"
chmod 700 "$HOME/.meetapedia"
# A copy, not the checkout: a moved or renamed repository must not stop the
# provider (the .venv broke twice that way).
cp "$HERE/governor.py" "$BIN/localgpu-governor.py"

write_agent() {  # label, then the program arguments
  label=$1; shift
  {
    echo '<?xml version="1.0" encoding="UTF-8"?>'
    echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
    echo '<plist version="1.0"><dict>'
    echo "  <key>Label</key><string>$label</string>"
    echo '  <key>ProgramArguments</key><array>'
    for a in "$@"; do echo "    <string>$a</string>"; done
    echo '  </array>'
    echo '  <key>RunAtLoad</key><true/>'
    echo '  <key>KeepAlive</key><true/>'
    echo "  <key>StandardOutPath</key><string>$LOGS/${label##*.}.log</string>"
    echo "  <key>StandardErrorPath</key><string>$LOGS/${label##*.}.log</string>"
    echo '</dict></plist>'
  } > "$AGENTS/$label.plist"
}

reload() {
  launchctl bootout "gui/$UID_/$1" 2>/dev/null || true
  # bootout returns before the job is gone; bootstrap answers error 5 until then.
  for _ in 1 2 3 4 5; do
    launchctl bootstrap "gui/$UID_" "$AGENTS/$1.plist" 2>/dev/null && return 0
    sleep 1
  done
  launchctl bootstrap "gui/$UID_" "$AGENTS/$1.plist"
}

if [ -f "$AGENTS/com.meetapedia.llama.plist" ]; then
  launchctl bootout "gui/$UID_/com.meetapedia.llama" 2>/dev/null || true
  rm -f "$AGENTS/com.meetapedia.llama.plist"
fi

write_agent com.meetapedia.governor /usr/bin/python3 "$BIN/localgpu-governor.py" run
write_agent com.meetapedia.tunnel "$CLOUDFLARED" tunnel --config "$HOME/.cloudflared/config.yml" run
reload com.meetapedia.governor
reload com.meetapedia.tunnel
echo "installed; state: /usr/bin/python3 $BIN/localgpu-governor.py status"
