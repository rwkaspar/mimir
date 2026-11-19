#!/usr/bin/env python3
"""Pre-render the mimir "dot draws the logo" boot-splash frame sequence.

The frame has no image tooling (PIL/ImageMagick are not installed there), so the
Plymouth animation ships as a flat PNG sequence, exactly like logo.png /
logo_vertical.png already do. Run this on a dev machine whenever the logo or the
animation changes, then commit the regenerated anim/ frames.

  python3 imaging/plymouth/photoframe/render_anim.py

Output: imaging/plymouth/photoframe/anim/{horizontal,vertical}/f0.png .. fN.png
(transparent, white-on-nothing, logo.png-sized so Plymouth centres them like the
static logo). install_bootsplash.sh copies the orientation that is in use.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
sys.path.insert(0, REPO)

from PIL import Image                       # noqa: E402
from frame import logo_anim as L            # noqa: E402

FRAMES = 48                                 # one sweep (0 -> full logo)
TILE_W = 520                                # == logo.png width; keeps centring


def _vertical_rotation():
    """Return the Image.rotate angle that turns a horizontal logo frame into the
    repo's shipped logo_vertical.png convention (so the boot animation matches
    the static vertical logo). Auto-detected, falls back to +90."""
    try:
        h = Image.open(os.path.join(HERE, 'logo.png')).convert('L')
        v = Image.open(os.path.join(HERE, 'logo_vertical.png')).convert('L')
        best, ang = None, 90
        for a in (90, -90):
            r = h.rotate(a, expand=True)
            if r.size != v.size:
                continue
            import numpy as np
            diff = float((np.asarray(r, dtype='int16') -
                          np.asarray(v, dtype='int16')).__abs__().mean())
            if best is None or diff < best:
                best, ang = diff, a
        return ang
    except Exception:
        return 90


def main():
    vang = _vertical_rotation()
    print('vertical rotation: %d deg' % vang)
    for orient in ('horizontal', 'vertical'):
        out = os.path.join(HERE, 'anim', orient)
        os.makedirs(out, exist_ok=True)
        for f in os.listdir(out):              # clean stale frames
            if f.endswith('.png'):
                os.remove(os.path.join(out, f))
        for i in range(FRAMES):
            p = i / (FRAMES - 1)
            tile = L.render_tile(TILE_W, p, supersample=3)
            if orient == 'vertical':
                tile = tile.rotate(vang, expand=True)
            tile.save(os.path.join(out, 'f%d.png' % i), optimize=True)
        print('%-10s -> %s  (%d frames, %s)' %
              (orient, out, FRAMES, tile.size))
    print('FRAMES = %d' % FRAMES)


if __name__ == '__main__':
    main()
