"""The whole wake against a fake frame server, panel simulated."""
import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import yaml
from PIL import Image

from frame import eink_cycle
from frame.eink_cycle import EinkCycle


class FakeFrame:
    """Just enough of frame/routes.py for the cycle."""

    def __init__(self):
        self.wifi = {'mode': 'normal', 'ssid': 'Heimnetz', 'ip': '192.168.1.50',
                     'hotspot_ssid': 'mimir-Setup', 'hotspot_password': 'photoframe'}
        self.sync_polls_until_done = 1
        self.posts = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, obj):
                body = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header('Content-Length', len(body))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                p = self.path.split('?')[0]
                if p == '/api/wifi/status':
                    self._send(outer.wifi)
                elif p == '/api/access':
                    self._send({'token': 'TOKEN', 'ips': ['192.168.1.50'],
                                'urls': ['http://192.168.1.50:8080/remote?token=TOKEN']})
                elif p == '/api/qrcode':
                    self._send({'modules': [[1, 0, 1], [0, 1, 0], [1, 0, 1]]})
                elif p == '/sync/status':
                    outer.sync_polls_until_done -= 1
                    self._send({'running': outer.sync_polls_until_done > 0})
                else:
                    self.send_error(404)

            def do_POST(self):
                outer.posts.append(self.path)
                self._send({'ok': True})

        self.httpd = HTTPServer(('127.0.0.1', 0), H)
        self.url = 'http://127.0.0.1:%d' % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


class FakePiSugar:
    def __init__(self, plugged=False, alarm_ok=True, battery=80.0):
        self.plugged, self.alarm_ok, self._battery = plugged, alarm_ok, battery
        self.log = []

    def battery(self):
        return self._battery

    def power_plugged(self):
        return self.plugged

    def sync_pi_to_rtc(self):
        self.log.append('pi2rtc')

    def sync_rtc_to_pi(self):
        self.log.append('rtc2pi')

    def set_alarm(self, when):
        self.log.append(('alarm', when))
        return self.alarm_ok


@pytest.fixture
def frame(tmp_path, monkeypatch):
    f = FakeFrame()
    photos = tmp_path / 'photos'
    for orient, size in (('horizontal', (800, 480)), ('vertical', (480, 800))):
        (photos / orient).mkdir(parents=True)
        for i, col in enumerate([(200, 40, 40), (40, 160, 60), (40, 60, 200)]):
            Image.new('RGB', size, col).save(photos / orient / ('p%d.jpg' % i))
    cfg = {'setup_complete': True, 'variant': 'eink',
           'frame': {'orientation': 'horizontal'},
           'photos': {'base_dir': str(photos)},
           'eink': {'driver': 'simulator', 'update_time': '06:00',
                    'manual_awake_minutes': 0}}
    cfg_path = tmp_path / 'config.yaml'
    cfg_path.write_text(yaml.dump(cfg))
    f.tmp, f.cfg_path, f.photos = tmp_path, cfg_path, photos
    # never wait in tests
    monkeypatch.setattr(eink_cycle.threading.Event, 'wait', lambda self, t=None: self.is_set())
    monkeypatch.setattr(eink_cycle, 'STAY_AWAKE_FLAG', str(tmp_path / 'no-such-flag'))
    yield f
    f.close()


def make(frame, **kw):
    c = EinkCycle(str(frame.cfg_path), simulate_to=str(frame.tmp / 'panel.png'),
                  api=frame.url, **kw)
    c.powered_off = False
    c.poweroff = lambda: setattr(c, 'powered_off', True)
    c.clock_synced = lambda: True
    return c


def set_cfg(frame, fn):
    cfg = yaml.safe_load(frame.cfg_path.read_text())
    fn(cfg)
    frame.cfg_path.write_text(yaml.dump(cfg))


def test_draws_a_photo_and_stays_up_without_a_pisugar(frame):
    c = make(frame)
    c.pisugar = lambda: None
    due = c.run_once()
    assert isinstance(due, datetime)
    panel = Image.open(frame.tmp / 'panel.png')
    assert panel.size == (800, 480)
    assert c.powered_off is False                     # nothing could wake it again
    st = json.loads((frame.photos / 'eink_state.json').read_text())
    assert st['last_image'].startswith('p') and st['next_wake'] == due.timestamp()
    assert '/sync/trigger' in frame.posts
    assert (frame.photos / 'eink_preview.png').exists()


def test_on_battery_arms_the_alarm_then_powers_off(frame):
    c = make(frame)
    ps = FakePiSugar(plugged=False)
    c.pisugar = lambda: ps
    due = c.run_once()
    assert ('alarm', due) in ps.log and 'pi2rtc' in ps.log
    assert c.powered_off is True


def test_never_powers_off_without_a_confirmed_alarm(frame):
    c = make(frame)
    ps = FakePiSugar(plugged=False, alarm_ok=False)
    c.pisugar = lambda: ps
    c.run_once()
    assert c.powered_off is False


@pytest.mark.parametrize('case', ['plugged', 'unknown', 'no_poweroff', 'stay_awake', 'flag'])
def test_reasons_to_stay_up(frame, case, monkeypatch):
    kw = {'allow_poweroff': False} if case == 'no_poweroff' else {}
    c = make(frame, **kw)
    ps = FakePiSugar(plugged={'plugged': True, 'unknown': None}.get(case, False))
    c.pisugar = lambda: ps
    if case == 'stay_awake':
        set_cfg(frame, lambda cfg: cfg['eink'].update(stay_awake=True))
    if case == 'flag':
        flag = frame.tmp / 'mimir-stay-awake'
        flag.write_text('')
        monkeypatch.setattr(eink_cycle, 'STAY_AWAKE_FLAG', str(flag))
    c.run_once()
    assert c.powered_off is False
    assert not any(isinstance(x, tuple) for x in ps.log)   # no alarm armed either


def test_portrait_frame_composes_portrait(frame):
    set_cfg(frame, lambda cfg: cfg['frame'].update(orientation='vertical'))
    c = make(frame)
    c.pisugar = lambda: None
    c.run_once()
    assert Image.open(frame.tmp / 'panel.png').size == (480, 800)


def test_offline_scheduled_wake_still_draws_and_skips_sync(frame):
    frame.wifi['mode'] = 'hotspot'
    c = make(frame)
    c.pisugar = lambda: None
    c.save_state(next_wake=__import__('time').time())      # = a scheduled wake
    c.run_once()
    assert '/sync/trigger' not in frame.posts
    assert c._drawn_key[0] == 'photo'


def test_manual_wake_without_wifi_explains_how_to_reconnect(frame):
    frame.wifi['mode'] = 'hotspot'
    c = make(frame)
    c.pisugar = lambda: None
    c.run_once()                                            # no next_wake = manual
    assert c._drawn_key[0] == 'setup' and c._drawn_key[1] == 'wifi_lost'


def test_setup_mode_shows_the_phone_screen_until_the_wizard_completes(frame):
    set_cfg(frame, lambda cfg: cfg.update(setup_complete=False))
    c = make(frame)
    c.pisugar = lambda: None
    seen = []
    real_draw = c.draw_setup

    def draw_then_finish(wifi):
        real_draw(wifi)
        seen.append(c._drawn_key)
        set_cfg(frame, lambda cfg: cfg.update(setup_complete=True))   # wizard done
    c.draw_setup = draw_then_finish
    c.run_once()
    assert seen and seen[0][1] == 'phone'
    assert 'wizard?token=TOKEN' in seen[0][3]
    assert c._drawn_key[0] == 'photo'                     # then the first picture


def test_setup_screen_is_not_redrawn_when_nothing_changed(frame):
    set_cfg(frame, lambda cfg: cfg.update(setup_complete=False))
    c = make(frame)
    shows = []
    c.backend().show = lambda img: shows.append(img.size)
    c.draw_setup(frame.wifi)
    c.draw_setup(frame.wifi)
    assert len(shows) == 1                                # e-paper refreshes are costly


def test_torn_config_read_is_not_mistaken_for_first_run(frame):
    c = make(frame)
    frame.cfg_path.write_text('')
    assert c.setup_state() == 'unknown'


def test_manual_wake_shows_the_remote_code_before_the_photo(frame):
    """Switched on by hand, on Wi-Fi: the phone-remote QR first, then a photo."""
    c = make(frame)
    c.pisugar = lambda: None
    keys = []
    push = c.push
    c.push = lambda img, key=None: (keys.append(key), push(img, key=key))[1]
    c.run_once()                                            # no next_wake = manual
    assert [k[0] for k in keys] == ['setup', 'photo'] and keys[0][1] == 'remote'
    assert keys[0][3] == 'http://192.168.1.50:8080/remote?token=TOKEN'


@pytest.mark.parametrize('case', ['scheduled', 'disabled'])
def test_no_remote_code_on_a_scheduled_wake_or_when_switched_off(frame, case):
    if case == 'disabled':
        set_cfg(frame, lambda cfg: cfg['eink'].update(remote_qr=False))
    c = make(frame)
    c.pisugar = lambda: None
    if case == 'scheduled':
        c.save_state(next_wake=__import__('time').time())
    keys = []
    push = c.push
    c.push = lambda img, key=None: (keys.append(key), push(img, key=key))[1]
    c.run_once()
    assert [k[0] for k in keys] == ['photo']
