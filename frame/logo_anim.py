"""mimir "a dot draws the logo" loading animation — shared geometry + renderer.

One source of truth for the self-drawing splash, used by two surfaces:

  * the Plymouth boot splash — `imaging/plymouth/photoframe/render_anim.py`
    pre-renders a PNG frame sequence from this module (the frame has no image
    tooling, so the frames ship pre-rendered, like the logos already do);
  * the native slideshow — `frame/slideshow.py` calls `render_tile()` live and
    writes it to /dev/fb0 as the cold-boot splash before the first photo.

The mark is the mimir "window" (rounded rect + two muntins) drawn as one pen
stroke that a bright dot traces, followed by the "mimir" wordmark wiped in left
-> right. Geometry is defined in the logo's native 520x390 space (matching
imaging/plymouth/photoframe/logo.svg) and scaled to the requested tile.

Everything is drawn on a transparent RGBA tile sized 520:390; callers paste it
centred (over black on fb0, or let Plymouth centre the sprite). Pure PIL, no
numpy — so it runs anywhere PIL does.
"""
import math

# --- brand constants (mirror logo.svg) ---------------------------------------
VIEW_W, VIEW_H = 520, 390
STROKE = 12                      # logo.svg stroke-width, in view units
FG = (237, 237, 240)            # #ededf0
DOT = (255, 255, 255)
WORD = "mimir"
WORD_SIZE = 86                   # logo.svg font-size
WORD_BASELINE = 330             # logo.svg text y
WORD_TRACK = 8                   # logo.svg letter-spacing
ASPECT = VIEW_H / VIEW_W         # tile height / tile width

# Phase split of the 0..1 progress: icon is traced, then the wordmark wipes in.
_ICON_END = 0.70                 # icon done at 70% of the animation
_DOT_FADE = (0.66, 0.82)        # dot fades out across this progress window


def _rounded_rect(x, y, w, h, r, step=3.0):
    """Perimeter of a rounded rect as a closed polyline, clockwise from the top
    edge just right of the top-left corner (a natural pen start)."""
    pts = []

    def arc(cx, cy, a0, a1):
        length = abs(a1 - a0) * r
        n = max(2, int(length / step))
        for i in range(n + 1):
            a = a0 + (a1 - a0) * i / n
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))

    def line(x0, y0, x1, y1):
        d = math.hypot(x1 - x0, y1 - y0)
        n = max(1, int(d / step))
        for i in range(1, n + 1):
            pts.append((x0 + (x1 - x0) * i / n, y0 + (y1 - y0) * i / n))

    x1, y1 = x + w, y + h
    pts.append((x + r, y))                       # start: top edge, past the corner
    line(x + r, y, x1 - r, y)                     # top edge
    arc(x1 - r, y + r, -math.pi / 2, 0)          # top-right corner
    line(x1, y + r, x1, y1 - r)                   # right edge
    arc(x1 - r, y1 - r, 0, math.pi / 2)          # bottom-right
    line(x1 - r, y1, x + r, y1)                   # bottom edge
    arc(x + r, y1 - r, math.pi / 2, math.pi)     # bottom-left
    line(x, y1 - r, x, y + r)                      # left edge
    arc(x + r, y + r, math.pi, math.pi * 1.5)    # top-left, back to start
    return pts


def _segment(x0, y0, x1, y1, step=3.0):
    d = math.hypot(x1 - x0, y1 - y0)
    n = max(1, int(d / step))
    return [(x0 + (x1 - x0) * i / n, y0 + (y1 - y0) * i / n) for i in range(n + 1)]


def _pen_path():
    """The three strokes of the window mark, in pen order. The dot traces each
    in turn and "lifts" (teleports) over the short gap between them."""
    rect = _rounded_rect(155, 48, 210, 133, 16)
    vmuntin = _segment(260, 54, 260, 175)        # vertical pane divider
    hmuntin = _segment(161, 114, 359, 114)       # horizontal pane divider
    return [rect, vmuntin, hmuntin]


def _seg_len(seg):
    return sum(math.hypot(seg[i + 1][0] - seg[i][0], seg[i + 1][1] - seg[i][1])
               for i in range(len(seg) - 1))


def _reveal(segs, frac):
    """Return (list of polylines to draw, head point) revealing the pen path up
    to `frac` of its total traced length. Gaps between segments are not counted
    (the pen lifts), so the dot's speed is constant along actual ink."""
    lengths = [_seg_len(s) for s in segs]
    total = sum(lengths) or 1.0
    target = max(0.0, min(1.0, frac)) * total
    drawn, head, acc = [], None, 0.0
    for seg, L in zip(segs, lengths):
        if acc + L <= target or L == 0:
            drawn.append(seg)
            head = seg[-1]
            acc += L
            continue
        # target lands inside this segment — walk it point by point
        want = target - acc
        part = [seg[0]]
        run = 0.0
        for i in range(len(seg) - 1):
            d = math.hypot(seg[i + 1][0] - seg[i][0], seg[i + 1][1] - seg[i][1])
            if run + d < want:
                part.append(seg[i + 1])
                run += d
            else:
                t = (want - run) / d if d else 0.0
                hx = seg[i][0] + (seg[i + 1][0] - seg[i][0]) * t
                hy = seg[i][1] + (seg[i + 1][1] - seg[i][1]) * t
                part.append((hx, hy))
                head = (hx, hy)
                break
        drawn.append(part)
        return drawn, head
    return drawn, head


def _font(px):
    from PIL import ImageFont
    for path in ('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
                 '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'):
        try:
            return ImageFont.truetype(path, px)
        except Exception:
            continue
    return ImageFont.load_default()


def _draw_polyline(d, pts, s, ox, oy, width, fill):
    """Draw a scaled polyline with round joints/caps (PIL has no round cap, so
    stamp a disc at each vertex)."""
    if len(pts) < 1:
        return
    sp = [(ox + x * s, oy + y * s) for x, y in pts]
    r = width / 2.0
    if len(sp) >= 2:
        d.line(sp, fill=fill, width=int(round(width)), joint='curve')
    for (x, y) in (sp[0], sp[-1]):               # round the two caps
        d.ellipse([x - r, y - r, x + r, y + r], fill=fill)


def render_tile(tile_w, progress, *, supersample=2, pad=None):
    """Render the logo animation at `progress` (0..1) onto a transparent RGBA
    tile of width `tile_w` and height `tile_w*ASPECT`. Supersampled then
    downscaled for smooth edges. Returns a PIL RGBA Image."""
    from PIL import Image, ImageDraw
    progress = max(0.0, min(1.0, progress))
    ss = max(1, int(supersample))
    W = int(tile_w) * ss
    H = int(tile_w * ASPECT) * ss
    if pad is None:
        pad = STROKE * 1.6
    pad *= ss
    s = (W - 2 * pad) / VIEW_W                    # uniform scale, width-fit
    ox = pad
    oy = pad
    width = STROKE * s
    img = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # --- 1. the window mark, traced by the dot ---
    icon_frac = min(1.0, progress / _ICON_END) if _ICON_END else 1.0
    segs = _pen_path()
    drawn, head = _reveal(segs, icon_frac)
    for poly in drawn:
        _draw_polyline(d, poly, s, ox, oy, width, FG)

    # --- 2. the wordmark, wiped in left -> right after the icon ---
    if progress > _ICON_END:
        word_frac = (progress - _ICON_END) / (1.0 - _ICON_END)
        word_frac = max(0.0, min(1.0, word_frac))
        wimg = Image.new('RGBA', (W, H), (0, 0, 0, 0))
        wd = ImageDraw.Draw(wimg)
        f = _font(max(8, int(WORD_SIZE * s)))
        tx = ox + (VIEW_W / 2) * s
        ty = oy + WORD_BASELINE * s
        wd.text((tx, ty), WORD, font=f, fill=FG, anchor='ms')   # middle/baseline
        bbox = wimg.getbbox()
        if bbox:
            cut = bbox[0] + int((bbox[2] - bbox[0]) * word_frac)
            if cut < bbox[2]:                     # mask off the not-yet-written tail
                wimg.paste((0, 0, 0, 0), (cut, 0, W, H))
            img.alpha_composite(wimg)

    # --- 3. the pen dot (with a soft glow), fading out near the end ---
    if head is not None and progress < _DOT_FADE[1]:
        a = 1.0
        if progress > _DOT_FADE[0]:
            a = 1.0 - (progress - _DOT_FADE[0]) / (_DOT_FADE[1] - _DOT_FADE[0])
        a = max(0.0, min(1.0, a))
        hx = ox + head[0] * s
        hy = oy + head[1] * s
        core = width * 0.75
        glow = Image.new('RGBA', (W, H), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        for rr, alpha in ((core * 2.6, 45), (core * 1.7, 90), (core, 255)):
            col = (DOT[0], DOT[1], DOT[2], int(alpha * a))
            gd.ellipse([hx - rr, hy - rr, hx + rr, hy + rr], fill=col)
        img.alpha_composite(glow)

    if ss != 1:
        img = img.resize((int(tile_w), int(tile_w * ASPECT)), Image.LANCZOS)
    return img
