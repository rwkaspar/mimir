"""The e-ink frame: wake, draw one picture, sleep.

An LCD frame is a daemon that never stops. An e-paper frame is the opposite —
the panel keeps its picture with no power at all, so the right thing to do
between pictures is to be OFF:

    PiSugar RTC alarm powers the Pi up
      └─ this cycle:  wait for the server ─ sync photos (if Wi-Fi) ─ pick the
                      next photo ─ fit + dither ─ push to the panel ─ arm the
                      next alarm ─ power off
    … hours of nothing, on the battery's self-discharge …

That is the "scheduled wake". Three situations keep the frame up instead:

  * **Setup not finished** — the panel shows the "set me up with your phone"
    screen (frame/setup_screen.py, shared with the LCD frame) and the server +
    hotspot stay reachable until the wizard completes.
  * **Someone switched it on** — a wake that is not near the scheduled time was
    a button press. The frame draws a new picture and stays reachable for a few
    minutes (``eink.manual_awake_minutes``) so settings can be changed from the
    phone, then powers off.
  * **No battery manager / on external power** — without a PiSugar nothing could
    wake the frame again, and on mains power there is no reason to switch off.
    The cycle then simply sleeps in-process until the next picture is due.

Powering off is the one irreversible step, so it is gated hard: only on battery,
only with a wake alarm that the PiSugar confirmed. Anything unclear → stay up.

The cycle is a client of the frame's own server (localhost:8080) for Wi-Fi state
and photo sync, exactly like the on-panel menu of the LCD frame — one sync
pipeline, one config, two displays.

Run:  python3 -m frame.eink_cycle <config> [--once] [--no-poweroff]
                                  [--simulate out.png]
"""
import argparse
import json
import logging
import os
import random
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta

import yaml

logger = logging.getLogger(__name__)

API = 'http://localhost:8080'
PHOTO_EXTS = ('.jpg', '.jpeg', '.png')
STAY_AWAKE_FLAG = '/boot/firmware/mimir-stay-awake'
WDAYS = {'de': ['Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa', 'So'],
         'en': ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']}
MONTHS = {'de': ['Jan', 'Feb', 'März', 'Apr', 'Mai', 'Jun', 'Jul', 'Aug', 'Sep',
                 'Okt', 'Nov', 'Dez'],
          'en': ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep',
                 'Oct', 'Nov', 'Dec']}


# --- pure helpers (unit-tested) ---------------------------------------------

def wake_slots(update_time='06:00', per_day=1):
    """Minutes-of-day at which a new picture is due: `per_day` evenly spaced
    slots, the first at `update_time`."""
    try:
        h, m = (int(x) for x in str(update_time).split(':'))
        first = (h * 60 + m) % 1440
    except Exception:
        first = 6 * 60
    per_day = max(1, min(24, int(per_day or 1)))
    step = 1440 // per_day
    return sorted((first + i * step) % 1440 for i in range(per_day))


def next_wake(now, update_time='06:00', per_day=1, min_gap_s=180):
    """The next due time strictly more than `min_gap_s` after `now`.

    The gap keeps a cycle that finishes just before a slot from arming an alarm
    it could miss while still shutting down."""
    slots = wake_slots(update_time, per_day)
    earliest = now + timedelta(seconds=min_gap_s)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for offset in (0, 1, 2):
        for mins in slots:
            t = day + timedelta(days=offset, minutes=mins)
            if t > earliest:
                return t
    return earliest + timedelta(days=1)       # unreachable


def is_scheduled_wake(now_ts, expected_ts, window_s=900):
    """Was this boot the RTC alarm (near the time we armed) or a button press?"""
    if not expected_ts:
        return False
    return abs(now_ts - float(expected_ts)) <= window_s


def pick_next(files, shown):
    """Choose the next photo: random among those not shown in the current
    round; when all have been shown, start a new round (never repeating the
    last one back-to-back). Returns (choice, new_shown)."""
    files = sorted(files)
    if not files:
        return None, []
    shown = [s for s in shown if s in files]
    pool = [f for f in files if f not in shown]
    if not pool:
        last = shown[-1] if shown else None
        shown = []
        pool = [f for f in files if f != last] or files
    choice = random.choice(pool)
    return choice, shown + [choice]


def date_line(now, lang='de'):
    lang = lang if lang in WDAYS else 'de'
    if lang == 'de':
        return '%s, %d. %s' % (WDAYS[lang][now.weekday()], now.day,
                               MONTHS[lang][now.month - 1])
    return '%s %d %s' % (WDAYS[lang][now.weekday()], now.day,
                         MONTHS[lang][now.month - 1])


# --- the cycle ---------------------------------------------------------------

class EinkCycle:
    def __init__(self, config_path, simulate_to=None, allow_poweroff=True,
                 api=API):
        self.config_path = config_path
        self.simulate_to = simulate_to
        self.allow_poweroff = allow_poweroff
        self.api = api
        self._stop = threading.Event()
        self._backend = None
        self._drawn_key = None
        self._last_draw = 0.0

    # --- config / state ---

    def cfg(self):
        try:
            with open(self.config_path) as f:
                return yaml.safe_load(f) or {}
        except Exception:
            return {}

    def ecfg(self):
        return self.cfg().get('eink', {}) or {}

    def photos_dir(self):
        c = self.cfg()
        base = (c.get('photos', {}) or {}).get('base_dir', '/srv/frame/photos')
        return base, (c.get('frame', {}) or {}).get('orientation', 'horizontal')

    def state_path(self):
        return os.path.join(self.photos_dir()[0], 'eink_state.json')

    def preview_path(self):
        return os.path.join(self.photos_dir()[0], 'eink_preview.png')

    def load_state(self):
        try:
            with open(self.state_path()) as f:
                return json.load(f)
        except Exception:
            return {}

    def save_state(self, **updates):
        st = self.load_state()
        st.update(updates)
        tmp = self.state_path() + '.tmp'
        try:
            with open(tmp, 'w') as f:
                json.dump(st, f)
            os.replace(tmp, self.state_path())
        except Exception as e:
            logger.warning('could not save state: %s', e)
        return st

    def setup_state(self):
        """'incomplete' | 'complete' | 'unknown' (torn read — see the same
        guard in frame/slideshow.py)."""
        c = self.cfg()
        if not c:
            return 'unknown'
        return 'complete' if c.get('setup_complete') else 'incomplete'

    # --- server API ---

    def api_get(self, path, timeout=4):
        try:
            with urllib.request.urlopen(self.api + path, timeout=timeout) as r:
                return json.loads(r.read().decode('utf-8'))
        except Exception:
            return None

    def api_post(self, path, obj=None, timeout=6):
        try:
            data = json.dumps(obj).encode() if obj is not None else b''
            req = urllib.request.Request(
                self.api + path, data=data, method='POST',
                headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read().decode('utf-8')
                return json.loads(body) if body else {}
        except Exception:
            return None

    def wait_for_server(self, seconds=90):
        """The server starts in parallel and takes a while on a Pi Zero."""
        end = time.time() + seconds
        while time.time() < end and not self._stop.is_set():
            st = self.api_get('/api/wifi/status', timeout=3)
            if st is not None:
                return st
            self._stop.wait(2)
        return None

    def sync(self, timeout_s):
        """Ask the server to sync and wait until it is done (or we run out of
        patience — on battery every awake minute costs)."""
        r = self.api_post('/sync/trigger')
        if not r or not r.get('ok'):
            logger.info('Sync not started (%s)', (r or {}).get('message', 'no server'))
            return False
        end = time.time() + timeout_s
        self._stop.wait(3)
        while time.time() < end and not self._stop.is_set():
            st = self.api_get('/sync/status')
            if st is not None and not st.get('running'):
                logger.info('Sync finished')
                return True
            self._stop.wait(4)
        logger.warning('Sync still running after %ds, drawing from what we have',
                       timeout_s)
        return False

    # --- panel ---

    def backend(self):
        if self._backend is None:
            from frame.display import eink
            self._backend = eink.make_backend(self.cfg(), self.simulate_to)
            logger.info('Panel: %s %dx%d palette %s', self._backend.name,
                        self._backend.width, self._backend.height,
                        self._backend.palette)
        return self._backend

    def canvas_size(self):
        b = self.backend()
        if self.photos_dir()[1] == 'vertical':
            return b.height, b.width
        return b.width, b.height

    def push(self, image, key=None):
        """Dither (unless the panel library does it better) and show. Also keeps
        a PNG of what is on the panel for the phone UI."""
        from frame.display import eink
        b = self.backend()
        shown = eink.dither(image, b.palette)
        try:
            shown.save(self.preview_path())
        except Exception as e:
            logger.debug('preview save failed: %s', e)
        b.show(image if b.dithers_itself else shown)
        self._drawn_key = key
        self._last_draw = time.time()

    def list_photos(self):
        base, orient = self.photos_dir()
        d = os.path.join(base, orient)
        try:
            return [os.path.join(d, f) for f in os.listdir(d)
                    if f.lower().endswith(PHOTO_EXTS)]
        except OSError:
            return []

    def draw_photo(self, battery=None):
        """Pick the next photo, compose it for the panel and show it.
        Returns the file shown, or None if there was nothing to show."""
        from PIL import Image
        from frame.display import eink
        e = self.ecfg()
        st = self.load_state()
        files = self.list_photos()
        choice, shown = pick_next(files, st.get('shown', []))
        if not choice:
            logger.warning('No photos to show')
            return None
        try:
            src = Image.open(choice)
            src.load()
        except Exception as ex:
            logger.warning('Cannot open %s (%s); skipping it', choice, ex)
            self.save_state(shown=shown)
            return None
        img = eink.fit(src, self.canvas_size(), mode=e.get('fit', 'cover'))
        img = eink.enhance(img, saturation=float(e.get('saturation', 1.3)),
                           contrast=float(e.get('contrast', 1.1)))
        lines = []
        if e.get('show_date', True):
            lang = (self.cfg().get('frame', {}) or {}).get('language', 'de')
            lines.append(date_line(datetime.now(), lang))
        low = float(e.get('low_battery_percent', 15))
        if battery is not None and battery <= low:
            lines.insert(0, 'Akku %d %%' % round(battery))
        eink.draw_caption(img, lines, position=e.get('date_position', 'br'))
        self.push(img, key=('photo', choice))
        self.save_state(shown=shown, last_image=os.path.basename(choice),
                        last_draw=time.time())
        logger.info('Showing %s', os.path.basename(choice))
        return choice

    def draw_setup(self, wifi, remote=False):
        """Draw the phone-setup screen for the current stage, if it changed.
        E-paper refreshes take 15–30 s and wear the panel, so never redraw an
        unchanged screen and never more often than every 30 s.

        `remote`: after setup, on Wi-Fi — draw the code that opens the phone
        remote instead (shown when the frame is switched on by hand)."""
        from frame import setup_screen
        info, qr_text = {}, None
        if remote:
            stage = 'remote'
            urls = (self.api_get('/api/access') or {}).get('urls') or []
            if not urls:
                return
            info = {'ssid': (wifi or {}).get('ssid') or 'WLAN'}
            qr_text = urls[0]
        elif wifi is None:
            stage = 'starting'
        elif wifi.get('mode') == 'hotspot':
            stage = 'wifi' if self.setup_state() != 'complete' else 'wifi_lost'
            info = {'ssid': wifi.get('hotspot_ssid') or 'mimir-Setup',
                    'password': wifi.get('hotspot_password') or 'photoframe',
                    'url': 'http://10.42.0.1:8080'}
            qr_text = setup_screen.wifi_qr_payload(info['ssid'], info['password'])
        else:
            stage = 'phone'
            acc = self.api_get('/api/access') or {}
            ips = acc.get('ips') or []
            host = ips[0] if ips else (wifi.get('ip') or 'mimir.local')
            info = {'ssid': wifi.get('ssid') or 'WLAN'}
            if acc.get('token'):
                qr_text = 'http://%s:8080/wizard?token=%s' % (host, acc['token'])
        key = ('setup', stage, tuple(sorted(info.items())), qr_text)
        if key == self._drawn_key:
            return
        if stage == 'starting' and self._drawn_key is not None:
            return                       # don't replace a real screen with "starting"
        if time.time() - self._last_draw < 30:
            return
        mods = None
        if qr_text:
            import urllib.parse
            qr = self.api_get('/api/qrcode?text=' +
                              urllib.parse.quote(qr_text, safe='')) or {}
            mods = qr.get('modules')
            if not mods:
                return                   # no code yet: wait rather than draw half
        lang = (self.cfg().get('frame', {}) or {}).get('language', 'de')
        img, _ = setup_screen.render(self.canvas_size(), stage, mods, info,
                                     theme='light', lang=lang)
        logger.info('Setup screen: %s', stage)
        self.push(img, key=key)

    # --- power ---

    def pisugar(self):
        if self.ecfg().get('pisugar', True) is False:
            return None
        try:
            from frame.power.pisugar import PiSugar
            p = PiSugar()
            return p if p.available() else None
        except Exception as e:
            logger.debug('PiSugar probe failed: %s', e)
            return None

    @staticmethod
    def clock_synced():
        try:
            out = subprocess.run(
                ['timedatectl', 'show', '-p', 'NTPSynchronized', '--value'],
                capture_output=True, text=True, timeout=5)
            return out.stdout.strip() == 'yes'
        except Exception:
            return False

    def fix_clock(self, ps):
        """The Pi has no RTC of its own. With NTP the Pi is right → teach the
        PiSugar; offline the PiSugar is right → teach the Pi."""
        if ps is None:
            return
        if self.clock_synced():
            ps.sync_pi_to_rtc()
        else:
            ps.sync_rtc_to_pi()

    def poweroff(self):
        logger.info('Powering off until the next picture')
        try:
            subprocess.run(['sync'], timeout=10)
        except Exception:
            pass
        subprocess.run(['sudo', '/sbin/shutdown', '-h', 'now'],
                       capture_output=True, timeout=30)

    # --- main ---

    def stop(self, *_):
        self._stop.set()

    def run_setup(self):
        """Stay up showing the setup screen until the wizard completes."""
        logger.info('Setup mode: waiting for the wizard')
        while not self._stop.is_set():
            if self.setup_state() == 'complete':
                logger.info('Setup complete')
                return True
            self.draw_setup(self.api_get('/api/wifi/status'))
            self._stop.wait(5)
        return False

    def run_once(self):
        """One wake. Returns the datetime the next picture is due."""
        e = self.ecfg()
        boot_ts = time.time()
        st = self.load_state()
        scheduled = is_scheduled_wake(boot_ts, st.get('next_wake'))
        logger.info('Wake: %s', 'scheduled' if scheduled else 'manual / first')

        wifi = self.wait_for_server(int(e.get('server_wait_seconds', 90)))
        fresh = False
        if self.setup_state() == 'incomplete':
            if not self.run_setup():
                return None
            wifi = self.api_get('/api/wifi/status')
            fresh = True                  # just set up from the phone: photo next

        ps = self.pisugar()
        self.fix_clock(ps)
        battery = ps.battery() if ps else None
        plugged = ps.power_plugged() if ps else None

        online = bool(wifi) and wifi.get('mode') != 'hotspot'
        hint = False
        remote = False
        awake_done = False
        if online:
            if (not scheduled and not fresh and e.get('remote_qr', True)
                    and self.setup_state() == 'complete'):
                # Switched on by hand: first show the code that opens the phone
                # remote. This frame has no touchscreen and the remote needs
                # the access token, so this is the way (back) into the settings.
                self._last_draw = 0.0
                self.draw_setup(wifi, remote=True)
                remote = (bool(self._drawn_key)
                          and self._drawn_key[:2] == ('setup', 'remote'))
            self.sync(int(e.get('sync_timeout_seconds', 240)))
        elif wifi and not scheduled and self.setup_state() == 'complete':
            # Switched on by hand and the frame cannot find its Wi-Fi: say how
            # to reconnect it instead of silently showing an old picture.
            self._last_draw = 0.0          # this screen is wanted now
            self.draw_setup(wifi)
            hint = bool(self._drawn_key) and self._drawn_key[:2] == ('setup', 'wifi_lost')

        if remote:
            # On battery the code stays for the whole "reachable" window (the
            # frame powers off after it); otherwise for a few minutes.
            on_battery = ps is not None and plugged is False
            minutes = float(e.get('manual_awake_minutes', 10) if on_battery
                            else e.get('remote_qr_minutes', 5))
            logger.info('Phone-remote code on the panel for %.0f min', minutes)
            self._stop.wait(minutes * 60)
            awake_done = on_battery

        if not hint:
            self.draw_photo(battery=battery)

        # A manual wake stays reachable for the phone for a while.
        if (not scheduled and ps is not None and plugged is False
                and not awake_done):
            minutes = float(e.get('manual_awake_minutes', 10))
            logger.info('Manual wake: staying up %.0f min for settings', minutes)
            self._stop.wait(minutes * 60)

        e = self.ecfg()                       # settings may have changed meanwhile
        due = next_wake(datetime.now(), e.get('update_time', '06:00'),
                        e.get('updates_per_day', 1))
        self.save_state(next_wake=due.timestamp(), battery=battery,
                        plugged=plugged, pisugar=bool(ps))

        # Escape hatch that needs no network and no working UI: a file named
        # `mimir-stay-awake` on the boot partition (reachable from any PC by
        # putting the SD card in a reader) keeps the frame up.
        held = os.path.exists(STAY_AWAKE_FLAG)
        can_off = (self.allow_poweroff and ps is not None and plugged is False
                   and not e.get('stay_awake', False) and not held
                   and not self._stop.is_set())
        if can_off:
            if ps.set_alarm(due):
                self.poweroff()
                self._stop.wait(120)          # the shutdown takes us down
            else:
                logger.error('Wake alarm NOT confirmed — staying up instead of '
                             'powering off into a sleep nothing would end')
        else:
            why = ('no PiSugar' if ps is None else
                   'on external power' if plugged else
                   'power-off disabled' if (not self.allow_poweroff
                                            or e.get('stay_awake') or held)
                   else 'unknown power state')
            logger.info('Staying up (%s); next picture %s', why,
                        due.strftime('%a %H:%M'))
        return due

    def run(self, once=False):
        while not self._stop.is_set():
            try:
                due = self.run_once()
            except Exception as ex:
                logger.error('Cycle failed: %s', ex, exc_info=True)
                due = datetime.now() + timedelta(minutes=15)
            if once or due is None:
                break
            wait = max(30.0, (due - datetime.now()).total_seconds())
            self._stop.wait(wait)
        if self._backend is not None:
            self._backend.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description='mimir e-ink cycle')
    ap.add_argument('config', nargs='?', default='config_frame.yaml')
    ap.add_argument('--once', action='store_true',
                    help='run a single wake and exit')
    ap.add_argument('--no-poweroff', action='store_true',
                    help='never power the Pi off (development)')
    ap.add_argument('--simulate', metavar='PNG',
                    help='write the frame to a PNG instead of a panel')
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    cycle = EinkCycle(args.config, simulate_to=args.simulate,
                      allow_poweroff=not args.no_poweroff)
    signal.signal(signal.SIGTERM, cycle.stop)
    signal.signal(signal.SIGINT, cycle.stop)
    cycle.run(once=args.once)
    return 0


if __name__ == '__main__':
    sys.exit(main())
