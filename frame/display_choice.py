"""Which display does this frame have? — the question the setup wizard asks.

The answer changes a lot (LCD: HDMI panel, slideshow, videos, clock, night
sleep; e-ink: SPI panel, one still picture per day, battery), so it is asked
once, early, instead of being inferred. Frames flashed from a ready-made
image already know (`variant_fixed`; one image for HDMI and one per e-ink
panel): there the wizard skips the question on HDMI and only asks for the
battery board on e-ink. `options()` is what the wizard shows,
`apply()` stores the answer and — when the variant or the panel changed —
hands over to scripts/switch_variant.sh (root: SPI, panel library, services,
reboot).
"""
import glob
import logging
import os
import subprocess

import yaml

from frame.services import variant

logger = logging.getLogger(__name__)

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SWITCH_SCRIPT = os.path.join(REPO_DIR, 'scripts', 'switch_variant.sh')

# Panels offered in the wizard: (eink.driver, label). Everything else in
# frame/display/eink.py PANELS still works through the config file.
EINK_PANELS = [
    ('waveshare:epd7in3e', '7,3" Spectra 6 – 6 Farben, 800×480 '
                           '(Waveshare 7.3inch E6, Good Display GDEP073E01)'),
    ('waveshare:epd7in3f', '7,3" ACeP – 7 Farben, 800×480 (Waveshare 7.3inch F)'),
    ('waveshare:epd5in65f', '5,65" ACeP – 7 Farben, 600×448 (Waveshare 5.65inch F)'),
    ('waveshare:epd4in01f', '4,01" ACeP – 7 Farben, 640×400 (Waveshare 4.01inch F)'),
    ('waveshare:epd7in5_V2', '7,5" schwarz-weiß, 800×480 (Waveshare 7.5inch V2)'),
    ('waveshare:epd7in5b_V2', '7,5" schwarz-weiß-rot, 800×480 (Waveshare 7.5inch B V2)'),
    ('inky', 'Pimoroni Inky Impression (wird erkannt)'),
]


def image_name(driver=None):
    """File name of the ready-made image: one for HDMI, one per e-ink panel
    in EINK_PANELS (the matrix in .github/workflows/build-image.yml)."""
    if not driver:
        return 'mimir-hdmi'
    return 'mimir-eink-' + driver.split(':')[-1].replace('epd', '', 1).replace('_', '-').lower()


# Model names as the PiSugar installer expects them ('' = no battery board).
PISUGAR_MODELS = ['', 'PiSugar 3', 'PiSugar 2 (4-LEDs)', 'PiSugar 2 (2-LEDs)',
                  'PiSugar 2 Pro']


def hdmi_connected(drm_glob='/sys/class/drm/card*-HDMI-A-*/status'):
    """True/False if a monitor is (not) plugged into HDMI, None if unknown."""
    seen = None
    for path in glob.glob(drm_glob):
        try:
            with open(path) as f:
                if f.read().strip() == 'connected':
                    return True
            seen = False
        except OSError:
            continue
    return seen


def can_switch():
    """The root helper exists and sudo lets us run it without a password
    (setup_pi.sh installs that rule; older installs lack it)."""
    if not os.path.exists(SWITCH_SCRIPT):
        return False
    try:
        r = subprocess.run(['sudo', '-n', '-l'], capture_output=True,
                           text=True, timeout=10)
    except Exception:
        return False
    return has_nopasswd_rule(r.stdout, SWITCH_SCRIPT)


def has_nopasswd_rule(sudo_listing, script):
    """Is there an explicit NOPASSWD rule for `script` in `sudo -l` output?

    Deliberately NOT "does `sudo -n <script>` work right now": Raspberry Pi OS
    caches a sudo password for every process of the user for 15 minutes, so
    that test passes on any install whenever someone has just used sudo.
    """
    for line in (sudo_listing or '').splitlines():
        if 'NOPASSWD:' in line:
            cmds = line.split('NOPASSWD:', 1)[1]
            if script in [c.strip() for c in cmds.split(',')]:
                return True
    return False


def options(config, hdmi=None, switchable=None):
    """What the wizard needs to draw the question."""
    cur = variant(config)
    eink = (config or {}).get('eink', {}) or {}
    if hdmi is None:
        hdmi = hdmi_connected()
    # A plugged-in monitor is a strong hint; no monitor is a weak one (an LCD
    # frame can simply be switched off), so only then fall back to the config.
    suggested = 'lcd' if hdmi else ('eink' if hdmi is False else cur)
    return {
        'variant': cur,
        'suggested': suggested,
        'hdmi_connected': hdmi,
        'confirmed': bool((config or {}).get('display_confirmed')),
        # Ready-made image for one display (HDMI, or one e-ink panel): the
        # wizard does not ask which display, only — on e-ink — for the
        # battery board.
        'fixed': bool((config or {}).get('variant_fixed')),
        'driver': eink.get('driver') or '',
        'pisugar_model': eink.get('pisugar_model') or '',
        'panels': [{'driver': d, 'label': l} for d, l in EINK_PANELS],
        'pisugar_models': PISUGAR_MODELS,
        'can_switch': can_switch() if switchable is None else switchable,
    }


def apply(config, config_path, data, run_switch=None):
    """Store the wizard's answer. Returns {'ok', 'restart', ...}.

    `restart` is True when the variant or the e-ink hardware changed: the
    switch script was started and the frame reboots within seconds.
    """
    want = data.get('variant')
    if want not in ('lcd', 'eink'):
        raise ValueError('variant must be lcd or eink')
    cur = variant(config)
    old_eink = dict((config or {}).get('eink', {}) or {})
    eink = dict(old_eink)
    if want == 'eink':
        driver = str(data.get('driver') or eink.get('driver') or '')
        known = [d for d, _ in EINK_PANELS]
        if driver not in known and not driver.startswith('waveshare:'):
            raise ValueError('unknown e-ink panel')
        model = str(data.get('pisugar_model', eink.get('pisugar_model') or ''))
        if model not in PISUGAR_MODELS:
            raise ValueError('unknown PiSugar model')
        eink['driver'] = driver
        if model:
            eink['pisugar_model'] = model
        else:
            eink.pop('pisugar_model', None)
    changed = want != cur or (want == 'eink' and (
        eink.get('driver') != old_eink.get('driver')
        or eink.get('pisugar_model') != old_eink.get('pisugar_model')))
    if changed and (config or {}).get('setup_complete'):
        # A frame in use is never rebuilt by a web request: this endpoint is
        # the wizard's. Later changes go through scripts/switch_variant.sh.
        return {'ok': False, 'error': 'setup_complete', 'restart': False}
    if changed and not (can_switch() if run_switch is None else True):
        return {'ok': False, 'error': 'cannot_switch', 'restart': False}

    with open(config_path) as f:
        cfg = yaml.safe_load(f) or {}
    cfg['display_confirmed'] = True
    if want == 'eink':
        cfg['eink'] = dict(cfg.get('eink') or {}, **eink)
        if 'pisugar_model' not in eink:
            cfg['eink'].pop('pisugar_model', None)
    # Written here AND kept in memory, so that a later whole-config save by
    # this (still running) server cannot undo the choice before the reboot.
    cfg['variant'] = want
    with open(config_path, 'w') as f:
        yaml.dump(cfg, f, default_flow_style=False)
    config['display_confirmed'] = True
    config['variant'] = want
    if want == 'eink':
        config['eink'] = cfg['eink']

    if changed:
        logger.info('Display choice: switching %s -> %s (%s)', cur, want,
                    eink.get('driver', '') if want == 'eink' else 'hdmi')
        (run_switch or _run_switch)(want)
    return {'ok': True, 'restart': changed, 'variant': want}


def _run_switch(want):
    """Start the root helper detached: it reboots the frame, and must outlive
    this server being stopped on the way down."""
    subprocess.Popen(['sudo', '-n', SWITCH_SCRIPT, want],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
