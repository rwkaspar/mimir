"""mimir Connect — frame-side client (DRAFT skeleton).

Maintains the outbound control channel to the Connect relay (see
docs/CONNECT.md), pulls photos pushed to this frame, and imports them into the
local photo library. Opt-in via config:

    connect:
      enabled: true
      relay_url: "https://connect.neotactiq.ai"
      device_token: ""     # set once, after first register
      pairing_code: ""     # returned by the relay

NOT wired into frame/server.py yet, and the happy path only. Degrades
gracefully like the MQTT bridge: if the relay is unreachable the frame runs
normally and retries with backoff.
"""

import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)


class ConnectClient:
    def __init__(self, app):
        self.app = app
        cfg = (app.config or {}).get('connect', {}) or {}
        self.relay_url = cfg.get('relay_url', 'https://connect.neotactiq.ai').rstrip('/')
        self.device_token = cfg.get('device_token', '') or getattr(app, 'access_token', '')
        self.pairing_code = cfg.get('pairing_code', '')
        self.jwt = ''
        self._stop = threading.Event()
        self._thread = None
        self.status = 'stopped'  # stopped|connecting|connected|error

    # --- lifecycle ----------------------------------------------------------
    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()

    def _run(self):
        try:
            import asyncio
            asyncio.run(self._agent_loop())
        except Exception as e:  # pragma: no cover - skeleton
            self.status = 'error'
            logger.warning(f"Connect client stopped: {e}")

    # --- registration -------------------------------------------------------
    def _register(self):
        """POST /v1/register to obtain pairing code + JWT. TODO: persist."""
        import socket
        data = urllib.parse.urlencode({
            'device_id': socket.gethostname(),
            'device_token': self.device_token,
        }).encode()
        req = urllib.request.Request(f"{self.relay_url}/v1/register", data=data)
        with urllib.request.urlopen(req, timeout=10) as r:
            body = json.loads(r.read().decode())
        self.pairing_code = body['pairing_code']
        self.jwt = body['jwt']
        # TODO: write pairing_code back into config so the settings page can
        # show the magic link https://connect.neotactiq.ai/s/<pairing_code>.
        logger.info(f"Connect registered; pairing code {self.pairing_code}")

    # --- control channel ----------------------------------------------------
    async def _agent_loop(self):
        import asyncio
        try:
            import websockets
        except Exception:
            logger.error("mimir Connect needs the 'websockets' package; "
                         "run: pip install websockets")
            self.status = 'error'
            return
        backoff = 1
        while not self._stop.is_set():
            try:
                if not self.jwt:
                    self._register()
                ws_url = (self.relay_url.replace('https://', 'wss://')
                          .replace('http://', 'ws://')
                          + f"/v1/agent?pairing_code={self.pairing_code}&jwt={self.jwt}")
                self.status = 'connecting'
                async with websockets.connect(ws_url) as ws:
                    self.status = 'connected'
                    backoff = 1
                    logger.info("Connect control channel up")
                    while not self._stop.is_set():
                        msg = await asyncio.wait_for(ws.recv(), timeout=60)
                        if msg.startswith('photo:'):
                            blob_id = msg.split(':', 1)[1]
                            if self._pull_and_import(blob_id):
                                await ws.send(f"pulled:{blob_id}")
            except Exception as e:
                self.status = 'error'
                logger.debug(f"Connect channel retry in {backoff}s: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    # --- pull + import ------------------------------------------------------
    def _pull_and_import(self, blob_id) -> bool:
        try:
            req = urllib.request.Request(
                f"{self.relay_url}/v1/blob/{blob_id}",
                headers={'Authorization': f'Bearer {self.jwt}'})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
                ctype = r.headers.get('Content-Type', 'image/jpeg')
            ext = {'image/jpeg': '.jpg', 'image/png': '.png',
                   'image/heic': '.heic', 'video/mp4': '.mp4'}.get(ctype, '.jpg')
            orient = (self.app.config.get('frame', {}) or {}).get('orientation', 'horizontal')
            base = Path(self.app.config.get('photos', {}).get('base_dir', '/srv/frame/photos'))
            dest = base / orient / f"received_{blob_id}{ext}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            logger.info(f"Connect imported photo -> {dest}")
            # TODO: trigger the viewer to refresh its playlist.
            return True
        except Exception as e:
            logger.warning(f"Connect import failed for {blob_id}: {e}")
            return False
