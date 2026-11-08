"""Native video interlude player.

On the Pi Zero 2 W the WPE/cog kiosk cannot decode slideshow videos — its GL
compositor eats the whole CMA pool, so the hardware H.264 decoder can't get
buffers and the screen goes black. Instead we play videos *natively*: stop the
kiosk (which frees CMA and the DRM master), decode each clip straight to the
panel with GStreamer's V4L2 hardware decoder and kmssink, then restart the
kiosk so the photo slideshow resumes.

A background scheduler interjects one video every `videos.native_interval_minutes`.
Videos live alongside photos in <photos_dir>/<orientation>/*.mp4 but are kept
out of the browser's /list (see routes.serve_photos_json), so cog only ever
shows photos.

Hard rules learned on this hardware:
  * The kiosk must be fully STOPPED (not just VT-switched) — it holds both the
    CMA and the DRM master, and VT-switch releases neither.
  * kmssink needs force-modesetting=true: with cog stopped nothing has set the
    display mode, so kmssink must do its own modeset (to a panel-native mode —
    the clip is encoded at the panel resolution for exactly this reason).
  * gst-launch must be shut down cleanly (natural EOS, or SIGINT) — NEVER
    SIGKILL: a hard kill orphans the VideoCore MMAL decode component and a few
    leaks exhaust the 64 MB gpu_mem pool until the next reboot.
"""
import glob
import logging
import os
import signal
import subprocess
import threading
import time

import yaml

logger = logging.getLogger(__name__)


class NativeVideoPlayer:
    """Plays videos by briefly dropping the kiosk and decoding to the panel."""

    def __init__(self, app, photos_dir):
        self.app = app
        self.photos_dir = str(photos_dir)
        self._lock = threading.Lock()
        self.playing = False
        self._rr = 0  # round-robin cursor

    def is_playing(self):
        with self._lock:
            return self.playing

    def _orientation(self):
        try:
            with open(self.app.config_path) as f:
                cfg = yaml.safe_load(f) or {}
            return cfg.get('frame', {}).get('orientation', 'horizontal')
        except Exception:
            return 'horizontal'

    def _videos_for_orientation(self):
        orient = self._orientation()
        pat = os.path.join(self.photos_dir, orient, '*.mp4')
        return sorted(glob.glob(pat))

    def play_next(self, count=1):
        """Play up to `count` videos for the current orientation (round-robin)."""
        vids = self._videos_for_orientation()
        if not vids:
            logger.debug("Native video: no videos for current orientation")
            return False
        start = self._rr % len(vids)
        self._rr = start + count
        chosen = [vids[(start + i) % len(vids)] for i in range(min(count, len(vids)))]
        return self.play(chosen)

    def play(self, paths):
        """Stop the kiosk, play each clip to the panel, restart the kiosk."""
        # Never interrupt sleep mode.
        energy = getattr(self.app, 'energy_save', None)
        if energy is not None and getattr(energy, 'sleeping', False):
            logger.debug("Native video: asleep, skipping")
            return False
        with self._lock:
            if self.playing:
                logger.info("Native video: already playing, skipping")
                return False
            self.playing = True
        try:
            self._stop_kiosk()
            for p in paths:
                self._play_one(p)
            return True
        except Exception as e:
            logger.error("Native video error: %s", e)
            return False
        finally:
            self._restore_kiosk()
            with self._lock:
                self.playing = False

    # --- display handoff ---

    def _stop_kiosk(self):
        # A boot splash can hold the DRM master; harmless if plymouth is absent.
        subprocess.run(['sudo', 'plymouth', 'quit'], capture_output=True, timeout=5)
        subprocess.run(['sudo', 'systemctl', 'stop', 'photo_frame_cage'],
                       capture_output=True, timeout=20)
        # Wait until cog is really gone (DRM released) before any kmssink modeset.
        for _ in range(40):
            if subprocess.run(['pgrep', '-x', 'cog'],
                              capture_output=True).returncode != 0:
                break
            time.sleep(0.5)
        time.sleep(1)

    def _restore_kiosk(self):
        # RESTART, not start: after a DRM grab cog otherwise comes back rendering
        # black. The restart also re-runs the modeset cog expects.
        subprocess.run(['sudo', 'systemctl', 'restart', 'photo_frame_cage'],
                       capture_output=True, timeout=20)
        time.sleep(1)
        # kmssink's modeset can leave the panel backlight off/dim; restore it.
        energy = getattr(self.app, 'energy_save', None)
        if energy is not None and hasattr(energy, '_backlight_on'):
            try:
                energy._backlight_on()
            except Exception as e:
                logger.warning("Native video: backlight restore failed: %s", e)

    # --- playback ---

    def _play_one(self, path):
        logger.info("Native video: playing %s", path)
        cmd = ['gst-launch-1.0', '-q',
               'filesrc', f'location={path}', '!', 'qtdemux', '!',
               'h264parse', '!', 'v4l2h264dec', '!',
               'kmssink', 'force-modesetting=true']
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        # gst-launch exits on natural EOS. Bound it by clip duration as a safety
        # net, but stop it with SIGINT (clean STREAMOFF), never SIGKILL.
        deadline = time.time() + self._duration(path) + 15
        while proc.poll() is None:
            if time.time() > deadline:
                logger.warning("Native video: watchdog expired, SIGINT %s", path)
                self._sigint(proc)
                break
            time.sleep(0.5)

    @staticmethod
    def _sigint(proc):
        for _ in range(2):
            try:
                proc.send_signal(signal.SIGINT)
                proc.wait(timeout=8)
                return
            except subprocess.TimeoutExpired:
                continue
            except Exception:
                return

    @staticmethod
    def _duration(path):
        try:
            out = subprocess.run(
                ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                 '-of', 'default=noprint_wrappers=1:nokey=1', path],
                capture_output=True, text=True, timeout=10)
            return max(5.0, float(out.stdout.strip()))
        except Exception:
            return 130.0


class VideoScheduler:
    """Daemon thread: interjects one native video every `interval_s` seconds."""

    def __init__(self, app, player, interval_s):
        self.app = app
        self.player = player
        self.interval_s = max(60, int(interval_s))
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("Video scheduler started (every %ds)", self.interval_s)

    def _enabled(self):
        try:
            with open(self.app.config_path) as f:
                cfg = yaml.safe_load(f) or {}
            v = cfg.get('videos', {}) or {}
            # Respect both the overall video switch and the native-player switch.
            return v.get('enabled', True) and v.get('native_enabled', True)
        except Exception:
            return True

    def _loop(self):
        while True:
            time.sleep(self.interval_s)
            try:
                if not self._enabled():
                    continue
                self.player.play_next(count=1)
            except Exception as e:
                logger.error("Video scheduler error: %s", e)
