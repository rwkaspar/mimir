"""Home Assistant integration over MQTT with auto-discovery.

When enabled in config (`mqtt.enabled: true` + broker details), the frame
connects to an MQTT broker and announces itself to Home Assistant via MQTT
discovery. A "mimir" device then appears automatically in HA with:

  - a power switch   (ON = awake, OFF = asleep)
  - a brightness number (0-100 %, DDC/CI backlight)
  - a CPU temperature sensor
  - a "Sync now" button

No YAML is needed on the HA side; the only setup is the broker address and
login, entered in the frame's web UI. Degrades gracefully: if paho-mqtt is
not installed or the broker is unreachable, the frame runs normally and the
bridge just stays offline (paho auto-reconnects).
"""

import json
import logging
import re
import socket
import threading
import time

logger = logging.getLogger(__name__)


def _slug(text):
    return re.sub(r'[^a-z0-9_]+', '_', (text or '').lower()).strip('_') or 'mimir'


class MqttBridge:
    """Bridges the frame's power/brightness/sync to Home Assistant via MQTT."""

    def __init__(self, app):
        self.app = app
        cfg = (app.config or {}).get('mqtt', {}) or {}
        self.host = cfg.get('host', '')
        self.port = int(cfg.get('port', 1883))
        self.username = cfg.get('username', '') or None
        self.password = cfg.get('password', '') or None
        self.discovery_prefix = cfg.get('discovery_prefix', 'homeassistant')
        self.device_id = _slug(cfg.get('device_id') or socket.gethostname() or 'mimir')
        self.base = f"mimir/{self.device_id}"
        self.avail_topic = f"{self.base}/availability"
        self._client = None
        self._stop = threading.Event()

    # --- lifecycle ----------------------------------------------------------
    def start(self):
        if not self.host:
            logger.warning("MQTT enabled but no broker host set; not starting")
            return False
        try:
            import paho.mqtt.client as mqtt
        except Exception:
            logger.error("MQTT enabled but paho-mqtt is not installed; "
                         "run: pip install paho-mqtt")
            return False
        try:
            # paho-mqtt 2.x requires a callback API version; 1.x doesn't have it.
            cid = f"mimir-{self.device_id}"
            try:
                self._client = mqtt.Client(
                    mqtt.CallbackAPIVersion.VERSION1, client_id=cid)
            except (AttributeError, TypeError):
                self._client = mqtt.Client(client_id=cid)
            if self.username:
                self._client.username_pw_set(self.username, self.password)
            self._client.will_set(self.avail_topic, 'offline', retain=True)
            self._client.on_connect = self._on_connect
            self._client.on_message = self._on_message
            self._client.reconnect_delay_set(min_delay=1, max_delay=30)
            self._client.connect_async(self.host, self.port, keepalive=60)
            self._client.loop_start()
            threading.Thread(target=self._state_loop, daemon=True).start()
            logger.info(f"MQTT bridge connecting to {self.host}:{self.port} "
                        f"as device '{self.device_id}'")
            # Let the energy manager push state on power/brightness changes.
            if self.app.energy_save:
                self.app.energy_save.on_state_change = self.publish_state
            return True
        except Exception as e:
            logger.error(f"MQTT bridge failed to start: {e}")
            return False

    def stop(self):
        self._stop.set()
        if self._client:
            try:
                self._client.publish(self.avail_topic, 'offline', retain=True)
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass

    # --- MQTT callbacks -----------------------------------------------------
    def _on_connect(self, client, userdata, flags, rc, *args):
        if rc != 0:
            logger.warning(f"MQTT connect failed (rc={rc})")
            return
        logger.info("MQTT connected; publishing discovery + state")
        client.publish(self.avail_topic, 'online', retain=True)
        client.subscribe(f"{self.base}/+/set")
        self._publish_discovery()
        self.publish_state()

    def _on_message(self, client, userdata, msg):
        try:
            payload = msg.payload.decode().strip()
            key = msg.topic.split('/')[-2]  # mimir/<id>/<key>/set
            es = self.app.energy_save
            if key == 'power' and es:
                (es.wake_display if payload.upper() == 'ON' else es.sleep_now)()
            elif key == 'brightness' and es:
                es.set_brightness(int(float(payload)))
            elif key == 'sync' and self.app.syncer:
                self.app.syncer.run_sync()
            self.publish_state()
        except Exception as e:
            logger.warning(f"MQTT command error on {msg.topic}: {e}")

    # --- discovery ----------------------------------------------------------
    def _device_block(self):
        return {
            'identifiers': [self.device_id],
            'name': f"mimir ({self.device_id})",
            'manufacturer': 'mimir',
            'model': 'Digital Photo Frame',
        }

    def _disc(self, component, key, config):
        """Publish one retained HA discovery config."""
        config.setdefault('availability_topic', self.avail_topic)
        config['device'] = self._device_block()
        config['unique_id'] = f"{self.device_id}_{key}"
        topic = f"{self.discovery_prefix}/{component}/{self.device_id}/{key}/config"
        self._client.publish(topic, json.dumps(config), retain=True)

    def _publish_discovery(self):
        self._disc('switch', 'power', {
            'name': 'Power',
            'command_topic': f"{self.base}/power/set",
            'state_topic': f"{self.base}/power/state",
            'icon': 'mdi:image-frame',
        })
        self._disc('number', 'brightness', {
            'name': 'Brightness',
            'command_topic': f"{self.base}/brightness/set",
            'state_topic': f"{self.base}/brightness/state",
            'min': 0, 'max': 100, 'step': 1,
            'unit_of_measurement': '%',
            'icon': 'mdi:brightness-6',
        })
        self._disc('sensor', 'cpu_temp', {
            'name': 'CPU temperature',
            'state_topic': f"{self.base}/cpu_temp/state",
            'unit_of_measurement': '°C',
            'device_class': 'temperature',
        })
        self._disc('button', 'sync', {
            'name': 'Sync now',
            'command_topic': f"{self.base}/sync/set",
            'icon': 'mdi:sync',
        })

    # --- state --------------------------------------------------------------
    def publish_state(self):
        if not self._client:
            return
        try:
            es = self.app.energy_save
            awake = not (es.is_sleeping() if es else False)
            self._client.publish(f"{self.base}/power/state",
                                 'ON' if awake else 'OFF', retain=True)
            if es:
                self._client.publish(f"{self.base}/brightness/state",
                                     str(es.get_brightness()), retain=True)
            info = self.app.sysinfo_cache.get() if self.app.sysinfo_cache else {}
            temp = info.get('cpu_temp')
            if temp is not None:
                self._client.publish(f"{self.base}/cpu_temp/state",
                                     str(temp), retain=True)
        except Exception as e:
            logger.debug(f"MQTT publish_state failed: {e}")

    def _state_loop(self):
        while not self._stop.wait(30):
            self.publish_state()
