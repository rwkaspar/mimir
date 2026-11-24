#!/bin/bash -e
# Runs inside the image chroot. Bakes the photo-frame app and a first-boot
# bootstrap into the image. The heavy apt packages are already installed by
# 00-packages, so first boot only has to build the venv and wire up services
# (via the existing scripts/setup_pi.sh), which is fast.
#
# We deliberately reuse the proven provisioning flow instead of hardcoding a
# user at build time: photo_frame_bootstrap.sh detects whatever user Pi Imager
# created on first boot and sets everything up for it.

REPO_URL="${PHOTOFRAME_REPO_URL:-https://github.com/rwkaspar/mimir.git}"
REPO_REF="${PHOTOFRAME_REPO_REF:-main}"
# Which display this image is for: lcd (HDMI screen) or eink (SPI e-paper).
# One image per variant (see .github/workflows/build-image.yml), so the frame
# never has to install software or switch services at the owner's home.
VARIANT="${PHOTOFRAME_VARIANT:-lcd}"
case "$VARIANT" in lcd|eink) ;; *) echo "unknown variant: $VARIANT"; exit 1 ;; esac
BOOT_DIR="/boot/firmware"
BOOT_REPO="${BOOT_DIR}/photo_frame"

echo "Baking photo-frame app from ${REPO_URL}@${REPO_REF} (variant: ${VARIANT})"

# App repo onto the boot partition; the bootstrap copies it to the detected
# user's home on first boot.
rm -rf "${BOOT_REPO}"
git clone --depth 1 --branch "${REPO_REF}" "${REPO_URL}" "${BOOT_REPO}"
# Drop the .git dir to keep the boot (FAT32) partition small; setup_pi.sh
# re-initialises git from origin on first boot for auto-update.
rm -rf "${BOOT_REPO}/.git"

cp "${BOOT_REPO}/scripts/photo_frame_bootstrap.sh" "${BOOT_DIR}/photo_frame_bootstrap.sh"
chmod +x "${BOOT_DIR}/photo_frame_bootstrap.sh"

# ---- Variant ----
# setup_pi.sh reads this on first boot (before any mimir.yaml the owner put on
# the boot partition, which still wins). `variant_fixed` tells the wizard not
# to ask which display the frame has.
mkdir -p /etc/mimir
cat > /etc/mimir/image.yaml <<YAML
variant: ${VARIANT}
variant_fixed: true
YAML

if [ "$VARIANT" = "eink" ]; then
    # SPI (panel) and I2C (PiSugar, Inky EEPROM) on from the first boot.
    CONFIG_TXT="${BOOT_DIR}/config.txt"
    grep -qE '^\s*dtparam=spi=on' "$CONFIG_TXT" \
        || printf "\n# mimir e-ink panel\ndtparam=spi=on\n" >> "$CONFIG_TXT"
    grep -qE '^\s*dtparam=i2c_arm=on' "$CONFIG_TXT" \
        || printf "dtparam=i2c_arm=on\n" >> "$CONFIG_TXT"
    # Both panel libraries, so choosing the panel in the wizard works without
    # a download. A failure here fails the build rather than shipping an image
    # that cannot drive a panel.
    bash "${BOOT_REPO}/scripts/install_eink_libs.sh" all
fi

# i2c-dev is needed for ddcutil (DDC/CI backlight control).
echo "i2c-dev" > /etc/modules-load.d/i2c-dev.conf

# WiFi regulatory domain: without a country the domain is world/00, which makes
# the onboard brcmfmac AP (Pi Zero 2 W) restrict channels and fail client
# association. Pin it so the setup hotspot works out of the box. The runtime
# WIFI_COUNTRY env / `iw reg set` still apply; this is the persistent default.
echo "options cfg80211 ieee80211_regdom=${PHOTOFRAME_WIFI_COUNTRY:-DE}" \
    > /etc/modprobe.d/cfg80211.conf

# Default hostname so the frame is reachable at mimir.local out of the box.
# (Pi Imager's own hostname customization still overrides this if set.)
echo "mimir" > /etc/hostname
sed -i 's/127\.0\.1\.1.*/127.0.1.1\tmimir/' /etc/hosts 2>/dev/null || \
    echo "127.0.1.1	mimir" >> /etc/hosts

# Run the bootstrap once on first boot. It copies the repo to the user's home,
# installs the full setup service (setup_pi.sh), and reboots into the frame.
cat > /etc/systemd/system/photo-frame-bootstrap.service <<'UNIT'
[Unit]
Description=Photo Frame Bootstrap (first boot)
After=multi-user.target
ConditionPathExists=/boot/firmware/photo_frame_bootstrap.sh

[Service]
Type=oneshot
ExecStart=/bin/bash /boot/firmware/photo_frame_bootstrap.sh
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

systemctl enable photo-frame-bootstrap.service

# ---- Runtime trim: disable background services the frame doesn't need, so a
# 512 MB Pi Zero 2 W keeps RAM/CPU for the slideshow. avahi is kept on purpose
# (hostname.local for SSH). NetworkManager is required (WiFi/hotspot).
for unit in bluetooth hciuart triggerhappy ModemManager \
            apt-daily.timer apt-daily-upgrade.timer \
            man-db.timer fstrim.timer; do
    systemctl disable "$unit" 2>/dev/null || true
    systemctl mask "$unit" 2>/dev/null || true
done

# ---- zram swap: a compressed RAM swap gives the Zero headroom against OOM
# without hammering the SD card.
cat > /etc/default/zramswap <<'ZRAM'
# Compressed RAM swap for low-memory boards (zram-tools).
ALGO=lz4
PERCENT=50
PRIORITY=100
ZRAM
systemctl enable zramswap.service 2>/dev/null || true

echo "photo-frame bake complete (variant: ${VARIANT}, runtime trims applied)"
