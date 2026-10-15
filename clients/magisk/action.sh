#!/system/bin/sh
#
# SMS + Clipboard Sync — Magisk "Action" button.
#
# Read-only status report. Safe to run at any time: it never starts, stops or
# reconfigures anything, so it cannot break a working setup.

STATE_DIR=/data/adb/clipboard-sync
RUNTIME_DIR=/data/local/tmp/clipboard-sync
LOG_FILE="$STATE_DIR/clipboard-sync.log"
PID_FILE="$STATE_DIR/clipboard-syncd.pid"
CONFIG_FILE="$STATE_DIR/config.conf"
MIGRATED_MARKER="$STATE_DIR/.migrated-v2"

# Version comes from module.prop so it cannot drift from the installed module.
MODDIR=${0%/*}
[ -f "$MODDIR/module.prop" ] || MODDIR=/data/adb/modules/sms_clipboard_sync
_ver=$(sed -n 's/^version=//p' "$MODDIR/module.prop" 2>/dev/null | head -n 1)
_vc=$(sed -n 's/^versionCode=//p' "$MODDIR/module.prop" 2>/dev/null | head -n 1)
echo "SMS + Clipboard Sync ${_ver:-unknown}${_vc:+ (versionCode $_vc)}"

# --- supervisor + bridges --------------------------------------------------
# Report the three processes separately: it is entirely normal for the
# clipboard bridge to run while the SMS forwarder is absent (e.g. the shell UID
# is unavailable on some ROMs), and a single collapsed "running" line would
# hide that.
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE" 2>/dev/null)" 2>/dev/null; then
  echo "Supervisor: running (pid $(cat "$PID_FILE"))"
else
  echo "Supervisor: stopped"
fi

if pgrep -f 'com.clipsync.bridge.Main' >/dev/null 2>&1; then
  echo "Clipboard : running (event driven, UID 1000)"
else
  echo "Clipboard : stopped"
fi

if pgrep -f 'com.clipsync.bridge.SmsForwarder' >/dev/null 2>&1; then
  echo "SMS       : running (event driven, UID 2000)"
else
  echo "SMS       : stopped"
fi

# --- server ----------------------------------------------------------------
# The regex strips at most one leading/trailing quote from the value, matching
# the Java parser's rule, so both `KEY='v'` and `KEY=v` print the same thing.
#
# `cfg <KEY>` reads a value straight out of the saved config without sourcing it,
# so a config containing shell syntax errors cannot take the action button down.
# It also checks the file first: `sed` on a missing path writes an error to stderr,
# which would pollute this status report.
cfg() {
  [ -f "$CONFIG_FILE" ] || return 0
  sed -n "s/^$1=['\"]\{0,1\}\([^'\"]*\).*/\1/p" "$CONFIG_FILE" 2>/dev/null | head -n 1
}

if [ -f "$CONFIG_FILE" ]; then
  echo "Server    : $(cfg SERVER_URL)"
  echo "Device    : $(cfg DEVICE_NAME)"
else
  echo "Config    : missing ($CONFIG_FILE)"
fi

# --- switches --------------------------------------------------------------
# Render an unset switch as "unset" rather than a bare "off ()": on a first boot
# the config may not be provisioned yet, and "off" would falsely suggest the user
# had turned the feature off.
switch_state() {
  case "$1" in
    '') echo "unset" ;;
    1)  echo "on" ;;
    *)  echo "off" ;;
  esac
}

show_switch() {
  _label="$1"
  _key="$2"
  _val=$(cfg "$_key")
  if [ -n "$_val" ]; then
    echo "$_label: $(switch_state "$_val") ($_key=$_val)"
  else
    echo "$_label: unset ($_key not in config)"
  fi
}

echo "--- Switches ---"
show_switch "SMS forward     " SMS_FORWARD
show_switch "WeChat (企业微信)" WX_SWITCH
show_switch "Toast           " SHOW_TOAST
_poll=$(cfg SMS_POLL_SECONDS)
echo "SMS poll        : ${_poll:-unset}${_poll:+s} (SMS_POLL_SECONDS)"

echo "--- Last SMS id ---"
if [ -f "$STATE_DIR/sms_state" ]; then
  echo "Last SMS id   : $(cat "$STATE_DIR/sms_state" 2>/dev/null)"
elif [ -f /data/local/tmp/clipboard-sync-sms/sms_state ]; then
  echo "Last SMS id   : $(cat /data/local/tmp/clipboard-sync-sms/sms_state 2>/dev/null)"
else
  echo "Last SMS id   : (not recorded yet)"
fi

# --- migration + legacy modules -------------------------------------------
# Report this because "SMS forwarding silently does nothing" is the single most
# likely symptom of a half-finished upgrade: the merged config must have been
# adopted (that is what .migrated-v2 records) and the old modules should be gone.
#
# The marker alone is NOT sufficient evidence, and this was observed for real: a
# module re-install can remove it while the imported config is still in place and
# perfectly good. So "migrated" is decided from the CONFIG (does it carry the
# merged key set?) and the marker is reported as supplementary detail. Reporting
# "not migrated" while SMS forwarding is working would be actively misleading.
echo "--- Upgrade status ---"
_cfg_has_sms=0
[ -n "$(cfg SMS_FORWARD)" ] && _cfg_has_sms=1
_imported=$(grep -ac 'migrate: imported' "$LOG_FILE" 2>/dev/null)
case "$_imported" in ''|*[!0-9]*) _imported=0 ;; esac

if [ "$_cfg_has_sms" = "1" ] && [ "$_imported" -gt 0 ]; then
  echo "Migrated      : yes (config carries the merged key set; import recorded in the log)"
elif [ "$_cfg_has_sms" = "1" ]; then
  echo "Migrated      : yes (config carries the merged key set)"
  if [ ! -f "$MIGRATED_MARKER" ]; then
    echo "                note: $MIGRATED_MARKER is absent (a re-install can remove it)."
    echo "                The setting above is taken from the live config, which is what matters."
  fi
elif [ -f "$MIGRATED_MARKER" ]; then
  echo "Migrated      : marker present, but SMS_FORWARD is missing from the config"
  echo "                -> the config did not survive; re-run service.sh"
else
  echo "Migrated      : no ($MIGRATED_MARKER absent and config lacks SMS_FORWARD)"
fi

# Show WHAT was imported and from where. This is what actually confirms success
# to the user, because a bare "yes" does not say the values are right.
_imp=$(grep -a 'migrate: imported' "$LOG_FILE" 2>/dev/null | tail -n 8 | sed -e 's/^.*migrate: imported /    - /')
if [ -n "$_imp" ]; then
  echo "  imported (from the log):"
  printf '%s\n' "$_imp"
fi
_drop=$(grep -a 'migrate: dropped legacy key' "$LOG_FILE" 2>/dev/null | tail -n 4 | sed -e 's/^.*migrate: dropped legacy key /    - /')
if [ -n "$_drop" ]; then
  echo "  dropped:"
  printf '%s\n' "$_drop"
fi

# The effective values, straight from the live config - the thing the bridges read.
echo "  effective config:"
echo "    SERVER_URL   = $(cfg SERVER_URL)"
echo "    DEVICE_TOKEN = $(cfg DEVICE_TOKEN | cut -c1-8)..."
echo "    DEVICE_NAME  = $(cfg DEVICE_NAME)"

if [ "$_cfg_has_sms" = "0" ]; then
  echo "Config warning: SMS_FORWARD is MISSING from the saved config."
  echo "                This is the pre-merge config; SMS forwarding will not run."
  echo "                Re-run service.sh (or reboot) to trigger migration."
fi

# Report each legacy module's state, including the SUCCESS case, so "the old
# module is gone" is a positive confirmation rather than an absence of output.
for _legacy in clipboard_sync_uploader Msg_2_Bark; do
  case "$_legacy" in
    clipboard_sync_uploader) _pretty="Clipboard Sync Uploader" ;;
    Msg_2_Bark)              _pretty="Msg2Bark" ;;
    *)                       _pretty="$_legacy" ;;
  esac
  if [ -d "/data/adb/modules/$_legacy" ]; then
    if [ -f "/data/adb/modules/$_legacy/remove" ]; then
      echo "Legacy        : $_pretty 待重启后删除 (已标记 remove)"
    else
      echo "Legacy        : $_pretty STILL INSTALLED ($_legacy)"
    fi
  else
    echo "Legacy        : $_pretty 已卸载并替换 ($_legacy)"
  fi
done

# --- log -------------------------------------------------------------------
if [ -f "$LOG_FILE" ]; then
  echo "Log size      : $(wc -c < "$LOG_FILE" | tr -d ' \t') bytes (capped at ~1 MB)"
else
  echo "Log size      : no log yet"
fi

echo "--- Last log lines ---"
tail -n 20 "$LOG_FILE" 2>/dev/null || echo "No log yet"
