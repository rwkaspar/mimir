#!/bin/bash
# mimir — install e-paper panel libraries into /opt/mimir-eink (run as root).
#
#   scripts/install_eink_libs.sh waveshare|inky|all
#
# Kept out of the system site-packages; the e-ink cycle adds the directory to
# its PYTHONPATH. Called by setup_eink.sh for the configured panel, and with
# `all` when the e-ink image is built, so that picking a panel in the wizard
# needs no network. Skips what is already there; exits non-zero if an install
# failed.

LIB_DIR="${MIMIR_EINK_LIB_DIR:-/opt/mimir-eink}"
WHAT="$1"

say() { echo "[eink] $*"; }

case "$WHAT" in waveshare|inky|all) ;; *) echo "usage: $0 waveshare|inky|all"; exit 2 ;; esac

mkdir -p "$LIB_DIR"
pip_target() {
    python3 -m pip install --quiet --upgrade --no-deps --target "$LIB_DIR" "$@"
}

RC=0
if [ "$WHAT" = "waveshare" ] || [ "$WHAT" = "all" ]; then
    if [ -d "$LIB_DIR/waveshare_epd" ]; then
        say "waveshare_epd already installed"
    else
        say "installing waveshare_epd (Waveshare e-Paper library)..."
        # The library lives in a subdirectory of Waveshare's repo; --no-deps
        # because its setup.py pulls RPi.GPIO, which the Pi-5-era kernel no
        # longer supports — spidev + gpiozero come from apt instead.
        pip_target "git+https://github.com/waveshare/e-Paper.git#subdirectory=RaspberryPi_JetsonNano/python" \
            && [ -d "$LIB_DIR/waveshare_epd" ] || RC=1
    fi
fi
if [ "$WHAT" = "inky" ] || [ "$WHAT" = "all" ]; then
    if [ -d "$LIB_DIR/inky" ]; then
        say "inky already installed"
    else
        say "installing inky (Pimoroni)..."
        # --no-deps: numpy/Pillow/spidev come from apt; pulling them again
        # into $LIB_DIR would shadow the system builds (and take ages on a
        # Zero). Only Inky's own small helpers are added.
        pip_target inky gpiodevice gpiod smbus2 && [ -d "$LIB_DIR/inky" ] || RC=1
    fi
fi
exit $RC
