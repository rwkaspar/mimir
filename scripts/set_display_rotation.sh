#!/bin/bash
# Set (or clear) the HDMI display rotation for portrait/landscape mounting.
# KMS (vc4-kms-v3d) honours a `video=<conn>:<mode>,rotate=N` token on the kernel
# cmdline; with disable_fw_kms_setup=1 we own the only video= token, so we can
# add/remove it surgically. A reboot applies it (the caller reboots).
#
# Usage: set_display_rotation.sh horizontal|vertical [rotate_degrees]
# Defaults vertical -> rotate=90. Run as root (sudoers NOPASSWD).
set -euo pipefail

ORIENT="${1:-horizontal}"
DEG="${2:-90}"
CONN="HDMI-A-1"
MODE="1920x1200@60"

CMDLINE=/boot/firmware/cmdline.txt
[ -f "$CMDLINE" ] || CMDLINE=/boot/cmdline.txt
[ -f "$CMDLINE" ] || { echo "no cmdline.txt found" >&2; exit 1; }

orig="$(cat "$CMDLINE")"
# Strip any video=HDMI-A-1:... token we previously added (and tidy spaces).
stripped="$(printf '%s' "$orig" | sed -E "s/ *video=${CONN}:[^ ]*//g" | tr -s ' ' | sed -E 's/^ +| +$//g')"

if [ "$ORIENT" = "vertical" ]; then
    new="$stripped video=${CONN}:${MODE},rotate=${DEG}"
else
    new="$stripped"
fi
new="$(printf '%s' "$new" | tr -s ' ' | sed -E 's/^ +| +$//g')"

# Safety: the result must still be a single line and keep the root= param,
# otherwise the system won't boot — refuse and leave the file untouched.
if [ "$(printf '%s' "$new" | wc -l)" != "0" ] || ! printf '%s' "$new" | grep -q 'root='; then
    echo "refusing to write an unsafe cmdline" >&2
    exit 2
fi

cp -a "$CMDLINE" "${CMDLINE}.mimir-bak"
printf '%s\n' "$new" > "$CMDLINE"
echo "cmdline updated for orientation=$ORIENT:"
echo "$new"
