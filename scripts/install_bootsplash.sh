#!/bin/bash
# Install the Photo Frame Plymouth boot splash (a dot draws the mimir logo).
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

# --- Orientation: install the pre-rendered logo animation --------------------
# The "a dot draws the mimir logo" splash ships as a flat PNG frame sequence
# (regenerate with render_anim.py after a logo/animation change); the vertical
# set is pre-rotated. The frame has no image tooling (ImageMagick/PIL absent),
# so nothing is rendered here — we just copy the frames for this orientation.
rm -rf "$DEST/anim"
mkdir -p "$DEST/anim"
cp "$SRC/anim/$ORIENTATION/"f*.png "$DEST/anim/"
FRAMES="$(ls "$DEST/anim/"f*.png 2>/dev/null | wc -l)"
[ "$FRAMES" -ge 1 ] || { echo "ERROR: no animation frames in $SRC/anim/$ORIENTATION"; exit 1; }
sed -i "s/@FRAMES@/${FRAMES}/" "$DEST/photoframe.script"
echo "Boot splash animation: $FRAMES frames ($ORIENTATION)"

# --- Kernel cmdline: enable the splash --------------------------------------
if [ -f "$CMDLINE" ]; then
    for param in "splash" "plymouth.ignore-serial-consoles"; do
        grep -q "$param" "$CMDLINE" || sed -i "s/\$/ $param/" "$CMDLINE"
    done
fi

# --- Seamless hand-off ------------------------------------------------------
# Keep the Plymouth splash up (and its spinner animating) until the kiosk
# compositor is about to start, instead of quitting at multi-user.target and
# leaving a ~5s black gap before swaybg paints. The cage service quits it with
# --retain-splash as its last step, right before labwc.
mkdir -p /etc/systemd/system/plymouth-quit.service.d
cat > /etc/systemd/system/plymouth-quit.service.d/retain.conf <<'EOF'
[Service]
# Do not quit here; cage quits Plymouth right before it starts labwc.
ExecStart=
ExecStart=/bin/true
EOF
# Nothing must block boot waiting for the splash to quit (we quit it late),
# otherwise multi-user.target would wait on a splash the kiosk hasn't reached.
systemctl mask plymouth-quit-wait.service 2>/dev/null || true
systemctl daemon-reload 2>/dev/null || true

# --- Activate + rebuild initramfs (so the theme ships in early boot) --------
plymouth-set-default-theme -R photoframe
echo "Boot splash installed. Takes effect on next reboot."
