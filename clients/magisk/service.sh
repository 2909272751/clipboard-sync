#!/system/bin/sh
#
# SMS + Clipboard Sync — boot service.
#
# Runs as root from Magisk's post-fs-data/late_start stage. Its only jobs are to
# materialize state + runtime directories and to launch the supervisor
# (system/bin/clipsyncd). All long-lived work happens in the supervisor/bridge so
# that this script can exit immediately.
MODDIR=${0%/*}

# Wait for Android framework services without adding a fixed boot delay.
# getprop returns "" before property service is up, which is not a shell error,
# so this loop is race free: it simply re-checks every 2s until the framework
# reports boot completed.
until [ "$(getprop sys.boot_completed)" = "1" ]; do
  sleep 2
done

STATE_DIR=/data/adb/clipboard-sync
RUNTIME_DIR=/data/local/tmp/clipboard-sync
RUNTIME_DIR_SMS=/data/local/tmp/clipboard-sync-sms
SAVED_CONFIG="$STATE_DIR/config.conf"
PROVISION_MARKER="$STATE_DIR/provision.id"
BUNDLED_CONFIG="$MODDIR/config.conf"
LOG_FILE="$STATE_DIR/clipboard-sync.log"
PID_FILE="$STATE_DIR/clipboard-syncd.pid"

# --- legacy module locations (one-click upgrade) ---------------------------
# This module is the merge of two older ones and is expected to be installed
# straight over them, so every boot has to be able to find what they left behind.
#
# The ids below are the ones the two old ZIPs really declared, and they must stay
# in sync with the ids in remove_legacy_modules() below:
#   clipboard_sync_uploader -> old Clipboard Sync  (see module.prop on device)
#   Msg_2_Bark              -> old Msg2Bark
LEGACY_CLIP_DIR=/data/adb/modules/clipboard_sync_uploader
LEGACY_MSG_DIR=/data/adb/modules/Msg_2_Bark
# Idempotency marker for the one-time import below. Deliberately a NEW name, not
# provision.id: provision.id is the ZIP-refresh mechanism and an old installer
# wrote the same value the merged module ships, which is the entire trap below.
MIGRATION_MARKER="$STATE_DIR/.migrated-v2"

MODULES_DIR=/data/adb/modules
# Staging file used to hand an imported config from migrate_legacy() to the
# provisioning block below. /data/local/tmp is reachable on every Android and is
# owned by shell, so it is the one staging location that cannot fail for an
# ownership reason.
MIGRATION_STAGE=/data/local/tmp/.smsg-migrate.conf

# Legacy imports and the module id of this module itself, both used by the
# removal step. The literals, not $_MODDIR, are what the safety check compares:
# MODDIR is derived from $0 and would be worthless if the script were started on
# a copy (which is exactly how the device sandbox test runs it).
MY_MODULE_ID=sms_clipboard_sync
LEGACY_CLIP_ID=clipboard_sync_uploader
LEGACY_MSG_ID=Msg_2_Bark

mkdir -p "$STATE_DIR"
chown 0:2000 "$STATE_DIR"
chmod 0710 "$STATE_DIR"

# --- log rotation ----------------------------------------------------------
# The upstream Msg2Bark module appended to an unbounded file and grew its log to
# 1.47 GB before anyone noticed. Never again: at boot we trim the bridge log
# down to the last LOG_KEEP_LINES lines once it exceeds LOG_MAX_BYTES.
#
# The previous contents are moved to <log>.1 first (overwriting the older
# archive), so the tail we keep is never written on top of itself — `tail` can
# safely read the archive and write the live file.
#
# The supervisor calls the same cap after every bridge restart, so a single long
# boot cannot grow the log either.
LOG_MAX_BYTES=1048576   # ~1 MB
LOG_KEEP_LINES=2000

cap_log() {
  _log="$1"
  [ -f "$_log" ] || return 0
  _size=$(wc -c < "$_log" 2>/dev/null | tr -d ' \t')
  case "$_size" in
    ''|*[!0-9]*) return 0 ;;
  esac
  [ "$_size" -gt "$LOG_MAX_BYTES" ] || return 0
  mv -f "$_log" "$_log.1" 2>/dev/null || return 0
  tail -n "$LOG_KEEP_LINES" "$_log.1" > "$_log" 2>/dev/null || : > "$_log"
  chmod 0640 "$_log" 2>/dev/null
  echo "$(date '+%Y-%m-%d %H:%M:%S') log rotated: was ${_size} bytes, kept last ${LOG_KEEP_LINES} lines" >> "$_log"
}

cap_log "$LOG_FILE"

# --- legacy config import (one-click upgrade from the two old modules) -------
#
# WHY THIS EXISTS
# ---------------
# On a phone that already runs the old modules, service.sh must pull the old
# settings in by itself; the user updates two more phones by installing the ZIP
# and rebooting, nothing else. Two things make that harder than a file copy:
#
#   1. The two old configs do NOT share a format. The old Clipboard module wrote
#      single-quoted shell assignments, but the old Msg2Bark module wrote BARE
#      `key=value` lines. Sourcing the bare one is wrong: a value containing
#      spaces, `$`, backticks or a `#` is then re-parsed by the shell. Both files
#      are therefore parsed by explicit key extraction, never with `.` / `source`.
#   2. The saved config is normally preserved across installs because
#      provision.id says "already provisioned" — and a legacy device's marker
#      carries the SAME id the merged module ships. See the trap note below.
#
# Everything here is best-effort by design: a device that cannot be migrated
# still boots and still forwards with whatever config it has.

# One line in the shared module log. The same timestamped format service.sh and
# the supervisor already use, so `action.sh`'s tail shows migration as one story.
mlog() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG_FILE"
}

# First line of <file> that assigns <key>, with one leading `export ` removed, a
# matching pair of surrounding quotes stripped and trailing comments dropped.
# Matches the Java config parser's rules so what we read here is what the bridge
# would have read. Never sources the file, so a value full of shell metacharacters
# cannot execute or abort the boot.
cfg_get() {
  _file="$1"
  _key="$2"
  [ -f "$_file" ] || return 0
  sed -n "s/^[ 	]*export[ 	][ 	]*$_key=//p;s/^[ 	]*$_key=//p" "$_file" 2>/dev/null \
    | head -n 1 \
    | sed -e "s/^'//" -e "s/'[ 	]*\$//" -e 's/^"//' -e 's/"[ 	]*$//' \
          -e 's/[ 	][ 	]*#.*$//' -e 's/[ 	]*$//'
}

# Does <key> appear as an assignment in <file> at all? Distinct from cfg_get:
# an explicitly empty `WX_WEBHOOK=` must be told apart from a missing key, and a
# missing SMS_FORWARD is the signal that a config predates the merge.
cfg_has_key() {
  _key="$1"
  _file="$2"
  [ -f "$_file" ] || return 1
  grep -q "^[ 	]*\(export[ 	][ 	]*\)\{0,1\}$_key=" "$_file" 2>/dev/null
}

# Is this a plausible integer switch? Same test used for both old modules' 0/1
# flags: anything else (empty, "true", a quoted string) is treated as ABSENT, and
# an absent switch either falls back to the bundled default or is refused.
is_switch() {
  case "$1" in
    ''|*[!0-9]*) return 1 ;;
  esac
  return 0
}

# Does a legacy module directory look like a real installed module rather than a
# leftover husk? require_prop=1 demands module.prop; the removal paths accept a
# directory on its own, because a half-removed module with no module.prop is
# exactly the case that still needs cleanup.
legacy_present() {
  _dir="$1"
  _require_prop="$2"
  [ -d "$_dir" ] || return 1
  if [ "$_require_prop" = "1" ]; then
    [ -f "$_dir/module.prop" ] || return 1
  fi
  return 0
}

# First readable Msg2Bark config out of the priority list in MIGRATION.md.
# The `.bak` fallback matters: a user who edited `config.conf` into a syntax error
# (or an aborted install) still gets their settings migrated instead of silently
# falling back to the bundled placeholder token.
find_msg_config() {
  for _c in "$LEGACY_MSG_DIR/config.conf" "$LEGACY_MSG_DIR/config.conf.bak"; do
    if [ -f "$_c" ] && [ -r "$_c" ]; then
      printf '%s' "$_c"
      return 0
    fi
  done
  return 1
}

migrate_legacy() {
  _msg_src=""
  _msg_token=""
  _msg_sw=""
  _msg_wxsw=""
  _msg_wx=""
  _murl=""
  _base=""
  _token=""
  _token_origin=""
  _clip_token=""
  _clip_src=""
  _clip_name=""
  _clip_toast=""
  _stage=""
  _target=""
  _changed=0

  # Nothing of ours to do on a fresh install: no old module at all.
  if ! legacy_present "$LEGACY_CLIP_DIR" 1 && ! legacy_present "$LEGACY_MSG_DIR" 1; then
    mlog "migrate: no legacy module present; nothing to import"
    return 0
  fi

  # What is provisioned right now. On a legacy device this is the old, SMS-less
  # config, still carrying the shipped PROVISION_ID.
  _have_saved=0
  [ -f "$SAVED_CONFIG" ] && _have_saved=1
  _saved_has_sms=0
  cfg_has_key SMS_FORWARD "$SAVED_CONFIG" && _saved_has_sms=1

  # -------------------------------------------------------------------------
  # IDEMPOTENCY / USER-EDIT GUARD - the gate that decides whether we run at all.
  #
  # Rules, in order:
  #   * saved config already has SMS_FORWARD -> it is OURS, a post-merge config.
  #     Never touch it, even if a legacy module is still lying around; that is
  #     exactly the "do not clobber user edits" requirement.
  #   * no saved config                     -> classic provisioning installs the
  #     bundled config; import into that instead (handled further down).
  #   * saved config but no SMS_FORWARD key -> THE PROVISIONING TRAP. It is an
  #     old-format config; import now, regardless of PROVISION_ID. If the marker id
  #     were honoured here the old SMS-less config would be kept, SMS_FORWARD would
  #     stay unset and the SMS forwarder would silently do nothing - which is
  #     exactly what was observed on the real device.
  # -------------------------------------------------------------------------
  if [ "$_have_saved" = "1" ] && [ "$_saved_has_sms" = "1" ]; then
    mlog "migrate: saved config already has SMS_FORWARD; treating as post-merge and leaving it alone"
    return 0
  fi

  # Run-once guard for the import. Only reached for a config that still needs
  # importing, so the marker cannot be used to skip a migration that never ran.
  if [ -f "$MIGRATION_MARKER" ]; then
    mlog "migrate: marker $MIGRATION_MARKER present; import already done, skipping"
    return 0
  fi

  # THE BASE IS ALWAYS THE BUNDLED CONFIG, and that is load-bearing.
  #
  # MIGRATION.md step 1 is "start from the bundled (new) config", and the reason is
  # not cosmetic: the saved pre-merge config is an OLD module's file, so it lacks
  # every key the merged module needs — SMS_FORWARD, WX_SWITCH, SMS_POLL_SECONDS.
  # Importing onto that file produced a config with a correct token and URL but no
  # SMS_FORWARD at all, which is the very failure this task exists to fix (measured
  # on device: only the old Clipboard module installed, saved config in its old
  # format, result had no SMS_FORWARD key).
  #
  # Importing onto the bundled config instead gives the merged module's full key
  # set, with each legacy value written over it below. The user's own edits are not
  # lost: this whole path only runs when the saved config is a PRE-MERGE file, and
  # once migration has run the saved config has SMS_FORWARD, so the guard at the
  # top of this function returns early on every later boot (verified: a
  # user-edited token survives the second boot unchanged).
  _target="$BUNDLED_CONFIG"
  _stage="$MIGRATION_STAGE"
  rm -f "$_stage" 2>/dev/null
  cp "$_target" "$_stage" 2>/dev/null || {
    mlog "migrate: cannot stage $_target for editing; legacy import skipped (boot continues)"
    return 1
  }
  # Keys the OLD config may carry that the merged module does not use belong to the
  # old modules and must not be inherited from it. Nothing is copied from the saved
  # file, so there is nothing to strip here — this note exists because it is the
  # obvious thing to "optimise" back in.

  if [ "$_have_saved" = "1" ]; then
    mlog "migrate: START legacy import (saved config lacks SMS_FORWARD -> mandatory migration)"
  else
    mlog "migrate: START legacy import (no saved config yet)"
  fi
  # -------------------------------------------------------------------------
  # 1. SERVER IDENTITY - priority a) Msg2Bark myserver_token, b) old Clipboard
  #    DEVICE_TOKEN, c) bundled placeholder. Rationale: the SMS was flowing
  #    through the Msg2Bark credentials, so that device row is the live one; the
  #    Clipboard token is the fallback when Msg2Bark supplied none.
  # -------------------------------------------------------------------------
  _msg_src=$(find_msg_config)
  if [ -n "$_msg_src" ]; then
    _msg_token=$(cfg_get "$_msg_src" myserver_token)
    _msg_sw=$(cfg_get "$_msg_src" myserver_switch)
    _msg_wxsw=$(cfg_get "$_msg_src" wx_switch)
    _msg_wx=$(cfg_get "$_msg_src" wx_webhook)
    _murl=$(cfg_get "$_msg_src" myserver_url)
    mlog "migrate: Msg2Bark source $_msg_src"
  elif legacy_present "$LEGACY_MSG_DIR" 1; then
    mlog "migrate: Msg2Bark module present but no readable config.conf/.bak; ignoring it"
  fi

  # Locate the old Clipboard config unconditionally: it is the fallback token
  # source AND the source of DEVICE_NAME / SHOW_TOAST below, so it is needed even
  # when Msg2Bark wins the token (both old modules can be installed at once).
  if legacy_present "$LEGACY_CLIP_DIR" 1 || [ -f "$SAVED_CONFIG" ]; then
    for _c in "$SAVED_CONFIG" "$LEGACY_CLIP_DIR/config.conf"; do
      [ -f "$_c" ] && [ -r "$_c" ] || continue
      _clip_src="$_c"
      _clip_token=$(cfg_get "$_c" DEVICE_TOKEN)
      [ -n "$_clip_token" ] && break
    done
  fi

  if [ -n "$_msg_token" ]; then
    _token="$_msg_token"
    _token_origin="Msg2Bark myserver_token"
  elif [ -n "$_clip_token" ]; then
    _token="$_clip_token"
    _token_origin="old Clipboard DEVICE_TOKEN ($_clip_src)"
  fi

  if [ -n "$_token" ]; then
    set_config_key "$_stage" DEVICE_TOKEN "'$_token'"
    mlog "migrate: imported DEVICE_TOKEN from $_token_origin"
    _changed=1
  else
    mlog "migrate: no usable legacy token found; keeping the bundled DEVICE_TOKEN (user must edit it)"
  fi

  # -------------------------------------------------------------------------
  # 2. SERVER_URL from Msg2Bark myserver_url - with the /api/push strip.
  #
  # The old module stored a FULL endpoint (...:5000/api/push) because it POSTed
  # straight to that string. The merged module's SERVER_URL is a BASE url: the
  # bridge appends the endpoint itself. Copying the value verbatim produces
  # .../api/push/api/push and EVERY push 404s. So strip a trailing '/api/push'
  # first, then any trailing '/', giving a clean base for either input form:
  #     http://host:5000/api/push  -> http://host:5000
  #     http://host:5000/api/push/ -> http://host:5000
  #     http://host:5000/          -> http://host:5000
  # Only a URL Msg2Bark actually supplied is rewritten; otherwise the bundled
  # SERVER_URL is left exactly as shipped.
  # -------------------------------------------------------------------------
  if [ -n "$_murl" ]; then
    _base=$(strip_endpoint_suffix "$_murl")
    if [ "$_base" = "$_murl" ]; then
      mlog "migrate: imported SERVER_URL from Msg2Bark myserver_url (no endpoint suffix to strip): $_base"
    else
      mlog "migrate: imported SERVER_URL from Msg2Bark myserver_url, stripped endpoint suffix: $_murl -> $_base"
    fi
    set_config_key "$_stage" SERVER_URL "'$_base'"
    _changed=1
  else
    mlog "migrate: Msg2Bark supplied no myserver_url; keeping the bundled SERVER_URL"
  fi

  # -------------------------------------------------------------------------
  # 3. SWITCHES from Msg2Bark. myserver_switch -> SMS_FORWARD is the one that
  #    fixes the reported trap; wx_switch -> WX_SWITCH comes along for free.
  #    Non-numeric values are refused rather than written, so a mangled legacy
  #    file cannot leave the forwarder with a value it cannot interpret.
  # -------------------------------------------------------------------------
  if is_switch "$_msg_sw"; then
    set_config_key "$_stage" SMS_FORWARD "$_msg_sw"
    mlog "migrate: imported SMS_FORWARD=$_msg_sw from Msg2Bark myserver_switch"
    _changed=1
  elif [ -n "$_msg_sw" ]; then
    mlog "migrate: Msg2Bark myserver_switch='$_msg_sw' is not numeric; refused, bundled SMS_FORWARD kept"
  else
    mlog "migrate: Msg2Bark supplied no myserver_switch; bundled SMS_FORWARD kept"
  fi

  if is_switch "$_msg_wxsw"; then
    set_config_key "$_stage" WX_SWITCH "$_msg_wxsw"
    mlog "migrate: imported WX_SWITCH=$_msg_wxsw from Msg2Bark wx_switch"
    _changed=1
  elif [ -n "$_msg_wxsw" ]; then
    mlog "migrate: Msg2Bark wx_switch='$_msg_wxsw' is not numeric; refused, bundled WX_SWITCH kept"
  fi

  # -------------------------------------------------------------------------
  # 4. WX_WEBHOOK - ONLY when the old value is non-empty. An empty legacy webhook
  #    must not wipe a working bundled one, and writing WX_WEBHOOK='' would be an
  #    explicit "off" for a feature the user may still want.
  # -------------------------------------------------------------------------
  if [ -n "$_msg_wx" ]; then
    set_config_key "$_stage" WX_WEBHOOK "'$_msg_wx'"
    mlog "migrate: imported WX_WEBHOOK from Msg2Bark wx_webhook"
    _changed=1
  else
    mlog "migrate: Msg2Bark wx_webhook empty or absent; bundled WX_WEBHOOK kept"
  fi

  # -------------------------------------------------------------------------
  # 4b. DEVICE_NAME and SHOW_TOAST from the old Clipboard module, when it is the
  #     one that supplied the server identity. DEVICE_NAME is purely cosmetic (the
  #     label the server shows) but importing it means the device keeps the name
  #     the user already recognises instead of reverting to the bundled one, and
  #     SHOW_TOAST preserves their clipboard-toast preference. Only non-empty
  #     values are taken, for the same reason as WX_WEBHOOK below. PROVISION_ID is
  #     deliberately NOT in this list (see the note at the end of the function).
  # -------------------------------------------------------------------------
  if [ -z "$_msg_src" ] && [ -n "$_clip_src" ]; then
    _clip_name=$(cfg_get "$_clip_src" DEVICE_NAME)
    if [ -n "$_clip_name" ]; then
      set_config_key "$_stage" DEVICE_NAME "'$_clip_name'"
      mlog "migrate: imported DEVICE_NAME from old Clipboard DEVICE_NAME"
      _changed=1
    fi
    _clip_toast=$(cfg_get "$_clip_src" SHOW_TOAST)
    if is_switch "$_clip_toast"; then
      set_config_key "$_stage" SHOW_TOAST "$_clip_toast"
      mlog "migrate: imported SHOW_TOAST=$_clip_toast from old Clipboard SHOW_TOAST"
      _changed=1
    fi
  fi

  # -------------------------------------------------------------------------
  # 5. DROPPED KEYS. Bark is removed by design and the two DB paths belonged to
  #    the old periodic SQLite scan, which no longer exists. Remove them from the
  #    staged target so a stale value cannot be picked up by anything that still
  #    looks for it, and name each one in the log so the user can see what left.
  # -------------------------------------------------------------------------
  _drop_tmp="$_stage.drop"
  rm -f "$_drop_tmp" 2>/dev/null
  while IFS= read -r _line; do
    case "$_line" in
      bark_[A-Za-z0-9_]*=*|msg_db_path=*|call_db_path=*)
        mlog "migrate: dropped legacy key ${_line%%=*}"
        ;;
      *)
        printf '%s\n' "$_line" >> "$_drop_tmp" 2>/dev/null
        ;;
    esac
  done < "$_stage"
  # Rewrite even when nothing was dropped: the file content is identical, and
  # doing it unconditionally keeps the file's mtime/ownership path uniform.
  if [ -f "$_drop_tmp" ]; then
    mv -f "$_drop_tmp" "$_stage" 2>/dev/null || {
      rm -f "$_drop_tmp" 2>/dev/null
      mlog "migrate: WARNING could not rewrite staged config; legacy keys may survive"
    }
  else
    # No line survived the filter, i.e. the file was nothing but dropped keys.
    # Truncating is the correct outcome; leaving the originals would reintroduce
    # exactly the keys this step exists to remove.
    : > "$_stage" 2>/dev/null
  fi

  # NOT copied on purpose: PROVISION_ID. The old Clipboard module's id is the
  # marker the merged ZIP happens to share, so copying it would pin the device to
  # the old config forever. The bundled id is left untouched, which is what lets a
  # future ZIP refresh this device normally.

  # -------------------------------------------------------------------------
  # 6. COMMIT. The result is left in $MIGRATION_STAGE for the provisioning block
  #    to install — migrate_legacy() never writes $SAVED_CONFIG itself, because
  #    two writers was measured to lose the imported values (see the note above
  #    the provisioning block). Only a file that still looks like one of our
  #    configs is staged, so a truncated write can never replace a working config.
  # -------------------------------------------------------------------------
  if [ "$_changed" != "1" ]; then
    rm -f "$_stage" 2>/dev/null
    mlog "migrate: no importable legacy values; nothing written"
    return 0
  fi
  if ! grep -q '^[ 	]*SERVER_URL=' "$_stage" 2>/dev/null; then
    rm -f "$_stage" 2>/dev/null
    mlog "migrate: WARNING staged config has no SERVER_URL; refusing to install it"
    return 1
  fi

  # $_stage IS $MIGRATION_STAGE: it was staged there at the top of this function
  # and rewritten in place, so there is nothing to move.
  if [ "$_have_saved" = "1" ]; then
    mlog "migrate: DONE imported legacy settings; staged for provisioning at $MIGRATION_STAGE"
  else
    mlog "migrate: DONE imported legacy settings for first-time provisioning at $MIGRATION_STAGE"
  fi
  return 0
}

# Turn a full push endpoint into the base URL the merged module expects.
# Belt and braces on purpose: sed cannot express an alternation in a portable
# POSIX way, so the two spellings are handled by two case patterns — `*/api/push`
# (no trailing slash) and `*/api/push/` — both leaving exactly the base.
strip_endpoint_suffix() {
  _u="$1"
  case "$_u" in
    */api/push) _u=${_u%/api/push} ;;
  esac
  case "$_u" in
    */api/push/) _u=${_u%/api/push/} ;;
  esac
  case "$_u" in
    */) _u=${_u%/} ;;
  esac
  printf '%s' "$_u"
}

# Replace <KEY> in <file> with <value>, appending the line when the key is absent.
# The whole match is consumed, so a `KEY=value  # old comment` line cannot leave
# its comment behind on the new line.
#
# The append branch checks that the file ends with a newline BEFORE appending.
# Without that check the new line is glued onto the last existing one
# (`SHOW_TOAST=1SMS_FORWARD=1`), which was measured on device: the Java parser
# then reads SMS_FORWARD as unset and the trap this task exists to fix comes back.
# A config file that does not end in a newline is entirely legal, so this cannot
# be left to the file's author.
set_config_key() {
  set_config_key_file="$1"
  set_config_key_name="$2"
  set_config_key_value="$3"
  set_config_key_tmp="$set_config_key_file.sk"
  rm -f "$set_config_key_tmp" 2>/dev/null
  sed "s|^[ 	]*$set_config_key_name=.*|$set_config_key_name=$set_config_key_value|" \
      "$set_config_key_file" > "$set_config_key_tmp" 2>/dev/null || return 1
  if grep -q "^[ 	]*$set_config_key_name=" "$set_config_key_tmp" 2>/dev/null; then
    mv -f "$set_config_key_tmp" "$set_config_key_file" 2>/dev/null || {
      rm -f "$set_config_key_tmp" 2>/dev/null
      return 1
    }
  else
    rm -f "$set_config_key_tmp" 2>/dev/null
    # Guarantee a line boundary first. `[ -s file ]` is true for a non-empty file
    # of any size; reading its last byte is done with tail -c 1 and compared to a
    # literal newline produced by printf, so no `$'\n'` (a bashism) is needed.
    if [ -s "$set_config_key_file" ]; then
      _nl=$(printf '\n')
      _last=$(tail -c 1 "$set_config_key_file" 2>/dev/null)
      [ "$_last" = "$_nl" ] || printf '\n' >> "$set_config_key_file" 2>/dev/null
    fi
    printf '%s=%s\n' "$set_config_key_name" "$set_config_key_value" >> "$set_config_key_file" 2>/dev/null || return 1
  fi
  return 0
}

# --- removal of the old modules (one-click update) --------------------------
#
# WHY THIS EXISTS
# ---------------
# The upgrade should not leave the user with two dead modules still installed,
# still enabled, still mounting. After the settings have been imported the old
# modules are asked to remove themselves.
#
# SAFETY RULES — all of these are load-bearing, do not "simplify" them away:
#   * The module id is hardcoded per call site and this module's own id is refused
#     outright. Deriving the id from $MODDIR would be worse, not better: the same
#     functions are exercised from a copy of this script in the device sandbox
#     test, where $MODDIR points at a scratch directory.
#   * Only a directory that actually exists is touched, so a phone that never had
#     the old modules is a silent no-op (which is also the second-boot case).
#   * Only the two legacy directories are ever removed. $STATE_DIR
#     (/data/adb/clipboard-sync) is shared state — the new config, the sms cursor,
#     the pidfiles and the log all live there — so it is never deleted here even
#     though it still carries an old name.
#   * Every step is best-effort: failures log and CONTINUE. A cleanup that cannot
#     run must never break forwarding, so nothing here can abort the boot.
#   * Nothing here requires a reboot to be safe: the legacy processes are killed
#     directly, and the `remove` marker only deletes the module tree at the next
#     boot. Migration has already written the new config by the time this runs, so
#     a failure can never leave the device with neither config nor old module.
#   * The pkill patterns below deliberately do not contain "clipsyncd" or our own
#     bridge classes. "clipboard-syncd" (the OLD supervisor's name) does not match
#     "clipsyncd" (ours), and "clipsync.bridge.Main"/"clipsync.bridge.SmsForwarder"
#     are matched exactly — so the running merged module cannot be hit. Note also
#     that only the active branch of a case is ever run, so this cannot kill the
#     processes it is about to start.
remove_legacy_modules() {
  for _id in "$LEGACY_CLIP_ID" "$LEGACY_MSG_ID"; do
    _dir="$MODULES_DIR/$_id"

    # Belt and braces: this function is the one place that can delete things, so
    # it refuses to touch this module even if a caller or a future edit ever
    # passes the wrong id.
    if [ "$_id" = "$MY_MODULE_ID" ]; then
      mlog "remove: refusing to remove the running module ($_id)"
      continue
    fi

    if [ ! -d "$_dir" ]; then
      mlog "remove: $_id not installed; nothing to do"
      continue
    fi

    mlog "remove: requesting removal of legacy module $_id"

    # APatch's own CLI, tried first: it is what exists on this device and it is
    # the only mechanism that reports a result we can log.
    _apd=$(command -v apd 2>/dev/null)
    [ -n "$_apd" ] || _apd=/data/adb/ap/bin/apd
    if [ -x "$_apd" ]; then
      # Note: on this device `apd module uninstall` exits 0 even for an unknown
      # id, so the exit status is only reported, never trusted.
      if "$_apd" module uninstall "$_id" >/dev/null 2>&1; then
        mlog "remove: apd module uninstall $_id returned success"
      else
        mlog "remove: apd module uninstall $_id failed; falling back to the remove marker"
      fi
    else
      mlog "remove: no apd CLI found; falling back to the remove marker"
    fi

    # Magisk/KernelSU convention, understood by APatch's module manager as well:
    # an empty `remove` file in the module directory asks the next boot to delete
    # the module tree. Safe and idempotent — a marker that is already there just
    # stays there.
    if : > "$_dir/remove" 2>/dev/null; then
      mlog "remove: marked $_id for removal via $_dir/remove (applied at next boot)"
    else
      mlog "remove: WARNING could not create $_dir/remove; $_id will stay installed (boot continues)"
    fi
  done

  # Stop whatever the old modules left running, exercised ALWAYS (not only when a
  # module directory is still present) so a phone whose modules were already
  # deleted by hand does not keep an orphan process alive forever.
  #
  # 'mf.sh' is the old Msg2Bark one-shot loop and 'clipboard-syncd' is the old
  # Clipboard supervisor — it contains "clipboard-sync" but NOT "clipsyncd", which
  # is our own supervisor and would otherwise be killed here. Our bridges are the
  # only other candidate and they are named exactly, so they are not matched by
  # accident. Failures are ignored: `pkill` exits non-zero when nothing matched,
  # which on a healthy upgraded device is the normal case.
  pkill -9 -f 'mf\.sh' >/dev/null 2>&1
  pkill -9 -f 'clipboard-syncd' >/dev/null 2>&1
  mlog "remove: legacy process cleanup attempted (mf.sh, clipboard-syncd)"
  return 0
}

# Import the old modules' settings FIRST, before anything is staged into the
# runtime directories, so the values the bridges read are already the imported
# ones. Failure is tolerated on purpose: an unmigratable device boots with the
# bundled config instead of not booting at all.
#
# migrate_legacy() only ever writes $MIGRATION_STAGE. The provisioning block
# below is the SINGLE writer of $SAVED_CONFIG — see the note there for why.
SAVED_PRE_MERGE=0
if [ -f "$SAVED_CONFIG" ] && ! cfg_has_key SMS_FORWARD "$SAVED_CONFIG"; then
  SAVED_PRE_MERGE=1
fi
migrate_legacy

# --- config provisioning ---------------------------------------------------
# The bundled config is the "factory" copy. It is installed when there is no
# saved config yet, or when its PROVISION_ID differs from the id recorded in the
# marker file — that lets a new ZIP with a new PROVISION_ID refresh a device
# without clobbering user edits on every single boot.
#
# It is ALSO installed when the saved config predates the merge (no SMS_FORWARD
# key), which is checked ABOVE with $SAVED_PRE_MERGE, before migrate_legacy()
# runs. That order matters twice over:
#
#   * $SAVED_PRE_MERGE must reflect the REAL on-disk state. Checking it after the
#     import would always read "false", because the import's whole job is to add
#     SMS_FORWARD — the trap would then re-open on exactly the devices this
#     change exists to fix.
#   * $SAVED_CONFIG has exactly ONE writer, this block. When a legacy import ran,
#     $MIGRATION_STAGE holds the merged result and is installed instead of the
#     bundled file. A second writer (migration writing $SAVED_CONFIG directly and
#     provisioning overwriting it afterwards) was measured to destroy the
#     imported token: the ZIP-refresh branch then copied the bundled config over
#     the freshly imported one, reverting DEVICE_TOKEN to the placeholder and
#     SERVER_URL to the unused IP form.
PROVISION_ID=""
if [ -f "$BUNDLED_CONFIG" ]; then
  # shellcheck disable=SC1090
  . "$BUNDLED_CONFIG"
fi

SAVED_PROVISION_ID=""
[ -f "$PROVISION_MARKER" ] && SAVED_PROVISION_ID=$(cat "$PROVISION_MARKER")

if [ ! -f "$SAVED_CONFIG" ] \
   || { [ -n "$PROVISION_ID" ] && [ "$PROVISION_ID" != "$SAVED_PROVISION_ID" ]; } \
   || [ "$SAVED_PRE_MERGE" = "1" ]; then
  if [ -f "$MIGRATION_STAGE" ]; then
    # A pre-merge config was imported; install the merged result.
    cp "$MIGRATION_STAGE" "$SAVED_CONFIG"
    rm -f "$MIGRATION_STAGE" 2>/dev/null
    : > "$MIGRATION_MARKER" 2>/dev/null
    chmod 0600 "$MIGRATION_MARKER" 2>/dev/null
    if [ "$SAVED_PRE_MERGE" = "1" ]; then
      mlog "provision: installed imported legacy config over the pre-merge saved config"
    else
      mlog "provision: installed imported legacy config"
    fi
  else
    cp "$BUNDLED_CONFIG" "$SAVED_CONFIG"
  fi
  chown 0:2000 "$SAVED_CONFIG"
  chmod 0640 "$SAVED_CONFIG"
  if [ -n "$PROVISION_ID" ]; then
    printf '%s' "$PROVISION_ID" > "$PROVISION_MARKER"
    chmod 600 "$PROVISION_MARKER"
  fi
fi

# Remove the old modules LAST, and only now that the merged config is on disk.
# This ordering is the whole reason removal cannot brick the boot path: if the
# migration above failed, the old module is still installed and still working.
remove_legacy_modules

# --- runtime staging -------------------------------------------------------
# /data/adb is intentionally inaccessible to Android's shell UID (1000), so the
# jar and the personalized config are staged in a shell-owned directory instead.
#
# TWO directories, one per bridge UID, because the two bridges need different
# ownership and the config holds this device's token:
#
#   $RUNTIME_DIR      owner 1000:1000, dir 0700, files 0600  -> clipboard bridge
#   $RUNTIME_DIR_SMS  owner 0:0,      dir 0777, files 0644  -> SMS forwarder
#
# Why the SMS directory is 0777 and not 0755 — measured on the device:
#   UID 2000 (shell) is NOT group-granted anything useful. `su 2000 -c id` gives
#       uid=2000(shell) gid=0(root) groups=0(root)
#   i.e. gid is root, not shell, so GROUP permission bits never apply to it. The
#   only bits that can grant UID 2000 access are the OTHER bits (or ownership).
#   Read is not enough here: the SMS forwarder must PERSIST its cursor and an
#   ownership marker, so it needs WRITE. With 0755 it could read but every write
#   failed, so the directory is 0777.
#
# SECURITY TRADEOFF (accepted, deliberately): a 0777 directory means any UID on
# the device could tamper with the SMS cursor. That is accepted because the
# alternative is a cursor that cannot persist at all, and the Java side adds a
# corrupt-cursor sanity check that re-derives a safe baseline instead of trusting
# the file. Only this one directory is widened; the clipboard staging keeps its
# 0700/1000:1000 privacy, which is why the directories are kept separate.
#
# Note /data/adb itself stays untouched: it is drwx------ root:root and UID 2000
# cannot write there even with 0777 on a child directory, so the SMS cursor
# cannot live in $STATE_DIR.
mkdir -p "$RUNTIME_DIR" "$RUNTIME_DIR_SMS"
cp "$MODDIR/framework/clipbridge.jar" "$RUNTIME_DIR/clipbridge.jar"
cp "$MODDIR/framework/clipbridge.jar" "$RUNTIME_DIR_SMS/clipbridge.jar"
cp "$SAVED_CONFIG" "$RUNTIME_DIR/config.conf"
cp "$SAVED_CONFIG" "$RUNTIME_DIR_SMS/config.conf"

chown -R 1000:1000 "$RUNTIME_DIR"
chmod 0700 "$RUNTIME_DIR"
chmod 0600 "$RUNTIME_DIR/clipbridge.jar" "$RUNTIME_DIR/config.conf"

chown -R 0:0 "$RUNTIME_DIR_SMS"
chmod 0777 "$RUNTIME_DIR_SMS"
chmod 0644 "$RUNTIME_DIR_SMS/clipbridge.jar" "$RUNTIME_DIR_SMS/config.conf"

# Pre-create the SMS state file UID 2000 writes to, with the correct mode and
# ownership, instead of leaving it to be created on first write. Creating it up
# front is more reliable than depending on the process umask, and it means the
# very first cursor update on a fresh install cannot fail because the path does
# not exist yet or because the containing directory mode was momentarily wrong.
#
# 0666 so UID 2000 can rewrite it in place. Left owned by root: the mode alone
# grants the write it needs, and root ownership means the file cannot be
# unlinked-and-replaced by another UID purely on the strength of the 0777 parent.
# Existing content is preserved — this must never clobber a live cursor, so the
# file is only created when absent.
SMS_STATE_FILE="$RUNTIME_DIR_SMS/sms_state"
if [ ! -e "$SMS_STATE_FILE" ]; then
  : > "$SMS_STATE_FILE"
  chown 0:0 "$SMS_STATE_FILE" 2>/dev/null || true
fi
chmod 0666 "$SMS_STATE_FILE" 2>/dev/null || true

# --- hostname resolution for the WeChat leg --------------------------------
# MEASURED ON DEVICE (Android 16): netd refuses DNS for the non-app UIDs the
# bridges run as. With the phone on wifi:
#     root     -> resolves qyapi.weixin.qq.com : YES
#     UID 1000, 1013, 2000, 1020               : NO
# while plain IP networking still works (the cloud push is fine because its URL
# is an IP literal). So the cloud leg needs no DNS and the WeChat leg always
# fails with UnknownHostException, because WX_WEBHOOK is a hostname.
#
# The fix is to seed a hosts file and bind-mount it over /system/etc/hosts.
# /system/etc/hosts lives on a read-only filesystem, so the bind mount is what
# makes it possible, and a bind mount does NOT survive a reboot — which is
# exactly why this has to be re-applied here on every boot.
#
# This step is strictly best-effort. If it fails we log one clear warning and
# carry on: clipboard sync and the cloud SMS push must keep working, and only
# the WeChat leg degrades. The module is never aborted over this.
HOSTS_FILE="$STATE_DIR/hosts"
HOSTS_MOUNT=/system/etc/hosts

# Resolve a hostname as root and print a validated dotted quad, or nothing.
# `ping` is used as the resolver because it is always present and needs no
# toolbox/busybox dependency.
#
# The parse is deliberately strict, and the validation is not optional:
# Android's ping prints  `PING host (1.2.3.4) 56(84) bytes of data.`, and a
# greedy `sed 's/.*(\([0-9.]*\)).*/\1/'` matches the LAST parenthesis, yielding
# "84" instead of the address. Verified on device. A bare grep for a dotted quad
# avoids the ambiguity entirely, and the explicit octet check below means a
# malformed digest can never be written into a system hosts file.
resolve_host() {
  _ip=$(ping -c1 -W2 "$1" 2>/dev/null \
        | grep -o '[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*' \
        | head -n 1)
  case "$_ip" in
    '') return 1 ;;
  esac
  # reject anything that is not exactly four 0-255 octets
  _o1=${_ip%%.*}; _rest=${_ip#*.}
  [ "$_rest" = "$_ip" ] && return 1
  _o2=${_rest%%.*}; _rest=${_rest#*.}
  [ "$_rest" = "${_rest#*.}" ] && return 1
  _o3=${_rest%%.*}; _o4=${_rest#*.}
  [ "$_o4" = "$_rest" ] && return 1
  for _o in "$_o1" "$_o2" "$_o3" "$_o4"; do
    case "$_o" in
      ''|*[!0-9]*) return 1 ;;
    esac
    [ "$_o" -le 255 ] || return 1
  done
  printf '%s' "$_ip"
}

# Last known good IP for a host, read back from the hosts file we wrote on a
# previous boot. This is the fallback for the case where service.sh runs before
# the network is up: the resolve above then returns nothing and we must still be
# able to produce a usable hosts entry.
known_host_ip() {
  [ -f "$HOSTS_FILE" ] || return 1
  _k=$(sed -n "s/^\([0-9][0-9.]*\)[ 	][ 	]*$1\$/\1/p" "$HOSTS_FILE" | head -n 1)
  [ -n "$_k" ] || return 1
  printf '%s' "$_k"
}

# One entry per line: "<hostname>". Parsed with a newline-delimited read rather
# than a word loop, because the names are separated by spaces in a variable and a
# plain `for` would iterate words instead of entries (which wrote each host twice).
HOSTS_HOSTS='qyapi.weixin.qq.com'

# The sync server ALSO has to be in here when SERVER_URL is a hostname.
#
# This is not obvious and it bit the real upgrade path: an older forwarder stored a
# FULL endpoint such as
#   http://sync.example.com:5000/api/push
# so a migrated config can carry a HOSTNAME, not an IP. Every bridge runs as UID
# 1000/2000, and Android 16 gives those UIDs no resolver (only root has one), so
# the forwarder logged
#   initial sync failed: java.net.UnknownHostException: Unable to resolve host
# and the cloud push silently did nothing. Measured on device: the same request
# returns HTTP 000 by hostname and HTTP 403 (i.e. reached the server) by IP.
#
# So SERVER_URL's host is extracted and added to the hosts list alongside the
# webhook host. When SERVER_URL is already an IP literal this adds nothing, which
# is why the bundled default (an IP) needs no entry.
server_url_host() {
  _u=$(cfg_get "$SAVED_CONFIG" SERVER_URL)
  [ -n "$_u" ] || return 1
  # strip scheme, then everything from the first '/' or ':' onward
  _h=$(printf '%s' "$_u" | sed -e 's#^[A-Za-z][A-Za-z0-9+.-]*://##' -e 's#[/:].*$##')
  [ -n "$_h" ] || return 1
  # only a name needs an entry; an IPv4 literal resolves without one
  case "$_h" in
    *[!0-9.]*) ;;
    *) return 1 ;;
  esac
  # must look like a hostname, not something that would corrupt the hosts file
  case "$_h" in
    *[!A-Za-z0-9.-]*) return 1 ;;
  esac
  printf '%s' "$_h"
}

apply_hosts() {
  # Skip if our bind mount is already in place. service.sh can be re-run (module
  # update, manual invocation) and a second bind mount would stack up.
  if mount 2>/dev/null | grep -q " $HOSTS_MOUNT "; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') hosts: $HOSTS_MOUNT already bind-mounted; skipping" >> "$LOG_FILE"
    return 0
  fi

  _tmp="$HOSTS_FILE.new"
  # localhost must never be dropped
  printf '127.0.0.1 localhost\n' > "$_tmp" 2>/dev/null || return 1

  # Build the effective host list: the webhook host(s) plus SERVER_URL's host when
  # it is a name.
  #
  # NOTE: the list is NEWLINE-delimited, not space-separated. The consumer below
  # reads it with `while IFS= read -r`, one hostname per line. Joining with spaces
  # produces a single line 'a.example b.example', which then fails the
  # no-whitespace guard and every host is skipped - observed on the device as
  # "hosts: nothing resolved" while root could resolve both names perfectly.
  _hostlist="$HOSTS_HOSTS"
  _srvhost=$(server_url_host)
  if [ -n "$_srvhost" ]; then
    # Append only when not already listed. grep -x -F matches a whole line, so a
    # name that merely contains another cannot produce a false positive.
    if ! printf '%s\n' "$_hostlist" | grep -q -x -F -- "$_srvhost" 2>/dev/null; then
      _hostlist="$_hostlist
$_srvhost"
    fi
  fi

  _resolved=0
  # Newline-delimited so each line is one hostname, not one word. A hostname must
  # be non-empty and contain no whitespace, otherwise it is skipped.
  printf '%s\n' "$_hostlist" | while IFS= read -r _host; do
    [ -n "$_host" ] || continue
    case "$_host" in
      *[!A-Za-z0-9.-]*) continue ;;
    esac
    _ip=$(resolve_host "$_host")
    if [ -z "$_ip" ]; then
      _ip=$(known_host_ip "$_host")
      if [ -n "$_ip" ]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') hosts: $_host not resolvable now (network not up?); reusing last known $_ip" >> "$LOG_FILE"
      else
        echo "$(date '+%Y-%m-%d %H:%M:%S') hosts: $_host not resolvable and no previous IP; skipped" >> "$LOG_FILE"
        continue
      fi
    else
      _resolved=$((_resolved + 1))
      echo "$(date '+%Y-%m-%d %H:%M:%S') hosts: resolved $_host -> $_ip" >> "$LOG_FILE"
    fi
    printf '%s %s\n' "$_ip" "$_host" >> "$_tmp"
  done

  # A `while` in a pipeline runs in a subshell, so _resolved does not survive it.
  # Decide from the file instead: localhost plus at least one host entry.
  _entries=$(grep -c '^[0-9]' "$_tmp" 2>/dev/null | tr -d ' \t')
  case "$_entries" in
    ''|*[!0-9]*) _entries=0 ;;
  esac

  # If no host could be added the file is just localhost. Installing that would
  # replace the real system hosts file with something strictly less useful, so
  # leave the system file alone in that case.
  if [ "$_entries" -lt 2 ] && [ ! -f "$HOSTS_FILE" ]; then
    rm -f "$_tmp" 2>/dev/null
    echo "$(date '+%Y-%m-%d %H:%M:%S') hosts: nothing resolved and no previous hosts file; not mounting" >> "$LOG_FILE"
    return 1
  fi

  mv -f "$_tmp" "$HOSTS_FILE" 2>/dev/null || return 1
  chmod 0644 "$HOSTS_FILE" 2>/dev/null

  if mount --bind "$HOSTS_FILE" "$HOSTS_MOUNT" 2>/dev/null; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') hosts: bind-mounted $HOSTS_FILE over $HOSTS_MOUNT" >> "$LOG_FILE"
    return 0
  fi

  # ONE warning, then continue. Clipboard + cloud SMS push are unaffected.
  echo "$(date '+%Y-%m-%d %H:%M:%S') hosts: WARNING bind-mount of $HOSTS_MOUNT failed; WeChat push may fail with UnknownHostException. Clipboard and cloud SMS push unaffected" >> "$LOG_FILE"
  return 1
}

# --- apply the hosts fix, with a bounded retry ------------------------------
#
# Applying this ONCE is not enough. service.sh waits for sys.boot_completed,
# which says the framework is up but says NOTHING about wifi/data being
# connected. Measured on the real device after a reboot:
#   15:28:17 hosts: qyapi.weixin.qq.com not resolvable and no previous IP; skipped
#   15:28:17 hosts: nothing resolved and no previous hosts file; not mounting
# ...and because apply_hosts is called exactly once, WeChat push stayed dead for
# the whole uptime (HTTP 000 to the webhook) even though root could resolve the
# name perfectly a minute later. The cloud leg survived only because SERVER_URL is
# an IP literal that needs no resolver.
#
# So retry in the BACKGROUND: the bridges must start immediately, and the hosts
# entry only becomes possible once the network is up. A background retry keeps
# this from delaying startup while still converging within the first minute.
#
# Bounded, so a device that never gets a network does not spin forever: 20 tries
# at 15s is ~5 minutes, after which one final warning is logged.
apply_hosts_bg() {
  _try=1
  while [ "$_try" -le 20 ]; do
    # Already mounted (a previous attempt or another instance won): nothing to do.
    if mount 2>/dev/null | grep -q " $HOSTS_MOUNT "; then
      return 0
    fi
    if apply_hosts >/dev/null 2>&1; then
      return 0
    fi
    # Only keep retrying while the failure looks like "network not up yet".
    # If we are past that and STILL cannot resolve, a further 5 minutes of
    # attempts will not help either, so stop early with one clear line.
    if ping -c1 -W2 qyapi.weixin.qq.com >/dev/null 2>&1; then
      echo "$(date '+%Y-%m-%d %H:%M:%S') hosts: network is up but apply_hosts still failed; giving up after attempt $_try" >> "$LOG_FILE"
      return 1
    fi
    _try=$((_try + 1))
    sleep 15
  done
  echo "$(date '+%Y-%m-%d %H:%M:%S') hosts: gave up after 20 attempts (~5 min); WeChat push will fail with UnknownHostException. Clipboard and cloud SMS push unaffected" >> "$LOG_FILE"
  return 1
}

# First attempt in the foreground (fast path when the network is already up, and
# it keeps the common case's log lines in boot order), the rest in the background.
if ! apply_hosts; then
  apply_hosts_bg &
fi

# --- restart the supervisor ------------------------------------------------
# Stop the previous supervisor first so it cannot restart the old bridge while
# this service instance is installing the updated runtime.
#
# Nothing here matches the bare pattern 'clipsyncd': that would also match this
# script's own cmdline (`sh /data/adb/modules/sms_clipboard_sync/service.sh` on
# some shells) and, worse, the supervisor we are about to start. Everything is
# killed by explicit pid, or by the bridge CLASS name, neither of which can
# appear in this script's own cmdline.
for _pf in "$STATE_DIR/.clipsyncd.clip.loop.pid" "$STATE_DIR/.clipsyncd.sms.loop.pid"; do
  _lp=$(cat "$_pf" 2>/dev/null)
  case "$_lp" in
    ''|*[!0-9]*) ;;
    *) kill -9 "$_lp" >/dev/null 2>&1 || true ;;
  esac
done
if [ -f "$PID_FILE" ]; then
  OLD_PID=$(cat "$PID_FILE" 2>/dev/null)
  case "$OLD_PID" in
    ''|*[!0-9]*) ;;
    *) kill -9 "$OLD_PID" >/dev/null 2>&1 || true ;;
  esac
fi
rm -f "$STATE_DIR"/.clipsyncd.*.pid "$STATE_DIR/clipboard-syncd.bridge.pid" 2>/dev/null
sleep 1
pkill -9 -f 'com.clipsync.bridge.Main' >/dev/null 2>&1 || true
pkill -9 -f 'com.clipsync.bridge.SmsForwarder' >/dev/null 2>&1 || true
sleep 1
nohup "$MODDIR/system/bin/clipsyncd" "$MODDIR" </dev/null >/dev/null 2>&1 &
