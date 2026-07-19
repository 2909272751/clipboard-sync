#!/system/bin/sh

STATE_DIR=/data/adb/clipboard-sync
LOG_FILE="$STATE_DIR/clipboard-sync.log"

echo "Clipboard Sync v1.3.0"
if pgrep -f 'com.clipsync.bridge.Main' >/dev/null 2>&1; then
  echo "Status: running (event driven)"
else
  echo "Status: stopped"
fi

if [ -f "$STATE_DIR/config.conf" ]; then
  server=$(sed -n "s/^SERVER_URL=['\"]\{0,1\}\([^'\"]*\).*/\1/p" "$STATE_DIR/config.conf" | head -n 1)
  echo "Server: ${server:-not configured}"
else
  echo "Config: missing"
fi

echo "--- Last log lines ---"
tail -n 20 "$LOG_FILE" 2>/dev/null || echo "No log yet"
