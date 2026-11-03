#!/usr/bin/env python3
"""Run the full photo-frame app locally on a non-Pi machine (dev/testing).

All Raspberry-Pi-only commands are mocked (see frame/mock.py), so every screen
and API works in an ordinary browser without flashing a Pi and without touching
your real Wi-Fi or display.

    python dev_server.py            # http://localhost:8080/
    PORT=9000 python dev_server.py  # custom port

State lives under ./dev/ (git-ignored): a dev config and a few sample photos.
Delete ./dev/ to reset. Pass --wizard to start in first-run setup mode.
"""

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
DEV = REPO / 'dev'
PHOTOS = DEV / 'photos'
CONFIG = DEV / 'dev_config.yaml'


def ensure_config(wizard: bool):
    import yaml
    cfg = {
        'setup_complete': not wizard,
        'photos': {'base_dir': str(PHOTOS)},
        'slideshow': {'interval': 8, 'order': 'random', 'transition': 'fade'},
        'frame': {'orientation': 'horizontal', 'name': 'mimir (dev)'},
        'energy_save': {'enabled': False, 'method': 'black_only'},
        'mqtt': {'enabled': False, 'host': '', 'port': 1883,
                 'username': '', 'password': ''},
    }
    if CONFIG.exists():
        # Preserve edits across runs, but honour --wizard each launch.
        existing = yaml.safe_load(CONFIG.read_text()) or {}
        existing.setdefault('photos', {})['base_dir'] = str(PHOTOS)
        existing['setup_complete'] = not wizard
        cfg = existing
    CONFIG.write_text(yaml.safe_dump(cfg, default_flow_style=False))


def seed_photos():
    for sub in ('horizontal', 'vertical'):
        (PHOTOS / sub).mkdir(parents=True, exist_ok=True)
    existing = list((PHOTOS / 'horizontal').glob('*.jpg'))
    if existing:
        return
    try:
        from PIL import Image, ImageDraw
    except Exception:
        print("[dev] Pillow not installed - no sample photos generated "
              "(the slideshow will just be empty). pip install pillow")
        return
    palettes = [(28, 60, 90), (90, 40, 70), (40, 80, 55),
                (95, 70, 30), (55, 45, 95), (20, 85, 90)]
    for i, base in enumerate(palettes):
        for orient, size in (('horizontal', (1920, 1200)),
                             ('vertical', (1200, 1920))):
            img = Image.new('RGB', size, base)
            d = ImageDraw.Draw(img)
            for y in range(size[1]):
                f = y / size[1]
                d.line([(0, y), (size[0], y)],
                       fill=tuple(int(c * (0.5 + f)) for c in base))
            d.text((size[0] // 2 - 60, size[1] // 2 - 20),
                   f"mimir dev #{i + 1}", fill=(255, 255, 255))
            img.save(PHOTOS / orient / f"sample_{i + 1}.jpg", quality=85)
    print(f"[dev] seeded sample photos in {PHOTOS}")


def main():
    wizard = '--wizard' in sys.argv
    DEV.mkdir(exist_ok=True)

    os.environ['FRAME_MOCK'] = '1'
    from frame import mock
    mock.install()

    ensure_config(wizard)
    seed_photos()

    os.environ.setdefault('PORT', '8080')
    port = os.environ['PORT']
    print(f"[dev] mimir dev server -> http://localhost:{port}/"
          + ("  (wizard mode)" if wizard else "")
          + f"\n[dev] wizard screen always at http://localhost:{port}/wizard")

    sys.argv = ['dev_server', str(CONFIG)]
    from frame.server import main as server_main
    server_main()


if __name__ == '__main__':
    main()
