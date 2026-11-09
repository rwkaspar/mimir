"""Native on-panel UI toolkit — menu/settings drawn straight to the framebuffer.

This is the on-panel operator UI that replaces the cog/WPE browser kiosk. It is
*modal*: when the user taps the hot corner, the slideshow pauses, a full-screen
menu is drawn to /dev/fb0, and when the user leaves the menu the slideshow
resumes. We do NOT composite a transparent overlay over live video (that would
need a DRM overlay plane while kmssink holds the master) — the menu replaces the
picture for as long as it is open, which is the right model for a photo frame.

Scope (see memory native-slideshow-replaces-cog):
  * On-panel, native: hot corner -> menu, Wi-Fi onboarding, brightness, sleep,
    info/status. The Wi-Fi backend already exists in frame/wifi.py (WiFiManager:
    scan_networks / connect_to_network / get_status / start_hotspot).
  * Deep config (photo-source providers, tokens): stays the web UI on :8080,
    opened from a phone browser on the same network. No browser runs on the
    panel. cog is removed entirely.

Rendering: PIL composes the screen; it is packed to RGB565 (this board's fb0
format) and written with `sudo dd` — the same path frame/slideshow.py uses for
photos. Needs: python3-pil, python3-numpy.

Touch input lives in TouchReader (filled in once the touch panel is reliably
enumerated — it is a USB device prone to an over-current latch; see memory
touch-display-overcurrent). The renderer and screen model below do not depend on
it, so they can be built and eyeballed first.
"""
import logging
import os
import struct
import subprocess
import threading
import time

logger = logging.getLogger(__name__)

# mimir logo (RGBA, icon + wordmark) — installed splash asset, repo fallback.
LOGO_PATH = '/usr/share/plymouth/themes/photoframe/logo.png'
LOGO_PATH_REPO = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                              'imaging', 'plymouth', 'photoframe', 'logo.png')


def _fb_geometry():
    def _read(p, default):
        try:
            with open(p) as f:
                return f.read().strip()
        except Exception:
            return default
    w, h = (int(x) for x in _read('/sys/class/graphics/fb0/virtual_size',
                                   '1920,1200').split(','))
    bpp = int(_read('/sys/class/graphics/fb0/bits_per_pixel', '16'))
    return w, h, bpp


class FbCanvas:
    """A PIL drawing surface that flushes to /dev/fb0 as RGB565 (16bpp)."""

    def __init__(self):
        from PIL import Image
        self.w, self.h, self.bpp = _fb_geometry()
        self._Image = Image
        self.img = Image.new('RGB', (self.w, self.h), (0, 0, 0))

    def draw(self):
        from PIL import ImageDraw
        return ImageDraw.Draw(self.img)

    def clear(self, color=(0, 0, 0)):
        self.img.paste(color, (0, 0, self.w, self.h))

    _logo = None
    _logo_loaded = False

    def paste_logo(self, x=48, y=40, width=150):
        """Composite the mimir logo (RGBA) into the top-left corner."""
        if not FbCanvas._logo_loaded:
            FbCanvas._logo_loaded = True
            for p in (LOGO_PATH, LOGO_PATH_REPO):
                if p and os.path.exists(p):
                    try:
                        im = self._Image.open(p).convert('RGBA')
                        h = int(im.height * width / im.width)
                        FbCanvas._logo = im.resize((width, h))
                        break
                    except Exception as e:
                        logger.warning('logo load failed: %s', e)
        if FbCanvas._logo is not None:
            self.img.paste(FbCanvas._logo, (x, y), FbCanvas._logo)

    @staticmethod
    def font(size):
        """A scalable TrueType font, falling back to PIL's bitmap default."""
        from PIL import ImageFont
        for path in ('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
                     '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'):
            if os.path.exists(path):
                try:
                    return ImageFont.truetype(path, size)
                except Exception:
                    pass
        return ImageFont.load_default()

    def _to_rgb565(self):
        import numpy as np
        a = np.asarray(self.img, dtype=np.uint16)  # HxWx3
        r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]
        packed = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        return packed.astype('<u2').tobytes()

    def flush(self, tmp='/tmp/native_ui.raw'):
        """Pack to RGB565 and write to the framebuffer (root via sudo dd)."""
        with open(tmp, 'wb') as f:
            f.write(self._to_rgb565())
        subprocess.run(['sudo', 'dd', 'if=%s' % tmp, 'of=/dev/fb0', 'bs=4M'],
                       capture_output=True, timeout=10)


# --- screen / widget model -------------------------------------------------

# Colours (dark UI, legible on the panel).
BG = (16, 18, 22)
FG = (236, 238, 240)
MUTED = (150, 156, 164)
ACCENT = (64, 142, 232)
ACCENT_FG = (255, 255, 255)
ROW = (38, 43, 51)
CARD = (26, 30, 36)


class Button:
    """A tappable rectangle with a label. rect = (x0, y0, x1, y1)."""

    def __init__(self, key, label, rect, kind='row'):
        self.key = key
        self.label = label
        self.rect = rect
        self.kind = kind  # 'row' | 'primary' | 'back'

    def hit(self, x, y):
        x0, y0, x1, y1 = self.rect
        return x0 <= x <= x1 and y0 <= y <= y1


class Screen:
    """A list-style screen: title + stacked buttons. Subclasses add behaviour."""

    title = 'Menu'

    def __init__(self, ui):
        self.ui = ui
        self.buttons = []

    def build(self):
        """Override to populate self.buttons for the current canvas size."""
        return []

    def render(self, canvas):
        self.buttons = self.build()
        canvas.clear(BG)
        # mimir logo, top-left of the SCREEN (outside the card).
        canvas.paste_logo(x=48, y=40, width=150)
        px0, py0, px1, py1 = self.ui.panel_rect()
        pw, ph = px1 - px0, py1 - py0
        d = canvas.draw()
        # The modal card.
        d.rounded_rectangle((px0, py0, px1, py1), radius=24, fill=CARD)
        # Small screen title, top-centre of the card.
        title_font = canvas.font(max(18, ph // 22))
        d.text(((px0 + px1) // 2, py0 + int(ph * 0.10)), self.title,
               font=title_font, fill=MUTED, anchor='mm')
        # Close "x", top-right of the card (always closes the whole menu).
        cs = max(44, int(ph * 0.07))
        cx1, cy0 = px1 - int(pw * 0.04), py0 + int(ph * 0.03)
        self.close_rect = (cx1 - cs, cy0, cx1, cy0 + cs)
        d.ellipse(self.close_rect, fill=ROW)
        x_font = canvas.font(int(cs * 0.56))
        d.text(((self.close_rect[0] + self.close_rect[2]) // 2,
                (self.close_rect[1] + self.close_rect[3]) // 2 - 1),
               '×', font=x_font, fill=FG, anchor='mm')
        label_font = canvas.font(max(16, ph // 26))
        for b in self.buttons:
            x0, y0, x1, y1 = b.rect
            fill = ACCENT if b.kind == 'primary' else ROW
            tcol = ACCENT_FG if b.kind == 'primary' else FG
            if b.kind == 'back':
                fill, tcol = CARD, MUTED
            d.rounded_rectangle(b.rect, radius=12, fill=fill)
            d.text(((x0 + x1) // 2, (y0 + y1) // 2), b.label, font=label_font,
                   fill=tcol, anchor='mm')
        canvas.flush()

    close_rect = None

    def on_tap(self, x, y):
        if self.close_rect:
            cx0, cy0, cx1, cy1 = self.close_rect
            if cx0 <= x <= cx1 and cy0 <= y <= cy1:
                return 'close'
        for b in self.buttons:
            if b.hit(x, y):
                return self.handle(b.key)
        return None

    def handle(self, key):
        """Return a Screen to navigate to, 'close' to exit, or None to stay."""
        return None

    def _rows(self, keys_labels, kinds=None):
        """Lay out the buttons to fill the lower part of the modal card."""
        kinds = kinds or {}
        px0, py0, px1, py1 = self.ui.panel_rect()
        pw, ph = px1 - px0, py1 - py0
        pad = int(pw * 0.08)
        n = len(keys_labels)
        x0 = px0 + pad
        col_w = pw - 2 * pad
        top = py0 + int(ph * 0.18)              # below the title (logo is outside)
        bottom = py1 - pad
        gap = max(10, int(ph * 0.025))
        row_h = max(44, (bottom - top - gap * (n - 1)) // max(1, n))
        out = []
        for i, (key, label) in enumerate(keys_labels):
            y0 = top + i * (row_h + gap)
            out.append(Button(key, label, (x0, y0, x0 + col_w, y0 + row_h),
                              kind=kinds.get(key, 'row')))
        return out


class RootMenu(Screen):
    title = 'Einstellungen'

    def build(self):
        return self._rows([
            ('wifi', 'WLAN'),
            ('brightness', 'Helligkeit'),
            ('sleep', 'Jetzt schlafen'),
            ('info', 'Info'),
        ])

    def handle(self, key):
        if key == 'wifi':
            return WifiScreen(self.ui)
        if key == 'info':
            return InfoScreen(self.ui)
        if key == 'sleep':
            self.ui.action('sleep')
            return 'close'
        # 'brightness' -> a +/- screen (to be added)
        return None


class WifiScreen(Screen):
    title = 'WLAN'

    def build(self):
        nets = self.ui.wifi_networks()[:5] if self.ui else []
        rows = [('net:%s' % n['ssid'], '%s  %s' % (n['ssid'], n.get('signal', '')))
                for n in nets]
        rows.append(('back', 'Zurück'))
        kinds = {'back': 'back'}
        return self._rows(rows, kinds)

    def handle(self, key):
        if key == 'back':
            return RootMenu(self.ui)
        if key.startswith('net:'):
            # -> password entry (on-screen keyboard) then connect(ssid, pw)
            return None
        return None


class InfoScreen(Screen):
    title = 'Info'

    def build(self):
        return self._rows([('back', 'Zurück')], kinds={'back': 'back'})

    def render(self, canvas):
        super().render(canvas)
        d = canvas.draw()
        f = canvas.font(max(20, canvas.h // 34))
        lines = self.ui.info_lines() if self.ui else []
        y = int(canvas.h * 0.22)
        for ln in lines:
            d.text((int(canvas.w * 0.12), y), ln, font=f, fill=MUTED)
            y += int(canvas.h * 0.05)

    def handle(self, key):
        return RootMenu(self.ui) if key == 'back' else None


class NativeUI:
    """Owns the canvas + current screen; driven by taps from a TouchReader.

    Hooks (set by the host, e.g. the slideshow daemon):
      * wifi_networks() -> [{'ssid','signal','security'}]  (WiFiManager.scan)
      * action(name)    -> perform 'sleep' etc.
      * info_lines()    -> list[str] for the Info screen
    """

    def __init__(self, hooks=None, orientation='horizontal'):
        self.canvas = FbCanvas()
        self.hooks = hooks or {}
        self.orientation = orientation
        self.screen = None

    def panel_rect(self):
        """The centred modal panel the menu lives in, sized by orientation.

        Horizontal (landscape panel): 1/3 width x 2/3 height — a portrait card.
        Vertical: 2/3 width x 1/3 height ("andersrum"). NOTE: true vertical
        mounting also needs the canvas + touch rotated 90 deg; that is handled
        where the frame is actually portrait (TODO), this only sizes the panel.
        """
        w, h = self.canvas.w, self.canvas.h
        if self.orientation == 'vertical':
            pw, ph = int(w * 2 / 3), int(h / 3)
        else:
            pw, ph = int(w / 3), int(h * 2 / 3)
        x0, y0 = (w - pw) // 2, (h - ph) // 2
        return x0, y0, x0 + pw, y0 + ph

    # hook helpers
    def wifi_networks(self):
        fn = self.hooks.get('wifi_networks')
        try:
            return fn() if fn else []
        except Exception as e:
            logger.warning('wifi_networks hook failed: %s', e)
            return []

    def action(self, name):
        fn = self.hooks.get('action')
        if fn:
            fn(name)

    def info_lines(self):
        fn = self.hooks.get('info_lines')
        return fn() if fn else []

    def open(self):
        self.screen = RootMenu(self)
        self.screen.render(self.canvas)

    def tap(self, x, y):
        """Feed a screen-space tap; returns True while the menu stays open."""
        if self.screen is None:
            return False
        nxt = self.screen.on_tap(x, y)
        if nxt == 'close':
            self.screen = None
            return False
        if isinstance(nxt, Screen):
            self.screen = nxt
            self.screen.render(self.canvas)
        return True


# --- touch input -----------------------------------------------------------

# This panel: wch.cn TouchScreen on /dev/input/event2 (MT node). Raw ABS range
# X 0..16383, Y 0..9599; linear map to the 1920x1200 panel, origin top-left
# (verified against the live panel — no flip/swap for horizontal orientation).
TOUCH_NODE = '/dev/input/event2'
TOUCH_XMAX = 16383
TOUCH_YMAX = 9599
HOT_CORNER_PX = 200
HOLD_MS = 1500

_EV_FMT = 'llHHi'
_EV_SZ = struct.calcsize(_EV_FMT)
EV_KEY, EV_ABS = 1, 3
ABS_X, ABS_Y = 0x00, 0x01
BTN_TOUCH = 0x14A


class TouchReader:
    """Reads touch events (stdlib) and maps them to screen taps + hot corner.

    Calls on_tap(x, y) on a quick touch-release, and on_hot_corner() once when a
    touch is held in the top-right HOT_CORNER_PX square for HOLD_MS. Coordinates
    are mapped linearly from the panel's raw ABS range to screen pixels; flip_x/
    flip_y/swap cover other mountings/orientations.
    """

    def __init__(self, screen_w, screen_h, on_tap=None, on_hot_corner=None,
                 node=TOUCH_NODE, flip_x=False, flip_y=False, swap=False):
        self.w, self.h = screen_w, screen_h
        self.on_tap = on_tap
        self.on_hot_corner = on_hot_corner
        self.node = node
        self.flip_x, self.flip_y, self.swap = flip_x, flip_y, swap
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def _map(self, rx, ry):
        fx, fy = rx / TOUCH_XMAX, ry / TOUCH_YMAX
        if self.swap:
            fx, fy = fy, fx
        if self.flip_x:
            fx = 1.0 - fx
        if self.flip_y:
            fy = 1.0 - fy
        return int(fx * self.w), int(fy * self.h)

    def _in_hot_corner(self, x, y):
        return x >= self.w - HOT_CORNER_PX and y <= HOT_CORNER_PX

    def run(self):
        import select
        try:
            f = open(self.node, 'rb')
        except Exception as e:
            logger.error('TouchReader: cannot open %s: %s', self.node, e)
            return
        rx = ry = None
        down = False
        down_t = 0.0
        hot_fired = False
        while not self._stop.is_set():
            r, _, _ = select.select([f], [], [], 0.1)
            now = time.time()
            try:
                if (down and not hot_fired and rx is not None
                        and ry is not None):
                    x, y = self._map(rx, ry)
                    if (self._in_hot_corner(x, y)
                            and (now - down_t) * 1000 >= HOLD_MS):
                        hot_fired = True
                        if self.on_hot_corner:
                            self.on_hot_corner()
                if not r:
                    continue
                data = f.read(_EV_SZ)
                if not data or len(data) < _EV_SZ:
                    continue
                _s, _u, et, code, val = struct.unpack(_EV_FMT, data)
                if et == EV_ABS and code == ABS_X:
                    rx = val
                elif et == EV_ABS and code == ABS_Y:
                    ry = val
                elif et == EV_KEY and code == BTN_TOUCH:
                    if val == 1:
                        down, down_t, hot_fired = True, now, False
                    elif val == 0:
                        if (down and not hot_fired and rx is not None
                                and ry is not None):
                            x, y = self._map(rx, ry)
                            if (now - down_t) * 1000 < HOLD_MS and self.on_tap:
                                self.on_tap(x, y)
                        down = False
            except Exception as e:
                logger.warning('TouchReader: event handling error: %s', e)
        f.close()


if __name__ == '__main__':
    # Standalone test: draw the root menu and report taps (rendering + mapping).
    # Run only when the slideshow is stopped (both own fb0). Flags: flipx flipy
    # swap to correct the mapping if taps land wrong.
    import sys
    logging.basicConfig(level=logging.INFO)
    flip_x = 'flipx' in sys.argv
    flip_y = 'flipy' in sys.argv
    swap = 'swap' in sys.argv
    ui = NativeUI(hooks={
        'info_lines': lambda: ['native UI render test'],
        'wifi_networks': lambda: [{'ssid': 'TestNet', 'signal': '80'}],
        'action': lambda name: logger.info('action: %s', name),
    })
    ui.open()
    logger.info('Root menu drawn (%dx%d). Tap buttons; Ctrl-C to quit.',
                ui.canvas.w, ui.canvas.h)

    def on_tap(x, y):
        logger.info('TAP screen=(%d,%d)', x, y)
        still_open = ui.tap(x, y)
        if not still_open:
            logger.info('menu closed -> reopening for test')
            ui.open()

    reader = TouchReader(ui.canvas.w, ui.canvas.h, on_tap=on_tap,
                         on_hot_corner=lambda: logger.info('HOT CORNER'),
                         flip_x=flip_x, flip_y=flip_y, swap=swap)
    try:
        reader.run()
    except KeyboardInterrupt:
        reader.stop()
