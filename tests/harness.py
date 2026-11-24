"""A real frame web server on a temp directory, for tests that talk HTTP.

It is the production handler and the production AppState; only the things
that need a Pi are replaced (Wi-Fi manager) or not started (sync, the
sleep loop). `remote=True` makes the server treat the test client like a
phone on the LAN — the kiosk on localhost skips the access token, and tests
that only ever run as localhost never see what a phone sees.
"""
import os
import subprocess
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import yaml
from PIL import Image

REPO = Path(__file__).resolve().parent.parent


class FakeWifi:
    def __init__(self, mode='normal'):
        self.mode = mode

    def get_status(self):
        return {'mode': self.mode, 'connected': True, 'ssid': 'TestNet',
                'ip': '192.168.1.50', 'hotspot_ssid': 'mimir-Setup'}

    def check_connectivity(self):
        return True

    def scan_networks(self):
        return [{'ssid': 'TestNet', 'signal': 80, 'security': 'WPA2'}]


class Frame:
    def __init__(self, root, server, app, token, photos_dir):
        self.root, self.server, self.app = root, server, app
        self.token, self.photos_dir = token, photos_dir
        self.port = server.server_port
        self.base = 'http://127.0.0.1:%d' % self.port

    def url(self, path, token=True):
        if not token:
            return self.base + path
        return self.base + path + ('&' if '?' in path else '?') + 'token=' + self.token

    def config(self):
        with open(self.app.config_path) as f:
            return yaml.safe_load(f) or {}

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def make_media(photos_dir, orientation='horizontal', photos=5, videos=1):
    d = Path(photos_dir) / orientation
    d.mkdir(parents=True, exist_ok=True)
    size = (1920, 1200) if orientation == 'horizontal' else (1200, 1920)
    names = []
    for i in range(photos):
        name = 'gph_%02d_gphoto_%02d.jpg' % (i, i)
        Image.new('RGB', size, (40 + i * 40, 90, 160 - i * 20)).save(d / name, 'JPEG')
        os.utime(d / name, (1_700_000_000 + i, 1_700_000_000 + i))
        names.append(name)
    for i in range(videos):
        name = 'gph_v%d_gphoto_v%d.mp4' % (i, i)
        try:
            subprocess.run(
                ['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i',
                 'testsrc=size=320x200:rate=10:duration=1', '-pix_fmt', 'yuv420p',
                 str(d / name)], check=True, capture_output=True, timeout=60)
            names.append(name)
        except Exception:
            pass                      # no ffmpeg here: run without a video
    return names


def start_frame(root, variant='lcd', orientation='horizontal', setup_complete=True,
                remote=True, photos=5, videos=1, extra=None, wifi_mode='normal'):
    from frame import routes
    from frame.config import AppState
    from frame.server import create_handler

    root = Path(root)
    photos_dir = root / 'photos'
    for o in ('horizontal', 'vertical'):
        (photos_dir / o).mkdir(parents=True, exist_ok=True)
    make_media(photos_dir, orientation, photos, videos)
    cfg = {
        'setup_complete': setup_complete,
        'display_confirmed': True,
        'variant': variant,
        'frame': {'name': 'mimir', 'orientation': orientation},
        'photos': {'base_dir': str(photos_dir)},
        'slideshow': {'interval': 20},
        'clock': {'enabled': True, 'size': 'm', 'position': 'br', 'show_date': True},
        'energy_save': {'schedule': {'enabled': True, 'off_time': '22:00',
                                     'on_time': '07:00', 'weekdays': [0, 1, 2, 3, 4, 5, 6]}},
        'security': {'access_token': 'TestToken-123_abc'},
    }
    if variant == 'eink':
        cfg['eink'] = {'driver': 'simulate'}
    for k, v in (extra or {}).items():
        cfg[k] = v
    config_path = root / 'config_frame.yaml'
    config_path.write_text(yaml.safe_dump(cfg))

    app = AppState(str(config_path))
    app.wizard_mode = not setup_complete
    app.wifi_manager = FakeWifi(wifi_mode)
    try:
        from frame.energy import EnergySaveManager
        app.energy_save = EnergySaveManager(app_state=app)     # not started
    except Exception:
        app.energy_save = None

    routes.PhotoFrameHandler.LOCAL_IPS = set() if remote else {'127.0.0.1', '::1'}
    handler = create_handler(app, str(photos_dir), str(REPO / 'viewer'),
                             cfg.get('slideshow', {}))
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return Frame(root, server, app, cfg['security']['access_token'], photos_dir)
