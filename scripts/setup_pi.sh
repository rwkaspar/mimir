#!/bin/bash
# Photo Frame — First-boot setup script
# Runs as root on the Pi. Expects the repo in the user's home directory
# (copied there by photo_frame_bootstrap.sh on first boot).
# Don't use set -e — we want to log errors, not silently abort
# (clear, cat to /dev/tty1, git config etc. can fail harmlessly)
trap 'log "ERROR on line $LINENO (exit $?)"' ERR

# Detect user and paths from script location
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
FRAME_HOME="$(dirname "$REPO_DIR")"
FRAME_USER="$(basename "$FRAME_HOME")"
PHOTOS_DIR="/srv/frame/photos"

# Log to boot partition (FAT32) so it's readable from Windows for debugging
SETUP_LOG="/boot/firmware/setup_log.txt"
log() {
    echo "$(date '+%H:%M:%S') $*" | tee -a "$SETUP_LOG"
}

# Helper: abort on critical errors (replaces set -e for important commands)
die() {
    log "FATAL: $*"
    sync
    exit 1
}

log "=== Photo Frame Setup ==="
log "User: ${FRAME_USER}, Repo: ${REPO_DIR}"

# ---- Variant: lcd (HDMI panel, native slideshow) or eink (SPI e-paper) ----
# Chosen BEFORE first boot by dropping a `mimir.yaml` onto the boot partition
# (e.g. `variant: eink` plus an `eink:` section) — it is merged into the config
# further down. On a re-run the existing config wins.
BOOT_OVERRIDE="/boot/firmware/mimir.yaml"
# The ready-made images (one for HDMI, one for e-ink) carry their variant in
# this file; it seeds a freshly created config. The boot override beats it.
IMAGE_DEFAULTS="/etc/mimir/image.yaml"
read_variant() {
    local v=""
    for f in "${REPO_DIR}/config_frame.yaml" "$BOOT_OVERRIDE" "$IMAGE_DEFAULTS"; do
        if [ -z "$v" ] && [ -f "$f" ]; then
            v="$(grep -E '^variant:' "$f" 2>/dev/null | head -1 | awk '{print $2}' | tr -d "\"'\r")"
        fi
    done
    [ "$v" = "eink" ] && echo eink || echo lcd
}
VARIANT="$(read_variant)"
log "Variant: ${VARIANT}"

# ---- Verify ----
if [ "$(id -u)" -ne 0 ]; then
    die "must run as root"
fi
if [ ! -d "$REPO_DIR" ]; then
    die "repo not found at $REPO_DIR"
fi

# ---- Show setup progress on screen ----
systemctl stop getty@tty1 2>/dev/null || true
setterm --cursor off > /dev/tty1 2>/dev/null || true
# Helper: show status on both log and display
show() {
    log "$1"
    printf '\033[2J\033[H' > /dev/tty1 2>/dev/null || true
    cat > /dev/tty1 2>/dev/null << SCREEN || true

        ==========================================

           Photo Frame  -  Setup

           $1

           Please wait...

        ==========================================
SCREEN
}

# ---- Desktop display manager conflict handling ----
# On desktop-based Pi OS images a running display manager occupies DRM/seat0,
# which keeps the slideshow from taking the display ("Could not take device",
# "Found 0 GPUs"). Disable any display manager and boot to multi-user.target.
# (Contributed idea from @romanlehmann, PR #3.)
DISPLAY_MANAGERS=(
    display-manager.service lightdm.service gdm.service gdm3.service
    sddm.service lxdm.service xdm.service
)
deactivate_display_manager() {
    log "Checking for desktop display manager conflicts..."
    for dm in "${DISPLAY_MANAGERS[@]}"; do
        if systemctl is-enabled "$dm" >/dev/null 2>&1 || systemctl is-active "$dm" >/dev/null 2>&1; then
            log "Disabling conflicting service: ${dm}"
            systemctl disable --now "$dm" 2>/dev/null || log "WARNING: could not disable ${dm}"
        fi
    done
    if systemctl set-default multi-user.target >/dev/null 2>&1; then
        log "Default target set to multi-user.target (no desktop login on boot)."
    else
        log "WARNING: failed to set default target to multi-user.target"
    fi
}
assert_no_display_manager_conflict() {
    local active_dms=()
    for dm in "${DISPLAY_MANAGERS[@]}"; do
        systemctl is-active "$dm" >/dev/null 2>&1 && active_dms+=("$dm")
    done
    if [ "${#active_dms[@]}" -gt 0 ]; then
        die "desktop display manager still active (${active_dms[*]}). This blocks the display for the slideshow. Disable it (e.g. sudo systemctl disable --now lightdm) and rerun setup."
    fi
}

# ---- Check internet (needed for apt-get) ----
if ping -c 1 -W 2 8.8.8.8 &>/dev/null; then
    log "Internet already available, skipping network wait."
else
    show "Waiting for network..."
    for i in $(seq 1 15); do
        if ping -c 1 -W 2 8.8.8.8 &>/dev/null; then
            log "Internet available after ${i} attempts."
            break
        fi
        sleep 2
    done
fi

log "Checking internet connectivity..."
if ! ping -c 1 -W 5 8.8.8.8 &>/dev/null; then
    log "No internet. Preparing WiFi hotspot..."
    # Unblock WiFi (may be soft-blocked on fresh install without WiFi config)
    rfkill unblock wifi 2>/dev/null || true
    nmcli radio wifi on 2>/dev/null || true

    # Wait for WiFi device to appear (brcmfmac can take a while on Pi Zero 2W)
    log "Waiting for WiFi device..."
    WIFI_READY=false
    for i in $(seq 1 30); do
        if nmcli -t -f TYPE device status 2>/dev/null | grep -q wifi; then
            WIFI_READY=true
            break
        fi
        log "  WiFi not ready yet ($i/30)... $(ip link show wlan0 2>&1 | head -1)"
        sleep 2
    done
    log "WiFi ready: ${WIFI_READY}"
    log "Devices: $(nmcli -t -f DEVICE,TYPE,STATE device status 2>&1)"
    log "Starting wifi_setup_server.py..."
    python3 "${REPO_DIR}/scripts/wifi_setup_server.py" 2>&1 | tee -a "$SETUP_LOG"
    if [ "${PIPESTATUS[0]}" -ne 0 ]; then
        log "ERROR: wifi_setup_server.py failed"
        log "Waiting for network (plug in Ethernet or power-cycle with WiFi configured)..."
        while ! ping -c 1 -W 5 8.8.8.8 &>/dev/null; do
            sleep 10
        done
    fi
    log "Internet connected! Continuing setup..."
    sleep 2
fi

# ---- System packages (with retry) ----
# The display runs natively (frame/slideshow.py -> /dev/fb0 + GStreamer kmssink)
# under the SYSTEM python3, so its deps come from apt, not the venv: python3-gi +
# the GStreamer introspection data (seamless video loop), python3-pil/-numpy
# (menu, clock, boot splash), python3-yaml, a TrueType font and gst-launch.
# No browser, no Wayland compositor: cog/labwc/swaybg/seatd are gone.
PACKAGES_COMMON="git python3-venv python3-dev python3-yaml python3-pil python3-numpy fonts-dejavu-core network-manager dnsmasq-base iptables qrencode libjpeg-dev zlib1g-dev libffi-dev libheif-dev ffmpeg fake-hwclock"
PACKAGES_LCD="python3-gi gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad ddcutil i2c-tools plymouth plymouth-themes"
# E-ink: SPI + GPIO bindings for the panel driver (Waveshare lib / Inky).
PACKAGES_EINK="python3-spidev python3-gpiozero python3-lgpio python3-pip"
if [ "$VARIANT" = "eink" ]; then
    PACKAGES="$PACKAGES_COMMON $PACKAGES_EINK"
    CRITICAL_PKGS="python3-dev python3-yaml python3-pil python3-numpy libheif-dev python3-spidev python3-gpiozero"
else
    PACKAGES="$PACKAGES_COMMON $PACKAGES_LCD"
    CRITICAL_PKGS="python3-dev python3-yaml python3-pil python3-numpy python3-gi gir1.2-gstreamer-1.0 gstreamer1.0-tools gstreamer1.0-plugins-bad libheif-dev ddcutil ffmpeg fonts-dejavu-core"
fi

MISSING_PKGS=""
for pkg in $PACKAGES; do
    if ! dpkg -s "$pkg" &>/dev/null; then
        MISSING_PKGS="$MISSING_PKGS $pkg"
    fi
done

if [ -n "$MISSING_PKGS" ]; then
    install_packages() {
        apt-get update -qq 2>&1 | tee -a "$SETUP_LOG"
        apt-get install -y --fix-missing $PACKAGES 2>&1 | tee -a "$SETUP_LOG"
    }

    show "Installing packages (this takes ~15 min)..."
    if ! install_packages; then
        log "First apt attempt failed, retrying in 10s..."
        sleep 10
        if ! install_packages; then
            log "Second apt attempt failed, retrying in 30s..."
            sleep 30
            install_packages || log "ERROR: apt-get install failed after 3 attempts"
        fi
    fi
    for pkg in $CRITICAL_PKGS; do
        if ! dpkg -s "$pkg" &>/dev/null; then
            log "WARNING: $pkg missing, attempting individual install..."
            apt-get install -y "$pkg" 2>&1 | tee -a "$SETUP_LOG" || log "WARNING: could not install $pkg"
        fi
    done
    log "Packages installed."
else
    log "All packages already installed, skipping."
fi

# ---- Timezone & NTP ----
log "Configuring timezone and NTP..."
timedatectl set-timezone Europe/Berlin 2>/dev/null || true
timedatectl set-ntp true 2>/dev/null || true

# ---- Persistent journal ----
# Storage=auto silently falls back to /run (RAM) when /var/log/journal is
# missing, so every reboot wiped the logs — and with them the reason for any
# overnight fault. Keep a bounded on-disk journal instead.
mkdir -p /etc/systemd/journald.conf.d /var/log/journal
cat > /etc/systemd/journald.conf.d/persistent.conf << 'EOF'
[Journal]
Storage=persistent
SystemMaxUse=200M
MaxRetentionSec=1year
EOF
systemd-tmpfiles --create --prefix /var/log/journal 2>/dev/null || true
systemctl restart systemd-journald 2>/dev/null || true

# ---- i2c-dev module (needed for ddcutil / DDC/CI backlight control) ----
if ! grep -q "i2c-dev" /etc/modules-load.d/i2c-dev.conf 2>/dev/null; then
    echo "i2c-dev" > /etc/modules-load.d/i2c-dev.conf
fi
modprobe i2c-dev 2>/dev/null || true

# ---- User groups ----
echo "Setting up user groups..."
usermod -aG video,input,render,netdev,i2c "${FRAME_USER}"
# spi/gpio exist on Raspberry Pi OS; needed by the e-ink panel driver.
for grp in spi gpio; do
    getent group "$grp" >/dev/null 2>&1 && usermod -aG "$grp" "${FRAME_USER}"
done

# ---- Photos directory ----
echo "Creating photos directory..."
mkdir -p "${PHOTOS_DIR}/horizontal" "${PHOTOS_DIR}/vertical"
# Copy default placeholder images only if photos directory is empty (first run)
if [ -d "${REPO_DIR}/viewer/defaults/horizontal" ] && [ -z "$(ls -A "${PHOTOS_DIR}/horizontal/" 2>/dev/null)" ]; then
    cp "${REPO_DIR}/viewer/defaults/horizontal/"*.jpg "${PHOTOS_DIR}/horizontal/" 2>/dev/null || true
    cp "${REPO_DIR}/viewer/defaults/vertical/"*.jpg "${PHOTOS_DIR}/vertical/" 2>/dev/null || true
fi
chown -R "${FRAME_USER}:${FRAME_USER}" "${PHOTOS_DIR}"

# ---- Python venv (with retry) ----
VENV_DIR="${REPO_DIR}/venv"
VENV_NEEDS_UPDATE=false

if [ ! -x "${VENV_DIR}/bin/python" ] || ! "${VENV_DIR}/bin/python" -c "import sys" &>/dev/null; then
    VENV_NEEDS_UPDATE=true
elif [ -f "${VENV_DIR}/.requirements_md5" ]; then
    CURRENT_MD5=$(md5sum "${REPO_DIR}/requirements.txt" 2>/dev/null | awk '{print $1}')
    SAVED_MD5=$(cat "${VENV_DIR}/.requirements_md5" 2>/dev/null)
    if [ "$CURRENT_MD5" != "$SAVED_MD5" ]; then
        VENV_NEEDS_UPDATE=true
    fi
else
    VENV_NEEDS_UPDATE=true
fi

if [ "$VENV_NEEDS_UPDATE" = true ]; then
    show "Installing Python dependencies..."
    cd "$REPO_DIR"
    pip_install() {
        su - "${FRAME_USER}" -c "cd ${REPO_DIR} && python3 -m venv venv && venv/bin/pip install --quiet -r requirements.txt"
    }
    if ! pip_install; then
        log "First pip attempt failed, retrying..."
        sleep 5
        pip_install || log "ERROR: Python venv/pip setup failed after 2 attempts"
    fi
    md5sum "${REPO_DIR}/requirements.txt" 2>/dev/null | awk '{print $1}' > "${VENV_DIR}/.requirements_md5"
    chown "${FRAME_USER}:${FRAME_USER}" "${VENV_DIR}/.requirements_md5"
else
    log "Python venv up to date, skipping."
fi

# ---- Config file ----
if [ ! -f "${REPO_DIR}/config_frame.yaml" ]; then
    echo "Creating default config..."
    cat > "${REPO_DIR}/config_frame.yaml" << 'YAML'
setup_complete: false
variant: lcd          # lcd = HDMI panel + native slideshow, eink = SPI e-paper
frame:
  name: photo-frame
  orientation: horizontal
photos:
  base_dir: /srv/frame/photos
  blur_darken: 0.6
  blur_radius: 40
  horizontal:
    width: 1920
    height: 1200
  vertical:
    width: 1200
    height: 1920
  quality: 85
  state_db: /srv/frame/photos/state.db
  tmp_dir: /tmp/frame_downloads
videos:
  enabled: true
  max_duration: 120
  max_filesize_mb: 100
slideshow:
  interval: 30
  fade_duration: 1.0
  transition: fade
  boot_splash: true   # draw the mimir logo on fb0 at cold boot (before photo 1)
clock:
  enabled: false
  size: m
  position: br
  show_date: true
synology:
  share_urls: []
  share_passphrases: []
google_photos:
  share_urls: []
immich:
  share_urls: []
  share_passphrases: []
icloud:
  share_urls: []
nextcloud:
  share_urls: []
  share_passphrases: []
energy_save:
  method: ddcci
mqtt:
  enabled: false
  host: ''
  port: 1883
  username: ''
  password: ''
connect:
  enabled: false
  relay_url: 'https://connect.neotactiq.ai'
logging:
  level: INFO
  file: /var/log/photo_frame.log
YAML
    # Ready-made image: its variant goes into the new config. Only here, on
    # creation — a later re-run must not undo a switch made by hand.
    if [ -f "$IMAGE_DEFAULTS" ]; then
        log "Applying image defaults from ${IMAGE_DEFAULTS}..."
        python3 "${REPO_DIR}/scripts/merge_config.py" "${REPO_DIR}/config_frame.yaml" "$IMAGE_DEFAULTS" 2>&1 | tee -a "$SETUP_LOG" \
            || log "WARNING: could not apply ${IMAGE_DEFAULTS}"
    fi
    chown "${FRAME_USER}:${FRAME_USER}" "${REPO_DIR}/config_frame.yaml"
fi

# ---- Merge boot-partition overrides (variant, e-ink panel, frame name, ...) ----
# `mimir.yaml` on the boot (FAT) partition is deep-merged into the config once
# and then renamed, so later edits in the UI are never overwritten by it.
if [ -f "$BOOT_OVERRIDE" ]; then
    log "Merging ${BOOT_OVERRIDE} into config..."
    if python3 "${REPO_DIR}/scripts/merge_config.py" "${REPO_DIR}/config_frame.yaml" "$BOOT_OVERRIDE" 2>&1 | tee -a "$SETUP_LOG"; then
        mv -f "$BOOT_OVERRIDE" "${BOOT_OVERRIDE}.applied" 2>/dev/null || true
    else
        log "WARNING: could not merge ${BOOT_OVERRIDE} (left in place)"
    fi
    chown "${FRAME_USER}:${FRAME_USER}" "${REPO_DIR}/config_frame.yaml"
    VARIANT="$(read_variant)"
    log "Variant after merge: ${VARIANT}"
fi

# ---- Systemd services ----
show "Configuring services..."

cat > /etc/systemd/system/photo_frame_server.service << EOF
[Unit]
Description=Digital Photo Frame Viewer Server
After=network.target

[Service]
Type=simple
User=${FRAME_USER}
WorkingDirectory=${REPO_DIR}
Environment="PORT=8080"
ExecStart=${REPO_DIR}/venv/bin/python -m frame.server ${REPO_DIR}/config_frame.yaml
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# The display owner. Exactly one of these is enabled, by variant.
#
# LCD: the native slideshow owns the panel directly (DRM/kmssink + fbcon) — no
# browser, no compositor. It runs under the SYSTEM python3 (python3-gi/GStreamer
# for the seamless video loop are apt packages; the venv has no gi).
cat > /etc/systemd/system/photo_frame_slideshow.service << EOF
[Unit]
Description=mimir native slideshow (photos + videos, no browser)
After=network.target photo_frame_server.service
Wants=photo_frame_server.service
Conflicts=display-manager.service

[Service]
Type=simple
User=${FRAME_USER}
SupplementaryGroups=video input render
TTYPath=/dev/tty1
WorkingDirectory=${REPO_DIR}
# Quit Plymouth so it releases the DRM master before the slideshow grabs it.
# ('+' = run as root; ExecStartPre otherwise runs as User=.)
ExecStartPre=+-/usr/bin/plymouth quit
ExecStart=/usr/bin/python3 -m frame.slideshow ${REPO_DIR}/config_frame.yaml
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# E-ink: wake -> sync -> draw one image -> set the RTC alarm -> power off.
# Stays up instead (redrawing on a timer) while in setup mode, on external power
# or without a PiSugar. See frame/eink_cycle.py.
cat > /etc/systemd/system/photo_frame_eink.service << EOF
[Unit]
Description=mimir e-ink cycle (draw one image, then sleep)
After=network.target photo_frame_server.service
Wants=photo_frame_server.service

[Service]
Type=simple
User=${FRAME_USER}
SupplementaryGroups=spi gpio i2c
WorkingDirectory=${REPO_DIR}
Environment="PYTHONPATH=/opt/mimir-eink"
ExecStart=/usr/bin/python3 -m frame.eink_cycle ${REPO_DIR}/config_frame.yaml
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF

# Diagnostic logger for the USB touch panel's presence (LCD builds). Harmless
# without a touch panel.
cat > /etc/systemd/system/touch-watch.service << EOF
[Unit]
Description=Touch USB presence watchdog (diagnostic logging)
After=multi-user.target

[Service]
Type=simple
ExecStart=${REPO_DIR}/scripts/touch-watch.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# Retire the old browser kiosk (labwc + cog) if an earlier setup installed it:
# it and the native slideshow both need the DRM master and must never co-run.
for old in photo_frame_cage photo_frame_cursor_hide; do
    if [ -f "/etc/systemd/system/${old}.service" ]; then
        log "Removing retired unit ${old}.service"
        systemctl disable --now "${old}.service" 2>/dev/null || true
        rm -f "/etc/systemd/system/${old}.service"
        rm -rf "/etc/systemd/system/${old}.service.d"
    fi
done

cat > /etc/systemd/system/photo_frame_update.service << EOF
[Unit]
Description=Digital Photo Frame Auto-Update
After=network-online.target
Wants=network-online.target
Before=photo_frame_server.service

[Service]
Type=oneshot
User=${FRAME_USER}
WorkingDirectory=${REPO_DIR}
ExecStart=${REPO_DIR}/scripts/update.sh
TimeoutStartSec=120
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/photo_frame_update.timer << 'EOF'
[Unit]
Description=Daily auto-update for photo frame

[Timer]
OnCalendar=*-*-* 04:00:00
RandomizedDelaySec=1800
Persistent=true

[Install]
WantedBy=timers.target
EOF

# ---- Polkit rule (NetworkManager without sudo for frame_user) ----
echo "Setting up polkit..."
cat > /etc/polkit-1/rules.d/10-photo-frame-wifi.rules << EOF
// Allow photo frame user to manage WiFi via NetworkManager
polkit.addRule(function(action, subject) {
    if (action.id.indexOf("org.freedesktop.NetworkManager.") === 0 &&
        subject.user === "${FRAME_USER}") {
        return polkit.Result.YES;
    }
});
EOF

# ---- Sudoers (iptables for captive portal) ----
echo "Setting up sudoers..."
# sudo matches a rule against the command's REAL path. ddcutil is /usr/bin/ddcutil
# on Debian; an earlier version of this file named /usr/sbin/ddcutil, a path that
# does not exist — so the rule never matched and every backlight/standby call
# only worked while some earlier interactive `sudo` had left a cached password
# behind (Raspberry Pi OS sets timestamp_type=global). Unattended, sleep and
# wake then failed with "a password is required", which looked exactly like a
# flaky DDC bus. Resolve the real paths instead of guessing them.
DDCUTIL_BIN="$(command -v ddcutil 2>/dev/null || echo /usr/bin/ddcutil)"
IPTABLES_BIN="$(command -v iptables 2>/dev/null || echo /usr/sbin/iptables)"
cat > /etc/sudoers.d/photo-frame << EOF
${FRAME_USER} ALL=(ALL) NOPASSWD: ${IPTABLES_BIN}
${FRAME_USER} ALL=(ALL) NOPASSWD: ${DDCUTIL_BIN}
${FRAME_USER} ALL=(ALL) NOPASSWD: /bin/systemctl stop photo_frame_slideshow
${FRAME_USER} ALL=(ALL) NOPASSWD: /bin/systemctl start photo_frame_slideshow
${FRAME_USER} ALL=(ALL) NOPASSWD: /bin/systemctl restart photo_frame_slideshow
${FRAME_USER} ALL=(ALL) NOPASSWD: /bin/systemctl stop photo_frame_eink
${FRAME_USER} ALL=(ALL) NOPASSWD: /bin/systemctl start photo_frame_eink
${FRAME_USER} ALL=(ALL) NOPASSWD: /bin/systemctl restart photo_frame_eink
${FRAME_USER} ALL=(ALL) NOPASSWD: /sbin/poweroff
${FRAME_USER} ALL=(ALL) NOPASSWD: ${REPO_DIR}/scripts/install_bootsplash.sh
${FRAME_USER} ALL=(ALL) NOPASSWD: ${REPO_DIR}/scripts/switch_variant.sh
${FRAME_USER} ALL=(ALL) NOPASSWD: /bin/dd
${FRAME_USER} ALL=(ALL) NOPASSWD: /usr/bin/setterm
${FRAME_USER} ALL=(ALL) NOPASSWD: /sbin/shutdown
${FRAME_USER} ALL=(ALL) NOPASSWD: /sbin/reboot
${FRAME_USER} ALL=(ALL) NOPASSWD: /usr/bin/tailscale up *
${FRAME_USER} ALL=(ALL) NOPASSWD: /usr/bin/tailscale status
${FRAME_USER} ALL=(ALL) NOPASSWD: /usr/bin/tailscale status --json
${FRAME_USER} ALL=(ALL) NOPASSWD: /usr/bin/pkill -f tailscale up
${FRAME_USER} ALL=(ALL) NOPASSWD: /bin/sh -c setterm --cursor off --blank force --powerdown 0 > /dev/tty1
${FRAME_USER} ALL=(ALL) NOPASSWD: /bin/sh -c curl -fsSL https\://tailscale.com/install.sh | sh
EOF
chmod 440 /etc/sudoers.d/photo-frame
visudo -c -f /etc/sudoers.d/photo-frame >/dev/null 2>&1 || log "WARNING: sudoers file failed validation"

# ---- Quiet boot ----
echo "Configuring quiet boot..."
CMDLINE="/boot/firmware/cmdline.txt"
if [ -f "$CMDLINE" ]; then
    # Add quiet boot params if not present
    for param in "quiet" "loglevel=0" "vt.global_cursor_default=0" "logo.nologo" "systemd.show_status=false"; do
        if ! grep -q "$param" "$CMDLINE"; then
            sed -i "s/$/ $param/" "$CMDLINE"
        fi
    done
    # Move the boot console off tty1 (the VT the slideshow takes over) so kernel
    # and systemd messages never appear where the frame will be — tty1 stays
    # black until the display starts. Debug console remains on tty3.
    sed -i "s/console=tty1/console=tty3/" "$CMDLINE"
fi

# Hide the firmware rainbow splash shown before the kernel loads.
CONFIG="/boot/firmware/config.txt"
if [ -f "$CONFIG" ] && ! grep -q "^disable_splash=1" "$CONFIG"; then
    printf "\n# Hide firmware rainbow splash\ndisable_splash=1\n" >> "$CONFIG"
fi

# ---- Boot splash (Plymouth: logo + spinner, rotated to the orientation) ----
if [ "$VARIANT" != "eink" ] && [ -f "${REPO_DIR}/scripts/install_bootsplash.sh" ]; then
    echo "Installing boot splash..."
    bash "${REPO_DIR}/scripts/install_bootsplash.sh" "" "${REPO_DIR}/config_frame.yaml" \
        || echo "WARNING: boot splash install failed (non-fatal)"
fi

# Disable login prompt on tty1 (permanently)
systemctl disable getty@tty1 2>/dev/null || true

# Disable desktop display managers that would otherwise take DRM/seat0.
deactivate_display_manager
assert_no_display_manager_conflict

# ---- E-ink extras: SPI on + the panel driver library ----
if [ "$VARIANT" = "eink" ]; then
    show "Preparing e-ink panel..."
    bash "${REPO_DIR}/scripts/setup_eink.sh" "${REPO_DIR}/config_frame.yaml" 2>&1 | tee -a "$SETUP_LOG" \
        || log "WARNING: e-ink setup reported errors (see above)"
fi

# ---- Enable services ----
show "Almost done..."
systemctl daemon-reload
systemctl enable photo_frame_server photo_frame_update.service photo_frame_update.timer
if [ "$VARIANT" = "eink" ]; then
    systemctl disable photo_frame_slideshow touch-watch 2>/dev/null || true
    systemctl enable photo_frame_eink
else
    systemctl disable photo_frame_eink 2>/dev/null || true
    systemctl enable photo_frame_slideshow touch-watch
fi

# ---- Set repo ownership ----
chown -R "${FRAME_USER}:${FRAME_USER}" "${REPO_DIR}"

# ---- Initialize git repo if missing (e.g. deployed from ZIP download) ----
if [ ! -d "${REPO_DIR}/.git" ]; then
    echo "Initializing git repo for auto-update..."
    su - "${FRAME_USER}" -c "cd ${REPO_DIR} && git init && git remote add origin https://github.com/rwkaspar/mimir.git && git fetch origin main && git reset --mixed origin/main" 2>/dev/null || true
fi

# ---- Ensure git branch is 'main' with correct upstream ----
su - "${FRAME_USER}" -c "cd ${REPO_DIR} && git branch -m master main 2>/dev/null; git branch --set-upstream-to=origin/main main 2>/dev/null" || true

# ---- Configure git safe directory (for auto-update as frame_user) ----
su - "${FRAME_USER}" -c "git config --global --add safe.directory ${REPO_DIR}" 2>/dev/null || true

# ---- Touchscreen (optional) ----
# A touch panel is NOT required: the whole setup runs from a phone (the frame
# shows a QR code), the on-panel menu is an extra for touch builds. So never
# block here — an earlier version waited for a USB replug and could hang the
# first boot for half an hour on a panel that enumerated late.
TOUCH_ID="27c0:0859"
if [ "$VARIANT" != "eink" ]; then
    if lsusb 2>/dev/null | grep -q "$TOUCH_ID"; then
        log "Touchscreen detected."
    else
        log "No touchscreen detected — continuing (setup works from the phone)."
    fi
fi

show "Setup complete!"
log "=== Setup complete! ==="

FIRST_RUN=false
if [ ! -f "${FRAME_HOME}/.photo-frame-setup-done" ]; then
    FIRST_RUN=true
fi

touch "${FRAME_HOME}/.photo-frame-setup-done"
systemctl disable photo-frame-firstboot.service 2>/dev/null || true

if [ "$FIRST_RUN" = true ]; then
    log "First run — rebooting..."
    sleep 3
    reboot
else
    log "Re-run complete — no reboot needed."
fi
