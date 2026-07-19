#!/system/bin/sh

SKIPUNZIP=0

ui_print "- Clipboard Sync Uploader v1.3.0"
if grep -q '^DEVICE_TOKEN=.\+' "$MODPATH/config.conf" 2>/dev/null; then
  ui_print "- This package contains a personalized server configuration"
  ui_print "- Reboot after installation; no manual token entry is required"
else
  ui_print "- Generic package installed"
  ui_print "- Configure /data/adb/clipboard-sync/config.conf after reboot"
fi

set_perm_recursive "$MODPATH" 0 0 0755 0644
set_perm "$MODPATH/service.sh" 0 0 0755
set_perm "$MODPATH/uninstall.sh" 0 0 0755
set_perm "$MODPATH/action.sh" 0 0 0755
set_perm "$MODPATH/customize.sh" 0 0 0755
set_perm "$MODPATH/system/bin/clipboard-syncd" 0 0 0755
