"""mimir Connect relay — DRAFT skeleton.

Implements the happy path of docs/CONNECT.md with in-memory stores so the flow
can be exercised end-to-end locally. NOT production ready:

  * stores are in-memory (lost on restart) — replace with a DB + blob store
  * auth is stubbed (device token / JWT / pairing-code checks are TODO)
  * no TLS (terminate at the reverse proxy), no rate limiting, no encryption

Run:  uvicorn app:app --reload --port 8800
"""

import secrets
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, WebSocket
from fastapi.responses import HTMLResponse, Response

app = FastAPI(title="mimir Connect relay (draft)")

BLOB_TTL_SECONDS = 24 * 3600
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
ALLOWED_TYPES = {"image/jpeg", "image/png", "image/heic", "video/mp4"}


@dataclass
class Frame:
    pairing_code: str
    device_id: str
    jwt: str
    last_seen: float = 0.0
    ws: Optional[WebSocket] = None


@dataclass
class Blob:
    id: str
    pairing_code: str
    content_type: str
    data: bytes
    caption: str = ""
    expires_at: float = 0.0


# pairing_code -> Frame ; blob_id -> Blob   (TODO: persist)
FRAMES: Dict[str, Frame] = {}
BLOBS: Dict[str, Blob] = {}


def _new_pairing_code() -> str:
    return "MIMIR-" + secrets.token_hex(2).upper()


def _purge_expired():
    now = time.time()
    for bid in [b for b, blob in BLOBS.items() if blob.expires_at < now]:
        BLOBS.pop(bid, None)


# --- frame registration -----------------------------------------------------
@app.post("/v1/register")
async def register(device_id: str = Form(...), device_token: str = Form(...)):
    # TODO: verify device_token against the frame's known key.
    for frame in FRAMES.values():
        if frame.device_id == device_id:
            return {"pairing_code": frame.pairing_code, "jwt": frame.jwt}
    code = _new_pairing_code()
    jwt = secrets.token_urlsafe(24)  # TODO: real signed, short-lived JWT
    FRAMES[code] = Frame(pairing_code=code, device_id=device_id, jwt=jwt)
    return {"pairing_code": code, "jwt": jwt}


# --- frame control channel --------------------------------------------------
@app.websocket("/v1/agent")
async def agent(ws: WebSocket):
    await ws.accept()
    code = ws.query_params.get("pairing_code", "")
    token = ws.query_params.get("jwt", "")
    frame = FRAMES.get(code)
    if not frame or frame.jwt != token:  # TODO: verify JWT signature
        await ws.close(code=4401)
        return
    frame.ws = ws
    frame.last_seen = time.time()
    try:
        while True:
            # Frame sends periodic pings / pull-confirmations.
            msg = await ws.receive_text()
            frame.last_seen = time.time()
            if msg.startswith("pulled:"):
                BLOBS.pop(msg.split(":", 1)[1], None)
    except Exception:
        pass
    finally:
        if frame.ws is ws:
            frame.ws = None


# --- sender uploads a photo -------------------------------------------------
@app.post("/v1/send")
async def send(pairing_code: str = Form(...), caption: str = Form(""),
               photo: UploadFile = File(...)):
    _purge_expired()
    frame = FRAMES.get(pairing_code)
    if not frame:
        raise HTTPException(404, "unknown pairing code")
    if photo.content_type not in ALLOWED_TYPES:
        raise HTTPException(415, "unsupported media type")
    data = await photo.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "file too large")
    bid = secrets.token_urlsafe(12)
    BLOBS[bid] = Blob(id=bid, pairing_code=pairing_code,
                      content_type=photo.content_type, data=data,
                      caption=caption, expires_at=time.time() + BLOB_TTL_SECONDS)
    if frame.ws is not None:
        try:
            await frame.ws.send_text(f"photo:{bid}")
        except Exception:
            pass  # frame offline; it'll get it on reconnect (TODO: queue list)
    return {"ok": True, "blob_id": bid, "delivered": frame.ws is not None}


# --- frame pulls a blob -----------------------------------------------------
@app.get("/v1/blob/{blob_id}")
async def get_blob(blob_id: str):
    # TODO: require + verify the frame's JWT (Authorization header).
    blob = BLOBS.get(blob_id)
    if not blob:
        raise HTTPException(404, "not found or expired")
    return Response(content=blob.data, media_type=blob.content_type,
                    headers={"X-Mimir-Caption": blob.caption})


# --- magic-link sender page -------------------------------------------------
@app.get("/s/{code}", response_class=HTMLResponse)
async def sender_page(code: str):
    if code not in FRAMES:
        return HTMLResponse("<h1>Unknown frame link</h1>", status_code=404)
    # Minimal sender form; real page gets mimir branding + preview.
    return HTMLResponse(f"""<!doctype html><meta charset=utf-8>
<title>Send a photo to mimir</title>
<body style="font-family:sans-serif;max-width:420px;margin:40px auto;padding:0 16px">
<h2>Send a photo to this frame</h2>
<form method=post action="/v1/send" enctype="multipart/form-data">
  <input type=hidden name=pairing_code value="{code}">
  <p><input type=file name=photo accept="image/*,video/mp4" required></p>
  <p><input name=caption placeholder="Caption (optional)" style="width:100%"></p>
  <button type=submit>Send</button>
</form></body>""")


@app.get("/healthz")
async def healthz():
    return {"ok": True, "frames": len(FRAMES), "pending_blobs": len(BLOBS)}
