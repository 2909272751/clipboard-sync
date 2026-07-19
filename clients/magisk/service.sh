#!/system/bin/sh
MODDIR=${0%/*}

# Wait for Android framework services without adding a fixed boot delay.
until [ "$(getprop sys.boot_completed)" = "1" ]; do
  sleep 2
done

STATE_DIR=/data/adb/clipboard-sync
RUNTIME_DIR=/data/local/tmp/clipboard-sync
SAVED_CONFIG="$STATE_DIR/config.conf"
PROVISION_MARKER="$STATE_DIR/provision.id"
BUNDLED_CONFIG="$MODDIR/config.conf"

mkdir -p "$STATE_DIR"
chown 0:2000 "$STATE_DIR"
chmod 0710 "$STATE_DIR"

PROVISION_ID=""
if [ -f "$BUNDLED_CONFIG" ]; then
  # shellcheck disable=SC1090
  . "$BUNDLED_CONFIG"
fi

SAVED_PROVISION_ID=""
[ -f "$PROVISION_MARKER" ] && SAVED_PROVISION_ID=$(cat "$PROVISION_MARKER")

if [ ! -f "$SAVED_CONFIG" ] || { [ -n "$PROVISION_ID" ] && [ "$PROVISION_ID" != "$SAVED_PROVISION_ID" ]; }; then
  cp "$BUNDLED_CONFIG" "$SAVED_CONFIG"
  chown 0:2000 "$SAVED_CONFIG"
  chmod 0640 "$SAVED_CONFIG"
  if [ -n "$PROVISION_ID" ]; then
    printf '%s' "$PROVISION_ID" > "$PROVISION_MARKER"
    chmod 600 "$PROVISION_MARKER"
  fi
fi

# /data/adb is intentionally inaccessible to Android's shell UID. Stage only the
# runtime jar and personalized config in a private shell-owned directory.
mkdir -p "$RUNTIME_DIR"
cp "$MODDIR/framework/clipbridge.jar" "$RUNTIME_DIR/clipbridge.jar"
cp "$SAVED_CONFIG" "$RUNTIME_DIR/config.conf"
chown -R 1000:1000 "$RUNTIME_DIR"
chmod 0700 "$RUNTIME_DIR"
chmod 0600 "$RUNTIME_DIR/clipbridge.jar" "$RUNTIME_DIR/config.conf"

# Stop the previous supervisor first so it cannot restart the old bridge while
# this service instance is installing the updated runtime.
PID_FILE="$STATE_DIR/clipboard-syncd.pid"
if [ -f "$PID_FILE" ]; then
  OLD_PID=$(cat "$PID_FILE" 2>/dev/null)
  case "$OLD_PID" in
    ''|*[!0-9]*) ;;
    *) kill -9 "$OLD_PID" >/dev/null 2>&1 || true ;;
  esac
fi
pkill -9 -f 'clipboard-syncd' >/dev/null 2>&1 || true
sleep 1
pkill -9 -f 'com.clipsync.bridge.Main' >/dev/null 2>&1 || true
nohup "$MODDIR/system/bin/clipboard-syncd" "$MODDIR" </dev/null >/dev/null 2>&1 &
