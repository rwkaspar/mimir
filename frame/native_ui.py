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
import re
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
        """Pack to RGB565 and write the framebuffer. The frame user is in the
        'video' group, so write /dev/fb0 directly (~5ms) instead of spawning a
        `sudo dd` per render (the old path, ~100ms+); fall back to sudo dd if the
        direct open fails."""
        data = self._to_rgb565()
        try:
            fd = os.open('/dev/fb0', os.O_WRONLY)
            try:
                os.pwrite(fd, data, 0)
            finally:
                os.close(fd)
            return
        except Exception as e:
            logger.debug('direct fb write failed (%s); sudo dd', e)
        with open(tmp, 'wb') as f:
            f.write(data)
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
DANGER = (184, 54, 54)


def _hit(rect, x, y):
    if not rect:
        return False
    x0, y0, x1, y1 = rect
    return x0 <= x <= x1 and y0 <= y <= y1


class Widget:
    """One stacked row on a Screen. `weight` sizes its share of the card; a tap
    returns a nav target (Screen), 'close', or None. Subclasses draw + react."""

    weight = 1.0

    def __init__(self, key, kind='row'):
        self.key = key
        self.kind = kind
        self.rect = None

    def hit(self, x, y):
        return _hit(self.rect, x, y)

    def render(self, scr, d, canvas, font):
        pass

    def tap(self, scr, x, y):
        return None


class Row(Widget):
    """A plain tappable row: navigates (target) or runs an action (on_tap)."""

    def __init__(self, key, label, target=None, on_tap=None, kind='row',
                 value=None):
        super().__init__(key, kind)
        self.label = label
        self.target = target     # callable -> Screen
        self.on_tap = on_tap     # callable -> None | 'close' | Screen
        self.value = value       # optional right-aligned status text

    def render(self, scr, d, canvas, font):
        x0, y0, x1, y1 = self.rect
        fill, tcol = ROW, FG
        if self.kind == 'primary':
            fill, tcol = ACCENT, ACCENT_FG
        elif self.kind == 'danger':
            fill, tcol = DANGER, ACCENT_FG
        elif self.kind == 'back':
            fill, tcol = CARD, MUTED
        d.rounded_rectangle(self.rect, radius=12, fill=fill)
        pad = int((y1 - y0) * 0.32)
        d.text((x0 + pad, (y0 + y1) // 2), self.label, font=font, fill=tcol,
               anchor='lm')
        if self.value is not None:
            d.text((x1 - pad, (y0 + y1) // 2), str(self.value), font=font,
                   fill=MUTED, anchor='rm')
        elif self.target is not None and self.kind != 'back':
            cy = (y0 + y1) // 2
            cx = x1 - pad
            s = max(7, int((y1 - y0) * 0.12))
            d.polygon([(cx - s, cy - s), (cx, cy), (cx - s, cy + s)], fill=MUTED)

    def tap(self, scr, x, y):
        if self.on_tap:
            return self.on_tap()
        if self.target:
            return self.target()
        return None


class Toggle(Widget):
    """Label + ON/OFF pill. get() -> bool, set(bool) applies + persists."""

    def __init__(self, key, label, get, set_):
        super().__init__(key)
        self.label = label
        self.get = get
        self.set_ = set_

    def render(self, scr, d, canvas, font):
        x0, y0, x1, y1 = self.rect
        d.rounded_rectangle(self.rect, radius=12, fill=ROW)
        pad = int((y1 - y0) * 0.32)
        d.text((x0 + pad, (y0 + y1) // 2), self.label, font=font, fill=FG,
               anchor='lm')
        on = bool(self.get())
        ph = min(int((y1 - y0) * 0.44), 44)     # compact switch, capped height
        pw = int(ph * 1.9)
        px1 = x1 - pad
        px0 = px1 - pw
        pcy = (y0 + y1) // 2
        track = ACCENT if on else (70, 78, 90)
        d.rounded_rectangle((px0, pcy - ph // 2, px1, pcy + ph // 2),
                            radius=ph // 2, fill=track)
        kr = ph // 2 - 3
        kx = px1 - kr - 3 if on else px0 + kr + 3
        d.ellipse((kx - kr, pcy - kr, kx + kr, pcy + kr), fill=(255, 255, 255))

    def tap(self, scr, x, y):
        try:
            self.set_(not bool(self.get()))
        except Exception as e:
            logger.warning('toggle %s failed: %s', self.key, e)
        return 'rerender'


class Stepper(Widget):
    """Label + value + [−]/[+] buttons. dec()/inc() mutate; fmt() shows value."""

    def __init__(self, key, label, fmt, dec, inc):
        super().__init__(key)
        self.label = label
        self.fmt = fmt
        self.dec = dec
        self.inc = inc
        self._minus = self._plus = None

    def render(self, scr, d, canvas, font):
        x0, y0, x1, y1 = self.rect
        d.rounded_rectangle(self.rect, radius=12, fill=ROW)
        pad = int((y1 - y0) * 0.32)
        d.text((x0 + pad, (y0 + y1) // 2), self.label, font=font, fill=FG,
               anchor='lm')
        bs = int((y1 - y0) * 0.66)
        cy = (y0 + y1) // 2
        pb1 = x1 - pad
        self._plus = (pb1 - bs, cy - bs // 2, pb1, cy + bs // 2)
        val_w = int(bs * 2.4)
        self._minus = (pb1 - bs - val_w - bs, cy - bs // 2, pb1 - bs - val_w, cy + bs // 2)
        for rect, plus in ((self._minus, False), (self._plus, True)):
            d.rounded_rectangle(rect, radius=10, fill=(70, 78, 90))
            mx = (rect[0] + rect[2]) // 2
            my = (rect[1] + rect[3]) // 2
            arm = int((rect[2] - rect[0]) * 0.26)
            d.line((mx - arm, my, mx + arm, my), fill=FG, width=3)   # minus bar
            if plus:
                d.line((mx, my - arm, mx, my + arm), fill=FG, width=3)  # vertical
        d.text(((self._minus[2] + self._plus[0]) // 2, cy), self.fmt(),
               font=font, fill=FG, anchor='mm')

    def tap(self, scr, x, y):
        try:
            if _hit(self._minus, x, y):
                self.dec()
                return 'rerender'
            if _hit(self._plus, x, y):
                self.inc()
                return 'rerender'
        except Exception as e:
            logger.warning('stepper %s failed: %s', self.key, e)
        return None


class Slider(Widget):
    """Label + draggable/ tappable track, 0..100. get()->int, set(int) applies."""

    weight = 1.5

    def __init__(self, key, label, get, set_, lo=0, hi=100):
        super().__init__(key)
        self.label = label
        self.get = get
        self.set_ = set_
        self.lo, self.hi = lo, hi
        self._track = None

    def render(self, scr, d, canvas, font):
        x0, y0, x1, y1 = self.rect
        d.rounded_rectangle(self.rect, radius=12, fill=ROW)
        pad = int((y1 - y0) * 0.24)
        val = max(self.lo, min(self.hi, int(self.get())))
        d.text((x0 + pad, y0 + pad), self.label, font=font, fill=FG, anchor='lm')
        d.text((x1 - pad, y0 + pad), '%d%%' % val, font=font, fill=MUTED,
               anchor='rm')
        tx0, tx1 = x0 + pad, x1 - pad
        ty = y1 - pad - 4
        self._track = (tx0, ty - 14, tx1, ty + 14)
        d.rounded_rectangle((tx0, ty - 6, tx1, ty + 6), radius=6, fill=(70, 78, 90))
        frac = (val - self.lo) / float(self.hi - self.lo or 1)
        kx = int(tx0 + frac * (tx1 - tx0))
        d.rounded_rectangle((tx0, ty - 6, kx, ty + 6), radius=6, fill=ACCENT)
        d.ellipse((kx - 15, ty - 15, kx + 15, ty + 15), fill=(255, 255, 255))

    def tap(self, scr, x, y):
        if not self._track:
            return None
        tx0, _, tx1, _ = self._track
        frac = max(0.0, min(1.0, (x - tx0) / float(tx1 - tx0 or 1)))
        try:
            self.set_(int(round(self.lo + frac * (self.hi - self.lo))))
        except Exception as e:
            logger.warning('slider %s failed: %s', self.key, e)
        return 'rerender'


class Days(Widget):
    """7 weekday toggles (Mo..So). get()->set of JS-getDay ints (0=Sun..6=Sat),
    set(list) persists. Rendered Mon-first for humans, mapped back on tap."""

    weight = 1.1
    _LABELS = ['Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa', 'So']
    _JS = [1, 2, 3, 4, 5, 6, 0]   # column i -> JS getDay value

    def __init__(self, key, get, set_):
        super().__init__(key)
        self.get = get
        self.set_ = set_
        self._cells = []

    def render(self, scr, d, canvas, font):
        x0, y0, x1, y1 = self.rect
        active = set(self.get() or [])
        n = 7
        gap = 8
        cw = (x1 - x0 - gap * (n - 1)) // n
        self._cells = []
        sfont = canvas.font(max(14, (y1 - y0) // 3))
        for i in range(n):
            cx0 = x0 + i * (cw + gap)
            rect = (cx0, y0, cx0 + cw, y1)
            self._cells.append(rect)
            on = self._JS[i] in active
            d.rounded_rectangle(rect, radius=10, fill=ACCENT if on else ROW)
            d.text(((cx0 + cx0 + cw) // 2, (y0 + y1) // 2), self._LABELS[i],
                   font=sfont, fill=ACCENT_FG if on else MUTED, anchor='mm')

    def tap(self, scr, x, y):
        for i, rect in enumerate(self._cells):
            if _hit(rect, x, y):
                active = set(self.get() or [])
                js = self._JS[i]
                active.discard(js) if js in active else active.add(js)
                try:
                    self.set_(sorted(active))
                except Exception as e:
                    logger.warning('days set failed: %s', e)
                return 'rerender'
        return None


class InfoBlock(Widget):
    """A non-interactive multi-line text block. lines() -> list[str]."""

    def __init__(self, key, lines, weight=2.2):
        super().__init__(key)
        self.lines = lines
        self.weight = weight

    def render(self, scr, d, canvas, font):
        x0, y0, x1, y1 = self.rect
        lines = self.lines() if callable(self.lines) else (self.lines or [])
        n = max(1, len(lines))
        lh = min(44, (y1 - y0) // n)               # cap line height (no spreading)
        sfont = canvas.font(max(16, int(lh * 0.6)))
        ly = y0 + lh // 2
        for ln in lines:
            d.text((x0 + 6, int(ly)), ln, font=sfont, fill=MUTED, anchor='lm')
            ly += lh


class QRBlock(Widget):
    """A QR code (from the server's /api/qrcode matrix) + a caption."""

    weight = 3.2

    def __init__(self, key, modules, caption=''):
        super().__init__(key)
        self.modules = modules      # list[list[int]] or None
        self.caption = caption

    def render(self, scr, d, canvas, font):
        x0, y0, x1, y1 = self.rect
        cap_h = int((y1 - y0) * 0.16)
        area = min(x1 - x0, (y1 - y0) - cap_h)
        mods = self.modules
        if mods:
            n = len(mods)
            quiet = 2
            cell = area // (n + 2 * quiet)
            side = cell * (n + 2 * quiet)
            ox = x0 + ((x1 - x0) - side) // 2
            oy = y0
            d.rectangle((ox, oy, ox + side, oy + side), fill=(255, 255, 255))
            for r, row in enumerate(mods):
                for c, v in enumerate(row):
                    if v:
                        cx = ox + (c + quiet) * cell
                        cy = oy + (r + quiet) * cell
                        d.rectangle((cx, cy, cx + cell, cy + cell), fill=(0, 0, 0))
        else:
            d.text(((x0 + x1) // 2, y0 + area // 2), 'QR …', font=font,
                   fill=MUTED, anchor='mm')
        if self.caption:
            d.text(((x0 + x1) // 2, y1 - cap_h // 2), self.caption, font=font,
                   fill=MUTED, anchor='mm')


class Screen:
    """A card holding a vertical stack of widgets. Subclasses supply widgets()."""

    title = 'Menü'
    close_rect = None

    def __init__(self, ui):
        self.ui = ui
        self._widgets = []

    def widgets(self):
        """Override: return the list of Widgets for this screen."""
        return []

    def back_row(self, label='Zurück'):
        return Row('back', label, target=lambda: RootMenu(self.ui), kind='back')

    def act(self, path, body=None, result='close'):
        """A row handler that POSTs to an endpoint and then closes (or rerenders)."""
        def fn():
            self.ui.api_post(path, body)
            return result
        return fn

    def render(self, canvas):
        self._widgets = self.widgets()
        canvas.clear(BG)
        canvas.paste_logo(x=48, y=40, width=150)
        px0, py0, px1, py1 = self.ui.panel_rect()
        pw, ph = px1 - px0, py1 - py0
        d = canvas.draw()
        d.rounded_rectangle((px0, py0, px1, py1), radius=24, fill=CARD)
        title_font = canvas.font(max(20, ph // 24))
        d.text((px0 + int(pw * 0.06), py0 + int(ph * 0.065)), self.title,
               font=title_font, fill=FG, anchor='lm')
        # Close button, top-right of the card (X drawn, not a glyph).
        cs = max(40, int(ph * 0.06))
        cx1 = px1 - int(pw * 0.05)
        cy0 = py0 + int(ph * 0.035)
        self.close_rect = (cx1 - cs, cy0, cx1, cy0 + cs)
        d.ellipse(self.close_rect, fill=ROW)
        m = int(cs * 0.3)
        d.line((cx1 - cs + m, cy0 + m, cx1 - m, cy0 + cs - m), fill=FG, width=3)
        d.line((cx1 - cs + m, cy0 + cs - m, cx1 - m, cy0 + m), fill=FG, width=3)
        # Lay widgets out at their natural heights, vertically centred in the
        # card (so a screen with few rows doesn't stretch into huge bars).
        pad = int(pw * 0.06)
        x0 = px0 + pad
        col_w = pw - 2 * pad
        top = py0 + int(ph * 0.14)
        bottom = py1 - pad
        gap = max(10, int(ph * 0.02))
        n = len(self._widgets)
        base = int(ph * 0.095)
        pref = [max(0.4, w.weight) * base for w in self._widgets]
        total = sum(pref) + gap * (n - 1)
        avail = bottom - top
        if total > avail and total > 0:            # too many rows -> shrink
            scale = avail / float(total)
            pref = [p * scale for p in pref]
            gap = int(gap * scale)
            y = top
        else:                                      # room to spare -> centre
            y = top + (avail - total) / 2.0
        row_font = canvas.font(max(17, ph // 28))
        for w, h in zip(self._widgets, pref):
            w.rect = (x0, int(y), x0 + col_w, int(y + h))
            w.render(self, d, canvas, row_font)
            y += h + gap
        canvas.flush()

    def on_tap(self, x, y):
        if _hit(self.close_rect, x, y):
            return 'close'
        for w in self._widgets:
            if w.hit(x, y):
                return w.tap(self, x, y)
        return None


def _shift_time(hhmm, mins):
    """Shift a 'HH:MM' string by `mins`, wrapping at 24h."""
    try:
        h, m = (int(x) for x in str(hhmm).split(':'))
    except Exception:
        h, m = 22, 0
    total = (h * 60 + m + mins) % (24 * 60)
    return '%02d:%02d' % (total // 60, total % 60)


def _q(text):
    import urllib.parse
    return urllib.parse.quote(str(text), safe='')


class RootMenu(Screen):
    title = 'Einstellungen'

    def widgets(self):
        return [
            Row('wifi', 'WLAN', target=lambda: WifiScreen(self.ui)),
            Row('display', 'Anzeige', target=lambda: DisplayScreen(self.ui)),
            Row('sleep', 'Schlaf', target=lambda: SleepScreen(self.ui)),
            Row('slideshow', 'Slideshow', target=lambda: SlideshowScreen(self.ui)),
            Row('albums', 'Fotoalben', target=lambda: AlbumsScreen(self.ui)),
            Row('system', 'System', target=lambda: SystemScreen(self.ui)),
        ]


class WifiScreen(Screen):
    title = 'WLAN'

    def widgets(self):
        st = self.ui.api_get('/api/wifi/status') or {}
        connected = bool(st.get('ssid'))
        lines = [
            'Status: ' + ('verbunden' if connected else 'nicht verbunden'),
            'Netz: ' + (st.get('ssid') or '—'),
            'IP: ' + (st.get('ip') or '—'),
        ]
        return [
            InfoBlock('st', lines, weight=2.0),
            Row('change', 'Am Handy ändern', target=lambda: QRScreen(self.ui, '/setup')),
            self.back_row(),
        ]


class DisplayScreen(Screen):
    title = 'Anzeige'
    state = None

    def widgets(self):
        if self.state is None:
            fs = self.ui.api_get('/api/frame/settings') or {}
            es = fs.get('energy_save', {}) or {}
            live = self.ui.read_brightness()        # real DDC/CI value
            self.state = {
                'brightness': live if live is not None else int(es.get('brightness', 80) or 80),
                'orientation': (fs.get('frame', {}) or {}).get('orientation', 'horizontal'),
            }

        def set_bright(v):
            self.state['brightness'] = v
            self.ui.api_post('/brightness', {'value': v})

        def toggle_orient():
            self.state['orientation'] = ('vertical'
                if self.state['orientation'] == 'horizontal' else 'horizontal')
            self.ui.api_post('/orientation', {'orientation': self.state['orientation']})
            return 'rerender'

        return [
            Slider('bright', 'Helligkeit', lambda: self.state['brightness'], set_bright),
            Row('orient', 'Ausrichtung', on_tap=toggle_orient,
                value=('Hochkant' if self.state['orientation'] == 'vertical' else 'Quer')),
            self.back_row(),
        ]


class SleepScreen(Screen):
    title = 'Schlaf'
    state = None

    def widgets(self):
        if self.state is None:
            s = self.ui.api_get('/schedule') or {}
            self.state = {
                'enabled': bool(s.get('enabled', False)),
                'off_time': s.get('off_time', '22:00'),
                'on_time': s.get('on_time', '07:00'),
                'weekdays': s.get('weekdays', [0, 1, 2, 3, 4, 5, 6]),
            }

        def push():
            self.ui.api_post('/schedule', {k: self.state[k]
                             for k in ('enabled', 'off_time', 'on_time', 'weekdays')})

        def set_enabled(v):
            self.state['enabled'] = v
            push()

        def shift(key, mins):
            self.state[key] = _shift_time(self.state[key], mins)
            push()

        def set_days(lst):
            self.state['weekdays'] = lst
            push()

        return [
            Toggle('en', 'Schlafmodus', lambda: self.state['enabled'], set_enabled),
            Stepper('off', 'Aus um', lambda: self.state['off_time'],
                    lambda: shift('off_time', -30), lambda: shift('off_time', 30)),
            Stepper('on', 'Ein um', lambda: self.state['on_time'],
                    lambda: shift('on_time', -30), lambda: shift('on_time', 30)),
            Days('days', lambda: self.state['weekdays'], set_days),
            Row('method', 'Methode & Test', target=lambda: MethodScreen(self.ui)),
            Row('now', 'Jetzt schlafen', on_tap=self.act('/sleep'), kind='primary'),
            self.back_row(),
        ]


class MethodScreen(Screen):
    title = 'Schlaf-Methode'
    _METHODS = ['ddcci', 'hdmi', 'dpms', 'brightness', 'black_only']
    _NAMES = {'ddcci': 'DDC/CI', 'hdmi': 'HDMI aus', 'dpms': 'DPMS',
              'brightness': 'Helligkeit 0', 'black_only': 'Nur schwarz'}
    state = None

    def widgets(self):
        if self.state is None:
            fs = self.ui.api_get('/api/frame/settings') or {}
            self.state = {'method': fs.get('sleep_method', 'ddcci')}

        def cycle():
            m = self.state['method']
            i = self._METHODS.index(m) if m in self._METHODS else 0
            self.state['method'] = self._METHODS[(i + 1) % len(self._METHODS)]
            self.ui.api_post('/api/frame/settings', {'sleep_method': self.state['method']})
            return 'rerender'

        return [
            Row('m', 'Methode', on_tap=cycle, value=self._NAMES.get(self.state['method'])),
            Row('test', 'Testen (8s)',
                on_tap=self.act('/api/sleep/test', {'method': self.state['method']}),
                kind='primary'),
            Row('sleepnow', 'Jetzt schlafen', on_tap=self.act('/sleep')),
            Row('wakenow', 'Jetzt wecken', on_tap=self.act('/wake')),
            self.back_row(),
        ]


class SlideshowScreen(Screen):
    title = 'Slideshow'
    state = None

    def widgets(self):
        if self.state is None:
            fs = self.ui.api_get('/api/frame/settings') or {}
            iv = (fs.get('slideshow', {}) or {}).get('interval', 10)
            self.state = {'interval': int(iv or 10)}

        def shift(delta):
            self.state['interval'] = max(10, min(3600, self.state['interval'] + delta))
            self.ui.api_post('/api/interval', {'interval': self.state['interval']})

        st = self.ui.api_get('/sync/status') or {}
        running = st.get('running')
        return [
            Stepper('iv', 'Intervall', lambda: '%ds' % self.state['interval'],
                    lambda: shift(-5), lambda: shift(5)),
            Row('sync', 'Jetzt synchronisieren',
                on_tap=self.act('/sync/trigger', result='rerender'),
                value=('läuft …' if running else None), kind='primary'),
            self.back_row(),
        ]


class AlbumsScreen(Screen):
    title = 'Fotoalben'

    def widgets(self):
        data = self.ui.api_get('/api/album_names') or {}
        names = [n for src in data.values() if isinstance(src, dict)
                 for n in src.values()]
        lines = ['Alben:'] + (['• ' + n for n in names[:5]]
                              or ['(keine konfiguriert)'])
        return [
            InfoBlock('names', lines, weight=2.6),
            Row('edit', 'Quellen am Handy bearbeiten',
                target=lambda: QRScreen(self.ui, '/remote')),
            self.back_row(),
        ]


class SystemScreen(Screen):
    title = 'System'

    def widgets(self):
        si = self.ui.api_get('/sysinfo') or {}
        ver = self.ui.api_get('/api/version') or {}
        ips = si.get('ips', []) or []
        lines = [
            'IP: ' + (ips[0] if ips else '—'),
            'Version: %s (%s)' % (ver.get('version', '?'), ver.get('date', '')),
            'Temp: %s °C   Disk: %s' % (si.get('cpu_temp', '?'), si.get('disk', '?')),
            'Fotos H/V: %s   Videos H/V: %s' % (si.get('photo_count', '?'),
                                               si.get('video_count', '?')),
        ]
        return [
            InfoBlock('si', lines, weight=2.6),
            Row('access', 'Zugang / QR', target=lambda: QRScreen(self.ui, '/remote')),
            Row('power', 'Aus / Neustart', target=lambda: PowerScreen(self.ui),
                kind='danger'),
            self.back_row(),
        ]


class PowerScreen(Screen):
    title = 'Aus / Neustart'

    def widgets(self):
        return [
            Row('shutdown', 'Ausschalten', on_tap=self.act('/shutdown'), kind='danger'),
            Row('reboot', 'Neustart', on_tap=self.act('/reboot')),
            Row('back', 'Abbrechen', target=lambda: RootMenu(self.ui), kind='back'),
        ]


class QRScreen(Screen):
    title = 'Mit dem Handy'

    def __init__(self, ui, path):
        super().__init__(ui)
        self.path = path
        self._mods = None
        self._url = None

    def widgets(self):
        if self._mods is None:
            acc = self.ui.api_get('/api/access') or {}
            token = acc.get('token', '')
            ips = acc.get('ips', []) or []
            ip = ips[0] if ips else 'mimir.local'
            sep = '&' if '?' in self.path else '?'
            self._url = 'http://%s:8080%s%stoken=%s' % (ip, self.path, sep, token)
            qr = self.ui.api_get('/api/qrcode?text=' + _q(self._url)) or {}
            self._mods = qr.get('modules')
        return [
            QRBlock('qr', self._mods, caption=self._url.replace('http://', '')),
            self.back_row(),
        ]


class NativeUI:
    """Owns the canvas + current screen; driven by taps from a TouchReader.

    Screens talk to the running server (frame/routes.py) over localhost:8080 via
    api_get/api_post — the same HTTP API the phone web UI uses, which is open to
    localhost — rather than reaching into the app object (the slideshow runs in a
    separate process). `hooks` is accepted for backward-compatibility but the
    screens use the API directly.
    """

    def __init__(self, hooks=None, orientation='horizontal',
                 api_base='http://localhost:8080'):
        self.canvas = FbCanvas()
        self.hooks = hooks or {}
        self.orientation = orientation
        self.api_base = api_base
        self.screen = None

    def panel_rect(self):
        """The centred settings card, sized by orientation. Roomy enough for
        sliders/steppers/day-pickers. NOTE: true vertical mounting also needs the
        canvas + touch rotated 90 deg (TODO); this only sizes the card."""
        w, h = self.canvas.w, self.canvas.h
        if self.orientation == 'vertical':
            pw, ph = int(w * 0.82), int(h * 0.52)
        else:
            pw, ph = int(w * 0.44), int(h * 0.82)
        x0, y0 = (w - pw) // 2, (h - ph) // 2
        return x0, y0, x0 + pw, y0 + ph

    # --- server API (localhost) ---
    def api_get(self, path):
        import json
        import urllib.request
        try:
            with urllib.request.urlopen(self.api_base + path, timeout=3) as r:
                return json.loads(r.read().decode('utf-8'))
        except Exception as e:
            logger.warning('api_get %s failed: %s', path, e)
            return None

    def api_post(self, path, obj=None):
        import json
        import urllib.request
        try:
            data = json.dumps(obj).encode('utf-8') if obj is not None else b''
            req = urllib.request.Request(
                self.api_base + path, data=data, method='POST',
                headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=5) as r:
                body = r.read().decode('utf-8')
                return json.loads(body) if body else {}
        except Exception as e:
            logger.warning('api_post %s failed: %s', path, e)
            return None

    def read_brightness(self):
        """The panel's real current brightness via DDC/CI (VCP 10), or None.

        The server's get_brightness() only returns a cached value (often the
        boot default), so the slider reads the hardware directly — the frame
        user has NOPASSWD sudo for ddcutil. ~0.5s, run once when Anzeige opens."""
        try:
            out = subprocess.run(['sudo', 'ddcutil', 'getvcp', '10'],
                                 capture_output=True, text=True, timeout=5)
            m = re.search(r'current value\s*=\s*(\d+)', out.stdout)
            if m:
                return max(0, min(100, int(m.group(1))))
        except Exception as e:
            logger.warning('read_brightness failed: %s', e)
        return None

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
        if nxt == 'rerender':
            self.screen.render(self.canvas)
            return True
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
                 node=TOUCH_NODE, flip_x=False, flip_y=False, swap=False,
                 on_hot_progress=None, on_hot_cancel=None):
        self.w, self.h = screen_w, screen_h
        self.on_tap = on_tap
        self.on_hot_corner = on_hot_corner
        # on_hot_progress(frac 0..1) is called repeatedly while a touch is held
        # in the hot corner (for a hold-progress indicator); on_hot_cancel() once
        # if the hold ends (released / moved out) before it completes.
        self.on_hot_progress = on_hot_progress
        self.on_hot_cancel = on_hot_cancel
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

    def _find_node(self):
        """Locate the touchscreen's absolute-coordinate event node.

        The USB panel re-enumerates on the flaky over-current transient (its
        event number can change), so we re-detect rather than trust a fixed
        path. This panel exposes TWO input nodes under the same name: the real
        multitouch digitizer (INPUT_PROP_DIRECT, absolute X/Y + MT, no REL) and
        a pointer/mouse-emulation node (PROP=0, REL axes, BTN_LEFT). We must
        bind the digitizer — the mouse node would feed relative deltas and
        BTN_LEFT instead of the ABS_X/Y + BTN_TOUCH this reader expects, so taps
        would map wrong. Both blocks match the name and both carry a mouseN
        handler, so we SCORE the candidates: +2 for PROP=INPUT_PROP_DIRECT
        (0x02), +1 for having no relative axes. Highest score wins. Falls back
        to self.node."""
        try:
            with open('/proc/bus/input/devices') as fh:
                blocks = fh.read().split('\n\n')
        except Exception:
            return self.node
        best, best_score = None, -1
        for b in blocks:
            if 'touchscreen' not in b.lower():
                continue
            m = re.search(r'Handlers=[^\n]*?(event\d+)', b)
            if not m:
                continue
            score = 0
            pm = re.search(r'PROP=([0-9a-fA-F]+)', b)
            if pm:
                try:
                    if int(pm.group(1), 16) & 0x02:  # INPUT_PROP_DIRECT
                        score += 2
                except ValueError:
                    pass
            if not re.search(r'^B: REL=', b, re.M):   # a real touchscreen has no relative axes
                score += 1
            if score > best_score:
                best, best_score = '/dev/input/' + m.group(1), score
        return best or self.node

    def run(self):
        """Read touch events, self-healing across device re-enumeration."""
        import select
        while not self._stop.is_set():
            node = self._find_node()
            try:
                f = open(node, 'rb')
            except Exception as e:
                logger.warning('TouchReader: cannot open %s (%s), retrying', node, e)
                self._stop.wait(2)
                continue
            logger.info('TouchReader: reading %s', node)
            rx = ry = None
            down = False
            down_t = 0.0
            hot_fired = False
            reopen = False
            prog_active = False      # a hold-in-corner is building progress
            last_prog = 0.0          # last on_hot_progress emit time
            while not self._stop.is_set() and not reopen:
                try:
                    r, _, _ = select.select([f], [], [], 0.1)
                except Exception:
                    reopen = True
                    break
                now = time.time()
                try:
                    if (down and not hot_fired and rx is not None
                            and ry is not None):
                        x, y = self._map(rx, ry)
                        if self._in_hot_corner(x, y):
                            elapsed = (now - down_t) * 1000
                            frac = min(1.0, elapsed / HOLD_MS)
                            prog_active = True
                            if self.on_hot_progress and now - last_prog >= 0.07:
                                last_prog = now
                                self.on_hot_progress(frac)
                            if elapsed >= HOLD_MS:
                                hot_fired = True
                                prog_active = False
                                if self.on_hot_progress:
                                    self.on_hot_progress(1.0)
                                if self.on_hot_corner:
                                    self.on_hot_corner()
                        elif prog_active:
                            # finger wandered out of the corner mid-hold -> cancel
                            prog_active = False
                            if self.on_hot_cancel:
                                self.on_hot_cancel()
                    if not r:
                        continue
                    try:
                        data = f.read(_EV_SZ)
                    except OSError as e:
                        logger.warning('TouchReader: %s read error (%s), reopening',
                                       node, e)
                        reopen = True
                        break
                    if not data or len(data) < _EV_SZ:
                        # Device went away (EOF) — reopen (it may re-enumerate).
                        logger.warning('TouchReader: %s closed, reopening', node)
                        reopen = True
                        break
                    _s, _u, et, code, val = struct.unpack(_EV_FMT, data)
                    if et == EV_ABS and code == ABS_X:
                        rx = val
                    elif et == EV_ABS and code == ABS_Y:
                        ry = val
                    elif et == EV_KEY and code == BTN_TOUCH:
                        if val == 1:
                            down, down_t, hot_fired = True, now, False
                            prog_active = False
                        elif val == 0:
                            if (down and not hot_fired and rx is not None
                                    and ry is not None):
                                x, y = self._map(rx, ry)
                                if ((now - down_t) * 1000 < HOLD_MS
                                        and self.on_tap):
                                    self.on_tap(x, y)
                            if prog_active and not hot_fired and self.on_hot_cancel:
                                self.on_hot_cancel()   # released before it filled
                            prog_active = False
                            down = False
                except Exception as e:
                    logger.warning('TouchReader: event handling error: %s', e)
            try:
                f.close()
            except Exception:
                pass
            if not self._stop.is_set():
                self._stop.wait(1)   # brief pause before reopen


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
