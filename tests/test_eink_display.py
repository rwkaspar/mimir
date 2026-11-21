"""Image pipeline + backends, without hardware."""
import sys
import types

import pytest
from PIL import Image

from frame.display import eink


def gradient(size):
    im = Image.new('RGB', size)
    px = im.load()
    for y in range(size[1]):
        for x in range(size[0]):
            px[x, y] = (x * 255 // size[0], y * 255 // size[1], 128)
    return im


@pytest.mark.parametrize('palette', sorted(eink.PALETTES))
def test_dither_only_uses_panel_inks(palette):
    out = eink.dither(gradient((160, 96)), palette)
    colours = {c for _, c in out.getcolors(maxcolors=100000)}
    assert colours <= set(eink.PALETTES[palette])
    assert len(colours) >= 2          # a gradient must not collapse to one ink


def test_dither_keeps_pure_black_and_white():
    im = Image.new('RGB', (40, 20), (255, 255, 255))
    im.paste((0, 0, 0), (0, 0, 20, 20))
    out = eink.dither(im, '7color')
    assert out.getpixel((5, 5)) == (0, 0, 0)
    assert out.getpixel((30, 5)) == (255, 255, 255)   # QR codes stay scannable


def test_fit_cover_and_contain():
    src = Image.new('RGB', (1920, 1200), (10, 200, 10))
    assert eink.fit(src, (800, 480), 'cover').size == (800, 480)
    c = eink.fit(src, (480, 800), 'contain')
    assert c.size == (480, 800)
    assert c.getpixel((5, 5)) == (255, 255, 255)       # border
    assert c.getpixel((240, 400))[1] > 150             # picture in the middle


def test_panel_defaults():
    assert eink.panel_defaults({'eink': {'driver': 'waveshare:epd7in3f'}}) == (800, 480, '7color')
    assert eink.panel_defaults({'eink': {'driver': 'waveshare:epd7in3e'}}) == (800, 480, 'spectra6')
    # unknown driver: explicit size wins, landscape-normalised, default palette bw
    assert eink.panel_defaults({'eink': {'driver': 'waveshare:epdX', 'width': 300, 'height': 400}}) == (400, 300, 'bw')
    assert eink.panel_defaults({}) == (800, 480, '7color')            # simulator
    assert eink.panel_defaults({'eink': {'driver': 'waveshare:epd7in3f', 'palette': 'nope'}})[2] == 'bw'


def test_simulator_writes_png(tmp_path):
    out = tmp_path / 'sub' / 'panel.png'
    b = eink.make_backend({'eink': {'driver': 'simulator'}}, simulate_to=str(out))
    b.show(eink.dither(gradient((b.width, b.height)), b.palette))
    assert Image.open(out).size == (800, 480)


class FakeEPD:
    """Mimics the shape of a waveshare_epd driver."""
    calls = []

    def __init__(self):
        self.width, self.height = 800, 480

    def init(self):
        FakeEPD.calls.append('init')
        return 0

    def getbuffer(self, image):
        FakeEPD.calls.append(('getbuffer', image.mode, image.size))
        return [0]

    def display(self, *bufs):
        FakeEPD.calls.append(('display', len(bufs)))

    def sleep(self):
        FakeEPD.calls.append('sleep')


@pytest.fixture
def fake_waveshare(monkeypatch):
    FakeEPD.calls = []
    pkg = types.ModuleType('waveshare_epd')
    mod = types.ModuleType('waveshare_epd.epdfake')
    mod.EPD, mod.EPD_WIDTH, mod.EPD_HEIGHT = FakeEPD, 800, 480
    monkeypatch.setitem(sys.modules, 'waveshare_epd', pkg)
    monkeypatch.setitem(sys.modules, 'waveshare_epd.epdfake', mod)
    return FakeEPD


def test_waveshare_colour_panel(fake_waveshare):
    b = eink.make_backend({'eink': {'driver': 'waveshare:epdfake', 'palette': '7color'}})
    assert (b.width, b.height, b.palette) == (800, 480, '7color')
    b.show(Image.new('RGB', (800, 480)))
    assert fake_waveshare.calls == ['init', ('getbuffer', 'RGB', (800, 480)),
                                    ('display', 1), 'sleep']


def test_waveshare_two_plane_panel(fake_waveshare):
    b = eink.make_backend({'eink': {'driver': 'waveshare:epdfake', 'palette': 'bwr'}})
    im = Image.new('RGB', (800, 480), (255, 255, 255))
    im.paste((0, 0, 0), (0, 0, 10, 10))
    im.paste((255, 0, 0), (20, 0, 30, 10))
    planes = []
    orig = fake_waveshare.getbuffer

    def grab(self, image):
        planes.append(image.copy())
        return orig(self, image)
    fake_waveshare.getbuffer = grab
    try:
        b.show(im)
    finally:
        fake_waveshare.getbuffer = orig
    black, colour = planes
    assert black.mode == colour.mode == '1'
    assert black.getpixel((5, 5)) == 0 and black.getpixel((25, 5)) != 0
    assert colour.getpixel((25, 5)) == 0 and colour.getpixel((5, 5)) != 0
    assert black.getpixel((100, 100)) != 0 and colour.getpixel((100, 100)) != 0
    assert ('display', 2) in fake_waveshare.calls and fake_waveshare.calls[-1] == 'sleep'


def test_panel_is_put_to_sleep_even_if_display_fails(fake_waveshare, monkeypatch):
    def boom(self, *bufs):
        raise RuntimeError('spi')
    monkeypatch.setattr(fake_waveshare, 'display', boom)
    b = eink.make_backend({'eink': {'driver': 'waveshare:epdfake'}})
    with pytest.raises(RuntimeError):
        b.show(Image.new('RGB', (800, 480)))
    assert fake_waveshare.calls[-1] == 'sleep'


def test_missing_driver_gives_a_useful_error():
    with pytest.raises(RuntimeError) as e:
        eink.make_backend({'eink': {'driver': 'waveshare:epd_does_not_exist'}})
    assert 'setup_eink.sh' in str(e.value)
    with pytest.raises(RuntimeError):
        eink.make_backend({'eink': {'driver': 'bogus'}})
