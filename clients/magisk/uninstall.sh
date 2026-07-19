#!/system/bin/sh

pkill -f 'com.clipsync.bridge.Main' >/dev/null 2>&1 || true
pkill -f '/system/bin/clipboard-syncd' >/dev/null 2>&1 || true
rm -rf /data/adb/clipboard-sync
rm -rf /data/local/tmp/clipboard-sync
