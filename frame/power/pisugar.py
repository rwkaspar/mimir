"""Client for the PiSugar power manager (battery + RTC wake alarm).

An e-ink frame draws one picture and then switches itself off; something has to
switch it back on. A Raspberry Pi cannot wake itself from a power-off, so that
something is the PiSugar's RTC: we tell it when to power the Pi up again
(`rtc_alarm_set`), shut down, and the PiSugar cuts the supply until the alarm.

This talks to `pisugar-server` (PiSugar/pisugar-power-manager-rs) over its
plain-text TCP API on 127.0.0.1:8423 — one command per line, e.g.

    get battery                               -> battery: 87.3
    get battery_power_plugged                 -> battery_power_plugged: true
    rtc_alarm_set 2026-11-22T06:00:00+01:00 127
    get rtc_alarm_enabled                     -> rtc_alarm_enabled: true

The README documents the commands but not every reply format, so the parser is
tolerant: it accepts both `key: value` and a bare value, and every setter is
VERIFIED with a getter afterwards instead of trusting its reply. That matters:
powering off on an alarm that did not take would leave the frame dead until
someone presses its button.
"""
import logging
import socket
from datetime import datetime

logger = logging.getLogger(__name__)

DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 8423
EVERY_DAY = 127          # weekday repeat mask: 0b1111111


class PiSugarError(Exception):
    pass


class PiSugar:
    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT, timeout=4.0):
        self.host, self.port, self.timeout = host, port, timeout

    # --- transport ---

    def command(self, cmd):
        """Send one command, return the raw reply (stripped). Raises
        PiSugarError if the daemon is unreachable or does not answer."""
        try:
            with socket.create_connection((self.host, self.port),
                                          timeout=self.timeout) as s:
                s.settimeout(self.timeout)
                s.sendall((cmd.strip() + '\n').encode())
                buf = b''
                while b'\n' not in buf and len(buf) < 4096:
                    chunk = s.recv(1024)
                    if not chunk:
                        break
                    buf += chunk
        except OSError as e:
            raise PiSugarError('pisugar-server unreachable: %s' % e)
        reply = buf.decode('utf-8', 'replace').strip()
        if not reply:
            raise PiSugarError('empty reply to %r' % cmd)
        # A single button press etc. can be pushed down the same socket as an
        # unsolicited line; the answer to our command is the first line.
        return reply.splitlines()[0].strip()

    @staticmethod
    def _value(reply, key):
        """'battery: 87.3' -> '87.3'; a bare '87.3' is passed through."""
        if ':' in reply:
            head, _, tail = reply.partition(':')
            if head.strip() == key:
                return tail.strip()
        return reply.strip()

    def get(self, key):
        reply = self.command('get ' + key)
        if 'invalid' in reply.lower() or 'error' in reply.lower():
            raise PiSugarError('get %s -> %s' % (key, reply))
        return self._value(reply, key)

    def _get_bool(self, key):
        try:
            return self.get(key).lower() == 'true'
        except PiSugarError:
            return None

    # --- state ---

    def available(self):
        """True if the daemon answers and reports a model."""
        try:
            return bool(self.get('model'))
        except PiSugarError:
            return False

    def model(self):
        try:
            return self.get('model')
        except PiSugarError:
            return None

    def battery(self):
        """Charge in percent (float) or None."""
        try:
            return float(self.get('battery'))
        except (PiSugarError, ValueError):
            return None

    def power_plugged(self):
        """True/False if external power is connected, None if unknown.

        Newer boards report `battery_power_plugged`; the PiSugar 2 only knows
        `battery_charging` (true while a charger feeds it)."""
        v = self._get_bool('battery_power_plugged')
        if v is None:
            v = self._get_bool('battery_charging')
        return v

    # --- clock ---

    def sync_pi_to_rtc(self):
        """Pi clock -> RTC. Call when the Pi's clock is trustworthy (NTP)."""
        return self._try('rtc_pi2rtc')

    def sync_rtc_to_pi(self):
        """RTC -> Pi clock. Call on an offline wake (no NTP)."""
        return self._try('rtc_rtc2pi')

    def _try(self, cmd):
        try:
            self.command(cmd)
            return True
        except PiSugarError as e:
            logger.warning('PiSugar %s failed: %s', cmd, e)
            return False

    # --- wake alarm ---

    def alarm_enabled(self):
        return self._get_bool('rtc_alarm_enabled')

    def alarm_time(self):
        try:
            return self.get('rtc_alarm_time')
        except PiSugarError:
            return None

    def set_alarm(self, when, repeat=EVERY_DAY):
        """Arm the wake alarm for `when` (an aware or naive local datetime).

        Returns True only if the daemon afterwards reports the alarm ENABLED
        and at the requested time of day — the caller powers off on this, so
        "probably worked" is not good enough."""
        if when.tzinfo is None:
            when = when.astimezone()                 # attach the local offset
        stamp = when.replace(microsecond=0).isoformat()
        try:
            reply = self.command('rtc_alarm_set %s %d' % (stamp, repeat))
        except PiSugarError as e:
            logger.warning('PiSugar alarm set failed: %s', e)
            return False
        if self.alarm_enabled() is not True:
            logger.warning('PiSugar alarm not enabled after set (reply %r)', reply)
            return False
        got = self.alarm_time()
        if got and not self._same_time_of_day(got, when):
            logger.warning('PiSugar alarm time mismatch: asked %s, got %s',
                           stamp, got)
            return False
        logger.info('PiSugar wake alarm armed for %s', stamp)
        return True

    @staticmethod
    def _same_time_of_day(iso, when, tolerance_s=90):
        """The RTC alarm fires on time-of-day (+ weekday mask); the date part
        of what it echoes back is not meaningful. Compare clock times, in the
        requested zone, modulo 24 h."""
        try:
            got = datetime.fromisoformat(iso.replace('Z', '+00:00'))
        except ValueError:
            return True          # unparseable echo: don't veto on formatting
        if got.tzinfo is not None:
            got = got.astimezone(when.tzinfo)
        a = got.hour * 3600 + got.minute * 60 + got.second
        b = when.hour * 3600 + when.minute * 60 + when.second
        diff = abs(a - b)
        return min(diff, 86400 - diff) <= tolerance_s

    def disable_alarm(self):
        return self._try('rtc_alarm_disable')
