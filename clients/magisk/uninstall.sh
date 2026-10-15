#!/system/bin/sh
#
# SMS + Clipboard Sync — uninstall cleanup.
#
# Magisk runs this before the module directory is deleted. Everything this
# module created outside MODDIR must be removed here, otherwise a stale
# config/token, a stale runtime jar or an orphaned bridge would survive the
# uninstall.

STATE_DIR=/data/adb/clipboard-sync

# Stop the bridges first (and the supervisor before them, so it cannot restart
# a bridge we are about to kill). Every kill is by explicit pid or by the bridge
# CLASS name: the pattern 'clipsyncd' would also match THIS script's own cmdline
# (it lives at .../system/bin/../uninstall.sh under the module dir, and Magisk
# runs it as `sh uninstall.sh`), so a bare pkill could take out the uninstaller
# itself before it removed anything.
for _pf in "$STATE_DIR/.clipsyncd.clip.loop.pid" "$STATE_DIR/.clipsyncd.sms.loop.pid"; do
  _lp=$(cat "$_pf" 2>/dev/null)
  case "$_lp" in
    ''|*[!0-9]*) ;;
    *) kill -9 "$_lp" >/dev/null 2>&1 || true ;;
  esac
done
if [ -f "$STATE_DIR/clipboard-syncd.pid" ]; then
  OLD_PID=$(cat "$STATE_DIR/clipboard-syncd.pid" 2>/dev/null)
  case "$OLD_PID" in
    ''|*[!0-9]*) ;;
    *) kill -9 "$OLD_PID" >/dev/null 2>&1 || true ;;
  esac
fi
sleep 1

pkill -9 -f 'com.clipsync.bridge.Main' >/dev/null 2>&1 || true
pkill -9 -f 'com.clipsync.bridge.SmsForwarder' >/dev/null 2>&1 || true

# Undo the boot-time hosts bind mount. Without this /system/etc/hosts keeps
# pointing at a file we are about to delete, so hostname resolution would stay
# overridden (or resolve to nothing) after the module is gone. Best effort: if it
# was never mounted, umount simply fails and we continue.
umount /system/etc/hosts >/dev/null 2>&1 || true

# Persistent state: config (contains the device token), logs, pidfiles, SMS state
# and the hosts file staged by service.sh.
rm -rf "$STATE_DIR"

# Staged runtime: jar + personalized config, one set per bridge UID.
rm -rf /data/local/tmp/clipboard-sync
rm -rf /data/local/tmp/clipboard-sync-sms

# Stale state from the Msg2Bark module this one replaces. Its /data/adb/modules
# directory and its multi-GB debug.log are outside this module's ownership and
# are handled by the migration step, but these two /data/local/tmp leftovers are
# ours to clear.
rm -f /data/local/tmp/msg2bark_last_id
rm -f /data/local/tmp/msg2bark.lock
