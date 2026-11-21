#!/bin/bash
# mimir — e-ink variant setup (run as root by setup_pi.sh, or by hand later).
#
#   sudo scripts/setup_eink.sh [config_frame.yaml]
#
# Does the three things an e-paper frame needs beyond the common setup:
#   1. turns the SPI bus on (the panel hangs off SPI0),
#   2. installs the panel driver library named by `eink.driver` into
#      /opt/mimir-eink (kept out of the system site-packages),
#   3. sizes the photo pipeline to the panel, so the sync composes pictures at
#      the panel's resolution instead of the LCD default of 1920x1200.
#
# The battery manager (PiSugar) is installed here too when one is configured
# (`eink.pisugar_model`) — without it the frame simply stays on.
#
# Safe to re-run: every step checks before it changes anything.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
CONFIG="${1:-${REPO_DIR}/config_frame.yaml}"
LIB_DIR="/opt/mimir-eink"
FRAME_USER="$(stat -c %U "$REPO_DIR")"

say() { echo "[eink] $*"; }

[ "$(id -u)" -eq 0 ] || { say "must run as root"; exit 1; }
[ -f "$CONFIG" ] || { say "config not found: $CONFIG"; exit 1; }

cfg_get() {   # cfg_get <key under eink:>  -> value or empty
    python3 - "$CONFIG" "$1" <<'PY'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1])) or {}
v = (cfg.get('eink') or {}).get(sys.argv[2], '')
print('' if v is None else v)
PY
}

DRIVER="$(cfg_get driver)"
say "driver: ${DRIVER:-<none set — simulator>}"

# ---- 1. SPI ----
CONFIG_TXT="/boot/firmware/config.txt"
if [ -f "$CONFIG_TXT" ]; then
    if grep -qE '^\s*dtparam=spi=on' "$CONFIG_TXT"; then
        say "SPI already enabled"
    else
        if grep -qE '^\s*#\s*dtparam=spi=on' "$CONFIG_TXT"; then
            sed -i -E 's/^\s*#\s*dtparam=spi=on/dtparam=spi=on/' "$CONFIG_TXT"
        else
            printf "\n# mimir e-ink panel\ndtparam=spi=on\n" >> "$CONFIG_TXT"
        fi
        say "SPI enabled (active after the next reboot)"
    fi
    # The PiSugar and Inky's EEPROM talk I2C.
    grep -qE '^\s*dtparam=i2c_arm=on' "$CONFIG_TXT" || {
        printf "dtparam=i2c_arm=on\n" >> "$CONFIG_TXT"
        say "I2C enabled (active after the next reboot)"
    }
fi

# ---- 2. panel library ----
mkdir -p "$LIB_DIR"
pip_target() {
    python3 -m pip install --quiet --upgrade --no-deps --target "$LIB_DIR" "$@"
}
case "$DRIVER" in
    waveshare:*)
        if [ -d "$LIB_DIR/waveshare_epd" ]; then
            say "waveshare_epd already installed"
        else
            say "installing waveshare_epd (Waveshare e-Paper library)..."
            # The library lives in a subdirectory of Waveshare's repo; --no-deps
            # because its setup.py pulls RPi.GPIO, which the Pi-5-era kernel no
            # longer supports — spidev + gpiozero come from apt instead.
            pip_target "git+https://github.com/waveshare/e-Paper.git#subdirectory=RaspberryPi_JetsonNano/python" \
                || say "WARNING: could not install waveshare_epd (no network?). Re-run this script later."
        fi
        MODULE="${DRIVER#waveshare:}"
        if [ -d "$LIB_DIR/waveshare_epd" ] && [ ! -f "$LIB_DIR/waveshare_epd/${MODULE}.py" ]; then
            say "WARNING: no driver '${MODULE}' in waveshare_epd. Available 7.x drivers:"
            for f in "$LIB_DIR"/waveshare_epd/epd7*.py; do
                [ -e "$f" ] && printf '%s ' "$(basename "$f" .py)"
            done
            echo
        fi
        ;;
    inky)
        if [ -d "$LIB_DIR/inky" ]; then
            say "inky already installed"
        else
            say "installing inky (Pimoroni)..."
            # --no-deps: numpy/Pillow/spidev come from apt; pulling them again
            # into $LIB_DIR would shadow the system builds (and take ages on a
            # Zero). Only Inky's own small helpers are added.
            pip_target inky gpiodevice gpiod smbus2 \
                || say "WARNING: could not install inky (no network?). Re-run this script later."
        fi
        ;;
    ""|simulator)
        say "no panel driver configured — the cycle writes a PNG (simulator)."
        say "set eink.driver (e.g. waveshare:epd7in3f) and re-run this script."
        ;;
    *)
        say "WARNING: unknown eink.driver '$DRIVER' (use waveshare:<module>, inky or simulator)"
        ;;
esac
chown -R "${FRAME_USER}:${FRAME_USER}" "$LIB_DIR" 2>/dev/null || true

# ---- 3. size the photo pipeline to the panel ----
if DIMS="$(cd "$REPO_DIR" && python3 -m frame.display.eink "$CONFIG" 2>/dev/null)"; then
    read -r PW PH PPAL <<< "$DIMS"
    say "panel: ${PW}x${PH}, palette ${PPAL}"
    TMP_OVERRIDE="$(mktemp)"
    cat > "$TMP_OVERRIDE" <<YAML
photos:
  horizontal:
    width: ${PW}
    height: ${PH}
  vertical:
    width: ${PH}
    height: ${PW}
videos:
  enabled: false
YAML
    python3 "${REPO_DIR}/scripts/merge_config.py" "$CONFIG" "$TMP_OVERRIDE" >/dev/null \
        && say "photo size set to the panel resolution"
    rm -f "$TMP_OVERRIDE"
    chown "${FRAME_USER}:${FRAME_USER}" "$CONFIG"
else
    say "WARNING: could not determine the panel size"
fi

# ---- 4. PiSugar (battery + RTC wake) ----
PISUGAR_MODEL="$(cfg_get pisugar_model)"
if command -v pisugar-server >/dev/null 2>&1 || systemctl list-unit-files 2>/dev/null | grep -q '^pisugar-server'; then
    say "pisugar-server already installed"
elif [ -n "$PISUGAR_MODEL" ]; then
    say "installing pisugar-server for '${PISUGAR_MODEL}'..."
    TMP_INST="$(mktemp --suffix=.sh)"
    if wget -q -O "$TMP_INST" https://cdn.pisugar.com/release/pisugar-power-manager.sh; then
        bash "$TMP_INST" -c release --non-interactive --model "$PISUGAR_MODEL" \
            || say "WARNING: pisugar-server install failed"
    else
        say "WARNING: could not download the PiSugar installer (no network?)"
    fi
    rm -f "$TMP_INST"
else
    say "no PiSugar configured (eink.pisugar_model) — the frame stays on between pictures."
    say "for battery use set e.g. pisugar_model: 'PiSugar 3' and re-run this script."
fi

say "done."
