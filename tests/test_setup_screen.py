"""The phone-setup screen renders at any panel size, both themes."""
import pytest

from frame import setup_screen as ss

QR = [[(x * y + x) % 2 for x in range(29)] for y in range(29)]
SIZES = [(1920, 1200), (1200, 1920), (800, 480), (480, 800), (600, 448),
         (400, 300), (250, 122)]


@pytest.mark.parametrize('size', SIZES)
@pytest.mark.parametrize('stage', ['starting', 'wifi', 'phone', 'wifi_lost', 'remote'])
@pytest.mark.parametrize('theme', ['dark', 'light'])
def test_renders_everywhere(size, stage, theme):
    img, hits = ss.render(size, stage, QR, {'ssid': 'Heimnetz'}, theme=theme, touch=True)
    assert img.size == size and img.mode == 'RGB'
    for rect in hits.values():
        x0, y0, x1, y1 = rect
        assert 0 <= x0 < x1 <= size[0] and 0 <= y0 < y1 <= size[1]


def test_continue_button_only_with_touch_on_the_phone_stage():
    assert 'continue' in ss.render((1920, 1200), 'phone', QR, touch=True)[1]
    assert 'continue' not in ss.render((1920, 1200), 'phone', QR, touch=False)[1]
    assert 'continue' not in ss.render((1920, 1200), 'wifi', QR, touch=True)[1]
    assert 'skip' in ss.render((1920, 1200), 'wifi_lost', QR, skippable=True)[1]


def test_light_theme_is_pure_black_and_white():
    # e-paper: no greys that would dither into speckle (anti-aliased glyph
    # edges aside, the fills are 0 or 255)
    img, _ = ss.render((800, 480), 'wifi', QR, theme='light')
    assert img.getpixel((2, 2)) == (255, 255, 255)
    for name in ('bg', 'fg', 'muted', 'accent'):
        assert set(ss.THEMES['light'][name]) <= {0, 255}


def test_qr_is_drawn_crisp():
    img, _ = ss.render((800, 480), 'wifi', QR, theme='light')
    colours = {c for _, c in img.crop((480, 110, 740, 370)).getcolors(100000)}
    assert colours == {(0, 0, 0), (255, 255, 255)}


def test_wifi_qr_payload_escapes():
    assert ss.wifi_qr_payload('mimir-Setup', 'photoframe') == 'WIFI:S:mimir-Setup;T:WPA;P:photoframe;;'
    assert ss.wifi_qr_payload('a;b', 'p:w') == r'WIFI:S:a\;b;T:WPA;P:p\:w;;'
