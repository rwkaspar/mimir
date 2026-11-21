"""PiSugar client against a fake pisugar-server."""
import socket
import threading
from datetime import datetime, timedelta, timezone

import pytest

from frame.power.pisugar import PiSugar


class FakeServer:
    """One command per connection, like `nc 127.0.0.1 8423`."""

    def __init__(self, prefixed=True, accept_alarm=True):
        self.prefixed = prefixed
        self.accept_alarm = accept_alarm
        self.state = {'battery': '87.5', 'battery_power_plugged': 'false',
                      'model': 'PiSugar 3', 'rtc_alarm_enabled': 'false',
                      'rtc_alarm_time': '2000-01-01T00:00:00+00:00'}
        self.commands = []
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self._run = True
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while self._run:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            with c:
                cmd = c.recv(1024).decode().strip()
                self.commands.append(cmd)
                c.sendall((self._answer(cmd) + '\n').encode())

    def _answer(self, cmd):
        parts = cmd.split()
        if parts[0] == 'get':
            key = parts[1]
            if key not in self.state:
                return 'Invalid request.'
            return ('%s: %s' % (key, self.state[key])) if self.prefixed else self.state[key]
        if parts[0] == 'rtc_alarm_set':
            if self.accept_alarm:
                self.state['rtc_alarm_enabled'] = 'true'
                self.state['rtc_alarm_time'] = parts[1]
            return 'rtc_alarm_set: done'
        return '%s: done' % parts[0]

    def close(self):
        self._run = False
        self.sock.close()


@pytest.fixture(params=[True, False], ids=['key-value', 'bare-value'])
def server(request):
    s = FakeServer(prefixed=request.param)
    yield s
    s.close()


def test_reads_state_in_both_reply_formats(server):
    ps = PiSugar(port=server.port)
    assert ps.available()
    assert ps.model() == 'PiSugar 3'
    assert ps.battery() == 87.5
    assert ps.power_plugged() is False
    server.state['battery_power_plugged'] = 'true'
    assert ps.power_plugged() is True


def test_plugged_falls_back_to_charging_on_old_boards(server):
    del server.state['battery_power_plugged']
    server.state['battery_charging'] = 'true'
    assert PiSugar(port=server.port).power_plugged() is True


def test_alarm_is_set_and_verified(server):
    ps = PiSugar(port=server.port)
    when = datetime(2026, 11, 22, 6, 0, tzinfo=timezone(timedelta(hours=1)))
    assert ps.set_alarm(when) is True
    assert 'rtc_alarm_set 2026-11-22T06:00:00+01:00 127' in server.commands


def test_alarm_that_did_not_take_is_reported():
    s = FakeServer(accept_alarm=False)
    try:
        when = datetime(2026, 11, 22, 6, 0, tzinfo=timezone.utc)
        assert PiSugar(port=s.port).set_alarm(when) is False
    finally:
        s.close()


def test_alarm_at_the_wrong_time_is_reported(server):
    ps = PiSugar(port=server.port)
    orig = server._answer

    def wrong(cmd):
        r = orig(cmd)
        if cmd.startswith('rtc_alarm_set'):
            server.state['rtc_alarm_time'] = '2026-11-22T09:30:00+01:00'
        return r
    server._answer = wrong
    when = datetime(2026, 11, 22, 6, 0, tzinfo=timezone(timedelta(hours=1)))
    assert ps.set_alarm(when) is False


def test_alarm_echo_in_another_zone_or_date_still_matches(server):
    ps = PiSugar(port=server.port)
    orig = server._answer

    def utc_echo(cmd):
        r = orig(cmd)
        if cmd.startswith('rtc_alarm_set'):
            # same instant-of-day, echoed in UTC with the RTC's own date
            server.state['rtc_alarm_time'] = '2000-01-01T05:00:00+00:00'
        return r
    server._answer = utc_echo
    when = datetime(2026, 11, 22, 6, 0, tzinfo=timezone(timedelta(hours=1)))
    assert ps.set_alarm(when) is True


def test_no_daemon_means_not_available():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    ps = PiSugar(port=port, timeout=0.5)
    assert ps.available() is False
    assert ps.battery() is None
    assert ps.power_plugged() is None
    assert ps.set_alarm(datetime(2026, 11, 22, 6, 0)) is False
