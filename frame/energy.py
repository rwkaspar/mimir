"""Energy save management and system info caching."""

import os
import json
import logging
import subprocess
import threading
import time
import socket
from pathlib import Path

from .services import DISPLAY_SERVICE

logger = logging.getLogger(__name__)


class SysinfoCache:
    """Background cache for system info to avoid slow shell calls on each request."""

    def __init__(self, photos_base_dir):
        self.photos_base = Path(photos_base_dir)
        self.data = {}
        self._update()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while True:
            time.sleep(30)
            self._update()

    def _update(self):
        info = {'hostname': socket.gethostname(), 'ips': [], 'tailscale_ip': ''}
        try:
            result = subprocess.run(
                ['tailscale', 'ip', '-4'], capture_output=True, text=True, timeout=5)
            ts = result.stdout.strip()
            if ts and result.returncode == 0:
                info['tailscale_ip'] = ts
        except Exception:
            pass
        try:
            result = subprocess.run(
                ['hostname', '-I'], capture_output=True, text=True, timeout=5)
            all_ips = result.stdout.strip().split()
            ts_ip = info['tailscale_ip']
            for ip in all_ips:
                if ':' in ip:
                    continue  # skip IPv6
                if ip == ts_ip:
                    continue  # exact match
                # CGNAT fallback: if tailscale CLI failed, detect by range
                if not ts_ip and self._is_cgnat(ip):
                    info['tailscale_ip'] = ip
                    continue
                info['ips'].append(ip)
        except Exception:
            pass
        try:
            result = subprocess.run(
                ['iwgetid', '-r'], capture_output=True, text=True, timeout=5)
            info['wifi_ssid'] = result.stdout.strip()
        except Exception:
            info['wifi_ssid'] = ''
        try:
            with open('/sys/class/thermal/thermal_zone0/temp') as f:
                info['cpu_temp'] = round(int(f.read().strip()) / 1000, 1)
        except Exception:
            info['cpu_temp'] = None
        try:
            result = subprocess.run(
                ['df', '-h', '/'], capture_output=True, text=True, timeout=5)
            lines = result.stdout.strip().split('\n')
            if len(lines) > 1:
                parts = lines[1].split()
                info['disk'] = f"{parts[2]}/{parts[1]} ({parts[4]})"
        except Exception:
            info['disk'] = ''
        h_dir = self.photos_base / 'horizontal'
        v_dir = self.photos_base / 'vertical'
        h_count = len(list(h_dir.glob('*.jpg'))) if h_dir.exists() else 0
        v_count = len(list(v_dir.glob('*.jpg'))) if v_dir.exists() else 0
        h_videos = len(list(h_dir.glob('*.mp4'))) if h_dir.exists() else 0
        v_videos = len(list(v_dir.glob('*.mp4'))) if v_dir.exists() else 0
        info['h_photos'] = h_count
        info['v_photos'] = v_count
        info['h_videos'] = h_videos
        info['v_videos'] = v_videos
        info['photo_count'] = f"H: {h_count} / V: {v_count}"
        info['video_count'] = f"H: {h_videos} / V: {v_videos}"
        # Touchscreen detection
        try:
            result = subprocess.run(
                ['lsusb'], capture_output=True, text=True, timeout=5)
            info['touchscreen'] = '27c0:0859' in result.stdout
        except Exception:
            info['touchscreen'] = False
        self.data = info

    @staticmethod
    def _is_cgnat(ip):
        """Check if IP is in Tailscale CGNAT range 100.64.0.0/10."""
        try:
            parts = ip.split('.')
            if len(parts) != 4:
                return False
            first, second = int(parts[0]), int(parts[1])
            return first == 100 and 64 <= second <= 127
        except (ValueError, IndexError):
            return False

    def get(self):
        return self.data


class EnergySaveManager:
    """Manages sleep schedule by stopping/starting the display service.

    During sleep: stops the native slideshow (frees RAM + the HW video decoder
    so videos can transcode at full power, screen shows black console).
    On wake: restarts the slideshow.
    Touch-to-wake: reads raw input events during sleep to detect touch/tap.
    """

    SCHEDULE_FILE = '/tmp/frame-schedule.json'

    SLEEP_METHODS = ('hdmi', 'ddcci', 'dpms', 'brightness', 'black_only')

    def __init__(self, app_state=None):
        self.app = app_state
        self.enabled = False
        self.off_time = '22:00'
        self.on_time = '07:00'
        self.weekdays = [0, 1, 2, 3, 4, 5, 6]  # all days (JS getDay: 0=Sun)
        self.sleeping = False
        # True when the current sleep was requested on-demand (HTTP /sleep, HA
        # OFF, the wizard test) rather than by the schedule. The scheduler must
        # NOT auto-wake such a sleep just because the clock is outside the
        # configured off-window — otherwise a manual sleep is undone within one
        # 30s _check() tick. Cleared on any wake.
        self._manual_sleep = False
        self._wake_event = threading.Event()
        self._healing = False   # a wake-healer thread is retrying a dropped panel
        # Sleep method from config
        config = app_state.config if app_state else {}
        self.method = config.get('energy_save', {}).get('method', 'ddcci')
        self.brightness = int(config.get('energy_save', {}).get('brightness', 100))
        self._load()
        self._thread = None
        # Called whenever power/brightness state changes, so the MQTT bridge can
        # push the new state to Home Assistant. Set by the bridge if enabled.
        self.on_state_change = None

    def start(self):
        """Start the sleep-check loop. Call after syncer is initialized."""
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _load(self):
        try:
            with open(self.SCHEDULE_FILE) as f:
                data = json.load(f)
                self.enabled = data.get('enabled', False)
                self.off_time = data.get('off_time', '22:00')
                self.on_time = data.get('on_time', '07:00')
                self.weekdays = data.get('weekdays', [0, 1, 2, 3, 4, 5, 6])
        except Exception:
            pass

    def _save(self):
        try:
            with open(self.SCHEDULE_FILE, 'w') as f:
                json.dump({
                    'enabled': self.enabled,
                    'off_time': self.off_time,
                    'on_time': self.on_time,
                    'weekdays': self.weekdays,
                }, f)
        except Exception as e:
            logger.error(f"Error saving schedule: {e}")

    def is_sleeping(self):
        return self.sleeping

    def get_schedule(self):
        return {
            'enabled': self.enabled,
            'off_time': self.off_time,
            'on_time': self.on_time,
            'weekdays': self.weekdays,
            'sleeping': self.sleeping,
        }

    def update_schedule(self, data):
        self.enabled = data.get('enabled', self.enabled)
        self.off_time = data.get('off_time', self.off_time)
        self.on_time = data.get('on_time', self.on_time)
        if 'weekdays' in data:
            self.weekdays = data['weekdays']
        self._save()
        self._check()

    def wake_display(self):
        """Wake display immediately (runs the heavy wake off-thread)."""
        if self.sleeping:
            threading.Thread(target=lambda: self._set_sleep(False), daemon=True).start()

    def sleep_now(self):
        """Put the display to sleep immediately (HA/manual 'off')."""
        if not self.sleeping:
            # Mark this as a manual sleep so the scheduler's _check() loop does
            # not immediately wake it back up when we're outside the off-window.
            self._manual_sleep = True
            threading.Thread(target=lambda: self._set_sleep(True), daemon=True).start()

    def set_brightness(self, value):
        """Set the display backlight brightness (0-100) via DDC/CI (VCP 10)."""
        value = max(0, min(100, int(value)))
        self.brightness = value
        try:
            subprocess.run(['sudo', 'ddcutil', 'setvcp', '10', str(value)],
                           capture_output=True, timeout=10)
        except Exception as e:
            logger.warning(f"Failed to set brightness: {e}")
        self._notify_state()
        return value

    def get_brightness(self):
        return self.brightness

    def _notify_state(self):
        """Tell the MQTT bridge (if any) that power/brightness state changed."""
        cb = self.on_state_change
        if cb:
            try:
                cb()
            except Exception as e:
                logger.debug(f"state-change callback failed: {e}")

    def _wayland_env(self):
        """Get environment with Wayland display vars for wlr-randr/wlopm."""
        env = os.environ.copy()
        env['XDG_RUNTIME_DIR'] = '/tmp/frame-runtime'
        env['WAYLAND_DISPLAY'] = 'wayland-0'
        return env

    def _backlight_off(self):
        """Turn off backlight using configured method."""
        method = self.method
        if method == 'ddcci':
            subprocess.run(['sudo', 'ddcutil', 'setvcp', 'd6', '4'],
                         capture_output=True, timeout=10)
        elif method == 'dpms':
            # Run as our own user (the compositor runs as robert too); sudo would
            # strip WAYLAND_DISPLAY/XDG_RUNTIME_DIR so wlopm couldn't connect.
            subprocess.run(['wlopm', '--off', '*'],
                         capture_output=True, timeout=5, env=self._wayland_env())
        elif method == 'brightness':
            subprocess.run(['sudo', 'ddcutil', 'setvcp', '10', '0'],
                         capture_output=True, timeout=10)
        elif method == 'hdmi':
            subprocess.run(['wlr-randr', '--output', 'HDMI-A-1', '--off'],
                         capture_output=True, timeout=5, env=self._wayland_env())
        # black_only: no additional command (framebuffer already zeroed)

    def _display_is_on(self):
        """Panel power state via DDC/CI VCP D6: True = on, False = standby/off,
        None = unreachable (panel dropped off USB/DDC — the recurring
        over-current transient on this unit)."""
        try:
            out = subprocess.run(['sudo', 'ddcutil', 'getvcp', 'd6'],
                                 capture_output=True, text=True, timeout=8)
            s = (out.stdout or '') + (out.stderr or '')
            if 'Display not found' in s or 'communication failed' in s.lower():
                return None
            if 'DPM: On' in s or 'sl=0x01' in s:
                return True
            if 'DPM:' in s or 'sl=0x' in s:
                return False
        except Exception as e:
            logger.debug("getvcp d6 failed: %s", e)
        return None

    def _ddc_wake(self, retries=3):
        """Send the DDC wake (D6=1) and VERIFY the panel reports On. A single
        blind setvcp was unreliable: if the panel was asleep/half-dropped the
        command silently did nothing and the frame stayed black until someone
        intervened. Retry a few times; if it still won't confirm, hand off to a
        background healer that keeps trying until the panel re-appears."""
        for _ in range(max(1, retries)):
            subprocess.run(['sudo', 'ddcutil', 'setvcp', 'd6', '1'],
                           capture_output=True, timeout=10)
            if self._display_is_on() is True:
                return True
            time.sleep(2)
        self._start_wake_healer()
        return False

    def _start_wake_healer(self):
        """Background retry loop for a panel that won't wake (USB/DDC dropped).
        Re-issues the DDC wake every 60s until the panel confirms On or the frame
        is put back to sleep — so a panel that re-enumerates hours later comes
        back on its own instead of staying black until a manual wake."""
        if self._healing:
            return
        self._healing = True
        threading.Thread(target=self._wake_heal_loop, daemon=True).start()

    def _wake_heal_loop(self):
        logger.warning("Wake: panel not responding to DDC (likely USB/DDC "
                       "dropped) — healer will retry every 60s until it returns")
        try:
            for attempt in range(1, 181):   # cap ~3h of 60s ticks
                if self.sleeping:
                    return
                time.sleep(60)
                if self.sleeping:
                    return
                subprocess.run(['sudo', 'ddcutil', 'setvcp', 'd6', '1'],
                               capture_output=True, timeout=10)
                if self._display_is_on() is True:
                    logger.info("Wake healer: panel back On after %d retr%s",
                                attempt, 'y' if attempt == 1 else 'ies')
                    return
            logger.warning("Wake healer: gave up after ~3h, panel still "
                           "unreachable (needs physical power-cycle)")
        finally:
            self._healing = False

    def _backlight_on(self):
        """Turn on backlight using configured method."""
        method = self.method
        if method == 'ddcci':
            self._ddc_wake()
        elif method == 'dpms':
            subprocess.run(['wlopm', '--on', '*'],
                         capture_output=True, timeout=5, env=self._wayland_env())
        elif method == 'brightness':
            subprocess.run(['sudo', 'ddcutil', 'setvcp', '10', '100'],
                         capture_output=True, timeout=10)
        elif method == 'hdmi':
            subprocess.run(['wlr-randr', '--output', 'HDMI-A-1', '--on'],
                         capture_output=True, timeout=5, env=self._wayland_env())
        # black_only: no additional command

    def _set_sleep(self, sleep):
        try:
            if sleep and not self.sleeping:
                self.sleeping = True
                self._notify_state()  # reflect "off" in HA immediately
                # Stop the slideshow to free RAM + the HW decoder and release the
                # display (returns to Linux console/framebuffer).
                subprocess.run(['sudo', 'systemctl', 'stop', DISPLAY_SERVICE],
                             capture_output=True, timeout=15)
                time.sleep(1)
                # Fill framebuffer with black — keeps HDMI signal active
                # (avoids ANMITE "no signal" blue screen that DPMS causes)
                subprocess.run(['sudo', 'dd', 'if=/dev/zero', 'of=/dev/fb0',
                                'bs=1M', 'count=10'],
                             capture_output=True, timeout=5)
                # Hide console cursor and prevent text from appearing
                subprocess.run(['sudo', 'sh', '-c',
                                'setterm --cursor off --blank force --powerdown 0 > /dev/tty1'],
                             capture_output=True, timeout=5)
                # Start touch-to-wake listener BEFORE backlight off
                # (DDC/CI standby may cut USB power, killing the touchscreen)
                self._wake_event.clear()
                threading.Thread(target=self._touch_wake_listener, daemon=True).start()
                time.sleep(0.2)  # let listener open FDs while devices exist
                # Turn off backlight via configured method
                self._backlight_off()
                logger.info(f"Sleep: stopped display ({self.method}), framebuffer black")
                # Auto-update from GitHub (cage stopped = more RAM for git/pip)
                self._run_update()
                # Trigger photo sync during sleep
                syncer = self.app.syncer if self.app else None
                if syncer:
                    logger.info("Sleep: triggering photo sync (videos at full power)")
                    # Display is off during sleep → transcode videos now, using
                    # all cores at normal priority.
                    syncer.run_sync(allow_videos=True, full_power=True)
                else:
                    logger.warning("Sleep: syncer not initialized, skipping sync")
            elif not sleep and self.sleeping:
                self._wake_event.set()  # stop touch listener
                # Stop any running sync before starting cage (free RAM for Chromium)
                syncer = self.app.syncer if self.app else None
                if syncer:
                    syncer.stop()
                    # A running video transcode blocks the sync in p.join() (up
                    # to 20 min), so the stop flag alone won't free RAM in time.
                    # Kill the transcode now, BEFORE the display comes back —
                    # otherwise the slideshow's HW decoder and ffmpeg fight over
                    # the RAM. Killing ffmpeg also unblocks the
                    # sync worker so it can honour the stop. The partial video is
                    # redone on the next sleep.
                    subprocess.run(['pkill', '-9', '-f', 'ffmpeg'],
                                   capture_output=True)
                    # Wait briefly for the sync to actually stop
                    for _ in range(30):
                        if not syncer.get_status().get('running'):
                            break
                        time.sleep(1)
                    logger.info("Wake: stopped sync (killed any transcode)")
                # Start the slideshow again (it grabs DRM master + decoder)
                subprocess.run(['sudo', 'systemctl', 'start', DISPLAY_SERVICE],
                             capture_output=True, timeout=15)
                # Give it a moment to take the display before the backlight returns
                time.sleep(3)
                # Restore backlight via configured method
                self._backlight_on()
                self.sleeping = False
                self._manual_sleep = False  # any wake clears the manual override
                logger.info(f"Wake: started display service ({self.method})")
                self._notify_state()  # reflect "on" in HA
        except Exception as e:
            logger.error(f"Sleep control error: {e}")

    def _run_update(self):
        """Run git pull via update.sh during sleep."""
        config = self.app.config if self.app else {}
        repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        update_script = os.path.join(repo_dir, 'scripts', 'update.sh')
        if not os.path.exists(update_script):
            logger.warning("Sleep: update.sh not found, skipping update")
            return
        try:
            logger.info("Sleep: running git pull (update.sh)")
            result = subprocess.run(
                ['bash', update_script],
                capture_output=True, text=True, timeout=120,
                cwd=repo_dir)
            for line in result.stdout.strip().split('\n'):
                if line:
                    logger.info(f"Sleep update: {line}")
            if result.returncode != 0:
                logger.warning(f"Sleep update: exited {result.returncode}")
                if result.stderr:
                    logger.warning(f"Sleep update stderr: {result.stderr[:200]}")
        except subprocess.TimeoutExpired:
            logger.warning("Sleep update: timed out after 120s")
        except Exception as e:
            logger.error(f"Sleep update error: {e}")

    def test_sleep_method(self, method, duration=8):
        """Test a sleep method EXACTLY like real sleep does it.

        Real sleep stops the display service first, so the panel sees a static
        signal — which DDC/CI power-off (ddcci) needs to take effect; with the
        compositor still rendering, many monitors ignore it. A test that only
        toggled the backlight command (compositor running) was therefore
        misleading (ddcci looked like "no reaction"). So replicate the real
        flow: stop cage, black the framebuffer, run the method, wait, then
        restore. Does NOT run update/sync. Call from a background thread — this
        kills the kiosk, so the wizard page reloads afterwards.
        """
        saved = self.method
        self.method = method
        try:
            # Brief head start so the wizard's localStorage write (resume step)
            # is flushed to disk before cog is killed by stopping the kiosk.
            time.sleep(2)
            subprocess.run(['sudo', 'systemctl', 'stop', DISPLAY_SERVICE],
                           capture_output=True, timeout=15)
            time.sleep(1)
            subprocess.run(['sudo', 'dd', 'if=/dev/zero', 'of=/dev/fb0',
                            'bs=1M', 'count=10'], capture_output=True, timeout=5)
            subprocess.run(
                ['sudo', 'sh', '-c',
                 'setterm --cursor off --blank force --powerdown 0 > /dev/tty1'],
                capture_output=True, timeout=5)
            self._backlight_off()
            time.sleep(max(1, int(duration)))
        except Exception as e:
            logger.error(f"Sleep test error: {e}")
        finally:
            try:
                self._backlight_on()
            except Exception:
                pass
            # Always bring the display back, even if the method failed.
            subprocess.run(['sudo', 'systemctl', 'start', DISPLAY_SERVICE],
                           capture_output=True, timeout=15)
            self.method = saved

    def _touch_wake_listener(self):
        """Listen for touch/input events during sleep to trigger wake.

        Opens input devices early (before backlight off) so FDs exist even if
        DDC/CI standby later cuts USB power.  Re-scans periodically in case
        devices disappear and reappear.
        """
        import glob
        import select

        RESCAN_INTERVAL = 5  # seconds

        def open_devices():
            fds = []
            for dev in glob.glob('/dev/input/event*'):
                try:
                    fds.append(os.open(dev, os.O_RDONLY | os.O_NONBLOCK))
                except Exception:
                    pass
            return fds

        def close_fds(fds):
            for fd in fds:
                try:
                    os.close(fd)
                except Exception:
                    pass

        fds = open_devices()
        if fds:
            logger.info(f"Touch wake: listening on {len(fds)} input devices")
        else:
            logger.warning("Touch wake: no input devices found")
        last_rescan = time.time()

        try:
            while not self._wake_event.is_set():
                # Re-scan for devices if we lost them (USB power restored)
                if not fds and time.time() - last_rescan > RESCAN_INTERVAL:
                    fds = open_devices()
                    if fds:
                        logger.info(f"Touch wake: found {len(fds)} devices on rescan")
                    last_rescan = time.time()

                if not fds:
                    self._wake_event.wait(timeout=1.0)
                    continue

                try:
                    readable, _, _ = select.select(fds, [], [], 1.0)
                except (OSError, ValueError):
                    # FDs became invalid (USB disconnected by monitor standby)
                    close_fds(fds)
                    fds = []
                    logger.info("Touch wake: input devices lost, will re-scan")
                    continue

                for fd in readable:
                    try:
                        data = os.read(fd, 4096)
                        if data and self.sleeping:
                            logger.info("Touch wake: input detected, waking display")
                            self.wake_display()
                            return
                    except OSError:
                        # Single device disconnected
                        try:
                            os.close(fd)
                        except Exception:
                            pass
                        fds.remove(fd)
        finally:
            close_fds(fds)

    def _check(self):
        if not self.enabled:
            if self.sleeping:
                self._set_sleep(False)
            return
        should_sleep = self._in_off_period()
        if should_sleep and not self.sleeping:
            self._set_sleep(True)
        elif not should_sleep and self.sleeping and not self._manual_sleep:
            # Only auto-wake sleeps the SCHEDULE itself started. A manual/HA
            # sleep stays asleep until an explicit wake (HTTP /wake, HA ON, or a
            # touch), so an on-demand "off" isn't undone seconds later.
            self._set_sleep(False)

    def _loop(self):
        while True:
            self._check()
            time.sleep(30)

    def _in_off_period(self):
        from datetime import datetime
        now = datetime.now()
        now_mins = now.hour * 60 + now.minute
        # JS getDay(): 0=Sunday, 1=Monday, ..., 6=Saturday
        # Python weekday(): 0=Monday, ..., 6=Sunday → convert
        js_day = (now.weekday() + 1) % 7
        off_parts = self.off_time.split(':')
        on_parts = self.on_time.split(':')
        off_mins = int(off_parts[0]) * 60 + int(off_parts[1])
        on_mins = int(on_parts[0]) * 60 + int(on_parts[1])

        if off_mins > on_mins:
            # e.g. 22:00 - 07:00 (overnight)
            in_period = now_mins >= off_mins or now_mins < on_mins
            # Check weekday: if after off_time, check today; if before on_time, check yesterday
            if now_mins >= off_mins:
                day_to_check = js_day
            else:
                day_to_check = (js_day - 1) % 7
        else:
            # e.g. 01:00 - 06:00 (same day)
            in_period = off_mins <= now_mins < on_mins
            day_to_check = js_day

        if not in_period:
            return False
        return day_to_check in self.weekdays
