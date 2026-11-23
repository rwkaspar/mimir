"""Which of the frame's pictures are in the slideshow.

Every picture the sync brought in is shown unless the owner took it out on the
phone page (/library). The choice is a list of file names in
`<photos_dir>/hidden.json` — a plain file because three separate processes
read it (web server, LCD slideshow, e-ink cycle) and none of them should need
the others running.

A name hides the picture in both orientations: the landscape and the portrait
rendering of one photo carry the same file name.
"""
import json
import logging
import os
import subprocess
import time

logger = logging.getLogger(__name__)

HIDDEN_FILE = 'hidden.json'
THUMB_DIR = '.thumbs'
THUMB_SIZE = 360                 # longest edge, px
NEW_DAYS = 7                     # "new" badge on the phone page
MEDIA_EXTS = ('.jpg', '.jpeg', '.png', '.mp4')
ORIENTATIONS = ('horizontal', 'vertical')

_cache = {}                      # path -> (mtime_ns, frozenset)


def _file(photos_dir):
    return os.path.join(str(photos_dir), HIDDEN_FILE)


def hidden(photos_dir):
    """Names taken out of the slideshow. Cheap to call often: the file is only
    re-read when it changed."""
    path = _file(photos_dir)
    try:
        stamp = os.stat(path).st_mtime_ns
    except OSError:
        return frozenset()
    hit = _cache.get(path)
    if hit and hit[0] == stamp:
        return hit[1]
    try:
        with open(path) as f:
            data = json.load(f)
        names = frozenset(n for n in data.get('hidden', []) if isinstance(n, str))
    except Exception as e:
        logger.warning('hidden list unreadable (%s): showing everything', e)
        names = frozenset()
    _cache[path] = (stamp, names)
    return names


def is_hidden(photos_dir, path):
    return os.path.basename(str(path)) in hidden(photos_dir)


def visible(paths, photos_dir):
    """`paths` without the hidden ones. If that would leave nothing, all of
    them: a frame with every picture unticked still shows pictures rather than
    a blank panel."""
    paths = list(paths)
    off = hidden(photos_dir)
    if not off:
        return paths
    keep = [p for p in paths if os.path.basename(str(p)) not in off]
    return keep or paths


def _valid_name(name):
    return (isinstance(name, str) and name and name == os.path.basename(name)
            and not name.startswith('.') and name.lower().endswith(MEDIA_EXTS))


def _existing_names(photos_dir):
    names = set()
    for orient in ORIENTATIONS:
        try:
            names.update(os.listdir(os.path.join(str(photos_dir), orient)))
        except OSError:
            pass
    return names


def set_hidden(photos_dir, names, hide):
    """Take `names` out of the slideshow (hide=True) or put them back.
    Returns the new set. Names of pictures that no longer exist are dropped
    on the way, so the list cannot grow forever."""
    names = [n for n in names if _valid_name(n)]
    current = set(hidden(photos_dir))
    if hide:
        current.update(names)
    else:
        current.difference_update(names)
    current &= _existing_names(photos_dir)
    path = _file(photos_dir)
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump({'hidden': sorted(current)}, f)
    os.replace(tmp, path)
    _cache.pop(path, None)
    return frozenset(current)


def listing(photos_dir, orientation):
    """The pictures of one orientation for the phone page, newest first."""
    d = os.path.join(str(photos_dir), orientation)
    off = hidden(photos_dir)
    now = time.time()
    items = []
    try:
        names = os.listdir(d)
    except OSError:
        names = []
    for name in names:
        if not _valid_name(name):
            continue
        try:
            added = os.stat(os.path.join(d, name)).st_mtime
        except OSError:
            continue
        items.append({
            'name': name,
            'video': name.lower().endswith('.mp4'),
            'hidden': name in off,
            'added': int(added),
            'new': now - added < NEW_DAYS * 86400,
        })
    items.sort(key=lambda i: (-i['added'], i['name']))
    return items


def thumb(photos_dir, orientation, name):
    """Path of a small JPEG of the picture (made on first use, then cached),
    or None. Full pictures are far too heavy for a phone grid, and the frame
    is a Pi Zero: decode at reduced size, once."""
    if orientation not in ORIENTATIONS or not _valid_name(name):
        return None
    src = os.path.join(str(photos_dir), orientation, name)
    if not os.path.isfile(src):
        return None
    out_dir = os.path.join(str(photos_dir), THUMB_DIR, orientation)
    out = os.path.join(out_dir, name + '.jpg')
    try:
        if os.path.getmtime(out) >= os.path.getmtime(src):
            return out
    except OSError:
        pass
    try:
        os.makedirs(out_dir, exist_ok=True)
        tmp = out + '.tmp'
        if name.lower().endswith('.mp4'):
            subprocess.run(
                ['ffmpeg', '-y', '-v', 'error', '-i', src, '-frames:v', '1',
                 '-vf', 'scale=%d:%d:force_original_aspect_ratio=decrease'
                 % (THUMB_SIZE, THUMB_SIZE), '-f', 'mjpeg', tmp],
                capture_output=True, timeout=30, check=True)
        else:
            from PIL import Image
            with Image.open(src) as im:
                im.draft('RGB', (THUMB_SIZE, THUMB_SIZE))   # fast JPEG downscale
                im = im.convert('RGB')
                im.thumbnail((THUMB_SIZE, THUMB_SIZE))
                im.save(tmp, 'JPEG', quality=70)
        os.replace(tmp, out)
        return out
    except Exception as e:
        logger.warning('thumbnail for %s failed: %s', name, e)
        return None


def sweep_thumbs(photos_dir):
    """Drop thumbnails whose picture is gone."""
    for orient in ORIENTATIONS:
        d = os.path.join(str(photos_dir), THUMB_DIR, orient)
        try:
            have = set(os.listdir(os.path.join(str(photos_dir), orient)))
            for t in os.listdir(d):
                if t[:-4] not in have:
                    os.unlink(os.path.join(d, t))
        except OSError:
            pass
