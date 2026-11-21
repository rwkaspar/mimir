#!/bin/bash
# Diagnostic: log every presence transition of the wch.cn touch panel
# (USB 27c0:0859) with power/thermal context, so the NEXT dropout is captured
# with a timestamp even across reboots. Paired with persistent journald (see
# setup_pi.sh), which holds the kernel-side USB disconnect reason.
#
# Installed as touch-watch.service by setup_pi.sh (LCD variant only). On a frame
# built without a touch panel it simply logs "init -> absent" once and idles.
LOG=/var/log/touch-watch.log
present() {
  for d in /sys/bus/usb/devices/*/; do
    [ "$(cat "$d/idVendor" 2>/dev/null)" = "27c0" ] &&
    [ "$(cat "$d/idProduct" 2>/dev/null)" = "0859" ] && return 0
  done
  return 1
}
last=""
while true; do
  if present; then cur=present; else cur=absent; fi
  if [ "$cur" != "$last" ]; then
    {
      echo "==== $(date '+%F %T') : ${last:-init} -> $cur ===="
      echo "throttled=$(vcgencmd get_throttled 2>/dev/null) | $(vcgencmd measure_temp 2>/dev/null) | uptime=$(cut -d' ' -f1 /proc/uptime)s"
      if [ "$cur" = "absent" ] && [ -n "$last" ]; then
        echo "--- kernel tail at drop ---"
        journalctl -k -n 50 --no-pager 2>/dev/null | tail -50
      fi
    } >> "$LOG" 2>&1
    last=$cur
  fi
  sleep 60
done
