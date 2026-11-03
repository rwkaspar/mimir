"""Development mock layer: makes the frame app runnable on a non-Pi machine.

Calling :func:`install` patches ``subprocess`` so that Raspberry-Pi-only
commands (ddcutil, wlopm, wlr-randr, nmcli, systemctl, iptables, dd,
tailscale, the bootsplash installer, the cage service, ...) are intercepted
and return plausible fake output instead of actually running. Everything else
(ffmpeg, hostname, df, lsusb, ...) still runs for real.

This lets the full web UI and API be exercised in an ordinary browser without
flashing a Pi, and without touching the dev machine's real Wi-Fi/display.

Enabled by :mod:`dev_server`; never imported in production.
"""

import logging
import subprocess

logger = logging.getLogger(__name__)

_real_run = subprocess.run
_real_popen = subprocess.Popen
_real_check_output = subprocess.check_output

# Programs that must never actually execute on a dev machine.
_INTERCEPT_PROGRAMS = {
    'ddcutil', 'wlopm', 'wlr-randr', 'systemctl', 'iptables', 'nmcli',
    'dd', 'vcgencmd', 'tailscale', 'plymouth', 'plymouth-set-default-theme',
    'cage', 'labwc', 'swaybg', 'raspi-config', 'reboot', 'shutdown',
}
# Substrings that mark a command as Pi-only even if the program itself is generic
# (e.g. `sudo sh -c '... /dev/fb0'`, the bootsplash script, the kiosk service).
_INTERCEPT_SUBSTR = ('install_bootsplash', 'photo_frame_cage', '/dev/fb0')


def _as_list(cmd):
    if isinstance(cmd, (list, tuple)):
        return [str(c) for c in cmd]
    if isinstance(cmd, str):
        return cmd.split()
    return [str(cmd)]


def _program_of(argv):
    """Return the real program name, skipping a leading sudo and its options."""
    i = 0
    while i < len(argv) and (argv[i] == 'sudo' or argv[i].startswith('-')
                             or ('=' in argv[i] and '/' not in argv[i])):
        i += 1
    prog = argv[i] if i < len(argv) else ''
    return prog.rsplit('/', 1)[-1]


def _should_mock(cmd):
    argv = _as_list(cmd)
    if not argv:
        return False, '', argv
    prog = _program_of(argv)
    if prog in _INTERCEPT_PROGRAMS:
        return True, prog, argv
    joined = ' '.join(argv)
    if any(s in joined for s in _INTERCEPT_SUBSTR):
        return True, prog, argv
    return False, prog, argv


def _fake_stdout(prog, argv):
    """Plausible stdout for the few commands whose output the app parses."""
    joined = ' '.join(argv)
    if prog == 'nmcli':
        if 'DEVICE,TYPE,STATE,CONNECTION' in joined:
            # check_connectivity(): a connected non-hotspot wlan0 line.
            return 'wlan0:wifi:connected:MockNet\neth0:ethernet:unavailable:\n'
        if 'wifi' in argv and 'list' in argv:
            return ('MockNet:88:WPA2\n'
                    'Cafe Guest:62:WPA2\n'
                    'Neighbour 5G:45:WPA2\n'
                    'OpenHotspot:30:\n')
        return ''
    if prog == 'tailscale':
        return ''  # no tailscale IP in dev
    if prog == 'vcgencmd':
        if 'measure_temp' in joined:
            return "temp=48.0'C\n"
        if 'get_throttled' in joined:
            return 'throttled=0x0\n'
        return ''
    return ''


def _completed(cmd, argv, prog, kwargs):
    text = bool(kwargs.get('text') or kwargs.get('universal_newlines')
                or kwargs.get('encoding'))
    out = _fake_stdout(prog, argv)
    stdout = out if text else out.encode()
    err = '' if text else b''
    logger.debug("mock: intercepted %s", ' '.join(argv))
    return subprocess.CompletedProcess(cmd, 0, stdout, err)


def _mock_run(cmd, *args, **kwargs):
    mock, prog, argv = _should_mock(cmd)
    if mock:
        return _completed(cmd, argv, prog, kwargs)
    return _real_run(cmd, *args, **kwargs)


def _mock_check_output(cmd, *args, **kwargs):
    mock, prog, argv = _should_mock(cmd)
    if mock:
        kwargs.setdefault('text', True)
        out = _fake_stdout(prog, argv)
        return out if kwargs.get('text') else out.encode()
    return _real_check_output(cmd, *args, **kwargs)


class _FakePopen:
    """Minimal Popen stand-in for intercepted commands."""

    def __init__(self, cmd, argv, kwargs):
        self.args = cmd
        self.returncode = 0
        text = bool(kwargs.get('text') or kwargs.get('universal_newlines')
                    or kwargs.get('encoding'))
        self.stdout = None
        self.stderr = None
        self._text = text

    def communicate(self, *a, **k):
        return ('' if self._text else b'', '' if self._text else b'')

    def wait(self, *a, **k):
        return 0

    def poll(self):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _mock_popen(cmd, *args, **kwargs):
    mock, prog, argv = _should_mock(cmd)
    if mock:
        logger.debug("mock: intercepted Popen %s", ' '.join(argv))
        return _FakePopen(cmd, argv, kwargs)
    return _real_popen(cmd, *args, **kwargs)


def install():
    """Patch subprocess so Pi-only commands are mocked. Idempotent."""
    if getattr(subprocess, '_frame_mocked', False):
        return
    subprocess.run = _mock_run
    subprocess.Popen = _mock_popen
    subprocess.check_output = _mock_check_output
    subprocess._frame_mocked = True
    logger.info("Frame dev mock installed (Pi commands intercepted)")
