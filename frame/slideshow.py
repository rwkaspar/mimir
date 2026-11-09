"""Native slideshow engine — photos AND videos, no browser.

Replaces the cog/WPE kiosk for the slideshow. Photos are blur-filled to the
panel resolution and written straight to the Linux framebuffer (/dev/fb0, RGB565
on this board); videos are hardware-decoded straight to the panel with
GStreamer + kmssink. Both share the one display with no compositor, so items —
photos and videos — play in a single shuffled sequence with no mode-switch
restart between them.

Design notes / hardware facts (see memory zero-video-ram-limit):
  * The kiosk (photo_frame_cage) must NOT run while this plays — it holds the
    CMA pool and DRM master. This daemon assumes it owns the display.
  * Photos: ffmpeg composites blur-fill -> rgb565le raw -> written to /dev/fb0.
    A still needs no DRM master, so it coexists with fbcon.
  * Videos: `gst-launch ... v4l2h264dec ! kmssink force-modesetting=true`. It
    grabs the DRM master for the clip, then releases it back to fbcon. Shut it
    down cleanly (EOS / SIGINT) — NEVER SIGKILL (leaks the MMAL decoder).
  * Resolution comes from the panel (fb0 geometry), capped for weak boards.
"""
import glob
import logging
import os
import random
import signal
import subprocess
import threading
import time

import yaml

logger = logging.getLogger(__name__)

PHOTO_EXTS = ('*.jpg', '*.jpeg', '*.png')
VIDEO_EXTS = ('*.mp4',)
# Upper resolution bound for a memory-constrained board (Pi Zero 2 W). The
# target is min(panel, this). 1920x1200 is validated; higher is unexplored.
LOW_MEM_MAX_DIM = 1920


class NativeSlideshow:
    def __init__(self, photos_dir, config_path, low_memory=True):
        self.photos_dir = str(photos_dir)
        self.config_path = str(config_path)
        self.low_memory = low_memory
        self.fb_w, self.fb_h, self.fb_bpp = self._fb_geometry()
        self._stop = threading.Event()
        # Daily-fixed play order: shuffled once per calendar day (and when the
        # media set changes), stable in between — see _daily_order().
        self._order = []
        self._order_key = None
        # Settings menu (hot corner): a TouchReader watches for a hold in the
        # top-right corner; _menu_req then pauses playback so the menu can own
        # the framebuffer. See _start_touch() / _open_menu().
        self._menu_req = threading.Event()
        self._ui = None
        self._touch = None
        self._native_ui = None
        self._menu_activity = 0.0
        # Target render size = panel, capped on weak boards.
        cap = LOW_MEM_MAX_DIM if low_memory else 10**9
        scale = min(1.0, cap / float(max(self.fb_w, self.fb_h)))
        self.out_w = (int(self.fb_w * scale) // 2) * 2
        self.out_h = (int(self.fb_h * scale) // 2) * 2
        logger.info("Slideshow: panel %dx%d bpp%d -> render %dx%d",
                    self.fb_w, self.fb_h, self.fb_bpp, self.out_w, self.out_h)

    # --- config ---

    def _cfg(self):
        try:
            with open(self.config_path) as f:
                return yaml.safe_load(f) or {}
        except Exception:
            return {}

    def _orientation(self):
        return self._cfg().get('frame', {}).get('orientation', 'horizontal')

    def _photo_seconds(self):
        ss = self._cfg().get('slideshow', {}) or {}
        # Accept a few likely keys; default 8s.
        for k in ('interval', 'duration', 'photo_seconds', 'interval_seconds'):
            if k in ss:
                try:
                    return max(2, int(ss[k]))
                except (TypeError, ValueError):
                    pass
        return 8

    def _blur_radius(self):
        photos = self._cfg().get('photos', {}) or {}
        try:
            return max(5, int(photos.get('blur_radius', 20)) // 2)
        except (TypeError, ValueError):
            return 10

    # --- framebuffer ---

    @staticmethod
    def _fb_geometry():
        def _read(p, default):
            try:
                with open(p) as f:
                    return f.read().strip()
            except Exception:
                return default
        size = _read('/sys/class/graphics/fb0/virtual_size', '1920,1200')
        w, h = (int(x) for x in size.split(','))
        bpp = int(_read('/sys/class/graphics/fb0/bits_per_pixel', '16'))
        return w, h, bpp

    def _fb_pixfmt(self):
        return 'rgb565le' if self.fb_bpp == 16 else 'bgra'

    def _write_fb(self, raw_path):
        # /dev/fb0 is root:video 660; use sudo dd (matches energy.py).
        subprocess.run(['sudo', 'dd', f'if={raw_path}', 'of=/dev/fb0', 'bs=4M'],
                       capture_output=True, timeout=10)

    # --- playlist ---

    def _videos_only(self):
        ss = self._cfg().get('slideshow', {}) or {}
        return bool(ss.get('videos_only', False))

    def _collect_videos(self):
        base = os.path.join(self.photos_dir, self._orientation())
        videos = []
        for pat in VIDEO_EXTS:
            videos += glob.glob(os.path.join(base, pat))
        return videos

    def _playlist(self):
        orient = self._orientation()
        base = os.path.join(self.photos_dir, orient)
        photos = []
        for pat in PHOTO_EXTS:
            photos += glob.glob(os.path.join(base, pat))
        videos = self._collect_videos()
        # Video-only mode: skip photos entirely (user wants videos back-to-back,
        # no photo hold between them). Falls back to photos only if no videos
        # exist, so the panel is never blank.
        if self._videos_only() and videos:
            items = videos
        else:
            items = photos + videos
        return self._daily_order(items)

    def _daily_order(self, items):
        """Return items in a shuffle that is fixed for the calendar day.

        The order is decided once per day (and whenever the media set changes),
        then stays stable so the rotation is predictable and there are no
        mid-day reshuffle jumps. A new day = a fresh order. This is decoupled
        from sleep, so it works the same on frames that never sleep: the new
        order simply takes effect at the first playlist rebuild after midnight
        (invisible if the frame is asleep then).
        """
        day = time.strftime('%Y-%m-%d', time.localtime())
        media_set = frozenset(items)
        key = (day, media_set)
        if key != self._order_key or not self._order:
            order = list(items)
            random.shuffle(order)
            self._order = order
            self._order_key = key
            logger.info("Slideshow: new play order for %s (%d items)",
                        day, len(order))
        return list(self._order)

    @staticmethod
    def _is_video(path):
        return path.lower().endswith('.mp4')

    # --- rendering ---

    def _render_photo(self, src, dst_raw):
        """Blur-fill composite -> RGB565 raw at panel size."""
        W, H, box = self.fb_w, self.fb_h, self._blur_radius()
        vf = (
            f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease,split=2[bg][fg];"
            f"[bg]scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H},boxblur={box}:1,eq=brightness=-0.2[bg2];"
            f"[bg2][fg]overlay=(W-w)/2:(H-h)/2,format={self._fb_pixfmt()}"
        )
        try:
            subprocess.run(
                ['ffmpeg', '-y', '-v', 'error', '-i', src,
                 '-filter_complex', vf, '-frames:v', '1', '-f', 'rawvideo', dst_raw],
                capture_output=True, timeout=30)
            return os.path.exists(dst_raw) and os.path.getsize(dst_raw) > 0
        except Exception as e:
            logger.warning("Render failed for %s: %s", src, e)
            return False

    def _play_video(self, path):
        logger.info("Slideshow video: %s", path)
        cmd = ['gst-launch-1.0', '-q',
               'filesrc', f'location={path}', '!', 'qtdemux', '!',
               'h264parse', '!', 'v4l2h264dec', '!',
               'kmssink', 'force-modesetting=true']
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        deadline = time.time() + self._duration(path) + 15
        while proc.poll() is None:
            if self._menu_req.is_set():
                self._prep_menu()     # draw menu to fb0 BEFORE kmssink releases
                self._sigint(proc)
                break
            if self._stop.is_set() or time.time() > deadline:
                self._sigint(proc)
                break
            time.sleep(0.3)

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
            return max(3.0, float(out.stdout.strip()))
        except Exception:
            return 60.0

    # --- video wall (video-only, gapless) ---

    _CONCAT_LIST = '/tmp/slideshow_concat.txt'
    _CONCAT_OUT = '/tmp/slideshow_concat.mp4'

    def _build_concat(self, videos):
        """Join the clips into ONE stream so a single decoder/kmssink plays them
        back-to-back with no per-clip modeset (= no black between clips).

        Our transcoder normalises every clip to the same codec params
        (Constrained Baseline, panel size, yuv420p, SAR 1:1), so a stream copy
        through the concat demuxer yields one continuous, seamlessly decodable
        file. Falls back to single-file playback if the join fails.
        """
        if len(videos) == 1:
            return videos[0]
        try:
            with open(self._CONCAT_LIST, 'w') as f:
                for v in videos:
                    f.write("file '%s'\n" % v.replace("'", "'\\''"))
            subprocess.run(
                ['ffmpeg', '-y', '-v', 'error', '-f', 'concat', '-safe', '0',
                 '-i', self._CONCAT_LIST, '-c', 'copy', '-movflags', '+faststart',
                 self._CONCAT_OUT],
                capture_output=True, timeout=120, check=True)
            if os.path.getsize(self._CONCAT_OUT) > 0:
                return self._CONCAT_OUT
        except Exception as e:
            logger.warning("Concat build failed (%s); playing clips singly", e)
        return None

    @staticmethod
    def _video_sig(videos):
        """A signature of the video set (path/size/mtime) to detect changes."""
        sig = []
        for v in sorted(videos):
            try:
                st = os.stat(v)
                sig.append((v, st.st_size, int(st.st_mtime)))
            except OSError:
                pass
        return tuple(sig)

    def _run_video_wall(self, videos):
        """Loop the clips as one gapless stream until stopped.

        Preferred path: one long-lived GStreamer pipeline that loops via a
        SEGMENT seek, so the decoder and the kmssink modeset stay up across the
        loop boundary — zero black between cycles. Falls back to relaunching
        gst-launch per cycle (a few seconds of black per loop) if python-gi /
        GStreamer bindings are unavailable.

        Order is already the daily-fixed shuffle from _playlist().
        """
        logger.info("Video wall: %d clip(s)", len(videos))
        try:
            import gi  # noqa: F401
            self._video_wall_seamless(videos)
            return
        except Exception as e:
            logger.warning("Seamless loop unavailable (%s); using relaunch loop", e)
        while not self._stop.is_set():
            joined = self._build_concat(videos)
            if joined:
                self._play_video(joined)
            else:
                for v in videos:
                    if self._stop.is_set():
                        break
                    self._play_video(v)
            if self._video_sig(self._collect_videos()) != self._video_sig(videos):
                return  # media changed -> let run() rebuild the playlist

    def _video_wall_seamless(self, videos):
        """One pipeline, looped with a non-flushing SEGMENT seek (no black)."""
        import gi
        gi.require_version('Gst', '1.0')
        from gi.repository import Gst, GLib
        Gst.init(None)

        joined = self._build_concat(videos) or videos[0]
        start_sig = self._video_sig(videos)
        pipe = Gst.parse_launch(
            'filesrc location=%s ! qtdemux ! h264parse ! v4l2h264dec ! '
            'kmssink force-modesetting=true' % joined)
        gloop = GLib.MainLoop()
        bus = pipe.get_bus()
        bus.add_signal_watch()

        def seg_seek(flush):
            flags = Gst.SeekFlags.SEGMENT
            if flush:
                flags |= Gst.SeekFlags.FLUSH
            return pipe.seek(1.0, Gst.Format.TIME, flags,
                             Gst.SeekType.SET, 0, Gst.SeekType.NONE, 0)

        def on_msg(_bus, msg):
            t = msg.type
            if t == Gst.MessageType.SEGMENT_DONE:
                seg_seek(False)          # seamless: no flush, no modeset
            elif t == Gst.MessageType.EOS:
                gloop.quit()
            elif t == Gst.MessageType.ERROR:
                err, dbg = msg.parse_error()
                logger.error("Video wall gst error: %s (%s)", err, dbg)
                gloop.quit()

        bus.connect('message', on_msg)

        def tick():
            # Exit the loop on stop, on a menu request (so the menu can take the
            # framebuffer), or when the video set changed. False stops the timer.
            if self._menu_req.is_set():
                self._prep_menu()     # draw menu to fb0 BEFORE kmssink releases
                gloop.quit()
                return False
            if self._stop.is_set():
                gloop.quit()
                return False
            if self._video_sig(self._collect_videos()) != start_sig:
                logger.info("Video wall: media changed, rebuilding")
                gloop.quit()
                return False
            return True

        GLib.timeout_add(1000, tick)
        pipe.set_state(Gst.State.PAUSED)
        pipe.get_state(Gst.CLOCK_TIME_NONE)
        if not seg_seek(True):
            logger.warning("Video wall: initial segment seek failed")
        pipe.set_state(Gst.State.PLAYING)
        try:
            gloop.run()
        finally:
            pipe.set_state(Gst.State.NULL)   # clean teardown, no MMAL leak

    # --- settings menu (hot corner) ---

    def _wait(self, seconds):
        """Sleep up to `seconds`, returning early on stop or a menu request."""
        end = time.time() + seconds
        while time.time() < end:
            if self._stop.is_set() or self._menu_req.is_set():
                return
            time.sleep(0.1)

    def _start_touch(self):
        """Start the TouchReader that opens the settings menu from a hot-corner
        hold and drives it while open. No-op if native UI deps are missing."""
        try:
            from frame import native_ui
        except Exception as e:
            logger.warning("Native UI unavailable, no touch menu: %s", e)
            return
        self._native_ui = native_ui

        def on_hot_corner():
            if self._ui is None or self._ui.screen is None:
                logger.info("Hot corner: opening settings")
                self._menu_req.set()

        def on_tap(x, y):
            if self._ui is not None and self._ui.screen is not None:
                self._menu_activity = time.time()
                self._ui.tap(x, y)

        self._touch = native_ui.TouchReader(
            self.fb_w, self.fb_h, on_tap=on_tap, on_hot_corner=on_hot_corner)
        threading.Thread(target=self._touch.run, daemon=True).start()
        logger.info("Touch menu armed (hot corner: hold top-right)")

    def _menu_hooks(self):
        return {
            'wifi_networks': lambda: [],   # TODO: wire WiFiManager.scan_networks
            'action': self._menu_action,
            'info_lines': self._info_lines,
        }

    def _menu_action(self, name):
        # The server owns sleep/power (energy.py has touch-to-wake). These are
        # POST endpoints on localhost:8080.
        endpoint = {'sleep': '/sleep', 'wake': '/wake',
                    'shutdown': '/shutdown', 'reboot': '/reboot'}.get(name)
        if not endpoint:
            return
        try:
            import urllib.request
            req = urllib.request.Request('http://localhost:8080' + endpoint,
                                         data=b'', method='POST')
            urllib.request.urlopen(req, timeout=5)
        except Exception as e:
            logger.warning("action %s failed: %s", name, e)

    def _info_lines(self):
        import socket
        try:
            ip = socket.gethostbyname(socket.gethostname())
        except Exception:
            ip = '?'
        return ['mimir', 'IP: %s' % ip]

    def _prep_menu(self):
        """Render the menu into the framebuffer. Called BEFORE a video pipeline
        is torn down so that when kmssink releases the display, fb0 already shows
        the menu instead of the stale last photo (no photo flash)."""
        if self._native_ui is None:
            return False
        if self._ui is None:
            self._ui = self._native_ui.NativeUI(
                hooks=self._menu_hooks(), orientation=self._orientation())
        if self._ui.screen is None:
            self._ui.open()                 # renders the menu to fb0
        self._menu_activity = time.time()
        return True

    def _open_menu(self):
        """Show the settings menu and block playback until it closes."""
        self._menu_req.clear()
        if not self._prep_menu():
            return
        # Block the slideshow while the menu is open; taps are handled by the
        # TouchReader thread. Auto-close after inactivity so we never hang.
        while (self._ui.screen is not None and not self._stop.is_set()):
            if time.time() - self._menu_activity > 45:
                logger.info("Settings menu: auto-close (idle)")
                self._ui.screen = None
                break
            time.sleep(0.1)
        logger.info("Settings menu closed, resuming slideshow")

    # --- main loop ---

    def stop(self):
        self._stop.set()

    def run(self):
        logger.info("Native slideshow starting")
        self._start_touch()
        buf_a = '/tmp/slideshow_a.raw'
        buf_b = '/tmp/slideshow_b.raw'
        cur, nxt = buf_a, buf_b
        while not self._stop.is_set():
            if self._menu_req.is_set():
                self._open_menu()
                continue
            items = self._playlist()
            if not items:
                logger.info("Slideshow: no media, waiting")
                self._stop.wait(10)
                continue
            # Video-only mode: play all clips as one gapless stream, loop.
            if self._videos_only() and all(self._is_video(x) for x in items):
                self._run_video_wall(items)
                if self._menu_req.is_set():
                    self._open_menu()
                continue
            # Pre-render the first photo so the loop can double-buffer.
            pending = None  # (raw_path, src) already rendered, ready to show
            for i, item in enumerate(items):
                if self._stop.is_set():
                    break
                if self._menu_req.is_set():
                    self._open_menu()
                    break  # rebuild playlist after the menu
                if self._is_video(item):
                    pending = None
                    self._play_video(item)
                    if self._menu_req.is_set():
                        self._open_menu()
                        break
                    continue
                # Photo: use the pre-rendered buffer if it matches, else render now.
                if pending and pending[1] == item:
                    raw = pending[0]
                else:
                    if not self._render_photo(item, cur):
                        continue
                    raw = cur
                self._write_fb(raw)
                # While this photo is up, render the NEXT photo into the other buf.
                nxt_item = self._next_photo(items, i)
                pending = None
                if nxt_item:
                    if self._render_photo(nxt_item, nxt):
                        pending = (nxt, nxt_item)
                    cur, nxt = nxt, cur  # swap buffers
                self._wait(self._photo_seconds())

    def _next_photo(self, items, i):
        for j in range(i + 1, len(items)):
            if not self._is_video(items[j]):
                return items[j]
        return None


def main():
    import sys
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    config_path = sys.argv[1] if len(sys.argv) > 1 else 'config_frame.yaml'
    cfg = {}
    try:
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        pass
    photos_dir = (cfg.get('photos', {}) or {}).get('base_dir', '/srv/frame/photos')
    show = NativeSlideshow(photos_dir, config_path, low_memory=True)

    # Set the stop flag on SIGTERM/SIGINT so the loop (and any playing video)
    # shuts down cleanly — a hard kill would leak the MMAL decoder component.
    def _handle(signum, frame):
        logger.info("Slideshow: signal %d, stopping", signum)
        show.stop()
    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    show.run()


if __name__ == '__main__':
    main()
