"""The first-run screen: "scan this with your phone".

A frame needs no touch panel and no keyboard to be set up — everything happens
on a phone. The panel only has to say how to get there, in two stages:

  * ``wifi``  — the frame is not on a network. It runs its own setup hotspot;
    the QR code joins that hotspot (a ``WIFI:`` payload every phone camera
    understands) and the phone's captive portal opens the Wi-Fi page.
  * ``phone`` — the frame is on the home network. The QR code opens the setup
    wizard (tokenised URL) on a phone in the same network. Touch builds also get
    a button to continue on the panel instead.

Plus ``starting`` while the server is still coming up.

This module only *draws*: ``render()`` returns a PIL image at the size asked
for and the tap targets on it. It has no idea what it is drawn on, so the LCD
slideshow (dark theme, RGB565 framebuffer) and the e-ink cycle (light theme,
dithered to the panel palette) share it — one layout, any resolution from a
250 px e-paper strip to a 1920 px panel, landscape or portrait.
"""
import logging

logger = logging.getLogger(__name__)

THEMES = {
    # LCD: same dark palette as the settings menu (frame/native_ui.py).
    'dark': {
        'bg': (16, 18, 22), 'fg': (236, 238, 240), 'muted': (150, 156, 164),
        'accent': (64, 142, 232), 'accent_fg': (255, 255, 255),
        'chip': (38, 43, 51),
    },
    # E-ink: pure black on pure white. Anything in between would be dithered
    # into speckle (or, on a 2-colour panel, vanish), so there are no greys.
    'light': {
        'bg': (255, 255, 255), 'fg': (0, 0, 0), 'muted': (0, 0, 0),
        'accent': (0, 0, 0), 'accent_fg': (255, 255, 255),
        'chip': (255, 255, 255),
    },
}

TEXT = {
    'de': {
        'starting_title': 'mimir startet …',
        'starting_body': ['Einen Moment bitte.'],
        'wifi_title': 'Willkommen bei mimir',
        'wifi_lead': 'Richte den Rahmen mit dem Handy ein:',
        'wifi_steps': [
            'Code mit der Handy-Kamera scannen. Das Handy verbindet sich '
            'mit dem WLAN „{ssid}“.',
            'Die Einrichtung öffnet sich von selbst. Falls nicht, im Browser '
            '{url} aufrufen.',
            'Dein Heim-WLAN auswählen – der Rahmen verbindet sich und zeigt '
            'den nächsten Schritt.',
        ],
        'wifi_manual': 'Ohne Kamera: WLAN „{ssid}“, Passwort „{password}“',
        'phone_title': 'Fast geschafft',
        'phone_lead': 'Der Rahmen ist im WLAN „{ssid}“.',
        'phone_steps': [
            'Handy mit demselben WLAN verbinden.',
            'Code mit der Handy-Kamera scannen.',
            'Fotoalben und Einstellungen am Handy festlegen.',
        ],
        'phone_touch': 'Hier am Rahmen weitermachen',
        'remote_title': 'Einstellungen am Handy',
        'remote_lead': 'Der Rahmen ist im WLAN „{ssid}“.',
        'remote_steps': [
            'Handy mit demselben WLAN verbinden.',
            'Code mit der Handy-Kamera scannen.',
            'Alben, Uhr, Schlafzeiten und mehr am Handy ändern.',
        ],
        'remote_foot': 'Dieser Code erscheint nach jedem Einschalten. '
                       '{after}',
        'remote_after_lcd': 'Die Fotos starten gleich von selbst.',
        'remote_after_eink': 'Danach zeigt der Rahmen wieder ein Foto.',
        'done_title': 'WLAN nicht erreichbar',
        'done_lead': 'Der Rahmen findet sein WLAN nicht. So verbindest du ihn neu:',
        'skip': 'Überspringen',
    },
    'en': {
        'starting_title': 'mimir is starting …',
        'starting_body': ['One moment please.'],
        'wifi_title': 'Welcome to mimir',
        'wifi_lead': 'Set up the frame with your phone:',
        'wifi_steps': [
            'Scan the code with your phone camera. The phone joins the '
            'Wi-Fi “{ssid}”.',
            'The setup page opens by itself. If it does not, open {url} '
            'in the browser.',
            'Pick your home Wi-Fi – the frame connects and shows the next step.',
        ],
        'wifi_manual': 'No camera? Wi-Fi “{ssid}”, password “{password}”',
        'phone_title': 'Almost there',
        'phone_lead': 'The frame is on the Wi-Fi “{ssid}”.',
        'phone_steps': [
            'Connect your phone to the same Wi-Fi.',
            'Scan the code with your phone camera.',
            'Choose photo albums and settings on the phone.',
        ],
        'phone_touch': 'Continue here on the frame',
        'remote_title': 'Settings on your phone',
        'remote_lead': 'The frame is on the Wi-Fi “{ssid}”.',
        'remote_steps': [
            'Connect your phone to the same Wi-Fi.',
            'Scan the code with your phone camera.',
            'Change albums, clock, sleep times and more on the phone.',
        ],
        'remote_foot': 'This code appears every time the frame is switched '
                       'on. {after}',
        'remote_after_lcd': 'The photos start by themselves in a moment.',
        'remote_after_eink': 'After that the frame shows a photo again.',
        'done_title': 'Wi-Fi not reachable',
        'done_lead': 'The frame cannot find its Wi-Fi. To reconnect it:',
        'skip': 'Skip',
    },
}


def wifi_qr_payload(ssid, password):
    """The ``WIFI:`` QR payload that makes a phone join a WPA network."""
    def esc(s):
        for ch in ('\\', ';', ',', ':', '"'):
            s = s.replace(ch, '\\' + ch)
        return s
    return 'WIFI:S:%s;T:WPA;P:%s;;' % (esc(ssid), esc(password))


def _font(size, bold=False):
    from PIL import ImageFont
    import os
    names = (['DejaVuSans-Bold.ttf', 'DejaVuSans.ttf'] if bold
             else ['DejaVuSans.ttf', 'DejaVuSans-Bold.ttf'])
    for n in names:
        p = '/usr/share/fonts/truetype/dejavu/' + n
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


_FONTS = {}


def font(size, bold=False):
    key = (int(size), bold)
    if key not in _FONTS:
        _FONTS[key] = _font(int(size), bold)
    return _FONTS[key]


def wrap(d, text, fnt, max_w):
    """Greedy word wrap to ``max_w`` pixels. Returns a list of lines."""
    lines, cur = [], ''
    for word in text.split():
        trial = (cur + ' ' + word).strip()
        if cur and d.textlength(trial, font=fnt) > max_w:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


def _logo(width, color):
    """The mimir mark + wordmark as an RGBA tile in ``color`` (None on failure)."""
    try:
        from PIL import Image
        from frame import logo_anim
        tile = logo_anim.render_tile(int(width), 1.0, supersample=2)
        solid = Image.new('RGBA', tile.size, tuple(color) + (255,))
        solid.putalpha(tile.getchannel('A'))
        return solid
    except Exception as e:
        logger.debug('logo render failed: %s', e)
        return None


def _draw_qr(d, modules, x, y, side):
    """Draw a QR matrix into a ``side`` square at (x, y), white quiet zone
    included. Returns the pixel size actually used (a multiple of the cell)."""
    n = len(modules)
    quiet = 2
    cell = max(1, side // (n + 2 * quiet))
    used = cell * (n + 2 * quiet)
    ox = x + (side - used) // 2
    oy = y + (side - used) // 2
    d.rectangle((ox, oy, ox + used - 1, oy + used - 1), fill=(255, 255, 255))
    for r, row in enumerate(modules):
        for c, v in enumerate(row):
            if v:
                cx = ox + (c + quiet) * cell
                cy = oy + (r + quiet) * cell
                d.rectangle((cx, cy, cx + cell - 1, cy + cell - 1), fill=(0, 0, 0))
    return used


def render(size, stage, qr_modules=None, info=None, theme='dark', lang='de',
           touch=False, skippable=False):
    """Draw the setup screen.

    size        (w, h) of the image, in the orientation it is read in
    stage       'starting' | 'wifi' | 'phone' | 'wifi_lost' | 'remote'
                ('remote' = after setup: the code that opens the phone remote,
                shown at power-on on a frame without a touchscreen)
    qr_modules  QR matrix (list of rows of 0/1) or None (drawn without a code)
    info        dict: ssid, password, url (hotspot page), for the text
    touch       True on a build with a working touch panel: adds the
                "continue here" button on the 'phone' stage
    skippable   adds a "skip" button (used for the Wi-Fi hint after setup)

    Returns ``(image, hits)``; ``hits`` maps a name ('continue', 'skip') to the
    (x0, y0, x1, y1) tap rect of a button that was drawn.
    """
    from PIL import Image, ImageDraw
    W, H = int(size[0]), int(size[1])
    th = THEMES.get(theme, THEMES['dark'])
    tx = TEXT.get(lang, TEXT['de'])
    info = dict(info or {})
    info.setdefault('ssid', 'mimir-Setup')
    info.setdefault('password', 'photoframe')
    info.setdefault('url', 'http://10.42.0.1:8080')

    img = Image.new('RGB', (W, H), th['bg'])
    d = ImageDraw.Draw(img)
    hits = {}
    landscape = W >= H * 1.15
    base = min(W, H)
    pad = max(8, int(base * 0.06))

    if stage == 'wifi':
        title, lead = tx['wifi_title'], tx['wifi_lead']
        steps = tx['wifi_steps']
        foot = tx['wifi_manual']
    elif stage == 'wifi_lost':
        title, lead = tx['done_title'], tx['done_lead']
        steps = tx['wifi_steps']
        foot = tx['wifi_manual']
    elif stage == 'phone':
        title, lead = tx['phone_title'], tx['phone_lead']
        steps = tx['phone_steps']
        foot = ''
    elif stage == 'remote':
        title, lead = tx['remote_title'], tx['remote_lead']
        steps = tx['remote_steps']
        info.setdefault('after', tx['remote_after_eink' if theme == 'light'
                                    else 'remote_after_lcd'])
        foot = tx['remote_foot']
    else:
        title, lead = tx['starting_title'], ''
        steps, foot = tx['starting_body'], ''
        qr_modules = None

    def fmt(s):
        try:
            return s.format(**info)
        except Exception:
            return s
    lead, foot = fmt(lead), fmt(foot)
    steps = [fmt(s) for s in steps]
    numbered = stage != 'starting'

    # --- regions: QR on the right (landscape) or below the text (portrait) ---
    has_qr = bool(qr_modules)
    if landscape:
        qr_side = min(int(H - 2 * pad), int(W * 0.40)) if has_qr else 0
        text_x0, text_x1 = pad, W - pad - (qr_side + pad if has_qr else 0)
        text_y0, text_y1 = pad, H - pad
        qr_x, qr_y = W - pad - qr_side, (H - qr_side) // 2
    else:
        qr_side = min(int(W - 2 * pad), int(H * 0.40)) if has_qr else 0
        text_x0, text_x1 = pad, W - pad
        text_y0 = pad
        text_y1 = H - pad - (qr_side + pad if has_qr else 0)
        qr_x, qr_y = (W - qr_side) // 2, H - pad - qr_side

    # --- buttons reserve room at the bottom of the text column ---
    buttons = []
    if stage == 'phone' and touch:
        buttons.append(('continue', tx['phone_touch'], True))
    if skippable:
        buttons.append(('skip', tx['skip'], False))
    btn_h = max(28, int(base * 0.085)) if buttons else 0
    btn_gap = max(6, int(base * 0.02))
    btn_block = (btn_h + btn_gap) * len(buttons)

    text_w = max(40, text_x1 - text_x0)
    avail_h = max(40, text_y1 - text_y0 - btn_block)

    # --- fit the text block: shrink the type until everything fits ---
    scale = base / 480.0
    size_px = max(10.0, 30.0 * scale)
    layout = None
    while True:
        f_title = font(size_px * 1.45, bold=True)
        f_body = font(size_px)
        f_small = font(max(9, size_px * 0.82))
        logo_w = int(min(text_w * 0.42, size_px * 7.5))
        logo_h = int(logo_w * 0.75) if logo_w >= 40 else 0
        gap = int(size_px * 0.55)
        line_h = int(size_px * 1.32)
        num_w = int(size_px * 1.7) if numbered else 0

        blocks = []            # (kind, payload, height)
        if logo_h:
            blocks.append(('logo', logo_w, logo_h + gap))
        t_lines = wrap(d, title, f_title, text_w)
        blocks.append(('title', t_lines, int(len(t_lines) * size_px * 1.45 * 1.25) + gap))
        if lead:
            l_lines = wrap(d, lead, f_body, text_w)
            blocks.append(('lead', l_lines, len(l_lines) * line_h + gap))
        for i, s in enumerate(steps):
            s_lines = wrap(d, s, f_body, text_w - num_w)
            blocks.append(('step', (i + 1, s_lines),
                           len(s_lines) * line_h + int(gap * 0.8)))
        if foot:
            f_lines = wrap(d, foot, f_small, text_w)
            blocks.append(('foot', f_lines,
                           int(len(f_lines) * size_px * 0.82 * 1.32) + gap // 2))
        total = sum(b[2] for b in blocks)
        if total <= avail_h or size_px <= 9.0:
            layout = blocks
            break
        size_px *= 0.93

    # --- draw the text block, vertically centred in its column ---
    y = text_y0 + max(0, (avail_h - sum(b[2] for b in layout)) // 2)
    for kind, payload, h in layout:
        if kind == 'logo':
            tile = _logo(payload, th['fg'])
            if tile is not None:
                img.paste(tile, (text_x0, int(y)), tile)
        elif kind == 'title':
            yy = y
            for ln in payload:
                d.text((text_x0, int(yy)), ln, font=f_title, fill=th['fg'])
                yy += size_px * 1.45 * 1.25
        elif kind == 'lead':
            yy = y
            for ln in payload:
                d.text((text_x0, int(yy)), ln, font=f_body, fill=th['muted'])
                yy += line_h
        elif kind == 'step':
            num, s_lines = payload
            if numbered:
                r = int(size_px * 0.62)
                cx, cy = text_x0 + r, int(y + line_h * 0.5)
                d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=th['accent'])
                d.text((cx, cy), str(num), font=font(size_px * 0.8, bold=True),
                       fill=th['accent_fg'], anchor='mm')
            yy = y
            for ln in s_lines:
                d.text((text_x0 + num_w, int(yy)), ln, font=f_body, fill=th['fg'])
                yy += line_h
        elif kind == 'foot':
            yy = y
            for ln in payload:
                d.text((text_x0, int(yy)), ln, font=f_small, fill=th['muted'])
                yy += size_px * 0.82 * 1.32
        y += h

    # --- buttons ---
    by = text_y1 - btn_block + btn_gap
    for name, label, primary in buttons:
        rect = (text_x0, int(by), text_x0 + min(text_w, int(base * 0.95)),
                int(by + btn_h))
        fill = th['accent'] if primary else th['chip']
        fg = th['accent_fg'] if primary else th['fg']
        d.rounded_rectangle(rect, radius=btn_h // 4, fill=fill,
                            outline=th['fg'] if theme == 'light' else None,
                            width=2 if theme == 'light' else 0)
        bf = font(btn_h * 0.42, bold=primary)
        while (d.textlength(label, font=bf) > (rect[2] - rect[0]) * 0.92
               and bf.size > 9):
            bf = font(bf.size - 1, bold=primary)
        d.text(((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2), label,
               font=bf, fill=fg, anchor='mm')
        hits[name] = rect
        by += btn_h + btn_gap

    # --- QR ---
    if has_qr and qr_side > 0:
        _draw_qr(d, qr_modules, qr_x, qr_y, qr_side)

    return img, hits
