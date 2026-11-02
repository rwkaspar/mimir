#!/bin/bash
# Install the Photo Frame Plymouth boot splash (logo + spinner).
#
# Usage: sudo install_bootsplash.sh [orientation] [config_path]
#   orientation : "horizontal" | "vertical" (default: read from config, else horizontal)
#   config_path : path to config_frame.yaml to read `orientation` from
#
# Safe to re-run: it rebuilds the installed theme from the repo originals, so
# re-running after an orientation change re-rotates the logo correctly.
set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${REPO_DIR}/imaging/plymouth/photoframe"
DEST="/usr/share/plymouth/themes/photoframe"
CMDLINE="/boot/firmware/cmdline.txt"

ORIENTATION="${1:-}"
CONFIG="${2:-}"

if [ -z "$ORIENTATION" ] && [ -n "$CONFIG" ] && [ -f "$CONFIG" ]; then
    # Grab `orientation: vertical` under the frame section (first match wins).
    ORIENTATION="$(grep -E '^\s*orientation:' "$CONFIG" | head -1 | awk '{print $2}')"
fi
[ "$ORIENTATION" = "vertical" ] || ORIENTATION="horizontal"
echo "Boot splash orientation: $ORIENTATION"

# --- Packages (Plymouth + the KMS renderer backend) --------------------------
if ! command -v plymouth-set-default-theme >/dev/null 2>&1; then
    echo "Installing plymouth..."
    apt-get install -y plymouth plymouth-themes || {
        echo "WARNING: could not install plymouth; skipping boot splash."; exit 0; }
fi

# --- Install theme files -----------------------------------------------------
mkdir -p "$DEST"
cp "$SRC/photoframe.plymouth" "$DEST/"
cp "$SRC/photoframe.script"   "$DEST/photoframe.script"
cp "$SRC/spinner.png"         "$DEST/spinner.png"

# --- Orientation: pick the pre-rotated logo + place the spinner --------------
# Both logo variants are shipped pre-rendered so the target needs no image
# tooling (ImageMagick/PIL are not installed on the frame).
if [ "$ORIENTATION" = "vertical" ]; then
    cp "$SRC/logo_vertical.png" "$DEST/logo.png"
    SPIN_DX=420; SPIN_DY=0
else
    cp "$SRC/logo.png"          "$DEST/logo.png"
    SPIN_DX=0;   SPIN_DY=400
fi
sed -i "s/@SPIN_DX@/${SPIN_DX}/; s/@SPIN_DY@/${SPIN_DY}/" "$DEST/photoframe.script"

# --- Kernel cmdline: enable the splash --------------------------------------
if [ -f "$CMDLINE" ]; then
    for param in "splash" "plymouth.ignore-serial-consoles"; do
        grep -q "$param" "$CMDLINE" || sed -i "s/\$/ $param/" "$CMDLINE"
    done
fi

# --- Seamless hand-off: retain the splash when Plymouth quits ---------------
# Without this, Plymouth clears to black at the end of boot, leaving a blank
# screen until the compositor/Chromium paints. --retain-splash freezes the
# last frame (the logo) on screen until something draws over it.
mkdir -p /etc/systemd/system/plymouth-quit.service.d
cat > /etc/systemd/system/plymouth-quit.service.d/retain.conf <<'EOF'
[Service]
ExecStart=
ExecStart=-/usr/bin/plymouth quit --retain-splash
EOF
systemctl daemon-reload 2>/dev/null || true

# --- Activate + rebuild initramfs (so the theme ships in early boot) --------
plymouth-set-default-theme -R photoframe
echo "Boot splash installed. Takes effect on next reboot."
