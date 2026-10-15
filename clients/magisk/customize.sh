#!/system/bin/sh
# SMS + Clipboard Sync - customize.sh (install-time script)
#
# Follows the Magisk/APatch module install convention (same shape as the fp-quiet
# module on this device), and also runs fine when invoked by hand.
#
# WHY THIS EXISTS: the migration itself happens at BOOT (service.sh), because the
# legacy module may still be mounted while this script runs and the user has to
# reboot anyway. But the user wants to know AT INSTALL TIME whether their old
# settings were found and whether the old modules will be removed. So this script
# does the same discovery the boot-time migration does, and PRINTS it, without
# writing anything. The boot path then performs the actual import.
#
# Read-only on purpose: nothing here modifies the saved config. Printing a value
# that later differs from what migration imports would be worse than printing
# nothing, so the discovery logic mirrors service.sh's exactly.

SKIPMOUNT=false
PROPFILE=false
POSTFSDATA=false
LATESTARTSERVICE=true

# ui_print is provided by the installer; fall back to echo when run manually.
if ! command -v ui_print >/dev/null 2>&1; then
  ui_print() { echo "$1"; }
fi

[ -z "$MODPATH" ] && MODPATH="${0%/*}"

VERSION=$(sed -n 's/^version=//p' "$MODPATH/module.prop" 2>/dev/null | head -n 1)

STATE_DIR=/data/adb/clipboard-sync
SAVED_CONFIG="$STATE_DIR/config.conf"
MIGRATION_MARKER="$STATE_DIR/.migrated-v2"
LEGACY_CLIP_DIR=/data/adb/modules/clipboard_sync_uploader
LEGACY_MSG_DIR=/data/adb/modules/Msg_2_Bark
BUNDLED_CONFIG="$MODPATH/config.conf"

# Read a value from a config file, stripping one pair of surrounding quotes.
# Mirrors the Java/shell parser: split on the FIRST '=', trim, unquote.
cfg_get() {
  _file="$1"; _key="$2"
  [ -f "$_file" ] || return 0
  sed -n "s/^$_key=['\"]\{0,1\}\([^'\"]*\).*/\1/p" "$_file" 2>/dev/null | head -n 1
}

# Strip a trailing /api/push (and then any trailing slash) from an old-style
# full endpoint, so it becomes the BASE url the merged client expects.
strip_endpoint() {
  printf '%s' "$1" | sed -e 's#/api/push/*$##' -e 's#/*$##'
}

ui_print " "
ui_print "***********************************************"
ui_print "   SMS + Clipboard Sync ${VERSION:-?}"
ui_print "***********************************************"
ui_print " "

# ---------------------------------------------------------------- detection --
MSG_SRC=""
for _c in "$LEGACY_MSG_DIR/config.conf" "$LEGACY_MSG_DIR/config.conf.bak"; do
  if [ -f "$_c" ] && [ -r "$_c" ]; then MSG_SRC="$_c"; break; fi
done
CLIP_PRESENT=0
[ -d "$LEGACY_CLIP_DIR" ] && [ -f "$LEGACY_CLIP_DIR/module.prop" ] && CLIP_PRESENT=1
MSG_PRESENT=0
[ -d "$LEGACY_MSG_DIR" ] && [ -f "$LEGACY_MSG_DIR/module.prop" ] && MSG_PRESENT=1

SAVED_PRE_MERGE=0
if [ -f "$SAVED_CONFIG" ] && ! grep -q '^SMS_FORWARD' "$SAVED_CONFIG" 2>/dev/null; then
  SAVED_PRE_MERGE=1
fi

ALREADY_MIGRATED=0
[ -f "$MIGRATION_MARKER" ] && ALREADY_MIGRATED=1

# --------------------------------------------------------------- legacy path --
if [ "$MSG_PRESENT" = "1" ] || [ "$CLIP_PRESENT" = "1" ] || [ "$SAVED_PRE_MERGE" = "1" ]; then
  ui_print "- 检测到旧版模块，本次为升级安装"
  ui_print " "
  [ "$CLIP_PRESENT" = "1" ] && ui_print "  [发现] Clipboard Sync Uploader (旧剪贴板同步)"
  [ "$MSG_PRESENT" = "1" ] && ui_print "  [发现] Msg2Bark (旧短信转发)"
  if [ -n "$MSG_SRC" ]; then
    ui_print "  [发现] Msg2Bark 配置: $MSG_SRC"
  elif [ "$MSG_PRESENT" = "1" ]; then
    ui_print "  [警告] Msg2Bark 已安装但配置不可读，将只导入剪贴板模块的设置"
  fi
  ui_print " "

  ui_print "- 将自动导入以下设置（重启后生效）:"
  ui_print " "

  # --- server URL -----------------------------------------------------------
  _url=""
  _src_url=""
  if [ -n "$MSG_SRC" ]; then
    _raw=$(cfg_get "$MSG_SRC" myserver_url)
    if [ -n "$_raw" ]; then
      _url=$(strip_endpoint "$_raw")
      _src_url="Msg2Bark"
    fi
  fi
  if [ -z "$_url" ]; then
    _raw=$(cfg_get "$SAVED_CONFIG" SERVER_URL)
    [ -n "$_raw" ] && { _url="$_raw"; _src_url="旧剪贴板模块"; }
  fi
  if [ -n "$_url" ]; then
    ui_print "    服务器地址 : $_url"
    ui_print "                 (来自 $_src_url)"
  else
    ui_print "    服务器地址 : 未找到 - 将保留模块自带值，需手工填写"
  fi

  # --- token ----------------------------------------------------------------
  _tok=""
  _src_tok=""
  if [ -n "$MSG_SRC" ]; then
    _tok=$(cfg_get "$MSG_SRC" myserver_token)
    [ -n "$_tok" ] && _src_tok="Msg2Bark"
  fi
  if [ -z "$_tok" ]; then
    _tok=$(cfg_get "$SAVED_CONFIG" DEVICE_TOKEN)
    [ -n "$_tok" ] && _src_tok="旧剪贴板模块"
  fi
  if [ -n "$_tok" ]; then
    # Show a prefix only: this is a secret, and the install UI can be screenshotted.
    _tok_show=$(printf '%s' "$_tok" | cut -c1-8)
    _tok_len=$(printf '%s' "$_tok" | wc -c | tr -d ' \t')
    ui_print "    设备 Token : ${_tok_show}...  (共 ${_tok_len} 位)"
    ui_print "                 (来自 $_src_tok)"
  else
    ui_print "    设备 Token : 未找到 - 将保留模块自带值，需手工填写"
  fi

  # --- SMS switch -----------------------------------------------------------
  _sw=$(cfg_get "$MSG_SRC" myserver_switch)
  case "$_sw" in
    1) ui_print "    短信转发   : 开启 (1)" ;;
    0) ui_print "    短信转发   : 关闭 (0)  <- 旧模块里是关闭的" ;;
    *) ui_print "    短信转发   : 开启 (默认)" ;;
  esac

  # --- WeChat ---------------------------------------------------------------
  _wxsw=$(cfg_get "$MSG_SRC" wx_switch)
  _wxurl=$(cfg_get "$MSG_SRC" wx_webhook)
  if [ -n "$_wxurl" ]; then
    if [ "$_wxsw" = "0" ]; then
      ui_print "    企业微信   : 已找到 Webhook，但旧模块里是关闭的 (0)"
    else
      ui_print "    企业微信   : 开启 (1)"
    fi
  else
    ui_print "    企业微信   : 未找到 Webhook，保持关闭"
  fi

  # --- dropped keys ---------------------------------------------------------
  if [ -n "$MSG_SRC" ] && grep -q '^bark_switch' "$MSG_SRC" 2>/dev/null; then
    ui_print " "
    ui_print "    (旧 Bark 配置将被丢弃 - 新版本已移除 Bark 功能)"
  fi

  ui_print " "
  if [ "$ALREADY_MIGRATED" = "1" ]; then
    ui_print "  注意: 已经迁移过一次，本次不会重复导入，你手工改过的设置会被保留"
  fi

else
  ui_print "- 全新安装（未检测到旧版模块）"
  ui_print " "
  ui_print "  模块自带一份默认配置。安装后请编辑:"
  ui_print "    $SAVED_CONFIG"
  ui_print "  填入服务器地址、设备 Token，然后执行:"
  ui_print "    sh $MODPATH/service.sh   (或直接重启)"
fi

# ------------------------------------------------------------- what happens --
ui_print " "
ui_print "- 重启后会自动完成:"
if [ "$CLIP_PRESENT" = "1" ] || [ "$MSG_PRESENT" = "1" ]; then
  [ "$CLIP_PRESENT" = "1" ] && ui_print "    * 读取并导入旧剪贴板模块的设置"
  [ "$MSG_PRESENT" = "1" ] && ui_print "    * 读取并导入 Msg2Bark 的设置"
fi
ui_print "    * 启动剪贴板同步 + 短信转发"
if [ "$MSG_PRESENT" = "1" ]; then
  ui_print "    * 卸载 Msg2Bark 模块，并替换为新模块"
fi
if [ "$CLIP_PRESENT" = "1" ]; then
  ui_print "    * 卸载 Clipboard Sync Uploader，并替换为新模块"
fi

ui_print " "
ui_print "- 查看状态 / 确认结果:"
ui_print "    sh $MODPATH/action.sh"
ui_print "  或点模块的「操作」按钮"
ui_print " "
ui_print "- 日志: $STATE_DIR/clipboard-sync.log"
ui_print " "

# ------------------------------------------------------------- sanity check --
if [ ! -f "$MODPATH/service.sh" ]; then
  ui_print "!! 警告: service.sh 缺失，模块不会生效"
fi
if [ ! -f "$MODPATH/framework/clipbridge.jar" ]; then
  ui_print "!! 警告: framework/clipbridge.jar 缺失，模块不会生效"
fi

ui_print "- 安装完成，重启后生效"
ui_print " "

# Standard installer action: fix ownership and modes.
set_perm_recursive "$MODPATH" 0 0 0755 0644 2>/dev/null
