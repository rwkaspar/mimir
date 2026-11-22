#!/bin/bash
# mimir — switch the hardware variant of an installed frame (run as root).
#
#   sudo scripts/switch_variant.sh lcd|eink [--no-reboot]
#
# The setup wizard asks which display the frame has; the server writes the
# answer into the config (variant, eink.driver, eink.pisugar_model) and calls
# this script, which does the parts that need root:
#
#   eink: packages for SPI/GPIO, scripts/setup_eink.sh (SPI on, panel library,
#         photo size = panel size, no videos, PiSugar), e-ink cycle enabled
#         instead of the slideshow
#   lcd:  photo size and videos back to the LCD defaults, slideshow enabled
#         instead of the e-ink cycle
#
# then reboots (SPI and the service change need it). Safe to re-run.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
CONFIG="${REPO_DIR}/config_frame.yaml"
LOG="${REPO_DIR}/switch_variant.log"
VARIANT="$1"
REBOOT=1
[ "$2" = "--no-reboot" ] && REBOOT=0

say() { echo "[variant] $*" | tee -a "$LOG"; }

[ "$(id -u)" -eq 0 ] || { echo "must run as root"; exit 1; }
case "$VARIANT" in lcd|eink) ;; *) echo "usage: $0 lcd|eink [--no-reboot]"; exit 2 ;; esac
[ -f "$CONFIG" ] || { echo "config not found: $CONFIG"; exit 1; }
FRAME_USER="$(stat -c %U "$REPO_DIR")"

say "$(date '+%F %T') switching to ${VARIANT}"

merge() {   # merge <yaml text> into the config
    local tmp; tmp="$(mktemp)"
    printf '%s\n' "$1" > "$tmp"
    python3 "${REPO_DIR}/scripts/merge_config.py" "$CONFIG" "$tmp" >/dev/null 2>>"$LOG" \
        || say "WARNING: could not update the config"
    rm -f "$tmp"
    chown "${FRAME_USER}:${FRAME_USER}" "$CONFIG"
}

merge "variant: ${VARIANT}"

if [ "$VARIANT" = "eink" ]; then
    # The ready-made image ships these; a script install of the LCD variant
    # does not.
    MISSING=""
    for p in python3-spidev python3-gpiozero python3-lgpio python3-pip; do
        dpkg -s "$p" >/dev/null 2>&1 || MISSING="$MISSING $p"
    done
    if [ -n "$MISSING" ]; then
        say "installing:${MISSING}"
        # shellcheck disable=SC2086
        DEBIAN_FRONTEND=noninteractive apt-get install -y $MISSING >>"$LOG" 2>&1 \
            || say "WARNING: could not install${MISSING} (no network?)"
    fi
    bash "${SCRIPT_DIR}/setup_eink.sh" "$CONFIG" 2>&1 | tee -a "$LOG"
    systemctl disable photo_frame_slideshow touch-watch >>"$LOG" 2>&1 || true
    systemctl enable photo_frame_eink >>"$LOG" 2>&1 \
        || say "WARNING: photo_frame_eink.service missing — re-run scripts/setup_pi.sh"
else
    merge "photos:
  horizontal: {width: 1920, height: 1200}
  vertical: {width: 1200, height: 1920}
videos:
  enabled: true"
    systemctl disable photo_frame_eink >>"$LOG" 2>&1 || true
    systemctl enable photo_frame_slideshow touch-watch >>"$LOG" 2>&1 \
        || say "WARNING: photo_frame_slideshow.service missing — re-run scripts/setup_pi.sh"
fi

sync
say "done."
if [ "$REBOOT" -eq 1 ]; then
    say "rebooting"
    sleep 2
    systemctl reboot
fi
