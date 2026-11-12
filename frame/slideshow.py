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
        # Hot-corner hold feedback: a centred progress overlay (dimmed backdrop +
        # ring + caption) while the corner is held. _fb_lock serialises the two
        # threads that write fb0 (main slideshow + the TouchReader-driven overlay).
        # _showing_photo means fb0 is ours (a photo is up, or a video reveal is
        # active) so the overlay can be drawn directly. _hold_bg is the cached
        # dimmed backdrop PIL image for the current hold.
        self._fb_lock = threading.Lock()
        self._fbfd = None          # cached /dev/fb0 fd for direct writes
        self._cur_fb_raw = None
        self._showing_photo = False
        self._hold_bg = None
        # Over video the ring can't be drawn under kmssink; a hold instead sets
        # _hold_req, the player tears down, and the main loop runs _hold_overlay
        # to draw the ring on a revealed framebuffer. _hold_frac is the latest
        # progress from the TouchReader thread.
        self._hold_req = threading.Event()
        self._hold_frac = 0.0
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
        # Accept a few likely keys; default 30s (a calm pace for a photo frame).
        for k in ('interval', 'duration', 'photo_seconds', 'interval_seconds'):
            if k in ss:
                try:
                    return max(2, int(ss[k]))
                except (TypeError, ValueError):
                    pass
        return 30

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

    def _fb_fd(self):
        """A cached write fd for /dev/fb0. The frame user is in the 'video'
        group, so it can write the framebuffer directly — a ~5ms os.write vs a
        ~100ms+ `sudo dd` process spawn per frame (the old path). -1 if it can't
        be opened (then callers fall back to sudo dd)."""
        if self._fbfd is None:
            try:
                self._fbfd = os.open('/dev/fb0', os.O_WRONLY)
            except Exception as e:
                logger.warning('open /dev/fb0 failed (%s); using sudo dd', e)
                self._fbfd = -1
        return self._fbfd

    def _fb_write_bytes(self, data):
        with self._fb_lock:
            fd = self._fb_fd()
            if fd >= 0:
                try:
                    os.pwrite(fd, data, 0)
                    return
                except Exception as e:
                    logger.debug('direct fb write failed (%s); sudo dd', e)
            with open(self._RING_RAW, 'wb') as f:   # fallback
                f.write(data)
            self._dd(self._RING_RAW)

    def _fb_write_file(self, path):
        try:
            with open(path, 'rb') as f:
                self._fb_write_bytes(f.read())
        except Exception as e:
            logger.debug('fb write %s failed: %s', path, e)

    def _write_fb(self, raw_path):
        self._fb_write_file(raw_path)
        self._cur_fb_raw = raw_path

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
        """Blur-fill composite -> RGB565 raw at panel size. In vertical mounting
        we compose in LOGICAL portrait, then transpose 90° (CCW, matching the
        menu's FbCanvas rotate) so the raw is already at the physical landscape
        panel size — the Pi can't rotate the framebuffer, so we bake it in."""
        box = self._blur_radius()
        if self._orientation() == 'vertical':
            W, H = self.fb_h, self.fb_w        # compose portrait (e.g. 1200x1920)
            post = ',transpose=2'              # -> landscape, upright when turned
        else:
            W, H = self.fb_w, self.fb_h
            post = ''
        vf = (
            f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease,split=2[bg][fg];"
            f"[bg]scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H},boxblur={box}:1,eq=brightness=-0.2[bg2];"
            f"[bg2][fg]overlay=(W-w)/2:(H-h)/2{post},format={self._fb_pixfmt()}"
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

    # --- clock / date overlay (photos only) ---

    _WDAYS = ['Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa', 'So']
    _MONTHS = ['Jan', 'Feb', 'März', 'Apr', 'Mai', 'Jun',
               'Jul', 'Aug', 'Sep', 'Okt', 'Nov', 'Dez']

    def _clock_cfg(self):
        c = self._cfg().get('clock', {}) or {}
        return {
            'enabled': bool(c.get('enabled', False)),
            'size': c.get('size', 'm') if c.get('size') in ('s', 'm', 'l') else 'm',
            'position': c.get('position', 'br') if c.get('position') in
                        ('tl', 'tr', 'bl', 'br') else 'br',
            'show_date': bool(c.get('show_date', True)),
        }

    def _clock_lines(self, cfg):
        t = time.localtime()
        lines = [time.strftime('%H:%M', t)]
        if cfg['show_date']:
            lines.append('%s, %d. %s' % (self._WDAYS[t.tm_wday], t.tm_mday,
                                         self._MONTHS[t.tm_mon - 1]))
        return lines

    def _overlay_clock(self, base_raw, cfg=None):
        """Composite the clock/date onto a copy of the clean photo (base_raw, the
        RGB565 the panel is currently showing) and push it to fb0. The base file
        is never modified, so each minute's refresh redraws from the clean photo
        (no text stacking). Drawn in LOGICAL orientation then rotated into the
        native framebuffer — same pipeline as photos and the hold ring. Photos
        only; over video kmssink owns the display."""
        cfg = cfg or self._clock_cfg()
        if not cfg['enabled'] or not base_raw or not os.path.exists(base_raw):
            return
        try:
            from PIL import Image, ImageDraw
            import numpy as np
            vert = self._orientation() == 'vertical'
            data = open(base_raw, 'rb').read()
            a = np.frombuffer(data, dtype='<u2').astype(np.uint32)
            a = a[:self.fb_w * self.fb_h].reshape(self.fb_h, self.fb_w)
            r = ((a >> 11) & 0x1F) << 3
            g = ((a >> 5) & 0x3F) << 2
            b = (a & 0x1F) << 3
            im = Image.fromarray(np.dstack([r, g, b]).astype('uint8'), 'RGB')
            if vert:
                im = im.rotate(-90, expand=True)      # native -> logical portrait
            self._draw_clock(im, cfg)
            if vert:
                im = im.rotate(90, expand=True)       # logical -> native landscape
            arr = np.asarray(im, dtype=np.uint16)
            rr, gg, bb = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
            packed = (((rr & 0xF8) << 8) | ((gg & 0xFC) << 3) | (bb >> 3)).astype('<u2')
            self._fb_write_bytes(packed.tobytes())
        except Exception as e:
            logger.debug('clock overlay failed: %s', e)

    def _draw_clock(self, im, cfg):
        from PIL import ImageDraw
        d = ImageDraw.Draw(im)
        lw, lh = im.size
        base = min(lw, lh)
        sizes = {'s': 0.052, 'm': 0.075, 'l': 0.10}
        fs = max(20, int(base * sizes[cfg['size']]))
        try:
            from frame import native_ui
            time_font = native_ui.FbCanvas.font(fs)
            date_font = native_ui.FbCanvas.font(max(14, int(fs * 0.5)))
        except Exception:
            return
        lines = self._clock_lines(cfg)
        margin = int(base * 0.045)
        pos = cfg['position']
        right = pos in ('tr', 'br')
        bottom = pos in ('bl', 'br')
        x = (lw - margin) if right else margin
        anchor_h = 'r' if right else 'l'
        # Stack time (big) then date (small). Anchor the block to the chosen edge.
        fonts = [time_font] + ([date_font] if len(lines) > 1 else [])
        heights = [f.getbbox(t)[3] - f.getbbox(t)[1] for f, t in zip(fonts, lines)]
        gap = int(fs * 0.12)
        block_h = sum(heights) + gap * (len(lines) - 1)
        y = (lh - margin - block_h) if bottom else margin
        for f, text, h in zip(fonts, lines, heights):
            for dx, dy, col in ((2, 2, (0, 0, 0)), (0, 0, (255, 255, 255))):
                d.text((x + dx, y + dy), text, font=f, fill=col,
                       anchor=anchor_h + 'a')
            y += h + gap

    def _play_video(self, path):
        self._showing_photo = False     # over video the fb0 ring is invisible
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
            if self._hold_req.is_set():
                self._sigint(proc)    # reveal fb0 for the hold-progress ring
                break
            if self._stop.is_set() or time.time() > deadline:
                self._sigint(proc)
                break
            time.sleep(0.1)

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
        self._showing_photo = False     # over video the fb0 ring is invisible
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
                    if self._stop.is_set() or self._hold_req.is_set():
                        break
                    self._play_video(v)
            if self._menu_req.is_set() or self._hold_req.is_set():
                return  # let run() handle the menu / hold overlay
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
            if self._hold_req.is_set():
                gloop.quit()          # reveal fb0 for the hold-progress ring
                return False
            if self._stop.is_set():
                gloop.quit()
                return False
            if self._video_sig(self._collect_videos()) != start_sig:
                logger.info("Video wall: media changed, rebuilding")
                gloop.quit()
                return False
            return True

        GLib.timeout_add(150, tick)
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
                # Don't render on the touch thread (a render would block event
                # reads and swallow follow-up taps) — just enqueue; _open_menu
                # drains and renders on its own thread.
                self._ui.enqueue_tap(x, y)

        def on_hot_corner_fire():
            self._hold_bg = None       # the menu is about to take the framebuffer
            on_hot_corner()

        # Portrait mounting: the UI is composed in LOGICAL portrait coords and
        # rotated 90° CCW (PIL rotate(90) / ffmpeg transpose=2) into the native
        # landscape framebuffer. Touch therefore maps raw -> logical as the exact
        # inverse of that render rotation: logical_x = 1 - raw_y_frac,
        # logical_y = raw_x_frac  ==  swap + flip_x. Derived & confirmed from live
        # raw capture (the visual hot corner read raw≈(0,0) = native top-left).
        orient = self._orientation()
        if orient == 'vertical':
            tw, th = self.fb_h, self.fb_w   # logical portrait (e.g. 1200x1920)
            swap, flip_x, flip_y = True, True, False
        else:
            tw, th = self.fb_w, self.fb_h
            swap, flip_x, flip_y = False, False, False
        self._touch = native_ui.TouchReader(
            tw, th, on_tap=on_tap, on_hot_corner=on_hot_corner_fire,
            on_hot_progress=self._on_hot_progress, on_hot_cancel=self._on_hot_cancel,
            swap=swap, flip_x=flip_x, flip_y=flip_y)
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

    # --- hot-corner hold feedback (a centred progress overlay while held) ---

    _RING_RAW = '/tmp/slideshow_ring.raw'

    def _dd(self, raw_path):
        subprocess.run(['sudo', 'dd', f'if={raw_path}', 'of=/dev/fb0', 'bs=4M'],
                       capture_output=True, timeout=10)

    def _hold_is_vertical(self):
        return self._orientation() == 'vertical'

    def _build_hold_bg(self, from_raw):
        """A dimmed backdrop for the hold overlay: the current photo darkened,
        or a dark panel when none is available (e.g. a hold over video).

        Returned in LOGICAL orientation (portrait for a vertical mount): the
        cached framebuffer is native landscape, so for vertical we rotate it
        90° CW back to logical. _draw_hold composes the ring/caption on top in
        logical space and rotates the whole thing 90° CCW before packing, so the
        photo lands back in native orientation and the ring/text come out
        upright on the turned panel — same pipeline as the menu and photos."""
        from PIL import Image, ImageEnhance
        import numpy as np
        vert = self._hold_is_vertical()
        try:
            if from_raw and os.path.exists(from_raw):
                data = open(from_raw, 'rb').read()
                a = np.frombuffer(data, dtype='<u2').astype(np.uint32)
                a = a[:self.fb_w * self.fb_h].reshape(self.fb_h, self.fb_w)
                r = ((a >> 11) & 0x1F) << 3
                g = ((a >> 5) & 0x3F) << 2
                b = (a & 0x1F) << 3
                rgb = np.dstack([r, g, b]).astype('uint8')
                im = ImageEnhance.Brightness(
                    Image.fromarray(rgb, 'RGB')).enhance(0.25)
                if vert:
                    im = im.rotate(-90, expand=True)   # native -> logical portrait
                return im
        except Exception as e:
            logger.debug("hold backdrop from photo failed: %s", e)
        lw, lh = (self.fb_h, self.fb_w) if vert else (self.fb_w, self.fb_h)
        return Image.new('RGB', (lw, lh), (16, 18, 22))

    def _draw_hold(self, frac):
        """Draw the centred progress ring + caption on the cached backdrop and
        push it to fb0 (smooth PIL arc, not a blocky numpy ring)."""
        from PIL import ImageDraw
        import numpy as np
        if self._hold_bg is None:
            return
        im = self._hold_bg.copy()
        d = ImageDraw.Draw(im)
        lw, lh = im.size
        cx, cy = lw // 2, lh // 2
        R = max(60, min(lw, lh) // 7)
        th = max(12, R // 6)
        box = (cx - R, cy - R, cx + R, cy + R)
        d.arc(box, 0, 360, fill=(70, 78, 90), width=th)              # track
        frac = max(0.0, min(1.0, frac))
        if frac > 0:
            d.arc(box, -90, -90 + int(frac * 360), fill=(64, 142, 232), width=th)
        try:
            f = self._native_ui.FbCanvas.font(max(24, R // 3))
            d.text((cx, cy + R + int(R * 0.55)), 'Einstellungen', anchor='mm',
                   fill=(214, 218, 224), font=f)
        except Exception:
            pass
        if self._hold_is_vertical():
            im = im.rotate(90, expand=True)   # logical portrait -> native landscape
        a = np.asarray(im, dtype=np.uint16)
        r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]
        packed = (((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)).astype('<u2')
        self._fb_write_bytes(packed.tobytes())

    def _on_hot_progress(self, frac):
        # Ignore while the menu is already open.
        if self._ui is not None and self._ui.screen is not None:
            return
        self._hold_frac = frac
        if self._showing_photo:
            # fb0 is ours (a photo is up): draw the overlay directly.
            try:
                if self._hold_bg is None:
                    self._hold_bg = self._build_hold_bg(self._cur_fb_raw)
                self._draw_hold(frac)
            except Exception as e:
                logger.debug("hold draw failed: %s", e)
        elif frac >= 0.12 and not self._hold_req.is_set():
            # Over video kmssink hides fb0: ask the player to tear down so the
            # main loop can run _hold_overlay on a revealed fb0. Debounced so a
            # quick accidental corner brush doesn't interrupt playback.
            self._hold_req.set()

    def _on_hot_cancel(self):
        # Released / wandered out before the hold filled.
        self._hold_req.clear()
        had_bg = self._hold_bg is not None
        self._hold_bg = None
        if had_bg and self._showing_photo and self._cur_fb_raw:
            self._fb_write_file(self._cur_fb_raw)   # restore the clean photo
            clk = self._clock_cfg()
            if clk['enabled']:
                self._overlay_clock(self._cur_fb_raw, clk)   # and its clock

    def _hold_overlay(self):
        """A hold happened over video: the player has torn down, so fb0 is ours.
        Draw the progress overlay (dimmed last photo / dark) until the hold
        completes (menu opens) or is cancelled."""
        try:
            self._hold_bg = self._build_hold_bg(self._cur_fb_raw)
            self._showing_photo = True
            while (self._hold_req.is_set() and not self._menu_req.is_set()
                   and not self._stop.is_set()):
                self._draw_hold(self._hold_frac)
                time.sleep(0.05)
        except Exception as e:
            logger.debug("hold overlay failed: %s", e)
        finally:
            self._hold_bg = None
            self._showing_photo = False
            self._hold_req.clear()

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
            self._ui.clear_taps()           # drop any taps from before it opened
            self._ui.open()                 # renders the menu to fb0
        self._menu_activity = time.time()
        return True

    def _open_menu(self):
        """Show the settings menu and block playback until it closes."""
        self._menu_req.clear()
        self._hold_req.clear()
        if not self._prep_menu():
            return
        # Block the slideshow while the menu is open. The TouchReader thread only
        # enqueues taps; we drain + render them HERE so a slow render never
        # blocks event reads. Auto-close after inactivity so we never hang.
        while (self._ui.screen is not None and not self._stop.is_set()):
            if time.time() - self._menu_activity > 45:
                logger.info("Settings menu: auto-close (idle)")
                self._ui.screen = None
                break
            if not self._ui.process_pending():   # renders queued taps on this thread
                break
            time.sleep(0.03)
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
                if self._hold_req.is_set():
                    self._hold_overlay()
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
                    if self._hold_req.is_set():
                        self._hold_overlay()     # draw the ring on revealed fb0
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
                self._showing_photo = True     # enable hot-corner ring over it
                self._hold_req.clear()         # photo path draws directly, no reveal
                clk = self._clock_cfg()
                if clk['enabled']:
                    self._overlay_clock(raw, clk)
                # While this photo is up, render the NEXT photo into the other buf.
                nxt_item = self._next_photo(items, i)
                pending = None
                if nxt_item:
                    if self._render_photo(nxt_item, nxt):
                        pending = (nxt, nxt_item)
                    cur, nxt = nxt, cur  # swap buffers
                self._wait_photo(self._photo_seconds(), raw)

    def _wait_photo(self, seconds, base_raw):
        """Hold the current photo for `seconds`, but keep the clock current: when
        the wall-clock minute rolls over, redraw the clock from the clean base.
        Returns early on stop / menu / hold, exactly like _wait(). While the
        hot-corner hold is drawing its ring we skip the refresh so the two don't
        fight over fb0."""
        end = time.time() + seconds
        last_min = time.localtime().tm_min
        while time.time() < end:
            if self._stop.is_set() or self._menu_req.is_set() or self._hold_req.is_set():
                return
            m = time.localtime().tm_min
            if m != last_min:
                last_min = m
                clk = self._clock_cfg()
                if clk['enabled'] and self._showing_photo and not self._hold_req.is_set():
                    self._overlay_clock(base_raw, clk)
            time.sleep(0.1)

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
