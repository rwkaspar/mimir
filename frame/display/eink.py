"""E-paper backends + the image pipeline that feeds them.

Pipeline (``compose`` then ``dither``):

    photo ──fit──▶ panel-sized RGB ──enhance──▶ ──date/battery──▶ ──dither──▶ panel

E-paper shows a handful of fixed inks, so a photo has to be turned into a
pattern of those inks. We do Floyd–Steinberg error diffusion to the panel's
palette; colour panels look washed out next to an LCD, so saturation and
contrast are lifted a little first (config ``eink.saturation`` / ``contrast``).

Backends:

  * ``simulator`` — writes "what the panel would show" as a PNG. The whole
    cycle can be developed and tested without hardware.
  * ``waveshare:<module>`` — any panel of the Waveshare e-Paper library
    (``waveshare_epd``), e.g. ``waveshare:epd7in3f``. Loaded by module name, so
    a panel we have never seen works as long as its driver follows the
    library's usual ``EPD().init() / getbuffer() / display() / sleep()`` shape.
  * ``inky`` — Pimoroni Inky boards, auto-detected through their EEPROM.

Config (``eink:`` section):

    driver: waveshare:epd7in3f     # or: inky | simulator
    palette: 7color                # optional; known panels pick their own
    width: 800                     # optional; read from the driver
    height: 480
    flip: false                    # panel mounted upside down
"""
import importlib
import logging
import os

from frame.display import DisplayBackend

logger = logging.getLogger(__name__)

# Inks, as the RGB the panel is addressed with. Order matters for the Waveshare
# colour drivers (their getbuffer quantises to the same list), not for us.
PALETTES = {
    'bw': [(0, 0, 0), (255, 255, 255)],
    'bwr': [(0, 0, 0), (255, 255, 255), (255, 0, 0)],
    'bwy': [(0, 0, 0), (255, 255, 255), (255, 255, 0)],
    # ACeP / "7-colour" (epd7in3f, epd5in65f, epd4in01f, Inky Impression 5.7"/4")
    '7color': [(0, 0, 0), (255, 255, 255), (0, 255, 0), (0, 0, 255),
               (255, 0, 0), (255, 255, 0), (255, 128, 0)],
    # Spectra 6 / E6 (epd7in3e, Inky Impression 7.3" 2025)
    'spectra6': [(0, 0, 0), (255, 255, 255), (255, 255, 0), (255, 0, 0),
                 (0, 0, 255), (0, 255, 0)],
}

# Panels we know: landscape size + palette. Only used to pick defaults (and to
# size the photo pipeline before the driver is ever loaded); an unlisted driver
# still works — its size is read from the module, its palette from the config.
PANELS = {
    'epd7in3f': (800, 480, '7color'),
    'epd7in3e': (800, 480, 'spectra6'),
    'epd5in65f': (600, 448, '7color'),
    'epd4in01f': (640, 400, '7color'),
    'epd7in5_V2': (800, 480, 'bw'),
    'epd7in5b_V2': (800, 480, 'bwr'),
    'epd5in83_V2': (648, 480, 'bw'),
    'epd4in2_V2': (400, 300, 'bw'),
    'epd4in2b_V2': (400, 300, 'bwr'),
    'epd2in13_V4': (250, 122, 'bw'),
    'epd2in7_V2': (264, 176, 'bw'),
}


def panel_defaults(cfg):
    """(width, height, palette) for the configured panel, landscape, from the
    config and the PANELS table — without touching any hardware."""
    e = (cfg or {}).get('eink', {}) or {}
    driver = str(e.get('driver', 'simulator'))
    module = driver.split(':', 1)[1] if ':' in driver else ''
    w, h, pal = PANELS.get(module, (800, 480, 'bw' if module else '7color'))
    w = int(e.get('width') or w)
    h = int(e.get('height') or h)
    if h > w:
        w, h = h, w
    pal = e.get('palette') or pal
    if pal not in PALETTES:
        logger.warning('unknown eink.palette %r, using bw', pal)
        pal = 'bw'
    return w, h, pal


# --- image pipeline ---------------------------------------------------------

def fit(image, size, mode='cover', background=(255, 255, 255)):
    """Scale `image` to exactly `size`. 'cover' fills the panel and crops the
    overflow (centre); 'contain' shows the whole picture on a plain border."""
    from PIL import Image
    W, H = size
    im = image.convert('RGB')
    sw, sh = im.size
    if mode == 'contain':
        k = min(W / sw, H / sh)
    else:
        k = max(W / sw, H / sh)
    nw, nh = max(1, round(sw * k)), max(1, round(sh * k))
    im = im.resize((nw, nh), Image.LANCZOS)
    out = Image.new('RGB', (W, H), background)
    out.paste(im, ((W - nw) // 2, (H - nh) // 2))
    return out


def enhance(image, saturation=1.0, contrast=1.0):
    from PIL import ImageEnhance
    if saturation and abs(saturation - 1.0) > 1e-3:
        image = ImageEnhance.Color(image).enhance(float(saturation))
    if contrast and abs(contrast - 1.0) > 1e-3:
        image = ImageEnhance.Contrast(image).enhance(float(contrast))
    return image


def draw_caption(image, lines, position='br'):
    """Small text (date, low-battery note) in a corner: white with a black
    outline, so it reads on any picture and survives dithering to 2 inks."""
    from PIL import ImageDraw
    from frame import setup_screen
    if not lines:
        return image
    d = ImageDraw.Draw(image)
    W, H = image.size
    base = min(W, H)
    size = max(11, int(base * 0.055))
    f = setup_screen.font(size, bold=True)
    margin = max(6, int(base * 0.035))
    stroke = max(1, size // 9)
    right = position in ('tr', 'br')
    bottom = position in ('bl', 'br')
    line_h = int(size * 1.3)
    y = (H - margin - line_h * len(lines)) if bottom else margin
    for text in lines:
        x = (W - margin) if right else margin
        d.text((x, y), text, font=f, fill=(255, 255, 255),
               stroke_width=stroke, stroke_fill=(0, 0, 0),
               anchor='ra' if right else 'la')
        y += line_h
    return image


def dither(image, palette):
    """Floyd–Steinberg to the named palette. Returns an RGB image whose pixels
    are exactly palette colours (so any driver-side quantise is a no-op)."""
    from PIL import Image
    colours = PALETTES[palette]
    flat = [c for rgb in colours for c in rgb]
    # Pad the 256-entry palette by repeating the first ink: PIL would otherwise
    # treat the zero padding as extra black entries (harmless) — repeating keeps
    # the index->ink mapping unambiguous.
    flat += list(colours[0]) * (256 - len(colours))
    pal = Image.new('P', (1, 1))
    pal.putpalette(flat)
    q = image.convert('RGB').quantize(palette=pal,
                                      dither=Image.Dither.FLOYDSTEINBERG)
    return q.convert('RGB')


# --- backends ---------------------------------------------------------------

class SimulatorBackend(DisplayBackend):
    """Writes the frame as a PNG instead of driving a panel."""

    name = 'simulator'

    def __init__(self, width, height, palette, path):
        self.width, self.height, self.palette = width, height, palette
        self.path = path

    def show(self, image):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        image.save(self.path)
        logger.info('E-ink simulator: wrote %s (%dx%d, %s)', self.path,
                    image.size[0], image.size[1], self.palette)


class WaveshareBackend(DisplayBackend):
    """A panel driven by Waveshare's `waveshare_epd.<module>` driver."""

    def __init__(self, module, palette=None, flip=False):
        self.name = 'waveshare:' + module
        self.flip = flip
        try:
            self._mod = importlib.import_module('waveshare_epd.' + module)
        except ImportError as e:
            raise RuntimeError(
                "Waveshare driver '%s' not found (%s). Run scripts/setup_eink.sh "
                "to install the waveshare_epd library, and check eink.driver."
                % (module, e))
        self._epd = self._mod.EPD()
        w = int(getattr(self._epd, 'width', getattr(self._mod, 'EPD_WIDTH', 0)))
        h = int(getattr(self._epd, 'height', getattr(self._mod, 'EPD_HEIGHT', 0)))
        if not w or not h:
            raise RuntimeError('driver %s reports no panel size' % module)
        self.width, self.height = max(w, h), min(w, h)
        self.palette = palette or PANELS.get(module, (0, 0, 'bw'))[2]

    def show(self, image):
        if self.flip:
            image = image.rotate(180)
        epd = self._epd
        if epd.init() == -1:
            raise RuntimeError('panel init failed (%s)' % self.name)
        try:
            # getbuffer accepts the panel's native size or its transpose (it
            # rotates a portrait image itself), so the image goes in as composed.
            if self.palette in ('bwr', 'bwy'):
                # Two-plane panels: one 1-bit buffer for black, one for the
                # colour ink (0 = ink in both).
                # After dithering every pixel is exactly black, white or the
                # ink, and their luma differs (black 0, red 76, yellow 226,
                # white 255) — so two thresholds split the planes.
                luma = image.convert('RGB').convert('L')
                black = luma.point(lambda v: 0 if v < 10 else 255).convert('1')
                colour = luma.point(
                    lambda v: 0 if 10 <= v < 250 else 255).convert('1')
                epd.display(epd.getbuffer(black), epd.getbuffer(colour))
            else:
                epd.display(epd.getbuffer(image))
        finally:
            # Deep-sleep the controller: e-paper must not be left powered.
            try:
                epd.sleep()
            except Exception as e:
                logger.warning('panel sleep failed: %s', e)

    def close(self):
        try:
            self._mod.epdconfig.module_exit(cleanup=True)
        except Exception:
            pass


class InkyBackend(DisplayBackend):
    """A Pimoroni Inky board, auto-detected via its EEPROM.

    Inky's own `set_image` dithers to a palette calibrated for the actual inks,
    which beats dithering to pure RGB primaries — so it gets the full-colour
    image (`dithers_itself`)."""

    name = 'inky'
    dithers_itself = True

    def __init__(self, saturation=0.5, flip=False):
        try:
            from inky.auto import auto
        except ImportError as e:
            raise RuntimeError("Inky library not found (%s). Run "
                               "scripts/setup_eink.sh." % e)
        self._inky = auto()
        w, h = self._inky.resolution
        self.width, self.height = max(w, h), min(w, h)
        self._native = (w, h)
        self.palette = 'spectra6' if 'e6' in type(self._inky).__module__ else '7color'
        self.saturation = saturation
        self.flip = flip

    def show(self, image):
        if image.size != self._native:
            image = image.rotate(90, expand=True)     # portrait -> native
        if self.flip:
            image = image.rotate(180)
        try:
            self._inky.set_image(image, saturation=self.saturation)
        except TypeError:                             # mono/3-colour boards
            self._inky.set_image(image)
        self._inky.show()


def make_backend(cfg, simulate_to=None):
    """Build the backend named by `eink.driver`. `simulate_to` forces the
    simulator (used by the tests and `--simulate`)."""
    e = (cfg or {}).get('eink', {}) or {}
    w, h, pal = panel_defaults(cfg)
    driver = str(e.get('driver', 'simulator'))
    flip = bool(e.get('flip', False))
    if simulate_to or driver == 'simulator':
        path = simulate_to or e.get('simulate_png') or '/tmp/mimir_eink.png'
        return SimulatorBackend(w, h, pal, path)
    if driver == 'inky':
        return InkyBackend(saturation=float(e.get('inky_saturation', 0.5)), flip=flip)
    if driver.startswith('waveshare:'):
        return WaveshareBackend(driver.split(':', 1)[1],
                                palette=e.get('palette'), flip=flip)
    raise RuntimeError("unknown eink.driver %r (use waveshare:<module>, inky "
                       "or simulator)" % driver)


def _main(argv):
    """`python3 -m frame.display.eink <config>` prints `W H palette` for the
    configured panel (landscape) — setup_eink.sh uses it to size the photo
    pipeline to the panel."""
    import yaml
    with open(argv[1]) as f:
        cfg = yaml.safe_load(f) or {}
    print('%d %d %s' % panel_defaults(cfg))
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(_main(sys.argv))
